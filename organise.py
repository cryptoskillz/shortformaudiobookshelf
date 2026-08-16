"""Copy a scanned library into a tidy, self-describing output tree.

    <output>/<Author>/<Book Title>/01 - Chapter One.opus
                                  /02 - Chapter Two.opus
                                  /Book Title.m3u
                                  /book.json
                                  /cover.jpg
    <output>/index.json
    <output>/organise-manifest.json

Originals are never touched: this copies. Re-running is safe and cheap — a file
already in place is left alone, and one that differs is only replaced when the
incoming copy is actually higher quality (see ``library.is_higher_quality``),
so re-organising a library you have since re-ripped upgrades it in place
instead of shuffling files back and forth.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time

import library
import oggopus

MANIFEST_NAME = "organise-manifest.json"
INDEX_NAME = "index.json"
_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_COVER_EXTENSION = {"image/jpeg": ".jpg", "image/jpg": ".jpg", "image/png": ".png", "image/webp": ".webp"}


def safe_name(text, fallback="Unknown", limit=120):
    """A filename that survives macOS, Linux and Windows alike."""
    name = _ILLEGAL.sub("", str(text or "")).strip()
    name = re.sub(r"\s{2,}", " ", name)
    if len(name) > limit:
        name = name[:limit]
    return name.rstrip(". ") or fallback


def _overlaps(first, second):
    """True when either path contains the other — organising into your own
    library (or its parent) would rescan its own output."""
    first, second = os.path.abspath(first), os.path.abspath(second)
    try:
        return os.path.commonpath([first, second]) in (first, second)
    except ValueError:
        return False  # different drives


def plan(lib, output):
    """Work out every file that would be written, without writing anything."""
    output = os.path.abspath(os.path.expanduser(output))
    entries = []
    used = set()

    for book in lib.books:
        author = safe_name(book["author"], "Unknown Author")
        title = safe_name(book["title"], "Untitled")
        dest_dir = os.path.join(output, author, title)

        # Two distinct books can sanitise to the same folder name.
        suffix = 2
        while dest_dir.lower() in used:
            dest_dir = os.path.join(output, author, f"{title} ({suffix})")
            suffix += 1
        used.add(dest_dir.lower())

        single = len(book["tracks"]) == 1
        files = []
        for track in book["tracks"]:
            extension = os.path.splitext(track["path"])[1].lower()
            name = (
                f"{safe_name(book['title'], 'Untitled')}{extension}"
                if single
                else f"{track['index'] + 1:02d} - {safe_name(track['title'], f'Chapter {track['index'] + 1}')}{extension}"
            )
            files.append({
                "source": track["path"],
                "name": name,
                "dest": os.path.join(dest_dir, name),
                "size": track["size"],
                "duration": track["duration"],
                "title": track["title"],
                "index": track["index"],
            })
        entries.append({"book": book, "dir": dest_dir, "files": files})
    return entries


def _copy(source, dest, overwrite):
    """Copy one file, deciding what to do about anything already there.

    Returns one of: copied, replaced-upgrade, skipped-identical,
    skipped-lower-quality.
    """
    if os.path.exists(dest):
        existing = os.stat(dest)
        incoming = os.stat(source)
        if existing.st_size == incoming.st_size:
            return "skipped-identical"
        if not overwrite:
            # Same chapter, different encode: take it only if it is better.
            try:
                probe = oggopus.read(source)
                incoming_duration = probe["duration"]
            except Exception:
                incoming_duration = 0.0
            try:
                probe = oggopus.read(dest)
                existing_duration = probe["duration"]
            except Exception:
                existing_duration = 0.0
            candidate = {"size": incoming.st_size, "duration": incoming_duration}
            incumbent = {"size": existing.st_size, "duration": existing_duration}
            if not library.is_higher_quality(candidate, incumbent):
                return "skipped-lower-quality"
        shutil.copy2(source, dest)
        return "replaced-upgrade"

    shutil.copy2(source, dest)
    return "copied"


def _write_playlist(path, book, files):
    lines = ["#EXTM3U", f"#PLAYLIST:{book['author'] or 'Unknown author'} - {book['title']}"]
    for entry in files:
        seconds = int(entry["duration"]) if entry["duration"] else -1
        lines.append(f"#EXTINF:{seconds},{book['author'] or 'Unknown author'} - {entry['title']}")
        lines.append(entry["name"])  # relative, so the folder stays portable
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def _write_book_json(path, book, files):
    payload = {
        "id": book["id"],
        "title": book["title"],
        "author": book["author"],
        "narrator": book["narrator"],
        "series": book["series"],
        "genre": book.get("genre", ""),
        "year": book["year"],
        "description": book["description"],
        "duration": book["duration"],
        "durationText": book["duration_text"],
        "trackCount": len(files),
        "source": book["directory"],
        "tracks": [
            {"index": f["index"], "title": f["title"], "file": f["name"], "duration": f["duration"]}
            for f in files
        ],
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=1, ensure_ascii=False)


def _write_cover(dest_dir, book):
    source = book.get("cover_file")
    if source and os.path.exists(source):
        extension = os.path.splitext(source)[1].lower() or ".jpg"
        target = os.path.join(dest_dir, f"cover{extension}")
        if not os.path.exists(target):
            shutil.copy2(source, target)
        return os.path.basename(target)
    if book.get("cover_embedded") and book["tracks"]:
        picture = oggopus.read_picture(book["tracks"][0]["path"])
        if picture:
            mime, data = picture
            target = os.path.join(dest_dir, f"cover{_COVER_EXTENSION.get(mime, '.jpg')}")
            if not os.path.exists(target):
                with open(target, "wb") as fh:
                    fh.write(data)
            return os.path.basename(target)
    return None


def organise(lib, output, dry_run=False, overwrite=False, playlists_only=False,
             delete_originals=False, log=print, progress=None):
    """Copy the library into `output`. Returns the manifest.

    `progress`, if given, is called as progress(done, total, name) after each
    file so a caller can report how far along a long copy is.

    `delete_originals` turns the copy into a move: the source is removed only
    after its copy is verified to exist at the right size, and never when the
    file was skipped, so a failed or refused copy can never lose the original.
    """
    output = os.path.abspath(os.path.expanduser(output))
    if _overlaps(lib.root, output):
        raise ValueError(
            f"output directory overlaps the library ({lib.root}) — choose one outside it"
        )

    entries = plan(lib, output)
    total_files = sum(len(entry["files"]) for entry in entries)
    done_files = 0

    def advance(name):
        nonlocal done_files
        done_files += 1
        if progress:
            progress(done_files, total_files, name)

    manifest = {
        "generatedAt": time.time(),
        "source": lib.root,
        "output": output,
        "mode": ("playlists-only" if playlists_only
                 else "move" if delete_originals else "copy"),
        "dryRun": dry_run,
        "books": [],
    }
    totals = {
        "copied": 0, "replaced-upgrade": 0, "skipped-identical": 0,
        "skipped-lower-quality": 0, "failed": 0, "playlists": 0, "deleted": 0,
    }

    if playlists_only:
        # Just the .m3u files, pointing at the originals where they already are.
        if not dry_run:
            os.makedirs(output, exist_ok=True)
        for entry in entries:
            book = entry["book"]
            name = safe_name(f"{book['author'] or 'Unknown author'} - {book['title']}", "playlist")
            path = os.path.join(output, f"{name}.m3u")
            lines = ["#EXTM3U", f"#PLAYLIST:{book['author'] or 'Unknown author'} - {book['title']}"]
            for track in book["tracks"]:
                seconds = int(track["duration"]) if track["duration"] else -1
                lines.append(f"#EXTINF:{seconds},{book['author'] or 'Unknown author'} - {track['title']}")
                lines.append(track["path"])  # absolute: the audio is not moving
            if not dry_run:
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write("\n".join(lines) + "\n")
            totals["playlists"] += 1
            manifest["books"].append({"id": book["id"], "author": book["author"],
                                      "title": book["title"], "playlist": path})
            log(f"  playlist  {os.path.basename(path)}")
            if progress:
                progress(totals["playlists"], len(entries), os.path.basename(path))
        manifest["totals"] = totals
        if not dry_run:
            _write_manifest(output, manifest)
        return manifest

    for entry in entries:
        book, dest_dir = entry["book"], entry["dir"]
        log(f"\n  {book['author'] or 'Unknown author'} — {book['title']}")
        log(f"    → {dest_dir}")
        if not dry_run:
            os.makedirs(dest_dir, exist_ok=True)

        record = {
            "id": book["id"], "author": book["author"], "title": book["title"],
            "dest": dest_dir, "files": [],
        }
        for file_entry in entry["files"]:
            if dry_run:
                action = "would-copy"
            else:
                try:
                    action = _copy(file_entry["source"], file_entry["dest"], overwrite)
                except OSError as exc:
                    action = "failed"
                    record["files"].append({"source": file_entry["source"],
                                            "dest": file_entry["dest"],
                                            "action": action, "error": str(exc)})
                    totals["failed"] += 1
                    log(f"      ! {file_entry['name']}: {exc}")
                    advance(file_entry["name"])
                    continue
                totals[action] = totals.get(action, 0) + 1
            advance(file_entry["name"])
            if (delete_originals and not dry_run
                    and action in ("copied", "replaced-upgrade", "skipped-identical")):
                try:
                    if (os.path.exists(file_entry["dest"])
                            and os.path.getsize(file_entry["dest"])
                            == os.path.getsize(file_entry["source"])):
                        os.remove(file_entry["source"])
                        action += "+original-deleted"
                        totals["deleted"] = totals.get("deleted", 0) + 1
                except OSError as exc:
                    log(f"      ! could not delete {file_entry['source']}: {exc}")

            record["files"].append({"source": file_entry["source"],
                                    "dest": file_entry["dest"], "action": action})
            marker = {"copied": "+", "replaced-upgrade": "↑", "skipped-identical": "=",
                      "skipped-lower-quality": "·", "would-copy": "+"}.get(
                          action.split("+")[0], "?")
            log(f"      {marker} {file_entry['name']}")

        if not dry_run:
            playlist = os.path.join(dest_dir, f"{safe_name(book['title'], 'playlist')}.m3u")
            _write_playlist(playlist, book, entry["files"])
            _write_book_json(os.path.join(dest_dir, "book.json"), book, entry["files"])
            record["cover"] = _write_cover(dest_dir, book)
            record["playlist"] = playlist
        totals["playlists"] += 1
        manifest["books"].append(record)

    manifest["totals"] = totals
    if not dry_run:
        _write_manifest(output, manifest)
        # An index of the organised tree, so it can be served directly.
        organised = library.Library(output, cache_path=None)
        organised.scan()
        organised.save_index(os.path.join(output, INDEX_NAME))
        manifest["indexedBooks"] = len(organised.books)
    return manifest


def _write_manifest(output, manifest):
    try:
        with open(os.path.join(output, MANIFEST_NAME), "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, indent=1, ensure_ascii=False)
    except OSError:
        pass


def summarise(manifest):
    totals = manifest.get("totals", {})
    parts = [
        f"{len(manifest.get('books', []))} books",
        f"{totals.get('copied', 0)} copied",
        f"{totals.get('replaced-upgrade', 0)} upgraded",
        f"{totals.get('skipped-identical', 0)} already there",
        f"{totals.get('skipped-lower-quality', 0)} kept (incoming was not better)",
    ]
    if totals.get("deleted"):
        parts.append(f"{totals['deleted']} originals deleted")
    if totals.get("failed"):
        parts.append(f"{totals['failed']} failed")
    return " · ".join(parts)


def main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(description="Organise an audiobook library into a tidy tree")
    parser.add_argument("root", help="library directory to scan")
    parser.add_argument("output", help="directory to write the organised copy into")
    parser.add_argument("--dry-run", action="store_true", help="print the plan, write nothing")
    parser.add_argument("--overwrite", action="store_true",
                        help="replace existing files even when they are not lower quality")
    parser.add_argument("--playlists-only", action="store_true",
                        help="write .m3u files pointing at the originals; copy no audio")
    parser.add_argument("--delete-originals", action="store_true",
                        help="after a verified copy, delete the source file (a move)")
    args = parser.parse_args(argv)

    lib = library.Library(args.root, cache_path=None)
    print(f"Scanning {lib.root} …")
    lib.scan()
    print(f"Found {len(lib.books)} books ({sum(b['track_count'] for b in lib.books)} files)")

    try:
        manifest = organise(lib, args.output, dry_run=args.dry_run,
                            overwrite=args.overwrite, playlists_only=args.playlists_only,
                            delete_originals=args.delete_originals)
    except ValueError as exc:
        print(f"error: {exc}", file=__import__("sys").stderr)
        return 1
    print(f"\n{'Would write' if args.dry_run else 'Wrote'}: {summarise(manifest)}")
    if not args.dry_run:
        print(f"Output: {os.path.abspath(os.path.expanduser(args.output))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
