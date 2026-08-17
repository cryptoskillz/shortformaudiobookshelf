#!/usr/bin/env python3
"""Shortform Audio Bookshelf — an audiobook server and player.

Scans a directory of Opus (and other) audio files, groups them into books,
and serves them to a browser player over HTTP with byte-range streaming, so
the same server works on this machine and from a phone on the same network.

    python3 server.py                      # uses saved settings
    python3 server.py ~/Audiobooks         # or an explicit directory
    python3 server.py --settings-list      # show what is configured

The directory is scanned once and the result kept in index.json, so startup
does not re-read the library; Rescan (or --rescan) refreshes it.

Nothing outside the library directory is ever served: every audio request is
resolved through the scanned index by book id and track number, so there is no
user-supplied path to traverse.
"""

from __future__ import annotations

import argparse
import base64
import getpass
import glob
import gzip
import hashlib
import hmac
import json
import mimetypes
import os
import re
import secrets
import socket
import sys
import threading
import time
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import library
import metadata as metadata_module
import oggopus
import organise as organise_module
import settings as settings_module
import users as users_module

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")


def state_paths(state_dir):
    """Everything the server persists, under one directory.

    Kept together so a container can mount a single /config volume and a
    desktop can keep using ~/.shortlistaudio.
    """
    root = os.path.abspath(os.path.expanduser(state_dir))
    return {
        "dir": root,
        "cache": os.path.join(root, "cache.json"),
        "progress": os.path.join(root, "progress.json"),
        "index": os.path.join(root, "index.json"),
        "covers": os.path.join(root, "covers"),
        "settings": os.path.join(root, "settings.json"),
    }


PATHS = state_paths(settings_module.STATE_DIR)
CHUNK = 256 * 1024
MAX_UPLOAD = 4 << 30   # 4 GB, comfortably past any single audiobook file
DRAIN_LIMIT = 8 << 20  # discard an unwanted body up to this size; beyond it, hang up
_RANGE = re.compile(r"bytes=(\d*)-(\d*)", re.IGNORECASE)
_UNSAFE_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _safe_upload_name(raw):
    """A filename that cannot escape the library directory.

    Everything that could steer the write elsewhere is removed rather than
    rejected — the client sends whatever the operating system gave it.
    """
    name = urllib.parse.unquote(raw or "")
    name = name.replace("\\", "/").split("/")[-1]     # drop any path the client sent
    name = _UNSAFE_NAME.sub("", name).strip().strip(".")
    name = re.sub(r"\s{2,}", " ", name)
    if not name or len(name) > 200:
        name = name[:200].strip()
    return name


def _unused_path(path):
    """`path`, or `name (2).ext` if something is already there."""
    if not os.path.exists(path):
        return path
    stem, extension = os.path.splitext(path)
    for suffix in range(2, 1000):
        candidate = f"{stem} ({suffix}){extension}"
        if not os.path.exists(candidate):
            return candidate
    return f"{stem} ({os.getpid()}){extension}"


SESSION_COOKIE = "sab_session"
SESSION_DAYS = 30


def is_private_client(address):
    """Whether a request came from a local network rather than the internet.

    Used to decide whether it is safe to show the first-run credentials. Behind
    a tunnel or reverse proxy the peer is the proxy, so a public visitor never
    sees them — which is the point.
    """
    try:
        import ipaddress
        ip = ipaddress.ip_address(address)
        return ip.is_private or ip.is_loopback or ip.is_link_local
    except ValueError:
        return False


def in_container():
    """Whether we are running inside a container.

    Worth knowing because the folder settings then refer to paths *inside* the
    container, and typing a host path into them would point at nothing.
    """
    return os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv")


def directory_writable(path, create=False):
    """Whether we can write into a directory. Returns (ok, reason).

    Bind-mounted folders that Docker created are owned by root, so a container
    running as PUID cannot write to them. That surfaces much later as an upload
    or an organise failing for no visible reason, so every folder we depend on
    is probed up front.
    """
    if not path:
        return True, ""
    try:
        if create:
            os.makedirs(path, exist_ok=True)
        elif not os.path.isdir(path):
            return False, "does not exist"
        probe = os.path.join(path, f".writetest.{os.getpid()}")
        with open(probe, "w") as fh:
            fh.write("ok")
        os.remove(probe)
        return True, ""
    except OSError as exc:
        return False, exc.strerror or str(exc)


def state_dir_writable(state_dir):
    """Whether we can actually persist anything. Returns (ok, reason)."""
    return directory_writable(state_dir, create=True)


def session_secret(state_dir):
    """A per-install key for signing session cookies, created on first use."""
    path = os.path.join(state_dir, "secret")
    try:
        with open(path, "rb") as fh:
            secret = fh.read().strip()
        if len(secret) >= 32:
            return secret
    except OSError:
        pass
    secret = secrets.token_hex(32).encode()
    try:
        os.makedirs(state_dir, exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(secret)
        os.chmod(path, 0o600)
    except OSError:
        pass  # an unwritable secret just means sessions end at restart
    return secret


def make_session(secret, username, days=SESSION_DAYS):
    expires = int(time.time() + days * 86400)
    payload = f"{username}|{expires}"
    signature = hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f"{payload}|{signature}".encode()).decode()


def read_session(secret, token):
    """The username inside a valid, unexpired token, or None."""
    try:
        raw = base64.urlsafe_b64decode(token.encode()).decode()
        username, expires, signature = raw.rsplit("|", 2)
    except Exception:
        return None
    expected = hmac.new(secret, f"{username}|{expires}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        return None
    try:
        if int(expires) < time.time():
            return None
    except ValueError:
        return None
    return username


def _remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


class ProgressStore:
    """Playback positions, per user and per book.

    Stored as {username: {bookId: entry}}. Two people listening to the same
    book must not overwrite each other, so the account is part of the key; with
    no accounts configured everything lands in one shared bucket keyed "".
    """

    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        self._data = {}
        try:
            with open(path, "r", encoding="utf-8") as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict):
                self._data = self._migrate(loaded)
        except OSError:
            pass
        except ValueError:
            # Corrupt rather than absent. Keep it: resume points are the one
            # piece of state here that cannot be regenerated by rescanning.
            damaged = f"{path}.corrupt"
            try:
                os.replace(path, damaged)
                print(f"warning: {path} was unreadable and has been moved to {damaged}; "
                      "resume positions start fresh", file=sys.stderr)
            except OSError:
                pass

    @staticmethod
    def _migrate(loaded):
        """Accept the old flat {bookId: entry} file from before accounts."""
        looks_flat = any(
            isinstance(value, dict) and "position" in value and "track" in value
            for value in loaded.values()
        )
        return {"": loaded} if looks_flat else loaded

    def all(self, user=""):
        with self._lock:
            return dict(self._data.get(user, {}))

    def get(self, user, book_id):
        with self._lock:
            return self._data.get(user, {}).get(book_id)

    def set(self, user, book_id, track, position, finished=False):
        entry = {
            "track": max(0, int(track)),
            "position": max(0.0, float(position)),
            "finished": bool(finished),
            "updatedAt": time.time(),
        }
        with self._lock:
            self._data.setdefault(user, {})[book_id] = entry
            snapshot = {k: dict(v) for k, v in self._data.items()}
        self._write(snapshot)
        return entry

    def clear(self, user, book_id):
        with self._lock:
            self._data.get(user, {}).pop(book_id, None)
            snapshot = {k: dict(v) for k, v in self._data.items()}
        self._write(snapshot)

    def rename_user(self, old, new):
        """Move one bucket to another name, merging if the target exists."""
        with self._lock:
            moving = self._data.pop(old, None)
            if not moving:
                return 0
            target = self._data.setdefault(new, {})
            for book_id, entry in moving.items():
                target.setdefault(book_id, entry)
            snapshot = {k: dict(v) for k, v in self._data.items()}
        self._write(snapshot)
        return len(moving)

    def forget_user(self, user):
        with self._lock:
            self._data.pop(user, None)
            snapshot = {k: dict(v) for k, v in self._data.items()}
        self._write(snapshot)

    def forget_book(self, book_id):
        """Drop one book from everybody — used when a book is removed."""
        with self._lock:
            for positions in self._data.values():
                positions.pop(book_id, None)
            snapshot = {k: dict(v) for k, v in self._data.items()}
        self._write(snapshot)

    def _write(self, snapshot):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            # The temp name carries the pid: two servers sharing this directory
            # would otherwise write the same scratch file and replace each
            # other's half-finished JSON, leaving a corrupt one behind.
            temp = f"{self.path}.{os.getpid()}.tmp"
            with open(temp, "w", encoding="utf-8") as fh:
                json.dump(snapshot, fh, indent=1)
            os.replace(temp, self.path)
        except OSError:
            pass  # losing a resume point is not worth crashing playback over


class MetadataStore:
    """Book details fetched from Audible/iTunes/Google, kept beside the library.

    This is an overlay, never a rewrite: the audio files are left exactly as
    they are, and anything stored here simply wins over the tags when a book is
    displayed. Deleting the file returns every book to what its tags say.
    """

    FIELDS = ("description", "narrator", "series", "seriesPart", "year",
              "publisher", "genre", "provider", "matchedTitle", "matchId", "link")

    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        self._data = {}
        try:
            with open(path, "r", encoding="utf-8") as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict):
                self._data = loaded
        except (OSError, ValueError):
            pass

    def get(self, book_id):
        with self._lock:
            entry = self._data.get(book_id)
            return dict(entry) if entry else None

    def set(self, book_id, values):
        entry = {key: values.get(key, "") for key in self.FIELDS}
        entry["cover"] = bool(values.get("cover"))
        entry["appliedAt"] = time.time()
        with self._lock:
            self._data[book_id] = entry
            snapshot = dict(self._data)
        self._write(snapshot)
        return entry

    def clear(self, book_id):
        with self._lock:
            self._data.pop(book_id, None)
            snapshot = dict(self._data)
        self._write(snapshot)

    def count(self):
        with self._lock:
            return len(self._data)

    def _write(self, snapshot):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            temp = f"{self.path}.{os.getpid()}.tmp"
            with open(temp, "w", encoding="utf-8") as fh:
                json.dump(snapshot, fh, indent=1, ensure_ascii=False)
            os.replace(temp, self.path)
        except OSError:
            pass


class ApiKeyStore:
    """Tokens for machines, scoped to one action each.

    A webhook cannot fill in a sign-in form, and handing an import tool an
    admin password would give it the ability to delete the library. A key here
    can trigger a rescan and nothing else, and only its hash is stored, so the
    file is useless to anyone who reads it.
    """

    SCOPES = ("rescan",)

    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        self._data = {}
        try:
            with open(path, "r", encoding="utf-8") as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict):
                self._data = loaded
        except (OSError, ValueError):
            pass

    @staticmethod
    def _hash(key):
        return hashlib.sha256(key.encode()).hexdigest()

    def create(self, name, scope="rescan"):
        if scope not in self.SCOPES:
            raise ValueError(f"scope must be one of {', '.join(self.SCOPES)}")
        key = "sab_" + secrets.token_urlsafe(32)
        with self._lock:
            self._data[self._hash(key)] = {
                "name": (name or "unnamed").strip()[:60] or "unnamed",
                "scope": scope,
                "createdAt": time.time(),
                "lastUsed": 0,
            }
            snapshot = dict(self._data)
        self._write(snapshot)
        return key            # the only time the caller ever sees it

    def resolve(self, key):
        """The entry for a presented key, or None. Records the use."""
        if not key:
            return None
        digest = self._hash(key)
        with self._lock:
            entry = self._data.get(digest)
            if not entry:
                return None
            entry["lastUsed"] = time.time()
            snapshot = dict(self._data)
            found = dict(entry, id=digest[:12])
        self._write(snapshot)
        return found

    def list(self):
        with self._lock:
            return [dict(v, id=k[:12]) for k, v in self._data.items()]

    def revoke(self, key_id):
        with self._lock:
            for digest in list(self._data):
                if digest.startswith(key_id):
                    del self._data[digest]
                    snapshot = dict(self._data)
                    break
            else:
                raise ValueError("no such key")
        self._write(snapshot)

    def _write(self, snapshot):
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
            temp = f"{self.path}.{os.getpid()}.tmp"
            with open(temp, "w", encoding="utf-8") as fh:
                json.dump(snapshot, fh, indent=1, ensure_ascii=False)
            os.replace(temp, self.path)
            os.chmod(self.path, 0o600)
        except OSError:
            pass


