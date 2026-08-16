# Shortform Audio Bookshelf

An audiobook server and player for a directory of Opus files. It scans the
directory, works out which files belong to which book, keeps the result in a
JSON index, and plays it in the browser — on this machine or from a phone on
the same network. It can also copy the library into a tidy, self-describing
tree with a playlist per book.

Pure Python 3 standard library. No pip install, no ffmpeg, no database.

Measured on a real 3,803-book / 17 GB library: **1.8s** first scan, **0.02s**
startup from the index.

## Run it

```bash
python3 server.py ~/Audiobooks --save-settings
```

That remembers the directory, so from then on:

```bash
python3 server.py
```

Open <http://localhost:7345>. The startup banner also prints a LAN address
(`http://192.168.x.x:7345`) that works from a phone on the same network.

## Running in Docker

The app has no dependencies beyond the Python standard library, so the image is
just Python plus five source files — 74 MB, nothing to compile. Three folders
are mapped in, and the app neither knows nor cares whether they are local disks
or NAS shares:

| Mount | What it is |
| --- | --- |
| `/library` | **input** — the messy folder. Uploads land here, scanning reads it. |
| `/media` | **output** — the tidy `Author/Title` copy that Organise writes. |
| `/config` | index, cover cache, resume positions, settings. Keep this one. |

### Portainer, without building anything

`docker-compose.portainer.yml` runs the stock `python:3.13-alpine` image with
the source mounted in, so a stack can be **pasted straight into Portainer's
editor** — no registry, no build context, no git repository.

1. Copy `server.py`, `library.py`, `organise.py`, `settings.py`, `oggopus.py`
   and the `web/` folder onto the NAS, e.g. `/volume1/docker/shortlistaudio/app`.
2. Paste the compose file into a new Portainer stack.
3. Edit the four host paths and set `PUID`/`PGID` to whoever owns your media
   (`id -u`, `id -g`).

Updating later is replacing those files and restarting the stack.

### Building an image instead

```bash
docker compose up -d           # uses docker-compose.yml
```

Or build here and move it over, if building on the NAS is awkward:

```bash
docker build -t shortlistaudio:latest .
docker save shortlistaudio:latest | gzip > shortlistaudio.tar.gz
# copy to the NAS, then: docker load < shortlistaudio.tar.gz
```

Change the **host** side of the port mapping (`"8080:7345"`) rather than the
port setting inside the app — the container always listens on 7345.

## Settings

Press **⚙** in the player to set the library folder, the organised output
folder, host and port, and the login — no command line needed. Each folder
field has a **Browse…** button that walks the server's filesystem, since a
browser file picker cannot hand back a real path and you may well be setting
this up from your phone. Changing the library folder rescans and switches to it
immediately.

Changing the host or port moves the running server there straight away — the
page follows to the new address by itself. No restart, which matters because
the web UI has no way to restart anything. The new socket is opened before the
old one closes, so a port that is already in use leaves you exactly where you
were, with the old address still serving and the setting rolled back rather
than saved as something that never took.

The login section confirms a new password twice, can show what you typed, and
has a **Check** button to test the current password before you replace it —
locking yourself out of your own library by a typo is easy to do otherwise.

Settings live in `~/.shortlistaudio/settings.json`. A command-line flag beats
the settings file, which beats the environment, which beats the defaults.

| Command | What it does |
| --- | --- |
| `--save-settings` | store the options given on this command line as the defaults |
| `--settings-list` | print what is currently configured |
| `--output DIR` | where `--organise` writes its copy |
| `--set-password` | set a username and password for the player |
| `--clear-password` | remove the login |
| `--host` / `--port` | bind address and port (default `0.0.0.0:7345`) |
| `--settings-file` | use a different settings file entirely |

Everything else lives in `~/.shortlistaudio/` too: `index.json` (the last scan),
`cache.json` (per-file metadata), `covers/` (art extracted from tags or fetched
from a provider), `metadata.json` (looked-up book details), `authors.json`
(author bios), `removed.json` (books you have hidden or deleted), and
`progress.json` (resume points). Put that directory somewhere else with
`--state-dir` or `$SHORTLIST_STATE_DIR` — which is how the container points it
at a mounted `/config`.

