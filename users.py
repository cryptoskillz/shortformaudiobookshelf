"""Accounts and roles.

Two roles, deliberately only two:

    admin      can do everything — scan, upload, organise, remove books, look
               up details, change settings and manage accounts.
    listener   can browse and play, and nothing else. Their own resume
               positions are private to them.

With no accounts at all the server is open and every request is treated as an
admin, which is how it behaved before accounts existed. The player warns about
that in Settings.

Passwords are stored only as PBKDF2-SHA256 hashes, reusing the helpers in
settings.py so there is one implementation of that to get right.
"""

from __future__ import annotations

import json
import os
import threading
import time

import settings as settings_module

ADMIN = "admin"
LISTENER = "listener"
ROLES = (ADMIN, LISTENER)

# Seeded on a brand-new install so there is always a way in, the way most
# self-hosted apps do it. Anything still using it is reported as insecure until
# the password is changed.
DEFAULT_USERNAME = "admin"
DEFAULT_PASSWORD = "admin"


def clean_username(raw):
    """Normalise a username into its key.

    Lower-cased on purpose: signing in as "Chris" when the account was created
    as "chris" is a needless way to be locked out, and there is no reason to
    allow two accounts that differ only in case.
    """
    name = " ".join(str(raw or "").split())
    return name[:64].lower()


class UserStore:
    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        self._data = {}
        try:
            with open(path, "r", encoding="utf-8") as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict):
                self._data = {k: v for k, v in loaded.items() if isinstance(v, dict)}
        except (OSError, ValueError):
            pass

    # ---- queries ---------------------------------------------------------

    def any(self):
        with self._lock:
            return bool(self._data)

    def get(self, username):
        with self._lock:
            entry = self._data.get(clean_username(username))
            return dict(entry, username=clean_username(username)) if entry else None

    def list(self):
        """Everyone, without password hashes."""
        with self._lock:
            return [
                {"username": name, "role": entry.get("role", LISTENER),
                 "createdAt": entry.get("createdAt", 0)}
                for name, entry in sorted(self._data.items())
            ]

    def admin_count(self):
        with self._lock:
            return sum(1 for e in self._data.values() if e.get("role") == ADMIN)

    def authenticate(self, username, password):
        """The user, or None. Returns None rather than raising on any mismatch."""
        entry = self.get(username)
        if not entry:
            return None
        if not settings_module.verify_password(password, entry.get("password_hash", "")):
            return None
        return entry

    # ---- changes ---------------------------------------------------------

    def create(self, username, password, role=LISTENER):
        name = clean_username(username)
        if not name:
            raise ValueError("a username is required")
        if not password:
            raise ValueError("a password is required")
        if role not in ROLES:
            raise ValueError(f"role must be one of {', '.join(ROLES)}")
        with self._lock:
            if name in self._data:
                raise ValueError(f"{name} already exists")
            self._data[name] = {
                "password_hash": settings_module.hash_password(password),
                "role": role,
                "createdAt": time.time(),
            }
            snapshot = dict(self._data)
        self._write(snapshot)
        return {"username": name, "role": role}

    def set_password(self, username, password):
        name = clean_username(username)
        if not password:
            raise ValueError("a password is required")
        with self._lock:
            if name not in self._data:
                raise ValueError(f"{name} does not exist")
            self._data[name]["password_hash"] = settings_module.hash_password(password)
            snapshot = dict(self._data)
        self._write(snapshot)

    def set_role(self, username, role):
        name = clean_username(username)
        if role not in ROLES:
            raise ValueError(f"role must be one of {', '.join(ROLES)}")
        with self._lock:
            if name not in self._data:
                raise ValueError(f"{name} does not exist")
            # Demoting the only admin would lock everyone out of administration.
            if (self._data[name].get("role") == ADMIN and role != ADMIN
                    and sum(1 for e in self._data.values() if e.get("role") == ADMIN) <= 1):
                raise ValueError("this is the only admin — promote someone else first")
            self._data[name]["role"] = role
            snapshot = dict(self._data)
        self._write(snapshot)

    def delete(self, username):
        name = clean_username(username)
        with self._lock:
            if name not in self._data:
                raise ValueError(f"{name} does not exist")
            if (self._data[name].get("role") == ADMIN
                    and sum(1 for e in self._data.values() if e.get("role") == ADMIN) <= 1):
                raise ValueError("this is the only admin — make someone else an admin first")
            del self._data[name]
            snapshot = dict(self._data)
        self._write(snapshot)

    def seed_default(self):
        """Create the default admin, but only on a completely empty install."""
        with self._lock:
            if self._data:
                return False
            self._data[DEFAULT_USERNAME] = {
                "password_hash": settings_module.hash_password(DEFAULT_PASSWORD),
                "role": ADMIN,
                "createdAt": time.time(),
            }
            snapshot = dict(self._data)
        self._write(snapshot)
        return True

    def using_default_password(self):
        """True while the seeded account still has its shipped password."""
        entry = self.get(DEFAULT_USERNAME)
        if not entry:
            return False
        return settings_module.verify_password(DEFAULT_PASSWORD,
                                               entry.get("password_hash", ""))

    def adopt(self, username, password_hash, role=ADMIN):
        """Take over an existing hash — used to migrate the old single login."""
        name = clean_username(username)
        if not name or not password_hash:
            return False
        with self._lock:
            if name in self._data:
                return False
            self._data[name] = {"password_hash": password_hash, "role": role,
                                "createdAt": time.time()}
            snapshot = dict(self._data)
        self._write(snapshot)
        return True

    def _write(self, snapshot):
        """Persist, or raise.

        Accounts are the one thing that must never fail quietly: swallowing the
        error here means the player says "account added" and then the password
        does not work, because nothing ever reached disk. The usual cause is a
        state directory the container cannot write to (wrong PUID/PGID).
        """
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
            temp = f"{self.path}.{os.getpid()}.tmp"
            with open(temp, "w", encoding="utf-8") as fh:
                json.dump(snapshot, fh, indent=1, ensure_ascii=False)
            os.replace(temp, self.path)
            os.chmod(self.path, 0o600)          # it holds credential hashes
        except OSError as exc:
            raise ValueError(
                f"could not save accounts to {self.path}: {exc.strerror or exc}. "
                "Check the folder exists and the server can write to it "
                "(in Docker, that usually means PUID/PGID)."
            ) from exc