class AuthorStore:
    """Author biographies, keyed by author name rather than by book.

    One writer usually has several books here, so storing the bio per author
    means looking it up once and every one of their books showing it.
    """

    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        self._data = {}
        try:
            with open(path, "r", encoding="utf-8") as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict):
                self._data = loaded
        except (OSError, ValueError):
            pass

    @staticmethod
    def key(author):
        return " ".join((author or "").lower().split())

    def get(self, author):
        with self._lock:
            entry = self._data.get(self.key(author))
            return dict(entry) if entry else None

    def set(self, author, values):
        entry = {
            "name": values.get("name", author),
            "bio": values.get("bio", ""),
            "summary": values.get("summary", ""),
            "born": values.get("born", ""),
            "died": values.get("died", ""),
            "provider": values.get("provider", ""),
            "link": values.get("link", ""),
            "appliedAt": time.time(),
        }
        with self._lock:
            self._data[self.key(author)] = entry
            snapshot = dict(self._data)
        self._write(snapshot)
        return entry

    def clear(self, author):
        with self._lock:
            self._data.pop(self.key(author), None)
            snapshot = dict(self._data)
        self._write(snapshot)

    def _write(self, snapshot):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            temp = f"{self.path}.{os.getpid()}.tmp"
            with open(temp, "w", encoding="utf-8") as fh:
                json.dump(snapshot, fh, indent=1, ensure_ascii=False)
            os.replace(temp, self.path)
        except OSError:
            pass


class RemovedStore:
    """Books hidden from the library, and whether their files were deleted.

    Removing without deleting has to be remembered, or the next rescan would
    find the files again and put the book straight back.
    """

    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        self._data = {}
        try:
            with open(path, "r", encoding="utf-8") as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict):
                self._data = loaded
        except (OSError, ValueError):
            pass

    def ids(self):
        with self._lock:
            return set(self._data)

    def get(self, book_id):
        with self._lock:
            entry = self._data.get(book_id)
            return dict(entry) if entry else None

    def all(self):
        with self._lock:
            return {key: dict(value) for key, value in self._data.items()}

    def add(self, book_id, record):
        with self._lock:
            self._data[book_id] = record
            snapshot = dict(self._data)
        self._write(snapshot)

    def restore(self, book_id):
        with self._lock:
            entry = self._data.pop(book_id, None)
            snapshot = dict(self._data)
        self._write(snapshot)
        return entry

    def _write(self, snapshot):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            temp = f"{self.path}.{os.getpid()}.tmp"
            with open(temp, "w", encoding="utf-8") as fh:
                json.dump(snapshot, fh, indent=1, ensure_ascii=False)
            os.replace(temp, self.path)
        except OSError:
            pass


def apply_metadata(store, covers_dir, book_id, candidate, want_cover=True):
    """Store a chosen match as the overlay for one book, fetching its cover."""
    stored_cover = False
    if want_cover and candidate.get("coverUrl"):
        picture = metadata_module.fetch_cover(str(candidate["coverUrl"]))
        if picture:
            content_type, data = picture
            extension = library.COVER_MIME_EXTENSIONS.get(content_type, ".jpg")
            target = os.path.join(covers_dir, f"{book_id}.fetched{extension}")
            try:
                os.makedirs(covers_dir, exist_ok=True)
                for stale in glob.glob(os.path.join(covers_dir, f"{book_id}.fetched.*")):
                    _remove(stale)
                with open(target, "wb") as fh:
                    fh.write(data)
                stored_cover = True
            except OSError:
                stored_cover = False

    narrators = candidate.get("narrators") or []
    genres = candidate.get("genres") or []
    entry = store.set(book_id, {
        "description": candidate.get("description", ""),
        "narrator": ", ".join(narrators) if isinstance(narrators, list) else str(narrators),
        "series": candidate.get("series", ""),
        "seriesPart": candidate.get("seriesPart", ""),
        "year": candidate.get("year", ""),
        "publisher": candidate.get("publisher", ""),
        "genre": genres[0] if genres else "",
        "provider": candidate.get("provider", ""),
        "matchedTitle": candidate.get("title", ""),
        "matchId": candidate.get("id", ""),
        "link": candidate.get("link", ""),
        "cover": stored_cover,
    })
    return entry, stored_cover


class MetadataJob:
    """Fetch descriptions for every book that has none, in the background.

    Deliberately separate from Rescan: rescanning is a local filesystem pass
    measured in seconds, while this makes one network call per book. Bolting
    them together would turn a 2-second action into an hour-long one.

    Only a clearly good match is applied. Anything doubtful is left alone and
    counted as skipped, because a wrong blurb silently attached to hundreds of
    books is far harder to undo than a blank field.
    """

    DELAY = 1.2          # seconds between lookups — these services are a courtesy
    MIN_SCORE = 85       # below this, leave the book alone

    def __init__(self):
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._state = self._idle()

    @staticmethod
    def _idle():
        return {"running": False, "done": 0, "total": 0, "current": "", "applied": 0,
                "skipped": 0, "failed": 0, "bios": 0, "stopped": False, "error": None,
                "lastError": "", "backingOff": False, "etaSeconds": 0, "recent": []}

    def snapshot(self):
        with self._lock:
            return dict(self._state, recent=list(self._state["recent"]))

    def stop(self):
        self._stop.set()

    def start(self, books, store, covers_dir, providers, min_score, want_covers,
              author_store=None):
        with self._lock:
            if self._state["running"]:
                return False
            self._state = self._idle()
            self._state.update(running=True, total=len(books))
        self._stop.clear()
        threading.Thread(
            target=self._run,
            args=(books, store, covers_dir, providers, min_score, want_covers, author_store),
            daemon=True,
        ).start()
        return True

    def _note(self, **changes):
        with self._lock:
            self._state.update(changes)

    def _run(self, books, store, covers_dir, providers, min_score, want_covers,
             author_store=None):
        seen_authors = set()
        try:
            for index, book in enumerate(books, start=1):
                if self._stop.is_set():
                    self._note(stopped=True)
                    break
                self._note(done=index, current=book["title"],
                           etaSeconds=int((len(books) - index) * (self.DELAY + 0.8)))
                try:
                    candidates, errors = metadata_module.search(
                        book["title"], book["author"], providers=providers, limit=3
                    )
                except Exception as exc:
                    with self._lock:
                        self._state["failed"] += 1
                        self._state["lastError"] = str(exc)
                    continue

                # A provider that refused is not the same as a book with no
                # good match, and counting both as "skipped" hides a run that
                # has quietly stopped working.
                if errors and not candidates:
                    message = "; ".join(f"{name}: {why}" for name, why in errors.items())
                    with self._lock:
                        self._state["failed"] += 1
                        self._state["lastError"] = message
                    if any("rate limited" in why for why in errors.values()):
                        with self._lock:
                            self._state["backingOff"] = True
                        # Backing off is the only polite response, and it beats
                        # burning through the rest of the library getting 429s.
                        self._stop.wait(60)
                        with self._lock:
                            self._state["backingOff"] = False
                    continue

                best = candidates[0] if candidates else None
                if best and best["score"] >= min_score:
                    apply_metadata(store, covers_dir, book["id"], best, want_cover=want_covers)
                    with self._lock:
                        self._state["applied"] += 1
                        self._state["recent"].insert(
                            0, f"{book['title']} → {best['provider']}")
                        del self._state["recent"][8:]
                else:
                    with self._lock:
                        self._state["skipped"] += 1

                # One bio per author, not per book: most authors here have
                # several books and the sources should not be asked twice.
                author = (book.get("author") or "").strip()
                key = author.lower()
                if (author_store is not None and author and key not in seen_authors
                        and not author_store.get(author)):
                    seen_authors.add(key)
                    try:
                        found, _ = metadata_module.search_authors(author, limit=2)
                        best = found[0] if found else None
                        if best and best.get("bio") and best["score"] >= min_score:
                            author_store.set(author, best)
                            with self._lock:
                                self._state["bios"] += 1
                    except Exception:
                        pass

                if not self._stop.is_set():
                    time.sleep(self.DELAY)
        except Exception as exc:
            self._note(error=str(exc))
        finally:
            self._note(running=False, current="")