### Login

```bash
python3 server.py --set-password
```

Prompts for a username and password and stores the password as a PBKDF2-SHA256
hash — never in the clear — in a `0600` settings file. The player then asks for
it in the browser's own login dialog, and every request is covered, including
audio streaming.

Be aware that HTTP Basic sends the password in cleartext on the wire. That is a
reasonable lock on your own network. It is **not** enough to put this server on
the open internet.

## Scanning

The directory is scanned once, and the result is written to `index.json`.
Startup loads that file instead of re-reading the library, which is the
difference between 1.8 seconds and 0.02 on a few thousand books. Startup also
stats the indexed files and warns if any changed or vanished.

Re-read the directory with the **Rescan** button in the player, or:

```bash
python3 server.py --rescan          # rescan, then serve
python3 server.py --scan-only       # print how everything grouped, then exit
```

`--scan-only` is the quickest way to check the scanner agrees with you.

### What it reads

Opus and Ogg Vorbis files are parsed directly: Vorbis comment tags (including
`GENRE`), exact duration from the Ogg granule position, and embedded cover art.
`.mp3`, `.m4a`, `.m4b`, `.flac`, `.wav` and `.aac` files are listed and streamed
too, but their tags and durations are not read — they show as "duration
unknown" and get a chapter-level scrubber instead of a book-wide one.

Cover art embedded in tags is extracted to `~/.shortlistaudio/covers/` the first
time it is requested, then served from there. Doing it lazily rather than during
the scan matters: on 3,803 books, extracting everything up front added 20
seconds and 291 MB for covers that may never be looked at.

### How files become books

Tags win when they exist. Files carrying an `ALBUM` tag are grouped by
(album artist, album), ordered by `TRACKNUMBER`.

Untagged files are grouped by directory, and each directory is then judged:

- **Chapters of one book** if most filenames start with a number, say
  "Chapter 4" / "Part Two", or share a long common prefix. The book takes the
  directory's name, and the author the directory above it.
- **A shelf of separate single-file books** otherwise — one file, one book,
  named after the file. This is the short-book case: a whole book in one file
  sitting alongside other whole books.

Track order falls back to a natural sort that understands both `2` before `10`
and "Part One" before "Part Two".

### Duplicates

Two rips of one book carry the same tags, so they group into a single book and
would otherwise appear as doubled chapters. The scan keeps the higher-bitrate
copy of each chapter and reports the rest:

```
Found 8 books (15 files) in 0.0s · dropped 4 duplicate file(s)
```

Matching is deliberately conservative. Two files are only candidates when they
share a disc/track number, or share a filename that says more than a bare
number — otherwise `Disc 2/01.opus` would look like a second copy of
`Disc 1/01.opus`. On top of that their durations must match, because
re-encoding does not change how long a chapter runs. `--scan-only` lists every
file that was set aside.

### Correcting the guesses

Drop a `shortlist.json` at the root of your library. Keys are paths relative to
that root, and a directory key applies to everything beneath it:

```json
{
  "Standalone/Fen Country.opus": { "author": "Edmund Crispin", "title": "Fen Country" },
  "Boxset/Disc 1": { "author": "Hilary Mantel", "title": "Wolf Hall" }
}
```

Press **Rescan** to pick the file up.

## Organising

Setting the output folder only records *where*. Organising is a separate
action, and **Rescan does not do it** — that only re-reads the library. In the
player, open **⚙ → Folders** and use **Preview** (shows the plan, writes
nothing) or **Organise now**. The copy runs on the server with a progress bar,
because a few thousand books take longer than a browser will wait on one
request. From the command line:

```bash
python3 server.py --organise ~/Audiobooks-tidy --dry-run   # see the plan
python3 server.py --organise ~/Audiobooks-tidy             # do it
python3 server.py --organise                               # use the saved --output
```

