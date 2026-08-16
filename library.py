"""Scan a directory of audio files and group them into audiobooks.

The hard part is not reading files, it is deciding what counts as a book.
Libraries in the wild are inconsistent: some books are one long file, some are
a folder of chapters, some have clean tags and some have none at all. The
scanner therefore works in two passes:

  1. Files that carry an ALBUM tag are grouped by (album artist, album). Tags
     win whenever they exist, because they are the author's own answer.
  2. Everything else is grouped by directory, and each directory is inspected
     to decide whether its files are chapters of one book or a shelf of
     separate single-file books (see ``_looks_like_one_book``).

A ``shortlist.json`` file at the library root can override any of this.
"""

from __future__ import annotations

import glob
import hashlib
import json
import os
import re
import time

import oggopus

# Formats we can read tags from natively, plus ones we can still play.
TAGGED_EXTENSIONS = {".opus", ".ogg", ".oga"}
PLAYABLE_EXTENSIONS = TAGGED_EXTENSIONS | {".m4b", ".m4a", ".mp3", ".flac", ".wav", ".aac"}
COVER_NAMES = ("cover", "folder", "front", "artwork", "album")
COVER_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp")
OVERRIDE_FILE = "shortlist.json"

MIME_TYPES = {
    ".opus": "audio/ogg",
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
    ".m4b": "audio/mp4",
    ".m4a": "audio/mp4",
    ".mp3": "audio/mpeg",
    ".flac": "audio/flac",
    ".wav": "audio/wav",
    ".aac": "audio/aac",
}

# Leading track markers: "01 - ", "1.", "Chapter 3 -", "Part 02_", "CD1 Track 4"
_TRACK_PREFIX = re.compile(
    r"^\s*(?:(?:cd|disc)\s*\d+\s*[-_. ]*)?"
    r"(?:(?:chapter|chap|ch|part|pt|track|tr)[\s._-]*)?"
    r"\d{1,4}\s*(?:of\s*\d{1,4})?\s*[-_.)\]]*\s*",
    re.IGNORECASE,
)
_CHAPTERISH = re.compile(r"(?:^|\W)(?:chapter|chap|ch|part|pt|track|disc|cd)\W*\d", re.IGNORECASE)
_LEADING_NUMBER = re.compile(r"^\s*\d{1,4}\b")
_DIGITS = re.compile(r"(\d+)")
_NOISE = re.compile(
    r"\b(?:unabridged|abridged|audiobook|audio\s*book|mp3|opus|\d{2,3}\s*kbps|\d{2,3}k)\b",
    re.IGNORECASE,
)
_ONLY_TRACK_MARKER = re.compile(
    r"^\s*(?:(?:cd|disc)\s*\d+\s*[-_. ]*)?"
    r"(?:chapter|chap|ch|part|pt|track|tr)?[\s._-]*\d{1,4}\s*(?:of\s*\d{1,4})?\s*$",
    re.IGNORECASE,
)