class OrganiseJob:
    """Runs an organise on a background thread and reports progress.

    Copying a large library takes minutes, which is far longer than a browser
    will wait on one request, so the POST starts the job and the page polls.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._state = {
            "running": False, "done": 0, "total": 0, "current": "",
            "mode": "", "summary": None, "error": None, "output": "", "finishedAt": 0,
        }

    def snapshot(self):
        with self._lock:
            return dict(self._state)

    def start(self, lib, output, dry_run, overwrite, playlists_only, delete_originals=False):
        with self._lock:
            if self._state["running"]:
                return False
            self._state = {
                "running": True, "done": 0, "total": 0, "current": "",
                "mode": "preview" if dry_run else ("playlists" if playlists_only else "copy"),
                "summary": None, "error": None, "output": output, "finishedAt": 0,
            }
        thread = threading.Thread(
            target=self._run,
            args=(lib, output, dry_run, overwrite, playlists_only, delete_originals),
            daemon=True,
        )
        thread.start()
        return True

    def _run(self, lib, output, dry_run, overwrite, playlists_only, delete_originals=False):
        def progress(done, total, name):
            with self._lock:
                self._state.update(done=done, total=total, current=name)
        try:
            manifest = organise_module.organise(
                lib, output, dry_run=dry_run, overwrite=overwrite,
                playlists_only=playlists_only, delete_originals=delete_originals,
                log=lambda *_: None, progress=progress,
            )
            summary = organise_module.summarise(manifest)
            with self._lock:
                self._state.update(running=False, summary=summary, current="",
                                   finishedAt=time.time())
        except Exception as exc:
            with self._lock:
                self._state.update(running=False, error=str(exc), current="",
                                   finishedAt=time.time())


class Handler(BaseHTTPRequestHandler):
    server_version = "ShortformAudioBookshelf/1.0"
    protocol_version = "HTTP/1.1"
    _body_read = 0  # bytes of this request's body already consumed
    current_user = None

    # -- plumbing ----------------------------------------------------------

    @property
    def lib(self):
        return self.server.library

    @property
    def progress(self):
        return self.server.progress

    @property
    def metadata_store(self):
        return self.server.metadata

    def _fetched_covers(self):
        """Every provider-fetched cover, listed once and cached.

        This used to glob the covers directory once per book. Building the
        library listing therefore cost books x files filename comparisons —
        with a few thousand of each, seconds per request, and worse while the
        metadata job was adding covers. The directory is now read once and the
        result reused until its mtime changes.
        """
        covers = self.server.paths["covers"]
        try:
            stamp = os.stat(covers).st_mtime_ns
        except OSError:
            return {}

        cached = getattr(self.server, "cover_map", None)
        if cached and cached[0] == stamp:
            return cached[1]

        mapping = {}
        try:
            with os.scandir(covers) as entries:
                for entry in entries:
                    book_id, marker, _ = entry.name.partition(".fetched.")
                    if marker:
                        mapping[book_id] = entry.path
        except OSError:
            return {}
        self.server.cover_map = (stamp, mapping)
        return mapping

    def _overlay_cover(self, book_id):
        """A cover downloaded from a metadata provider, if there is one."""
        return self._fetched_covers().get(book_id)

    def _apply_overlay(self, payload, book_id):
        """Let looked-up details win over what the tags said."""
        entry = self.metadata_store.get(book_id)
        if self._overlay_cover(book_id):
            payload["hasCover"] = True
        if not entry:
            return payload
        for key in ("description", "narrator", "series", "year", "publisher"):
            if entry.get(key):
                payload[key] = entry[key]
        if entry.get("genre") and not payload.get("genre"):
            payload["genre"] = entry["genre"]
        payload["metadata"] = {
            "provider": entry.get("provider", ""),
            "matchedTitle": entry.get("matchedTitle", ""),
            "link": entry.get("link", ""),
            "appliedAt": entry.get("appliedAt", 0),
        }
        return payload

    def log_message(self, fmt, *args):
        if self.server.verbose:
            sys.stderr.write(f"{self.address_string()} - {fmt % args}\n")

    def _compress(self, body, mime):
        """gzip a response when it is worth it and the client asked.

        The library listing is close to a megabyte of titles and authors, which
        compresses about six times over. Uncompressed that is the whole of a
        slow first load through a tunnel. Never applied to audio or images:
        they are already compressed and would only cost CPU.
        """
        if len(body) < 1024:
            return body, None
        if "gzip" not in (self.headers.get("Accept-Encoding") or "").lower():
            return body, None
        if not (mime.startswith("text/") or "json" in mime or "javascript" in mime
                or "xml" in mime or mime == "audio/x-mpegurl"):
            return body, None
        return gzip.compress(body, 6), "gzip"

    def _send_json(self, payload, status=HTTPStatus.OK):
        body = json.dumps(payload).encode("utf-8")
        body, encoding = self._compress(body, "application/json")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        if encoding:
            self.send_header("Content-Encoding", encoding)
            self.send_header("Vary", "Accept-Encoding")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_bytes(self, data, mime, status=HTTPStatus.OK, cache="no-store"):
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _error(self, status, message):
        self._send_json({"error": message}, status)
        self._discard_body()

    def _discard_body(self):
        """Consume any request body we did not read.

        On a keep-alive connection an unread body is still sitting in the
        socket, and the next request would be parsed starting from those
        leftover bytes. Rejecting an upload before reading it is exactly when
        this happens. Anything too large to be worth reading gets the
        connection closed instead.
        """
        try:
            remaining = int(self.headers.get("Content-Length") or 0) - self._body_read
        except ValueError:
            return
        if remaining <= 0:
            return
        if remaining > DRAIN_LIMIT:
            self.close_connection = True
            return
        while remaining > 0:
            chunk = self.rfile.read(min(CHUNK, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
        self._body_read += remaining

    def _read_body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        self._body_read += len(raw)
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}

    # -- authentication ----------------------------------------------------

    def _cookie(self, name):
        raw = self.headers.get("Cookie") or ""
        for part in raw.split(";"):
            key, _, value = part.strip().partition("=")
            if key == name:
                return value
        return ""

    def _authorised(self):
        """Resolve the caller to an account, or None if they cannot be let in.

        A signed session cookie is checked first: that is how the browser stays
        signed in, and unlike HTTP Basic it can actually be signed out again.
        Basic is still accepted so curl and other API clients keep working.

        With no accounts configured the server is open and the caller is
        treated as an admin, as it behaved before accounts existed.
        """
        store = self.server.users
        if not store.any():
            self.current_user = {"username": "", "role": users_module.ADMIN, "open": True}
            return True

        token = self._cookie(SESSION_COOKIE)
        if token:
            username = read_session(self.server.secret, token)
            if username:
                user = store.get(username)
                if user:
                    self.current_user = user
                    return True

        header = self.headers.get("Authorization", "")
        if header.startswith("Basic "):
            try:
                decoded = base64.b64decode(header[6:]).decode("utf-8")
                username, _, password = decoded.partition(":")
            except (ValueError, UnicodeDecodeError):
                return False
            user = store.authenticate(username, password)
            if user:
                self.current_user = user
                return True
        return False

    @property
    def user_key(self):
        """Whose progress we are reading or writing."""
        return (self.current_user or {}).get("username", "")

    @property
    def is_admin(self):
        return (self.current_user or {}).get("role") == users_module.ADMIN

    def _require_admin(self):
        """True when the caller may change things; sends a 403 when not."""
        if self.is_admin:
            return True
        self._error(HTTPStatus.FORBIDDEN, "this needs an admin account")
        return False

    def _challenge(self):
        """Refuse the request.

        Only an API client that already tried HTTP Basic gets a
        WWW-Authenticate header back. Sending it to a browser would pop the
        native credentials box, which cannot be dismissed or signed out of;
        the player shows its own sign-in screen on a plain 401 instead.
        """
        body = json.dumps({"error": "sign in required", "signInRequired": True}).encode()
        self.send_response(HTTPStatus.UNAUTHORIZED)
        if self.headers.get("Authorization", "").startswith("Basic "):
            self.send_header("WWW-Authenticate",
                             'Basic realm="Shortform Audio Bookshelf", charset="UTF-8"')
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _base_url(self):
        host = self.headers.get("Host") or f"{self.server.server_address[0]}:{self.server.server_address[1]}"
        return f"http://{host}"

    # -- routing -----------------------------------------------------------

    def do_GET(self):
        self._route()

    def do_HEAD(self):
        self._route()

    def do_POST(self):
        self._body_read = 0
        self.current_user = None
        try:
            self._route_post()
        finally:
            # Whatever happened, the request body must be off the socket before
            # the next request on this connection is parsed.
            try:
                self._discard_body()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass

    def _route_post(self):
        parsed = urllib.parse.urlparse(self.path)
        parts = [p for p in parsed.path.split("/") if p]
        query = urllib.parse.parse_qs(parsed.query)

        # Signing in and out must work without already being signed in.
        if parts == ["api", "login"]:
            return self._handle_login()
        if parts == ["api", "logout"]:
            return self._handle_logout()

        if not self._authorised():
            return self._challenge()
        try:
            if parts == ["api", "rescan"]:
                return self._handle_rescan()
            if parts == ["api", "settings"]:
                return self._handle_save_settings()
            if parts == ["api", "settings", "password", "verify"]:
                return self._handle_verify_password()
            if parts == ["api", "upload"]:
                return self._handle_upload(query)
            if parts == ["api", "organise"]:
                return self._handle_organise()
            if parts == ["api", "import"]:
                return self._handle_import()
            if parts == ["api", "authors"]:
                return self._handle_author_apply()
            if parts[:2] == ["api", "users"] and len(parts) in (2, 4):
                return self._handle_user_change(parts[1:])
            if parts == ["api", "metadata", "fetch-all"]:
                return self._handle_fetch_all()
            if parts == ["api", "metadata", "fetch-all", "stop"]:
                if not self._require_admin():
                    return
                self.server.metadata_job.stop()
                return self._send_json({"stopping": True})
            if len(parts) == 3 and parts[:2] == ["api", "progress"]:
                return self._handle_set_progress(parts[2])
            if len(parts) == 4 and parts[:2] == ["api", "books"] and parts[3] == "metadata":
                return self._handle_metadata_apply(parts[2])
            if len(parts) == 4 and parts[:2] == ["api", "books"] and parts[3] == "remove":
                return self._handle_remove_book(parts[2])
            if len(parts) == 4 and parts[:2] == ["api", "books"] and parts[3] == "restore":
                return self._handle_restore_book(parts[2])
            self._error(HTTPStatus.NOT_FOUND, "unknown endpoint")
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            self.log_message("error handling %s: %s", self.path, exc)
            try:
                self._error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _route(self):
        self.current_user = None
        parsed = urllib.parse.urlparse(self.path)
        # The page shell carries no library data, so it loads before sign-in
        # and shows its own sign-in screen.
        shell = [urllib.parse.unquote(p) for p in parsed.path.split("/") if p]
        if not shell or (len(shell) == 1 and shell[0] in ("index.html", "app.js", "style.css")):
            return self._serve_static(shell[0] if shell else "index.html")
        if shell == ["api", "first-run"]:
            return self._route_api(shell[1:], urllib.parse.parse_qs(parsed.query))
        if not self._authorised():
            return self._challenge()
        parts = [urllib.parse.unquote(p) for p in parsed.path.split("/") if p]
        query = urllib.parse.parse_qs(parsed.query)
        try:
            if not parts or parts == ["index.html"]:
                return self._serve_static("index.html")
            if parts[0] == "api":
                return self._route_api(parts[1:], query)
            if parts[0] == "audio" and len(parts) == 3:
                return self._handle_audio(parts[1], parts[2])
            if parts[0] == "cover" and len(parts) == 2:
                return self._handle_cover(parts[1])
            if len(parts) == 1:
                return self._serve_static(parts[0])
            self._error(HTTPStatus.NOT_FOUND, "not found")
        except (BrokenPipeError, ConnectionResetError):
            pass  # the browser seeked or closed the tab mid-stream
        except Exception as exc:  # a bad file should not take the server down
            self.log_message("error handling %s: %s", self.path, exc)
            try:
                self._error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _route_api(self, parts, query):
        if parts == ["library"]:
            return self._handle_library()
        if parts == ["settings"]:
            return self._handle_get_settings()
        if parts == ["first-run"]:
            # The default credentials are only offered to a client on the local
            # network. Printing them on a publicly reachable sign-in page would
            # hand the library to anyone who found the address.
            local = is_private_client(self.client_address[0])
            return self._send_json({
                "defaultPassword": (local
                                    and self.server.users.using_default_password()),
                "accountsConfigured": self.server.users.any(),
                "stateWritable": getattr(self.server, "state_writable", True),
                "stateProblem": getattr(self.server, "state_problem", ""),
            })
        if parts == ["users"]:
            return self._handle_users()

        if parts == ["browse"]:
            return self._handle_browse(query)
        if parts == ["authors", "search"]:
            return self._handle_author_search(query)
        if parts == ["metadata", "fetch-all", "status"]:
            return self._send_json(self.server.metadata_job.snapshot())
        if parts == ["organise", "status"]:
            return self._send_json(self.server.organise_job.snapshot())
        if parts == ["progress"]:
            return self._send_json(self.progress.all(self.user_key))
        if len(parts) == 2 and parts[0] == "books":
            return self._handle_book(parts[1])
        if len(parts) == 3 and parts[0] == "books" and parts[2].startswith("playlist"):
            return self._handle_playlist(parts[1], query)
        if len(parts) == 4 and parts[0] == "books" and parts[2:] == ["metadata", "search"]:
            return self._handle_metadata_search(parts[1], query)
        if len(parts) == 2 and parts[0] == "progress":
            entry = self.progress.get(self.user_key, parts[1])
            return self._send_json(entry or {})
        return self._error(HTTPStatus.NOT_FOUND, "unknown endpoint")

    # -- handlers ----------------------------------------------------------

    def _enriched_progress(self, book, entry):
        """Add book-wide elapsed time and fraction to a saved resume point.

        Only the server knows each track's offset within the book, so the
        library grid would otherwise draw "chapter 5 of 12, 2 minutes in" as
        two minutes into the whole book.
        """
        if not entry:
            return None
        index = min(max(0, int(entry.get("track", 0))), len(book["tracks"]) - 1)
        offset = book["tracks"][index]["offset"] if book["tracks"] else 0.0
        elapsed = offset + float(entry.get("position", 0.0))
        total = book["duration"]
        fraction = 1.0 if entry.get("finished") else (
            max(0.0, min(1.0, elapsed / total)) if total > 0 else 0.0
        )
        return dict(entry, elapsed=round(elapsed, 3), fraction=round(fraction, 4))

    def _handle_library(self):
        hidden = self.server.removed.ids()
        with self.server.scan_lock:
            books = [b for b in self.lib.books if b["id"] not in hidden]
        saved = self.progress.all(self.user_key)
        progress = {}
        for book in books:
            entry = saved.get(book["id"])
            if entry:
                progress[book["id"]] = self._enriched_progress(book, entry)
        self._send_json(
            {
                "root": self.lib.root,
                "scannedAt": self.lib.scanned_at,
                "count": len(books),
                "books": [self._apply_overlay(self.lib.book_summary(b), b["id"]) for b in books],
                "progress": progress,
            }
        )

    def _handle_book(self, book_id):
        book = self.lib.by_id.get(book_id)
        if book and book_id in self.server.removed.ids():
            book = None
        if not book:
            return self._error(HTTPStatus.NOT_FOUND, "book not found")
        detail = self._apply_overlay(self.lib.book_detail(book), book_id)
        detail["progress"] = self._enriched_progress(book, self.progress.get(self.user_key, book_id))
        detail["authorBio"] = self.server.authors.get(book["author"]) if book["author"] else None
        self._send_json(detail)

    def _handle_rescan(self):
        if not self._require_admin():
            return
        with self.server.scan_lock:
            started = time.time()
            books = self.lib.scan()
            self.lib.save_index(self.server.index_path)
        self._send_json(
            {
                "count": len(books),
                "tracks": sum(b["track_count"] for b in books),
                "duplicates": sum(len(b.get("duplicates") or []) for b in books),
                "seconds": round(time.time() - started, 2),
            }
        )

    def _handle_set_progress(self, book_id):
        if book_id not in self.lib.by_id:
            return self._error(HTTPStatus.NOT_FOUND, "book not found")
        body = self._read_body()
        if body.get("clear"):
            self.progress.clear(self.user_key, book_id)
            return self._send_json({"cleared": True})
        entry = self.progress.set(
            self.user_key, book_id,
            body.get("track", 0),
            body.get("position", 0),
            body.get("finished", False),
        )
        self._send_json(entry)

    # -- settings ----------------------------------------------------------

    def _handle_get_settings(self):
        values = settings_module.load(self.server.settings_file)
        self._send_json({
            # Report the library actually loaded, not merely the saved setting:
            # showing a stale default here would let Save switch libraries by
            # accident when the server was started with a path argument.
            "library": self.lib.root,
            "savedLibrary": values["library"],
            "output": values["output"],
            "host": values["host"],
            "port": values["port"],
            "username": self.user_key,
            "role": (self.current_user or {}).get("role", ""),
            "authEnabled": self.server.users.any(),
            "settingsFile": self.server.settings_file,
            "stateDir": self.server.paths["dir"],
            "activeLibrary": self.lib.root,          # what is loaded right now
            "libraryExists": os.path.isdir(self.lib.root),
            "stateWritable": getattr(self.server, "state_writable", True),
            "stateProblem": getattr(self.server, "state_problem", ""),
            "inContainer": in_container(),
            "libraryWritable": directory_writable(self.lib.root)[0],
            "libraryProblem": directory_writable(self.lib.root)[1],
            "outputWritable": directory_writable(values["output"])[0] if values["output"] else True,
            "outputProblem": directory_writable(values["output"])[1] if values["output"] else "",
            "importDir": getattr(self.server, "import_dir", "") or values.get("import_dir", ""),
            "importExists": os.path.isdir(getattr(self.server, "import_dir", "")
                                          or values.get("import_dir", "") or "/nonexistent"),
            "bookCount": len(self.lib.books),
            "boundHost": self.server.bound[0],
            "boundPort": self.server.bound[1],
        })

    def _handle_browse(self, query):
        if not self._require_admin():
            return
        """List the directories inside one directory, for the folder picker.

        The picker has to run on the server: a browser file input cannot hand
        back a real filesystem path, and you may well be setting this up from a
        phone. Only directory names and a count of audio files are returned —
        never file contents — and the whole endpoint sits behind the login when
        one is configured.
        """
        raw = (query.get("path") or [""])[0].strip()
        path = os.path.abspath(os.path.expanduser(raw)) if raw else os.path.expanduser("~")
        if not os.path.isdir(path):
            return self._error(HTTPStatus.NOT_FOUND, f"not a directory: {path}")

        directories, audio_files = [], 0
        try:
            with os.scandir(path) as entries:
                for entry in entries:
                    if entry.name.startswith("."):
                        continue
                    try:
                        if entry.is_dir():
                            directories.append({"name": entry.name,
                                                "path": os.path.join(path, entry.name)})
                        elif os.path.splitext(entry.name)[1].lower() in library.PLAYABLE_EXTENSIONS:
                            audio_files += 1
                    except OSError:
                        continue
        except PermissionError:
            return self._error(HTTPStatus.FORBIDDEN, f"no permission to read {path}")

        directories.sort(key=lambda d: library.natural_key(d["name"]))
        parent = os.path.dirname(path)
        values = settings_module.load(self.server.settings_file)
        shortcuts = [
            {"name": "Home", "path": os.path.expanduser("~")},
            {"name": "Library", "path": self.lib.root},
        ]
        if values.get("output"):
            shortcuts.append({"name": "Output", "path": values["output"]})
        if os.path.isdir("/Volumes"):
            shortcuts.append({"name": "Volumes", "path": "/Volumes"})

        self._send_json({
            "path": path,
            "parent": parent if parent != path else None,
            "directories": directories,
            "audioFiles": audio_files,
            "writable": os.access(path, os.W_OK),
            "shortcuts": [s for s in shortcuts if s["path"] and os.path.isdir(s["path"])],
        })

    def _handle_metadata_search(self, book_id, query):
        if not self._require_admin():
            return
        """Look the book up at Audible / iTunes / Google and return candidates.

        Nothing is stored here — the caller picks one. Searching a few thousand
        books automatically would hammer services that are doing us a favour by
        being open, so this is one book at a time.
        """
        book = self.lib.by_id.get(book_id)
        if not book:
            return self._error(HTTPStatus.NOT_FOUND, "book not found")

        title = (query.get("title") or [""])[0].strip() or book["title"]
        author = (query.get("author") or [""])[0].strip() or book["author"]
        requested = (query.get("providers") or ["audible,itunes"])[0]
        providers = tuple(p for p in requested.split(",") if p in metadata_module.PROVIDERS)

        candidates, errors = metadata_module.search(
            title, author, providers=providers or ("audible", "itunes"), limit=5
        )
        self._send_json({
            "query": {"title": title, "author": author, "providers": list(providers)},
            "candidates": candidates,
            "errors": errors,
        })

    def _handle_metadata_apply(self, book_id):
        if not self._require_admin():
            return
        book = self.lib.by_id.get(book_id)
        if not book:
            return self._error(HTTPStatus.NOT_FOUND, "book not found")
        body = self._read_body()

        if body.get("clear"):
            self.metadata_store.clear(book_id)
            for stale in glob.glob(os.path.join(self.server.paths["covers"], f"{book_id}.fetched.*")):
                _remove(stale)
            return self._send_json({"cleared": True})

        entry, stored_cover = apply_metadata(
            self.metadata_store, self.server.paths["covers"], book_id, body,
            want_cover=bool(body.get("applyCover", True)),
        )
        self._send_json({"applied": True, "coverStored": stored_cover, "entry": entry})

    def _handle_login(self):
        body = self._read_body()
        username = users_module.clean_username(body.get("username", ""))
        user = self.server.users.authenticate(username, str(body.get("password", "")))
        if not user:
            # One message for both wrong-user and wrong-password, so the form
            # cannot be used to find out which accounts exist.
            return self._error(HTTPStatus.UNAUTHORIZED, "wrong username or password")

        token = make_session(self.server.secret, user["username"])
        payload = json.dumps({"signedIn": True, "username": user["username"],
                              "role": user["role"]}).encode()
        self.send_response(HTTPStatus.OK)
        self.send_header("Set-Cookie",
                         f"{SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite=Lax; "
                         f"Max-Age={SESSION_DAYS * 86400}")
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def _handle_logout(self):
        """A real sign-out: the cookie is expired, so the next request is
        anonymous. Nothing is cached by the browser to get stuck on."""
        payload = json.dumps({"signedOut": True}).encode()
        self.send_response(HTTPStatus.OK)
        self.send_header("Set-Cookie",
                         f"{SESSION_COOKIE}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0")
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def _handle_users(self):
        """The account list, plus who is asking — the UI hides what they cannot do."""
        store = self.server.users
        self._send_json({
            "you": {"username": self.user_key,
                    "role": (self.current_user or {}).get("role", ""),
                    "open": bool((self.current_user or {}).get("open"))},
            "accountsConfigured": store.any(),
            "defaultPassword": store.using_default_password(),
            "users": store.list() if self.is_admin else [],
        })

    def _handle_user_change(self, parts):
        """Create, update or delete an account.

        A listener may change their own password and nothing else; everything
        else needs an admin.
        """
        store = self.server.users
        body = self._read_body()
        # parts arrives without the leading "api": ["users"] or ["users", name, action]
        action = parts[-1] if len(parts) > 2 else "create"
        target = users_module.clean_username(parts[1]) if len(parts) > 2 else ""

        try:
            if action == "create":
                if not self._require_admin():
                    return
                created = store.create(
                    body.get("username", ""), body.get("password", ""),
                    body.get("role", users_module.LISTENER),
                )
                return self._send_json({"created": created})

            if action == "password":
                own = bool(target) and target == self.user_key
                if not own and not self._require_admin():
                    return
                # Changing your own password means proving you know it.
                if own and not self.is_admin:
                    if not store.authenticate(target, str(body.get("current", ""))):
                        return self._error(HTTPStatus.FORBIDDEN, "current password is wrong")
                store.set_password(target, body.get("password", ""))
                return self._send_json({"updated": target})

            if action == "role":
                if not self._require_admin():
                    return
                store.set_role(target, body.get("role", ""))
                return self._send_json({"updated": target, "role": body.get("role")})

            if action == "delete":
                if not self._require_admin():
                    return
                store.delete(target)
                self.progress.forget_user(target)
                return self._send_json({"deleted": target})
        except ValueError as exc:
            return self._error(HTTPStatus.BAD_REQUEST, str(exc))

        self._error(HTTPStatus.NOT_FOUND, "unknown account action")

    def _handle_author_search(self, query):
        if not self._require_admin():
            return
        name = (query.get("name") or [""])[0].strip()
        if not name:
            return self._error(HTTPStatus.BAD_REQUEST, "no author name given")
        candidates, errors = metadata_module.search_authors(name, limit=3)
        self._send_json({"name": name, "candidates": candidates, "errors": errors})

    def _handle_author_apply(self):
        if not self._require_admin():
            return
        body = self._read_body()
        author = str(body.get("author", "")).strip()
        if not author:
            return self._error(HTTPStatus.BAD_REQUEST, "no author given")
        if body.get("clear"):
            self.server.authors.clear(author)
            return self._send_json({"cleared": True})
        entry = self.server.authors.set(author, body)
        self._send_json({"applied": True, "entry": entry})

    def _handle_fetch_all(self):
        """Look up details for every book that has no description yet."""
        if not self._require_admin():
            return
        body = self._read_body()
        providers = tuple(p for p in (body.get("providers") or ["audible", "itunes"])
                          if p in metadata_module.PROVIDERS) or ("audible", "itunes")
        try:
            min_score = int(body.get("minScore", MetadataJob.MIN_SCORE))
        except (TypeError, ValueError):
            min_score = MetadataJob.MIN_SCORE

        pending = []
        for book in self.lib.books:
            if book.get("description"):
                continue                       # the file already says something
            if self.metadata_store.get(book["id"]):
                continue                       # already looked up
            pending.append({"id": book["id"], "title": book["title"], "author": book["author"]})

        started = self.server.metadata_job.start(
            pending, self.metadata_store, self.server.paths["covers"],
            providers, min_score, bool(body.get("covers", True)),
            author_store=self.server.authors if body.get("authors", True) else None,
        )
        if not started:
            return self._error(HTTPStatus.CONFLICT, "a lookup is already running")
        self._send_json({"started": True, "queued": len(pending),
                         "estimateSeconds": int(len(pending) * (MetadataJob.DELAY + 0.8))})

    def _handle_remove_book(self, book_id):
        if not self._require_admin():
            return
        """Hide a book, and optionally delete its files.

        Deleting is irreversible, so it needs an explicit confirm flag as well
        as the button press — a stray request must not be able to erase audio.
        """
        book = self.lib.by_id.get(book_id)
        if not book:
            return self._error(HTTPStatus.NOT_FOUND, "book not found")
        body = self._read_body()
        delete_files = bool(body.get("deleteFiles"))

        if delete_files and not body.get("confirm"):
            return self._error(HTTPStatus.BAD_REQUEST,
                               "deleting files needs confirm: true")

        deleted, failed = [], []
        if delete_files:
            for track in book["tracks"]:
                try:
                    os.remove(track["path"])
                    deleted.append(track["path"])
                except FileNotFoundError:
                    deleted.append(track["path"])
                except OSError as exc:
                    failed.append({"path": track["path"], "error": str(exc)})
            # Take the book's own folder with it, but only when it is empty and
            # is not the library root itself.
            directory = book.get("directory") or ""
            if (not failed and directory and os.path.abspath(directory) != self.lib.root
                    and os.path.isdir(directory) and not os.listdir(directory)):
                try:
                    os.rmdir(directory)
                except OSError:
                    pass

        self.server.removed.add(book_id, {
            "title": book["title"], "author": book["author"],
            "paths": [t["path"] for t in book["tracks"]],
            "filesDeleted": delete_files and not failed,
            "removedAt": time.time(),
        })
        self.progress.forget_book(book_id)
        self.metadata_store.clear(book_id)
        for stale in glob.glob(os.path.join(self.server.paths["covers"], f"{book_id}.fetched.*")):
            _remove(stale)

        self._send_json({
            "removed": True,
            "filesDeleted": len(deleted),
            "failed": failed,
            "hiddenOnly": not delete_files,
        })

    def _handle_restore_book(self, book_id):
        if not self._require_admin():
            return
        # Check before popping: a book whose files were deleted must stay
        # hidden, and an early pop would un-hide it with no audio behind it.
        entry = self.server.removed.get(book_id)
        if not entry:
            return self._error(HTTPStatus.NOT_FOUND, "that book was not removed")
        if entry.get("filesDeleted"):
            return self._send_json({
                "restored": False,
                "error": "its files were deleted, so there is nothing to restore",
            })
        missing = [p for p in entry.get("paths", []) if not os.path.exists(p)]
        if missing:
            return self._send_json({
                "restored": False,
                "error": f"{len(missing)} of its files are no longer on disk",
            })
        self.server.removed.restore(book_id)
        self._send_json({"restored": True, "title": entry.get("title", "")})

    def _handle_import(self):
        """Bring files in from the download folder into the library.

        This is the mirror of organise: the source is the download folder, the
        destination is the library that actually gets served. Nothing is ever
        played out of the download folder.
        """
        if not self._require_admin():
            return
        body = self._read_body()
        source = str(body.get("importDir")
                     or getattr(self.server, "import_dir", "")
                     or settings_module.load(self.server.settings_file).get("import_dir")
                     or "").strip()
        if not source:
            return self._error(HTTPStatus.BAD_REQUEST,
                               "no download folder set — choose one in Settings first")
        source = os.path.abspath(os.path.expanduser(source))
        if not os.path.isdir(source):
            return self._error(HTTPStatus.BAD_REQUEST, f"not a directory: {source}")
        if organise_module._overlaps(source, self.lib.root):
            return self._error(HTTPStatus.BAD_REQUEST,
                               "the download folder overlaps the library — they must be separate")

        try:
            incoming = library.Library(source, cache_path=None,
                                       cover_dir=self.server.paths["covers"])
            incoming.scan()
        except FileNotFoundError as exc:
            return self._error(HTTPStatus.BAD_REQUEST, str(exc))

        started = self.server.organise_job.start(
            incoming, self.lib.root,
            dry_run=bool(body.get("dryRun")),
            overwrite=False,
            playlists_only=False,
            delete_originals=bool(body.get("deleteOriginals")) and bool(body.get("confirm")),
        )
        if not started:
            return self._error(HTTPStatus.CONFLICT, "an import or organise is already running")
        self._send_json({"started": True, "source": source,
                         "books": len(incoming.books), "destination": self.lib.root})

    def _handle_organise(self):
        """Start an organise run into the configured output folder."""
        if not self._require_admin():
            return
        body = self._read_body()
        values = settings_module.load(self.server.settings_file)
        output = str(body.get("output") or values.get("output") or "").strip()
        if not output:
            return self._error(HTTPStatus.BAD_REQUEST,
                               "no output folder set — choose one in Settings first")
        output = os.path.abspath(os.path.expanduser(output))
        if organise_module._overlaps(self.lib.root, output):
            return self._error(HTTPStatus.BAD_REQUEST,
                               "the output folder overlaps the library — choose one outside it")

        started = self.server.organise_job.start(
            self.lib, output,
            dry_run=bool(body.get("dryRun")),
            overwrite=bool(body.get("overwrite")),
            playlists_only=bool(body.get("playlistsOnly")),
            delete_originals=bool(body.get("deleteOriginals")) and bool(body.get("confirm")),
        )
        if not started:
            return self._error(HTTPStatus.CONFLICT, "an organise is already running")
        self._send_json({"started": True, "output": output})

    def _handle_verify_password(self):
        """Check a password without changing anything, so you can confirm the
        one you have before replacing it."""
        body = self._read_body()
        username = users_module.clean_username(body.get("username") or self.user_key)
        if not self.server.users.any():
            return self._send_json({"valid": False, "accountsConfigured": False})
        if username != self.user_key and not self.is_admin:
            return self._error(HTTPStatus.FORBIDDEN, "this needs an admin account")
        valid = bool(self.server.users.authenticate(username, str(body.get("password", ""))))
        self._send_json({"valid": valid, "accountsConfigured": True})

    def _handle_save_settings(self):
        if not self._require_admin():
            return
        body = self._read_body()
        values = settings_module.load(self.server.settings_file)

        library_path = str(body.get("library", "")).strip()
        if library_path:
            expanded = os.path.abspath(os.path.expanduser(library_path))
            if not os.path.isdir(expanded):
                return self._error(HTTPStatus.BAD_REQUEST, f"not a directory: {expanded}")
            values["library"] = expanded

        output_path = str(body.get("output", "")).strip()
        values["output"] = os.path.abspath(os.path.expanduser(output_path)) if output_path else ""

        import_path = str(body.get("importDir", "")).strip()
        values["import_dir"] = (os.path.abspath(os.path.expanduser(import_path))
                                if import_path else "")
        self.server.import_dir = values["import_dir"]

        if body.get("host"):
            values["host"] = str(body["host"]).strip()
        if body.get("port"):
            try:
                port = int(body["port"])
            except (TypeError, ValueError):
                return self._error(HTTPStatus.BAD_REQUEST, "port must be a number")
            if not 1 <= port <= 65535:
                return self._error(HTTPStatus.BAD_REQUEST, "port must be between 1 and 65535")
            values["port"] = port

        # Try the address change before writing anything: if the port is taken,
        # the saved settings should keep describing where the server actually
        # is, not an address it failed to reach.
        moved, rebind_error = False, None
        if (values["host"], values["port"]) != self.server.bound:
            rebind_error = rebind(self.server, values["host"], values["port"])
            if rebind_error:
                values["host"], values["port"] = self.server.bound
            else:
                moved = True

        settings_module.save(values, self.server.settings_file)

        # Pointing at a different library means loading it, not just recording it.
        rescanned = False
        if values["library"] != self.lib.root:
            with self.server.scan_lock:
                fresh = library.Library(values["library"],
                                        cache_path=self.server.paths["cache"],
                                        cover_dir=self.server.paths["covers"])
                fresh.scan()
                fresh.save_index(self.server.index_path)
                self.server.library = fresh
            rescanned = True

        self._send_json({
            "saved": True,
            "rescanned": rescanned,
            "bookCount": len(self.lib.books),
            "rebound": moved,
            "rebindError": rebind_error,
            "port": values["port"],
            "host": values["host"],
        })

    def _handle_login(self):
        body = self._read_body()
        username = users_module.clean_username(body.get("username", ""))
        user = self.server.users.authenticate(username, str(body.get("password", "")))
        if not user:
            # One message for both wrong-user and wrong-password, so the form
            # cannot be used to find out which accounts exist.
            return self._error(HTTPStatus.UNAUTHORIZED, "wrong username or password")

        token = make_session(self.server.secret, user["username"])
        payload = json.dumps({"signedIn": True, "username": user["username"],
                              "role": user["role"]}).encode()
        self.send_response(HTTPStatus.OK)
        self.send_header("Set-Cookie",
                         f"{SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite=Lax; "
                         f"Max-Age={SESSION_DAYS * 86400}")
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def _handle_logout(self):
        """A real sign-out: the cookie is expired, so the next request is
        anonymous. Nothing is cached by the browser to get stuck on."""
        payload = json.dumps({"signedOut": True}).encode()
        self.send_response(HTTPStatus.OK)
        self.send_header("Set-Cookie",
                         f"{SESSION_COOKIE}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0")
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def _handle_users(self):
        """The account list, plus who is asking — the UI hides what they cannot do."""
        store = self.server.users
        self._send_json({
            "you": {"username": self.user_key,
                    "role": (self.current_user or {}).get("role", ""),
                    "open": bool((self.current_user or {}).get("open"))},
            "accountsConfigured": store.any(),
            "defaultPassword": store.using_default_password(),
            "users": store.list() if self.is_admin else [],
        })

    def _handle_user_change(self, parts):
        """Create, update or delete an account.

        A listener may change their own password and nothing else; everything
        else needs an admin.
        """
        store = self.server.users
        body = self._read_body()
        # parts arrives without the leading "api": ["users"] or ["users", name, action]
        action = parts[-1] if len(parts) > 2 else "create"
        target = users_module.clean_username(parts[1]) if len(parts) > 2 else ""

        try:
            if action == "create":
                if not self._require_admin():
                    return
                created = store.create(
                    body.get("username", ""), body.get("password", ""),
                    body.get("role", users_module.LISTENER),
                )
                return self._send_json({"created": created})

            if action == "password":
                own = bool(target) and target == self.user_key
                if not own and not self._require_admin():
                    return
                # Changing your own password means proving you know it.
                if own and not self.is_admin:
                    if not store.authenticate(target, str(body.get("current", ""))):
                        return self._error(HTTPStatus.FORBIDDEN, "current password is wrong")
                store.set_password(target, body.get("password", ""))
                return self._send_json({"updated": target})

            if action == "role":
                if not self._require_admin():
                    return
                store.set_role(target, body.get("role", ""))
                return self._send_json({"updated": target, "role": body.get("role")})

            if action == "delete":
                if not self._require_admin():
                    return
                store.delete(target)
                self.progress.forget_user(target)
                return self._send_json({"deleted": target})
        except ValueError as exc:
            return self._error(HTTPStatus.BAD_REQUEST, str(exc))

        self._error(HTTPStatus.NOT_FOUND, "unknown account action")

    def _handle_author_search(self, query):
        if not self._require_admin():
            return
        name = (query.get("name") or [""])[0].strip()
        if not name:
            return self._error(HTTPStatus.BAD_REQUEST, "no author name given")
        candidates, errors = metadata_module.search_authors(name, limit=3)
        self._send_json({"name": name, "candidates": candidates, "errors": errors})

    def _handle_author_apply(self):
        if not self._require_admin():
            return
        body = self._read_body()
        author = str(body.get("author", "")).strip()
        if not author:
            return self._error(HTTPStatus.BAD_REQUEST, "no author given")
        if body.get("clear"):
            self.server.authors.clear(author)
            return self._send_json({"cleared": True})
        entry = self.server.authors.set(author, body)
        self._send_json({"applied": True, "entry": entry})

    def _handle_fetch_all(self):
        """Look up details for every book that has no description yet."""
        if not self._require_admin():
            return
        body = self._read_body()
        providers = tuple(p for p in (body.get("providers") or ["audible", "itunes"])
                          if p in metadata_module.PROVIDERS) or ("audible", "itunes")
        try:
            min_score = int(body.get("minScore", MetadataJob.MIN_SCORE))
        except (TypeError, ValueError):
            min_score = MetadataJob.MIN_SCORE

        pending = []
        for book in self.lib.books:
            if book.get("description"):
                continue                       # the file already says something
            if self.metadata_store.get(book["id"]):
                continue                       # already looked up
            pending.append({"id": book["id"], "title": book["title"], "author": book["author"]})

        started = self.server.metadata_job.start(
            pending, self.metadata_store, self.server.paths["covers"],
            providers, min_score, bool(body.get("covers", True)),
            author_store=self.server.authors if body.get("authors", True) else None,
        )
        if not started:
            return self._error(HTTPStatus.CONFLICT, "a lookup is already running")
        self._send_json({"started": True, "queued": len(pending),
                         "estimateSeconds": int(len(pending) * (MetadataJob.DELAY + 0.8))})

    def _handle_remove_book(self, book_id):
        if not self._require_admin():
            return
        """Hide a book, and optionally delete its files.

        Deleting is irreversible, so it needs an explicit confirm flag as well
        as the button press — a stray request must not be able to erase audio.
        """
        book = self.lib.by_id.get(book_id)
        if not book:
            return self._error(HTTPStatus.NOT_FOUND, "book not found")
        body = self._read_body()
        delete_files = bool(body.get("deleteFiles"))

        if delete_files and not body.get("confirm"):
            return self._error(HTTPStatus.BAD_REQUEST,
                               "deleting files needs confirm: true")

        deleted, failed = [], []
        if delete_files:
            for track in book["tracks"]:
                try:
                    os.remove(track["path"])
                    deleted.append(track["path"])
                except FileNotFoundError:
                    deleted.append(track["path"])
                except OSError as exc:
                    failed.append({"path": track["path"], "error": str(exc)})
            # Take the book's own folder with it, but only when it is empty and
            # is not the library root itself.
            directory = book.get("directory") or ""
            if (not failed and directory and os.path.abspath(directory) != self.lib.root
                    and os.path.isdir(directory) and not os.listdir(directory)):
                try:
                    os.rmdir(directory)
                except OSError:
                    pass

        self.server.removed.add(book_id, {
            "title": book["title"], "author": book["author"],
            "paths": [t["path"] for t in book["tracks"]],
            "filesDeleted": delete_files and not failed,
            "removedAt": time.time(),
        })
        self.progress.forget_book(book_id)
        self.metadata_store.clear(book_id)
        for stale in glob.glob(os.path.join(self.server.paths["covers"], f"{book_id}.fetched.*")):
            _remove(stale)

        self._send_json({
            "removed": True,
            "filesDeleted": len(deleted),
            "failed": failed,
            "hiddenOnly": not delete_files,
        })

    def _handle_restore_book(self, book_id):
        if not self._require_admin():
            return
        # Check before popping: a book whose files were deleted must stay
        # hidden, and an early pop would un-hide it with no audio behind it.
        entry = self.server.removed.get(book_id)
        if not entry:
            return self._error(HTTPStatus.NOT_FOUND, "that book was not removed")
        if entry.get("filesDeleted"):
            return self._send_json({
                "restored": False,
                "error": "its files were deleted, so there is nothing to restore",
            })
        missing = [p for p in entry.get("paths", []) if not os.path.exists(p)]
        if missing:
            return self._send_json({
                "restored": False,
                "error": f"{len(missing)} of its files are no longer on disk",
            })
        self.server.removed.restore(book_id)
        self._send_json({"restored": True, "title": entry.get("title", "")})

    def _handle_import(self):
        """Bring files in from the download folder into the library.

        This is the mirror of organise: the source is the download folder, the
        destination is the library that actually gets served. Nothing is ever
        played out of the download folder.
        """
        if not self._require_admin():
            return
        body = self._read_body()
        source = str(body.get("importDir")
                     or getattr(self.server, "import_dir", "")
                     or settings_module.load(self.server.settings_file).get("import_dir")
                     or "").strip()
        if not source:
            return self._error(HTTPStatus.BAD_REQUEST,
                               "no download folder set — choose one in Settings first")
        source = os.path.abspath(os.path.expanduser(source))
        if not os.path.isdir(source):
            return self._error(HTTPStatus.BAD_REQUEST, f"not a directory: {source}")
        if organise_module._overlaps(source, self.lib.root):
            return self._error(HTTPStatus.BAD_REQUEST,
                               "the download folder overlaps the library — they must be separate")

        try:
            incoming = library.Library(source, cache_path=None,
                                       cover_dir=self.server.paths["covers"])
            incoming.scan()
        except FileNotFoundError as exc:
            return self._error(HTTPStatus.BAD_REQUEST, str(exc))

        started = self.server.organise_job.start(
            incoming, self.lib.root,
            dry_run=bool(body.get("dryRun")),
            overwrite=False,
            playlists_only=False,
            delete_originals=bool(body.get("deleteOriginals")) and bool(body.get("confirm")),
        )
        if not started:
            return self._error(HTTPStatus.CONFLICT, "an import or organise is already running")
        self._send_json({"started": True, "source": source,
                         "books": len(incoming.books), "destination": self.lib.root})

    def _handle_organise(self):
        """Start an organise run into the configured output folder."""
        if not self._require_admin():
            return
        body = self._read_body()
        values = settings_module.load(self.server.settings_file)
        output = str(body.get("output") or values.get("output") or "").strip()
        if not output:
            return self._error(HTTPStatus.BAD_REQUEST,
                               "no output folder set — choose one in Settings first")
        output = os.path.abspath(os.path.expanduser(output))
        if organise_module._overlaps(self.lib.root, output):
            return self._error(HTTPStatus.BAD_REQUEST,
                               "the output folder overlaps the library — choose one outside it")

        started = self.server.organise_job.start(
            self.lib, output,
            dry_run=bool(body.get("dryRun")),
            overwrite=bool(body.get("overwrite")),
            playlists_only=bool(body.get("playlistsOnly")),
            delete_originals=bool(body.get("deleteOriginals")) and bool(body.get("confirm")),
        )
        if not started:
            return self._error(HTTPStatus.CONFLICT, "an organise is already running")
        self._send_json({"started": True, "output": output})

    def _handle_verify_password(self):
        """Check a password without changing anything, so you can confirm the
        one you have before replacing it."""
        body = self._read_body()
        username = users_module.clean_username(body.get("username") or self.user_key)
        if not self.server.users.any():
            return self._send_json({"valid": False, "accountsConfigured": False})
        if username != self.user_key and not self.is_admin:
            return self._error(HTTPStatus.FORBIDDEN, "this needs an admin account")
        valid = bool(self.server.users.authenticate(username, str(body.get("password", ""))))
        self._send_json({"valid": valid, "accountsConfigured": True})

    def _handle_save_settings(self):
        if not self._require_admin():
            return
        body = self._read_body()
        values = settings_module.load(self.server.settings_file)

        library_path = str(body.get("library", "")).strip()
        if library_path:
            expanded = os.path.abspath(os.path.expanduser(library_path))
            if not os.path.isdir(expanded):
                return self._error(HTTPStatus.BAD_REQUEST, f"not a directory: {expanded}")
            values["library"] = expanded

        output_path = str(body.get("output", "")).strip()
        values["output"] = os.path.abspath(os.path.expanduser(output_path)) if output_path else ""

        import_path = str(body.get("importDir", "")).strip()
        values["import_dir"] = (os.path.abspath(os.path.expanduser(import_path))
                                if import_path else "")
        self.server.import_dir = values["import_dir"]

        if body.get("host"):
            values["host"] = str(body["host"]).strip()
        if body.get("port"):
            try:
                port = int(body["port"])
            except (TypeError, ValueError):
                return self._error(HTTPStatus.BAD_REQUEST, "port must be a number")
            if not 1 <= port <= 65535:
                return self._error(HTTPStatus.BAD_REQUEST, "port must be between 1 and 65535")
            values["port"] = port

        # Try the address change before writing anything: if the port is taken,
        # the saved settings should keep describing where the server actually
        # is, not an address it failed to reach.
        moved, rebind_error = False, None
        if (values["host"], values["port"]) != self.server.bound:
            rebind_error = rebind(self.server, values["host"], values["port"])
            if rebind_error:
                values["host"], values["port"] = self.server.bound
            else:
                moved = True

        settings_module.save(values, self.server.settings_file)

        # Pointing at a different library means loading it, not just recording it.
        rescanned = False
        if values["library"] != self.lib.root:
            with self.server.scan_lock:
                fresh = library.Library(values["library"],
                                        cache_path=self.server.paths["cache"],
                                        cover_dir=self.server.paths["covers"])
                fresh.scan()
                fresh.save_index(self.server.index_path)
                self.server.library = fresh
            rescanned = True

        self._send_json({
            "saved": True,
            "rescanned": rescanned,
            "bookCount": len(self.lib.books),
            "rebound": moved,
            "rebindError": rebind_error,
            "port": values["port"],
            "host": values["host"],
        })

    def _handle_set_password(self):
        body = self._read_body()
        values = settings_module.load(self.server.settings_file)
        already_set = bool(values["username"] and values["password_hash"])

        # Once a login exists, changing or removing it needs the current one —
        # otherwise anyone already holding an open page could take it over.
        if already_set and not settings_module.verify_password(
            str(body.get("current", "")), values["password_hash"]
        ):
            return self._error(HTTPStatus.FORBIDDEN, "current password is wrong")

        if body.get("clear"):
            values["username"], values["password_hash"] = "", ""
            settings_module.save(values, self.server.settings_file)
            self.server.auth = None
            return self._send_json({"authEnabled": False})

        username = str(body.get("username", "")).strip()
        password = str(body.get("password", ""))
        if not username or not password:
            return self._error(HTTPStatus.BAD_REQUEST, "username and password are both required")

        values["username"] = username
        values["password_hash"] = settings_module.hash_password(password)
        settings_module.save(values, self.server.settings_file)
        self.server.auth = {"username": username, "password_hash": values["password_hash"]}
        self._send_json({"authEnabled": True, "username": username})

    # -- upload ------------------------------------------------------------

    def _handle_upload(self, query):
        if not self._require_admin():
            return self._discard_body()
        """Receive one file into the library root.

        The body is the raw file — no multipart — with the name in the query
        string, so the client can send a File object straight from a picker or
        a drop and we can stream it to disk without buffering it in memory.
        """
        raw_name = (query.get("name") or [""])[0]
        name = _safe_upload_name(raw_name)
        if not name:
            return self._error(HTTPStatus.BAD_REQUEST, "missing or unusable filename")
        extension = os.path.splitext(name)[1].lower()
        if extension not in library.PLAYABLE_EXTENSIONS:
            return self._error(
                HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                f"{extension or 'that'} is not an audio format this player handles",
            )

        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._error(HTTPStatus.BAD_REQUEST, "bad Content-Length")
        if length <= 0:
            return self._error(HTTPStatus.BAD_REQUEST, "empty upload")
        if length > MAX_UPLOAD:
            return self._error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                               f"file is larger than the {MAX_UPLOAD // (1 << 30)} GB limit")

        root = self.lib.root
        if not os.path.isdir(root):
            return self._error(HTTPStatus.CONFLICT, f"library directory is missing: {root}")

        destination = _unused_path(os.path.join(root, name))
        temp = f"{destination}.{os.getpid()}.part"
        written = 0
        try:
            with open(temp, "wb") as fh:
                while written < length:
                    chunk = self.rfile.read(min(CHUNK, length - written))
                    if not chunk:
                        break
                    fh.write(chunk)
                    written += len(chunk)
                    self._body_read += len(chunk)
            if written != length:
                raise OSError(f"upload ended early ({written} of {length} bytes)")
            # An .opus that is not really Ogg would sit in the library as an
            # unplayable ghost, so check before putting it in place.
            if extension in library.TAGGED_EXTENSIONS:
                oggopus.read(temp)
            os.replace(temp, destination)
        except oggopus.OggFormatError:
            _remove(temp)
            return self._error(HTTPStatus.BAD_REQUEST,
                               f"{name} is not a readable Ogg/Opus file")
        except OSError as exc:
            _remove(temp)
            return self._error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))

        self._send_json({
            "stored": os.path.basename(destination),
            "renamed": os.path.basename(destination) != name,
            "bytes": written,
            "directory": root,
        })

    def _handle_playlist(self, book_id, query):
        book = self.lib.by_id.get(book_id)
        if not book:
            return self._error(HTTPStatus.NOT_FOUND, "book not found")
        local = query.get("local", ["0"])[0] not in ("0", "", "false")
        base = self._base_url()
        author = book["author"] or "Unknown author"

        lines = ["#EXTM3U", f"#PLAYLIST:{author} - {book['title']}"]
        for track in book["tracks"]:
            seconds = int(track["duration"]) if track["duration"] else -1
            lines.append(f"#EXTINF:{seconds},{author} - {track['title']}")
            if local:
                lines.append(track["path"])
            else:
                lines.append(f"{base}/audio/{book_id}/{track['index']}")
        body = ("\n".join(lines) + "\n").encode("utf-8")

        safe = re.sub(r"[^\w\-. ]+", "_", f"{author} - {book['title']}").strip() or "playlist"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "audio/x-mpegurl; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Disposition", f'attachment; filename="{safe}.m3u"')
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _handle_cover(self, book_id):
        book = self.lib.by_id.get(book_id)
        if not book:
            return self._error(HTTPStatus.NOT_FOUND, "book not found")
        # A cover pulled from a metadata provider wins, then the book's own
        # folder, then art extracted from the tags on first request.
        path = self._overlay_cover(book_id) or self.lib.cover_path(book)
        if path:
            mime = mimetypes.guess_type(path)[0] or "image/jpeg"
            return self._stream_file(path, mime, cacheable=True)

        # Caching the extracted art failed — usually an unwritable state
        # directory. Serve it straight from the tags rather than showing a
        # broken image for every book.
        picture = self.lib.cover_bytes(book)
        if picture:
            mime, data = picture
            return self._send_bytes(data, mime, cache="public, max-age=3600")

        # Distinguish "the scan saw no art" from "the art will not parse now",
        # because they have completely different causes.
        if not book.get("cover_embedded"):
            return self._error(HTTPStatus.NOT_FOUND,
                               "the scan found no cover art in this book's tags — "
                               "rescan if the files have changed since")
        self._error(HTTPStatus.NOT_FOUND,
                    "the tags claim cover art but it could not be read now")

    def _handle_audio(self, book_id, track_index):
        book = self.lib.by_id.get(book_id)
        if not book:
            return self._error(HTTPStatus.NOT_FOUND, "book not found")
        try:
            index = int(track_index)
        except ValueError:
            return self._error(HTTPStatus.BAD_REQUEST, "bad track index")
        if not 0 <= index < len(book["tracks"]):
            return self._error(HTTPStatus.NOT_FOUND, "track not found")
        track = book["tracks"][index]
        if not os.path.exists(track["path"]):
            return self._error(HTTPStatus.NOT_FOUND, "file missing — rescan the library")
        self._stream_file(track["path"], track["mime"], cacheable=True)

    # -- byte-range file streaming ----------------------------------------

    def _stream_file(self, path, mime, cacheable=False):
        size = os.path.getsize(path)
        start, end = 0, size - 1
        status = HTTPStatus.OK
        header = self.headers.get("Range")

        if header:
            match = _RANGE.search(header)
            if match:
                first, last = match.group(1), match.group(2)
                if first:
                    start = int(first)
                    if last:
                        end = min(int(last), size - 1)
                elif last:  # suffix range: the final N bytes
                    start = max(0, size - int(last))
                if start >= size or start > end:
                    self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                status = HTTPStatus.PARTIAL_CONTENT

        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if status == HTTPStatus.PARTIAL_CONTENT:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header(
            "Cache-Control", "public, max-age=3600" if cacheable else "no-store"
        )
        self.end_headers()
        if self.command == "HEAD":
            return

        with open(path, "rb") as fh:
            fh.seek(start)
            remaining = length
            while remaining > 0:
                chunk = fh.read(min(CHUNK, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    # -- static player files ----------------------------------------------

    def _serve_static(self, name):
        safe = os.path.normpath(name).lstrip("./\\")
        path = os.path.join(WEB_DIR, safe)
        if not os.path.abspath(path).startswith(WEB_DIR) or not os.path.isfile(path):
            return self._error(HTTPStatus.NOT_FOUND, "not found")
        mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
        compressible = (mime.startswith("text/")
                        or mime in ("application/javascript", "application/json"))
        if compressible:
            mime += "; charset=utf-8"
            try:
                with open(path, "rb") as fh:
                    body = fh.read()
            except OSError:
                return self._error(HTTPStatus.NOT_FOUND, "not found")
            body, encoding = self._compress(body, mime)
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", mime)
            if encoding:
                self.send_header("Content-Encoding", encoding)
                self.send_header("Vary", "Accept-Encoding")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
            return
        self._stream_file(path, mime)


def configure_server(httpd, **attributes):
    """Attach the shared state a Handler needs onto a server instance."""
    for name, value in attributes.items():
        setattr(httpd, name, value)
    httpd.daemon_threads = True
    return httpd


def rebind(current, host, port):
    """Move the server to a new address without restarting the process.

    The new socket is opened *before* the old one is closed, so a port that is
    already taken leaves the running server exactly as it was instead of
    dropping you with nothing listening. The swap itself happens in
    serve_forever's loop back in main().
    """
    if (host, port) == current.bound:
        return None
    try:
        replacement = ThreadingHTTPServer((host, port), Handler)
    except OSError as exc:
        return exc.strerror or str(exc)

    configure_server(
        replacement,
        library=current.library, progress=current.progress, metadata=current.metadata,
        authors=current.authors, metadata_job=current.metadata_job, users=current.users,
        secret=current.secret,
        removed=current.removed, scan_lock=current.scan_lock,
        verbose=current.verbose, index_path=current.index_path,
        settings_file=current.settings_file,
        organise_job=current.organise_job, state=current.state,
        paths=current.paths, import_dir=current.import_dir,
        state_writable=current.state_writable,
        state_problem=current.state_problem, bound=(host, port),
    )
    current.state["next"] = replacement
    # Let the response reach the browser before the socket goes away.
    threading.Thread(
        target=lambda: (time.sleep(0.4), current.shutdown()), daemon=True
    ).start()
    return None


def local_ip():
    """Best-effort LAN address, so the printed URL works from a phone."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))  # no packets are sent by connect() on UDP
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def build_parser():
    parser = argparse.ArgumentParser(description="Shortform Audio Bookshelf — audiobook server and player")
    parser.add_argument("root", nargs="?", help="library directory (default: the saved setting)")
    parser.add_argument("--output", help="directory --organise writes the tidy copy into")
    parser.add_argument("--import-dir", dest="import_dir",
                        help="download folder to import from (files are taken out of it, "
                             "not played from it)")
    parser.add_argument("--port", type=int)
    parser.add_argument("--host", help="bind address (default: all interfaces)")
    parser.add_argument("-v", "--verbose", action="store_true", default=None, help="log every request")

    parser.add_argument("--rescan", action="store_true", help="re-read the directory instead of loading index.json")
    parser.add_argument("--scan-only", action="store_true", help="print the library and exit")
    parser.add_argument("--no-cache", action="store_true", help="ignore the per-file metadata cache")
    parser.add_argument("--index", help="index file (default: index.json in the state directory)")
    parser.add_argument("--state-dir", default=settings_module.STATE_DIR,
                        help="where index, cache, covers, progress and settings live "
                             f"(default: $SHORTLIST_STATE_DIR or {settings_module.STATE_DIR})")

    parser.add_argument("--organise", nargs="?", const=True, metavar="DIR",
                        help="copy the library into a tidy tree and exit (default: the saved output directory)")
    parser.add_argument("--dry-run", action="store_true", help="with --organise, print the plan and write nothing")
    parser.add_argument("--overwrite", action="store_true",
                        help="with --organise, replace existing files even when not higher quality")
    parser.add_argument("--playlists-only", action="store_true",
                        help="with --organise, write .m3u files only and copy no audio")

    parser.add_argument("--settings-list", action="store_true", help="print the current settings and exit")
    parser.add_argument("--save-settings", action="store_true", help="store the given options as the defaults")
    parser.add_argument("--set-password", nargs="?", const=True, metavar="USERNAME",
                        help="create or reset an admin account, then exit")
    parser.add_argument("--list-users", action="store_true", help="print the accounts and exit")
    parser.add_argument("--remove-accounts", action="store_true",
                        help="delete every account, leaving the server open (last resort)")
    parser.add_argument("--settings-file", help="settings file (default: settings.json in the state directory)")
    return parser


