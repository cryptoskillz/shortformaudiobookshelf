"""Persistent settings, so the server can be started with no arguments at all.

Values resolve in this order, first one wins:

    command-line flag  →  settings.json  →  environment  →  built-in default

Everything lives in ``~/.shortlistaudio/`` by default:

    settings.json   library and output directories, host/port, credentials
    index.json      the last scan
    cache.json      parsed per-file metadata
    covers/         cover art extracted from tags
    progress.json   resume points

Passwords are stored as a PBKDF2-SHA256 hash, never in the clear. That protects
the password itself if the file is read, but note that HTTP Basic auth sends it
over the wire in cleartext, so it is a lock on your own network — not something
to expose to the internet.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets

# Where index, cache, covers, progress and settings live. In a container this
# is a mounted volume; on a desktop it is a dotfile directory in $HOME.
STATE_DIR = os.path.abspath(os.path.expanduser(
    os.environ.get("SHORTLIST_STATE_DIR") or os.path.join("~", ".shortlistaudio")
))
SETTINGS_FILE = os.path.join(STATE_DIR, "settings.json")

DEFAULTS = {
    "library": "~/Audiobooks",
    "output": "",
    "host": "0.0.0.0",
    "port": 7345,
    "scan_on_start": False,
    "verbose": False,
    "username": "",
    "password_hash": "",
}

# Settings that may also come from the environment.
ENV_KEYS = {
    "library": "SHORTLIST_LIBRARY",
    "output": "SHORTLIST_OUTPUT",
    "port": "SHORTLIST_PORT",
    "host": "SHORTLIST_HOST",
}

_ITERATIONS = 260_000


# --------------------------------------------------------------- passwords

def hash_password(password, salt=None, iterations=_ITERATIONS):
    salt = salt or secrets.token_hex(16)
    derived = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), iterations)
    return f"pbkdf2_sha256${iterations}${salt}${derived.hex()}"


def verify_password(password, stored):
    """Constant-time check of a password against a stored hash."""
    try:
        algorithm, iterations, salt, expected = stored.split("$", 3)
    except (ValueError, AttributeError):
        return False
    if algorithm != "pbkdf2_sha256":
        return False
    try:
        derived = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt), int(iterations)
        )
    except ValueError:
        return False
    return hmac.compare_digest(derived.hex(), expected)


# ---------------------------------------------------------------- settings

def load(path=SETTINGS_FILE):
    """Settings from disk, merged over the environment and the defaults."""
    values = dict(DEFAULTS)

    for key, variable in ENV_KEYS.items():
        raw = os.environ.get(variable)
        if raw:
            values[key] = int(raw) if key == "port" and raw.isdigit() else raw

    try:
        with open(path, "r", encoding="utf-8") as fh:
            stored = json.load(fh)
        if isinstance(stored, dict):
            for key, value in stored.items():
                if key in DEFAULTS and value not in (None, ""):
                    values[key] = value
    except (OSError, ValueError):
        pass  # no settings file yet, or it is unreadable — defaults stand

    for key in ("library", "output"):
        if values[key]:
            values[key] = os.path.abspath(os.path.expanduser(values[key]))
    values["port"] = int(values["port"])
    return values


def save(values, path=SETTINGS_FILE):
    """Write settings, preserving any keys we did not touch."""
    existing = {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            loaded = json.load(fh)
        if isinstance(loaded, dict):
            existing = loaded
    except (OSError, ValueError):
        pass

    existing.update({k: v for k, v in values.items() if k in DEFAULTS})
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    temp = f"{path}.{os.getpid()}.tmp"  # unique: two servers may share this directory
    with open(temp, "w", encoding="utf-8") as fh:
        json.dump(existing, fh, indent=1, ensure_ascii=False)
    os.replace(temp, path)
    try:
        os.chmod(path, 0o600)  # it holds a credential hash
    except OSError:
        pass
    return path


def describe(values):
    lines = []
    for key in ("library", "output", "host", "port", "scan_on_start", "verbose"):
        lines.append(f"  {key:<14} {values.get(key) or '—'}")
    lines.append(f"  {'auth':<14} " + (f"enabled (user {values['username']})"
                                       if values.get("username") and values.get("password_hash")
                                       else "disabled"))
    return "\n".join(lines)