This **copies** — your original files are never moved, renamed or deleted — into:

```
<output>/<Author>/<Book Title>/01 - Chapter One.opus
                              /02 - Chapter Two.opus
                              /Book Title.m3u
                              /book.json
                              /cover.jpg
<output>/index.json
<output>/organise-manifest.json
```

`book.json` holds the metadata for that one book, the `.m3u` uses relative
filenames so the folder stays portable, and `index.json` describes the whole
organised tree. The manifest records every file written.

Re-running is safe and cheap. A file already in place is left alone; one that
differs is replaced **only when the incoming copy is higher quality**, compared
by bitrate. So re-organising after re-ripping a book upgrades it in place:

```
Wrote: 8 books · 0 copied · 1 upgraded · 14 already there · 1 kept (incoming was not better)
```

Pass `--overwrite` to replace regardless. Organising into a directory inside
your library is refused, since the scan would then find its own output.

To get the playlists without duplicating the audio:

```bash
python3 server.py --organise ~/Playlists --playlists-only
```

That writes one `.m3u` per book pointing at the originals where they already
are, and copies nothing.

## Playing

- Click a book, then **Play**, or click any chapter to start there.
- Filter the library by **author**, **genre**, or listening state (in progress /
  not started / finished), and search across title, author, narrator and genre.
- The scrubber spans the **whole book**, not the current file — dragging it past
  a chapter boundary loads the right file at the right offset.
- Chapters advance automatically; −15 / +30 cross chapter boundaries too.
- Position is saved every few seconds and on pause, so **Resume** picks up
  mid-sentence. **Start over** clears it.
- Speed 0.75×–3×, remembered between sessions. Sleep timer, including
  "end of chapter".
- Lock-screen and headphone controls work through the Media Session API.

Keyboard: `space` play/pause, `←`/`→` skip 15s/30s, `↑`/`↓` previous/next
chapter, `esc` back to the library.

## Book details from Audible, Apple or Google

Files that carry no `DESCRIPTION` tag have nothing to show — and most do not.
**Find details** on a book page searches the same public sources Audiobookshelf
uses, and no API key is needed for any of them:

| Source | Gives you |
| --- | --- |
| Audible | description, narrator, runtime, series, publisher, cover — best for audiobooks |
| Apple Books | description, cover — good where Audible has no match |
| Google Books | description, publisher — heavily rate limited without a key, so it is off by default |

Candidates are listed with cover, narrator, year and blurb, scored by how well
they match the title and author. **Nothing is applied until you pick one**, and
a confident wrong blurb is worse than a blank field, so there is no automatic
matching.

What you pick is stored as an *overlay* in `metadata.json` beside the index —
**your audio files are never modified**. The looked-up description, narrator,
series, year and publisher win over the tags when the book is displayed, a
fetched cover wins over embedded art, and **Remove details** puts everything
back to exactly what the tags say.

### Author bios

The same dialog has an **About the author** section. Bios come from Wikipedia
(better coverage) and Open Library (better precision), and are stored per
author, so looking one up once shows it on every book that author wrote.

Picking matters here too: searching "Jocelyn K. Glei" on Wikipedia returns a
New York experience designer, and Open Library has an entry for James Clear
with no biography at all. Candidates show what each source thinks the person
is, and one with no bio cannot be selected.

### Doing the whole library at once

**⚙ → Book details → Look up missing details** works through every book that
has no description yet, about a second each, in the background with a progress
bar and a Stop button. Only a confident match is applied — the title and author
have to line up — and anything doubtful is counted as skipped and left alone.

Books that find no confident match are tried again next time you run it, so if
a large slice of your library never matches, expect the queued count to stay
high. The count is reported before the run starts.

## Removing books

**Remove…** on a book page hides it from the library and forgets its progress
and looked-up details. The files stay on disk, and the removal is remembered so
a rescan does not simply put the book back.