_WORD_NUMBERS = {
    w: n
    for n, w in enumerate(
        "zero one two three four five six seven eight nine ten eleven twelve "
        "thirteen fourteen fifteen sixteen seventeen eighteen nineteen twenty".split()
    )
}
_WORD_NUMBERS.update(
    {"thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}
)
_WORD_NUMBER_RE = re.compile(r"\b(" + "|".join(sorted(_WORD_NUMBERS, key=len, reverse=True)) + r")\b", re.IGNORECASE)


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def natural_key(text):
    """Sort key that orders "track2" before "track10"."""
    return [int(p) if p.isdigit() else p.lower() for p in _DIGITS.split(text)]


def track_sort_key(text):
    """Like natural_key, but audiobooks also spell their numbers out:
    "Part One" must sort before "Part Two", not after "Part Three"."""
    return natural_key(_WORD_NUMBER_RE.sub(lambda m: str(_WORD_NUMBERS[m.group(0).lower()]), text))


def clean_name(raw):
    """Tidy a filename or directory name for display."""
    name = raw.replace("_", " ").strip()
    name = _NOISE.sub("", name)
    name = re.sub(r"[\[({]\s*[\])}]", "", name)      # empty brackets left behind
    name = re.sub(r"\s*[-–—]\s*$", "", name)
    return re.sub(r"\s{2,}", " ", name).strip(" -–—_.") or raw


def strip_track_prefix(stem):
    stripped = _TRACK_PREFIX.sub("", stem)
    return stripped if len(stripped) >= 2 else stem


def parse_track_number(tags):
    """TRACKNUMBER may be "7", "07", or "7/12"."""
    raw = tags.get("tracknumber") or tags.get("track") or ""
    match = re.match(r"\s*(\d+)", str(raw))
    return int(match.group(1)) if match else None


def bitrate(size, duration):
    """Bits per second, or 0 when the duration is unknown."""
    return (size * 8.0) / duration if duration and duration > 0 else 0.0


def is_higher_quality(candidate, incumbent, margin=0.05):
    """True when `candidate` is meaningfully better than `incumbent`.

    Both arguments are dicts carrying ``size`` and ``duration``. Bitrate is the
    real comparison; file size only stands in when neither side has a known
    duration. If exactly one side has a duration the two are not comparable on
    equal terms, so we keep what is already there. The margin stops a rounding
    difference from triggering a pointless re-copy.
    """
    candidate_rate = bitrate(candidate.get("size", 0), candidate.get("duration", 0))
    incumbent_rate = bitrate(incumbent.get("size", 0), incumbent.get("duration", 0))
    if candidate_rate and incumbent_rate:
        return candidate_rate > incumbent_rate * (1 + margin)
    if candidate_rate or incumbent_rate:
        return False
    return candidate.get("size", 0) > incumbent.get("size", 0) * (1 + margin)


def same_recording(a, b, tolerance=0.01):
    """Whether two files plausibly hold the same audio at different quality.

    Identical content re-encoded keeps its running time, so a duration match is
    the check that separates "the same chapter twice" from "two chapters that
    happen to sit at the same track number".
    """
    first, second = a.get("duration") or 0.0, b.get("duration") or 0.0
    if first <= 0 or second <= 0:
        return False  # unknown durations: not safe to call it a duplicate
    return abs(first - second) <= max(1.0, min(first, second) * tolerance)


def book_id(author, title):
    digest = hashlib.sha1(f"{author}\x00{title}".encode("utf-8")).hexdigest()
    return digest[:12]


def format_duration(seconds):
    seconds = int(seconds or 0)
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


# --------------------------------------------------------------------------
# file-level metadata
# --------------------------------------------------------------------------

def _read_file(path, rel_path):
    """Metadata for one audio file. Never raises — unreadable tags just mean
    we fall back to the filename, which is exactly what untagged files need."""
    stat = os.stat(path)
    info = {
        "path": path,
        "rel": rel_path,
        "size": stat.st_size,
        "mtime": stat.st_mtime,
        "duration": 0.0,
        "tags": {},
        "has_picture": False,
        "tagged": False,
    }
    if os.path.splitext(path)[1].lower() in TAGGED_EXTENSIONS:
        try:
            parsed = oggopus.read(path)
            info["tags"] = parsed["tags"]
            info["duration"] = parsed["duration"]
            info["has_picture"] = parsed["has_picture"]
            info["tagged"] = True
        except Exception:
            # Malformed or half-copied files must not abort a whole scan; the
            # filename-derived fallback still gives us a playable entry.
            pass
    return info


def _looks_like_one_book(files):
    """Decide whether several untagged files in one directory are chapters of a
    single book, or a shelf of separate single-file books.

    Chapters almost always announce themselves: sequential leading numbers, or
    an explicit "Chapter 4" marker, or a long shared filename prefix.
    """
    if len(files) < 2:
        return True
    stems = [os.path.splitext(os.path.basename(f["path"]))[0] for f in files]

    numbered = sum(1 for s in stems if _LEADING_NUMBER.match(s) or _CHAPTERISH.search(s))
    if numbered >= max(2, int(len(stems) * 0.6)):
        return True

    prefix = os.path.commonprefix([s.lower() for s in stems]).strip()
    return len(prefix) >= 4


def _directory_book_names(directory, root, stem):
    """Guess (author, title) from where a file sits in the tree."""
    rel_dir = os.path.relpath(directory, root)
    parts = [] if rel_dir == "." else rel_dir.split(os.sep)
    if not parts:
        return "", clean_name(strip_track_prefix(stem))
    if len(parts) == 1:
        return "", clean_name(parts[0])
    return clean_name(parts[-2]), clean_name(parts[-1])


def _find_cover(directory, memo=None):
    """Look for cover art sitting next to the audio.

    Memoised per directory: in a flat library every book shares one directory,
    and listing plus sorting a few thousand entries once per book turns the
    scan quadratic.
    """
    if memo is not None and directory in memo:
        return memo[directory]
    result = _scan_for_cover(directory)
    if memo is not None:
        memo[directory] = result
    return result


def _scan_for_cover(directory):
    try:
        entries = sorted(os.listdir(directory), key=natural_key)
    except OSError:
        return None
    images = [e for e in entries if os.path.splitext(e)[1].lower() in COVER_EXTENSIONS]
    for preferred in COVER_NAMES:
        for name in images:
            if os.path.splitext(name)[0].lower() == preferred:
                return os.path.join(directory, name)
    return os.path.join(directory, images[0]) if images else None


# --------------------------------------------------------------------------
# scanning
# --------------------------------------------------------------------------

COVER_MIME_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
}