def _resolve(args):
    """Settings file, overridden by whichever flags were actually given."""
    values = settings_module.load(args.settings_file)
    for key, given in (
        ("library", args.root), ("output", args.output), ("import_dir", args.import_dir),
        ("port", args.port),
        ("host", args.host), ("verbose", args.verbose),
    ):
        if given is not None:
            values[key] = os.path.abspath(os.path.expanduser(given)) if key in ("library", "output") else given
    return values


def _configure_password(args, paths):
    """Create or reset an admin account from the command line.

    This is the way back in when nobody can sign in: it writes straight to
    users.json, which is the only thing the server actually reads.
    """
    store = users_module.UserStore(os.path.join(paths["dir"], "users.json"))
    existing = store.list()
    if existing:
        print("Accounts:")
        for account in existing:
            print(f"  {account['username']} ({account['role']})")
        print()

    username = (args.set_password if isinstance(args.set_password, str) else "").strip()
    if not username:
        username = input("Username to create or reset: ").strip()
    if not username:
        print("Cancelled — no changes made.")
        return 1

    password = getpass.getpass("New password: ")
    if not password:
        print("Cancelled — an empty password would leave the account unusable.")
        return 1
    if password != getpass.getpass("Confirm:      "):
        print("Passwords did not match — no changes made.")
        return 1

    try:
        if store.get(username):
            store.set_password(username, password)
            store.set_role(username, users_module.ADMIN)
            print(f"\nPassword reset for {username}, and they are an admin.")
        else:
            store.create(username, password, users_module.ADMIN)
            print(f"\nCreated admin account {username}.")
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print("Restart the server if it is running, then sign in at the player.")
    return 0