Ticking **also delete the audio files** makes it permanent: the confirm button
changes to say so, the files are listed, and the request needs an explicit
confirmation flag, so a stray click or a replayed request cannot erase audio.
An emptied book folder is tidied up, but never the library root or an author
folder that still has books in it. There is no trash — deleted is deleted.

## Adding books

**Add books** in the header opens a drop zone — drag audio files in, or pick
them. They are written straight into the library folder (the unorganised root),
and the library is rescanned automatically so they appear right away.

Uploads are checked before they land: the filename is reduced to a bare name so
nothing can be written outside the library, the extension must be one the player
handles, and an `.opus` that is not really an Ogg stream is rejected rather than
left sitting in the library as an unplayable ghost. A name that already exists
becomes `name (2).opus` instead of overwriting.

## Playlists from the server

Every book exposes an M3U playlist for use outside this player:

```
http://localhost:7345/api/books/<book-id>/playlist.m3u          # streaming URLs
http://localhost:7345/api/books/<book-id>/playlist.m3u?local=1  # local file paths
```

The streaming form opens in VLC, mpv, or anything else that plays HTTP audio.
The **Download .m3u** button on each book page gives you that one.

## API

| Endpoint | Purpose |
| --- | --- |
| `GET /api/library` | all books, with resume points |
| `GET /api/books/<id>` | one book with its track list |
| `POST /api/rescan` | re-read the directory and rewrite the index |
| `GET /audio/<id>/<track>` | the audio, with byte-range support |
| `GET /cover/<id>` | cover image |
| `GET`/`POST /api/progress/<id>` | resume point (`{"clear": true}` to reset) |
| `GET`/`POST /api/settings` | folders, host and port |
| `POST /api/settings/password` | set or clear the login |
| `POST /api/settings/password/verify` | check a password without changing it |
| `GET /api/books/<id>/metadata/search` | candidates from Audible / Apple / Google |
| `GET /api/authors/search?name=` | author bio candidates from Wikipedia / Open Library |
| `POST /api/authors` | apply or clear an author bio |
| `POST /api/metadata/fetch-all` | start the bulk lookup (`/status`, `/stop`) |
| `POST /api/books/<id>/remove` | hide a book (`deleteFiles` + `confirm` to delete) |
| `POST /api/books/<id>/restore` | un-hide a book whose files still exist |
| `POST /api/books/<id>/metadata` | apply a chosen match (`{"clear": true}` to remove) |
| `GET /api/browse?path=` | sub-folders of one directory, for the picker |
| `POST /api/upload?name=` | raw file body, saved into the library root |
| `POST /api/organise` | start an organise run (`dryRun`, `playlistsOnly`) |
| `GET /api/organise/status` | progress and summary of the current run |

## Notes

- Nothing outside the library directory is served. Audio requests are resolved
  through the scanned index by book id and track number, so no path from the
  client ever reaches the filesystem, and uploaded filenames are stripped to a
  bare name before use.
- With no login set, anyone who can reach the server can not only listen but
  also upload files, change the library folder, and read directory names through
  the folder picker. On a home network that is usually fine; if it is not, set a
  login. The player warns about this in the settings panel.
- Audio is streamed as-is — there is no transcoding, so the browser must
  support the format. Opus works everywhere except older Safari.
- The library grid renders every book at once. At 3,803 books that is about
  three seconds and 26k DOM nodes, with cover images loaded lazily as you
  scroll. Well past that, it would want windowing.

## Files

| File | Contents |
| --- | --- |
| [`server.py`](server.py) | HTTP server, range streaming, JSON API, CLI |
| [`library.py`](library.py) | directory scan, grouping, dedup, index |
| [`organise.py`](organise.py) | the tidy-copy tree, playlists, manifest |
| [`metadata.py`](metadata.py) | Audible / Apple / Google lookup and match ranking |
| [`settings.py`](settings.py) | settings file, password hashing |
| [`oggopus.py`](oggopus.py) | Ogg/Opus tag, duration and cover parsing |
| [`web/`](web) | the player (plain HTML/CSS/JS, no build step) |