class Library:
    def __init__(self, root, cache_path=None, cover_dir=None):
        self.root = os.path.abspath(os.path.expanduser(root))
        self.cache_path = cache_path
        # Where cover art pulled out of tags is written. Without it, embedded
        # covers are re-parsed out of the audio file on every single request.
        self.cover_dir = cover_dir
        self.books = []
        self.by_id = {}
        self.scanned_at = 0.0
        self._cache = self._load_cache()
        self._dir_covers = {}  # directory -> cover path, valid for one scan

    # ---- cache of parsed file metadata, keyed by path/size/mtime ----------

    def _load_cache(self):
        if not self.cache_path or not os.path.exists(self.cache_path):
            return {}
        try:
            with open(self.cache_path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return {}

    def _save_cache(self, used_keys):
        if not self.cache_path:
            return
        pruned = {k: v for k, v in self._cache.items() if k in used_keys}
        try:
            os.makedirs(os.path.dirname(self.cache_path), exist_ok=True)
            temp = f"{self.cache_path}.{os.getpid()}.tmp"
            with open(temp, "w", encoding="utf-8") as fh:
                json.dump(pruned, fh)
            os.replace(temp, self.cache_path)
        except OSError:
            pass  # a cold cache is slow, not broken

    # ---- the scan --------------------------------------------------------

    def scan(self):
        if not os.path.isdir(self.root):
            raise FileNotFoundError(f"library directory not found: {self.root}")

        self._dir_covers = {}
        files, used_keys = [], set()
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
            for name in sorted(filenames, key=natural_key):
                if name.startswith("."):
                    continue
                if os.path.splitext(name)[1].lower() not in PLAYABLE_EXTENSIONS:
                    continue
                path = os.path.join(dirpath, name)
                try:
                    stat = os.stat(path)
                except OSError:
                    continue
                key = f"{path}|{stat.st_size}|{int(stat.st_mtime)}"
                used_keys.add(key)
                cached = self._cache.get(key)
                if cached:
                    info = dict(cached, path=path)
                else:
                    info = _read_file(path, os.path.relpath(path, self.root))
                    self._cache[key] = {k: v for k, v in info.items() if k != "path"}
                files.append(info)

        overrides = self._load_overrides()
        books = self._group(files, overrides)
        for book in books:
            self._finalise(book)

        books.sort(key=lambda b: (natural_key(b["author"] or "zz"), natural_key(b["title"])))
        self.books = books
        self.by_id = {b["id"]: b for b in books}
        self.scanned_at = time.time()
        self._save_cache(used_keys)
        return books

    def _load_overrides(self):
        """Optional shortlist.json: {"relative/path": {"author":…, "title":…}}.

        A path may name a file or a directory; directory entries apply to every
        file beneath them.
        """
        path = os.path.join(self.root, OVERRIDE_FILE)
        if not os.path.exists(path):
            return {}
        try:
            with open(path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except (OSError, ValueError):
            return {}
        return {os.path.normpath(k): v for k, v in raw.items() if isinstance(v, dict)}

    def _override_for(self, rel_path, overrides):
        if not overrides:
            return None
        candidate = os.path.normpath(rel_path)
        while True:
            if candidate in overrides:
                return overrides[candidate]
            parent = os.path.dirname(candidate)
            if parent == candidate or not parent:
                return None
            candidate = parent

    def _group(self, files, overrides):
        groups = {}       # key -> book dict under construction
        untagged = {}     # directory -> files with no album tag

        def bucket(key, author, title, info):
            book = groups.setdefault(
                key, {"author": author, "title": title, "files": []}
            )
            book["files"].append(info)

        for info in files:
            tags = info.get("tags") or {}
            stem = os.path.splitext(os.path.basename(info["path"]))[0]
            override = self._override_for(info["rel"], overrides)

            if override:
                author = str(override.get("author", "")).strip()
                title = str(override.get("title", "")).strip() or clean_name(stem)
                bucket(("ovr", author.lower(), title.lower()), author, title, info)
                continue

            album = (tags.get("album") or "").strip()
            if album:
                author = (
                    tags.get("albumartist")
                    or tags.get("album artist")
                    or tags.get("artist")
                    or ""
                ).strip()
                bucket(("tag", author.lower(), album.lower()), author, album, info)
            else:
                untagged.setdefault(os.path.dirname(info["path"]), []).append(info)

        for directory, group_files in untagged.items():
            if _looks_like_one_book(group_files):
                stem = os.path.splitext(os.path.basename(group_files[0]["path"]))[0]
                author, title = _directory_book_names(directory, self.root, stem)
                # A lone tagged artist still beats a guessed directory name.
                artist = (group_files[0].get("tags") or {}).get("artist", "").strip()
                author = author or artist
                key = ("dir", directory.lower(), title.lower())
                for info in group_files:
                    bucket(key, author, title, info)
            else:
                # A shelf of standalone books: one file, one book.
                for info in group_files:
                    tags = info.get("tags") or {}
                    stem = os.path.splitext(os.path.basename(info["path"]))[0]
                    title = (tags.get("title") or "").strip() or clean_name(
                        strip_track_prefix(stem)
                    )
                    author = (tags.get("artist") or "").strip()
                    if not author and os.path.dirname(info["path"]) != self.root:
                        author = clean_name(os.path.basename(directory))
                    bucket(("file", info["path"].lower()), author, title, info)

        return list(groups.values())

    def _deduplicate(self, files):
        """Collapse the same chapter appearing twice at different quality.

        Two rips of one book — a 32k and a 64k encode, say — carry the same
        tags, so they group into a single book and would otherwise show up as
        doubled chapters. Keep the higher bitrate copy of each.

        The matching is deliberately conservative. A file is only a candidate
        duplicate of another when it has the same disc/track number, or the
        same filename once track prefixes are stripped *and* that filename says
        something more than a bare number — otherwise "Disc 2/01.opus" would
        look like a second copy of "Disc 1/01.opus". On top of that the two
        durations have to match, because re-encoding does not change how long
        a chapter runs.
        """
        seen, kept, dropped = {}, [], []
        for info in files:
            tags = info.get("tags") or {}
            number = parse_track_number(tags)
            if number is not None:
                disc = re.match(r"\s*(\d+)", str(tags.get("discnumber") or "1"))
                key = ("n", disc.group(1) if disc else "1", number)
            else:
                stem = os.path.splitext(os.path.basename(info["path"]))[0]
                if _ONLY_TRACK_MARKER.match(stem):
                    kept.append(info)
                    continue
                key = ("s", clean_name(strip_track_prefix(stem)).lower())

            previous = seen.get(key)
            if previous is None or not same_recording(info, previous):
                if previous is None:
                    seen[key] = info
                kept.append(info)
                continue

            if is_higher_quality(info, previous):
                seen[key] = info
                kept[kept.index(previous)] = info
                dropped.append({"kept": info["path"], "dropped": previous["path"]})
            else:
                dropped.append({"kept": previous["path"], "dropped": info["path"]})
        return kept, dropped

    def _finalise(self, book):
        book["files"], book["duplicates"] = self._deduplicate(book["files"])
        files = book["files"]
        numbers = [parse_track_number(f.get("tags") or {}) for f in files]
        if all(n is not None for n in numbers) and len(set(numbers)) == len(numbers):
            order = sorted(zip(numbers, files), key=lambda pair: pair[0])
            files = [f for _, f in order]
        else:
            files.sort(key=lambda f: track_sort_key(f["rel"]))

        tracks, offset = [], 0.0
        for index, info in enumerate(files):
            tags = info.get("tags") or {}
            stem = os.path.splitext(os.path.basename(info["path"]))[0]
            title = (tags.get("title") or "").strip()
            if not title or title.lower() == book["title"].lower():
                if len(files) == 1:
                    title = clean_name(stem)
                elif _ONLY_TRACK_MARKER.match(stem):
                    # "01", "track 7", "Chapter 3" — the number is all there is,
                    # so a plain sequential label reads better than the filename.
                    title = f"Chapter {index + 1}"
                else:
                    title = clean_name(strip_track_prefix(stem)) or f"Chapter {index + 1}"
            tracks.append(
                {
                    "index": index,
                    "title": title,
                    "path": info["path"],
                    "rel": info["rel"],
                    "size": info["size"],
                    "mtime": info["mtime"],
                    "duration": round(float(info.get("duration") or 0.0), 3),
                    "offset": round(offset, 3),
                    "mime": MIME_TYPES.get(
                        os.path.splitext(info["path"])[1].lower(), "application/octet-stream"
                    ),
                }
            )
            offset += float(info.get("duration") or 0.0)

        first_tags = (files[0].get("tags") or {}) if files else {}
        directory = os.path.dirname(files[0]["path"]) if files else self.root

        book["files"] = files
        book["tracks"] = tracks
        book["id"] = book_id(book["author"], book["title"])
        book["duration"] = round(offset, 3)
        book["duration_text"] = format_duration(offset)
        book["track_count"] = len(tracks)
        book["directory"] = directory
        book["narrator"] = (first_tags.get("narrator") or first_tags.get("performer") or "").strip()
        book["year"] = (first_tags.get("date") or first_tags.get("year") or "")[:4]
        book["series"] = (first_tags.get("series") or "").strip()
        book["genre"] = (first_tags.get("genre") or "").strip()
        book["description"] = (first_tags.get("description") or first_tags.get("comment") or "").strip()
        book["untimed"] = offset <= 0.0  # e.g. mp3/m4b, which we don't decode
        book["cover_file"] = _find_cover(directory, self._dir_covers)
        book["cover_embedded"] = bool(files and files[0].get("has_picture"))
        book["has_cover"] = bool(book["cover_file"] or book["cover_embedded"])

    def cover_path(self, book):
        """A file on disk holding this book's cover, or None.

        Art embedded in tags is extracted the first time it is asked for and
        kept, so it is parsed out of the audio once rather than on every
        request. Doing this lazily rather than during the scan matters: on a
        library of a few thousand single-file books, extracting everything up
        front adds half a minute and hundreds of megabytes for covers that may
        never be looked at.
        """
        if book.get("cover_file") and os.path.exists(book["cover_file"]):
            return book["cover_file"]
        if not book.get("cover_embedded") or not book.get("tracks"):
            return None

        cached = book.get("cover_cache")
        if cached and os.path.exists(cached):
            return cached
        if not self.cover_dir:
            return None

        existing = glob.glob(os.path.join(self.cover_dir, f"{book['id']}.*"))
        if existing:
            book["cover_cache"] = existing[0]
            return existing[0]

        picture = oggopus.read_picture(book["tracks"][0]["path"])
        if not picture:
            return None
        mime, data = picture
        path = os.path.join(self.cover_dir, f"{book['id']}{COVER_MIME_EXTENSIONS.get(mime, '.jpg')}")
        try:
            os.makedirs(self.cover_dir, exist_ok=True)
            temp = f"{path}.tmp"
            with open(temp, "wb") as fh:
                fh.write(data)
            os.replace(temp, path)  # concurrent requests must not see a half file
        except OSError:
            # An unwritable cache is a performance problem, not a reason to
            # show a broken image for every book in the library.
            return None
        book["cover_cache"] = path
        return path

    def cover_bytes(self, book):
        """The cover as (mime, data), read straight from the tags.

        The fallback for when the cache cannot be written: slower, because it
        re-parses the file each time, but it still shows the artwork.
        """
        if not book.get("cover_embedded") or not book.get("tracks"):
            return None
        return oggopus.read_picture(book["tracks"][0]["path"])

    # ---- the persisted index ---------------------------------------------
    #
    # The scan result is written to a single JSON file so the server can start
    # from it instead of walking the directory: the tree only changes when you
    # add a book, and a rescan is one button away.

    INDEX_VERSION = 1
    _BOOK_FIELDS = (
        "id", "title", "author", "narrator", "series", "genre", "year",
        "description", "duration", "duration_text", "track_count", "directory",
        "untimed", "cover_file", "cover_embedded", "cover_cache", "has_cover",
        "duplicates",
    )

    def to_index(self):
        return {
            "version": self.INDEX_VERSION,
            "root": self.root,
            "scannedAt": self.scanned_at,
            "bookCount": len(self.books),
            "trackCount": sum(b["track_count"] for b in self.books),
            "books": [
                dict({field: book.get(field) for field in self._BOOK_FIELDS},
                     tracks=book["tracks"])
                for book in self.books
            ],
        }

    def save_index(self, path):
        if not path:
            return
        try:
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            temp = f"{path}.{os.getpid()}.tmp"  # unique: two servers may share this directory
            with open(temp, "w", encoding="utf-8") as fh:
                json.dump(self.to_index(), fh, indent=1, ensure_ascii=False)
            os.replace(temp, path)
        except OSError:
            pass  # an unwritable index costs a rescan, nothing more

    def load_index(self, path):
        """Restore a previous scan. Returns False if there is nothing usable."""
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return False
        if not isinstance(data, dict) or data.get("version") != self.INDEX_VERSION:
            return False
        if os.path.abspath(data.get("root", "")) != self.root:
            return False  # the index describes a different library

        books = []
        for raw in data.get("books", []):
            book = {field: raw.get(field) for field in self._BOOK_FIELDS}
            book["tracks"] = raw.get("tracks") or []
            book["files"] = []
            book["duplicates"] = book.get("duplicates") or []
            if not book.get("id") or not book["tracks"]:
                continue
            books.append(book)
        if not books and data.get("bookCount"):
            return False

        self.books = books
        self.by_id = {b["id"]: b for b in books}
        self.scanned_at = data.get("scannedAt", 0.0)
        return True

    def index_drift(self):
        """Indexed files that have since been deleted or rewritten.

        A stat per file is cheap and catches the cases that break playback.
        Files *added* since the scan need a full walk to notice, which is what
        the Rescan button is for.
        """
        changed = []
        for book in self.books:
            for track in book["tracks"]:
                try:
                    stat = os.stat(track["path"])
                except OSError:
                    changed.append((track["path"], "missing"))
                    continue
                if stat.st_size != track.get("size") or (
                    track.get("mtime") and abs(stat.st_mtime - track["mtime"]) > 1
                ):
                    changed.append((track["path"], "modified"))
        return changed

    # ---- serialisation for the API ---------------------------------------

    def book_summary(self, book):
        return {
            "id": book["id"],
            "title": book["title"],
            "author": book["author"] or "Unknown author",
            "narrator": book["narrator"],
            "series": book["series"],
            "genre": book.get("genre", ""),
            "year": book["year"],
            "duration": book["duration"],
            "durationText": book["duration_text"],
            "trackCount": book["track_count"],
            "hasCover": book["has_cover"],
            "untimed": book["untimed"],
        }

    def book_detail(self, book):
        detail = self.book_summary(book)
        detail["description"] = book["description"]
        detail["directory"] = book["directory"]
        detail["duplicates"] = book.get("duplicates") or []
        detail["tracks"] = [
            {
                "index": t["index"],
                "title": t["title"],
                "duration": t["duration"],
                "offset": t["offset"],
                "rel": t["rel"],
            }
            for t in book["tracks"]
        ]
        return detail