def _list_users(paths):
    store = users_module.UserStore(os.path.join(paths["dir"], "users.json"))
    accounts = store.list()
    if not accounts:
        print("No accounts — the server is open to anyone who can reach it.")
        return 0
    print(f"{len(accounts)} account(s) in {os.path.join(paths['dir'], 'users.json')}:")
    for account in accounts:
        print(f"  {account['username']:<20} {account['role']}")
    return 0


def _remove_all_accounts(paths):
    """Last resort: delete every account so the server is open again."""
    path = os.path.join(paths["dir"], "users.json")
    if not os.path.exists(path):
        print("There are no accounts to remove.")
        return 0
    answer = input(
        "This deletes every account and leaves the server open to anyone on\n"
        "your network. Type REMOVE to confirm: "
    ).strip()
    if answer != "REMOVE":
        print("Cancelled — no changes made.")
        return 1
    try:
        os.replace(path, path + ".removed")
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"Accounts moved to {path}.removed — the server is now open.")
    print("Create a new admin in the player, or with --set-password.")
    return 0


def main(argv=None):
    args = build_parser().parse_args(argv)

    # Everything persistent hangs off the state directory unless overridden
    # one file at a time.
    paths = state_paths(args.state_dir)
    args.settings_file = args.settings_file or paths["settings"]
    args.index = args.index or paths["index"]
    values = _resolve(args)

    if args.settings_list:
        print(f"Settings file: {args.settings_file}")
        print(settings_module.describe(values))
        return 0
    if args.list_users:
        return _list_users(paths)
    if args.set_password:
        return _configure_password(args, paths)
    if args.remove_accounts:
        return _remove_all_accounts(paths)
    if args.save_settings:
        path = settings_module.save(values, args.settings_file)
        print(f"Saved to {path}:\n{settings_module.describe(values)}")

    lib = library.Library(
        values["library"],
        cache_path=None if args.no_cache else paths["cache"],
        cover_dir=paths["covers"],
    )

    # Load the previous scan unless asked not to; scanning is the fallback.
    loaded = False
    if not (args.rescan or args.scan_only or args.organise) and not args.no_cache:
        loaded = lib.load_index(args.index)
    # An index with nothing in it is worth no time at all to rebuild, and
    # trusting it strands anyone who has just corrected a wrong mount: the
    # library is full, the index says empty, and only a manual Rescan bridges
    # the two.
    if loaded and not lib.books and os.path.isdir(lib.root):
        print("The saved index is empty — scanning in case the library has changed.")
        loaded = False

    if loaded:
        print(f"Loaded {len(lib.books)} books from {args.index}")
        drift = lib.index_drift()
        if drift:
            print(f"warning: {len(drift)} indexed file(s) changed or went missing "
                  f"(e.g. {os.path.basename(drift[0][0])}) — press Rescan in the player")
    else:
        print(f"Scanning {lib.root} …")
        started = time.time()
        try:
            lib.scan()
        except FileNotFoundError as exc:
            # Serving on regardless is deliberate. Exiting here means a
            # container restart loop and a browser showing nothing at all,
            # which tells you far less than a running player that says the
            # folder is missing and lets you point it somewhere else.
            if args.scan_only or args.organise:
                print(f"error: {exc}", file=sys.stderr)
                print("Set one with:  python3 server.py /path/to/audiobooks --save-settings",
                      file=sys.stderr)
                return 1
            print(f"warning: {exc}")
            print("Starting anyway with an empty library — set the folder in the player "
                  "under Settings, or with --save-settings.")
        tracks = sum(b["track_count"] for b in lib.books)
        duplicates = sum(len(b.get("duplicates") or []) for b in lib.books)
        print(f"Found {len(lib.books)} book{'s' if len(lib.books) != 1 else ''} "
              f"({tracks} file{'s' if tracks != 1 else ''}) in {time.time() - started:.1f}s"
              + (f" · dropped {duplicates} duplicate file(s)" if duplicates else ""))
        lib.save_index(args.index)

    books = lib.books

    if args.scan_only:
        for book in books:
            author = book["author"] or "Unknown author"
            genre = f", {book['genre']}" if book.get("genre") else ""
            print(f"\n  {author} — {book['title']}  [{book['duration_text']}, "
                  f"{book['track_count']} file(s){genre}]")
            for track in book["tracks"]:
                print(f"      {track['index'] + 1:>3}. {track['title']}  "
                      f"({library.format_duration(track['duration'])})")
            for duplicate in book.get("duplicates") or []:
                print(f"      · duplicate ignored (lower quality): {duplicate['dropped']}")
        return 0

    if args.organise:
        destination = args.organise if isinstance(args.organise, str) else values["output"]
        if not destination:
            print("error: no output directory. Pass --organise DIR, or save one with "
                  "--output DIR --save-settings", file=sys.stderr)
            return 1
        try:
            manifest = organise_module.organise(
                lib, destination, dry_run=args.dry_run, overwrite=args.overwrite,
                playlists_only=args.playlists_only,
            )
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(f"\n{'Would write' if args.dry_run else 'Wrote'}: {organise_module.summarise(manifest)}")
        if not args.dry_run:
            print(f"Output: {os.path.abspath(os.path.expanduser(destination))}")
        return 0

    if not books:
        print(f"warning: no audio files under {lib.root} — the player will be empty")

    try:
        httpd = ThreadingHTTPServer((values["host"], values["port"]), Handler)
    except OSError as exc:
        print(f"error: cannot listen on {values['host']}:{values['port']} — {exc.strerror}",
              file=sys.stderr)
        print(f"Another copy may already be running. Try --port {values['port'] + 1}.",
              file=sys.stderr)
        return 1

    # Accounts live in their own file. A login set before accounts existed is
    # adopted as the first admin so nobody is locked out by upgrading.
    user_store = users_module.UserStore(os.path.join(paths["dir"], "users.json"))
    progress_store = ProgressStore(paths["progress"])
    # Nothing persists if the state directory cannot be written, which on a NAS
    # is almost always the config folder being owned by root while the container
    # runs as PUID. Say so loudly and keep serving: a container that exits here
    # just restart-loops, and the reason is buried in logs nobody can reach.
    writable, why = state_dir_writable(paths["dir"])
    if not writable:
        print("\n" + "!" * 70, file=sys.stderr)
        print(f"  {paths['dir']} is not writable: {why}", file=sys.stderr)
        print("  Accounts, resume positions and the index cannot be saved.", file=sys.stderr)
        print("  In Docker this is usually the config folder's owner not matching", file=sys.stderr)
        print("  PUID/PGID. On the host:", file=sys.stderr)
        print(f"      sudo chown -R {os.getuid()}:{os.getgid()} <your config folder>", file=sys.stderr)
        print("!" * 70 + "\n", file=sys.stderr)

    for label, folder in (("library", values["library"]),
                          ("download", values["import_dir"]),
                          ("output", values["output"])):
        if not folder:
            continue
        # A distinct name: reusing `why` here overwrote the state directory's
        # reason, so the player reported another folder's problem as the
        # config folder's, alongside a contradictory "writable: true".
        ok, folder_problem = directory_writable(folder)
        if not ok and os.path.isdir(folder):
            print(f"warning: the {label} folder {folder} is not writable ({folder_problem}). "
                  f"Uploads and organising will fail. "
                  f"Try: chown -R {os.getuid()}:{os.getgid()} {folder}", file=sys.stderr)

    if not user_store.any() and not (values.get("username") and values.get("password_hash")):
        # A brand-new install: seed a default admin so there is always a way in.
        try:
            if user_store.seed_default():
                print(f"Created the default admin account "
                      f"'{users_module.DEFAULT_USERNAME}' / '{users_module.DEFAULT_PASSWORD}' "
                      f"— change it in the player.")
        except ValueError as exc:
            print(f"warning: {exc}", file=sys.stderr)
    if values.get("username") and values.get("password_hash") and not user_store.any():
        if user_store.adopt(values["username"], values["password_hash"]):
            # Positions saved before accounts existed sit in the anonymous
            # bucket; they belong to the person who had the only login.
            moved = progress_store.rename_user("", values["username"])
            print(f"Existing login '{values['username']}' is now an admin account"
                  + (f", with {moved} saved position(s)." if moved else "."))

    state = {}
    configure_server(
        httpd,
        library=lib,
        progress=progress_store,
        metadata=MetadataStore(os.path.join(paths["dir"], "metadata.json")),
        authors=AuthorStore(os.path.join(paths["dir"], "authors.json")),
        removed=RemovedStore(os.path.join(paths["dir"], "removed.json")),
        users=user_store,
        secret=session_secret(paths["dir"]),
        metadata_job=MetadataJob(),
        scan_lock=threading.Lock(),
        verbose=bool(values["verbose"]),
        index_path=args.index,
        settings_file=args.settings_file,
        bound=(values["host"], values["port"]),
        organise_job=OrganiseJob(),
        state=state,
        paths=paths,
        import_dir=values["import_dir"],
        state_writable=writable,
        state_problem=why,
    )

    def banner(server):
        host, port = server.bound
        print(f"\n  Player:   http://localhost:{port}")
        if host in ("0.0.0.0", "::"):
            print(f"  Network:  http://{local_ip()}:{port}   (phones, tablets, other machines)")
        accounts = server.users.list()
        if accounts:
            print("  Accounts: " + ", ".join(f"{a['username']} ({a['role']})" for a in accounts))
        else:
            print("  Accounts: none — anyone on the network can listen and change things")
        if server.users.using_default_password():
            print(f"\n  !! Signing in with {users_module.DEFAULT_USERNAME} / "
                  f"{users_module.DEFAULT_PASSWORD} still works. Change it in the\n"
                  f"     player under the account menu, Settings, Accounts.")
        print("\nCtrl-C to stop.\n")

    banner(httpd)

    # Changing host or port in the web UI swaps in a new server here rather
    # than asking for a restart the browser cannot perform.
    try:
        while True:
            httpd.serve_forever()
            httpd.server_close()
            replacement = state.pop("next", None)
            if replacement is None:
                break
            httpd = replacement
            print(f"Moved to {httpd.bound[0]}:{httpd.bound[1]}")
            banner(httpd)
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
