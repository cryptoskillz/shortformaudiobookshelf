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

## Running on a NAS with Portainer

The repo is public, so Portainer can deploy it straight from GitHub — no
registry, no copying files, and updates are one button.

### 1. Create the folders on the NAS

Three, and the app neither knows nor cares that they live on a NAS:

| Mount | What it is |
| --- | --- |
| `/library` | **the library** — what gets scanned, served and played. |
| `/incoming` | **downloads** — where new files arrive. Import files them into the library; nothing is ever played from here. |
| `/export` | optional tidy export for other tools. Leave it out if you do not want one. |
| `/config` | index, cover cache, accounts, resume positions. **Back this one up.** |

```
/volume1/media/audiobooks-incoming
/volume1/media/audiobooks
/volume1/docker/shortform-audio/config
```

### 2. Add the stack in Portainer

**Stacks → Add stack → Web editor**, and paste `docker-compose.portainer.yml`
from this repo. It pulls a prebuilt image, so there is nothing to build or
clone on the NAS.

### 3. Set the environment variables

| Name | Example |
| --- | --- |
| `LIBRARY_PATH` | `/volume1/docker/media/audiobooks-shortform` |
| `IMPORT_PATH` | `/volume1/docker/media/downloads/audiobooks-incoming` |
| `MEDIA_PATH` | `/volume1/docker/media/audiobooks-export` (optional) |
| `CONFIG_PATH` | `/volume1/docker/shortform-audio/config` |
| `HOST_PORT` | `7345` |
| `PUID` / `PGID` | whatever `id -u` and `id -g` report for the user that owns your media |
| `TZ` | `Europe/London` |

`PUID`/`PGID` matter: get them wrong and uploads and Organise fail with
permission errors, because the container will not own the files it writes.

### 4. Deploy, then sign in

Open `http://<nas>:7345` and sign in with **admin / admin**. Change that
password immediately under **⚙ → Settings → Accounts**; the player warns you
until you do.

### Updating

**Re-pull image and redeploy**, the same as any other app. A push to `main`
rebuilds `ghcr.io/cryptoskillz/shortformaudiobookshelf:latest` through GitHub
Actions, and Portainer pulls it. Nothing in `/config` is touched, so accounts,
index and saved places survive.

### Building it yourself instead

`docker-compose.yml` builds from source rather than pulling. Use it with a
Portainer **Repository** stack (point it at this repo, compose path
`docker-compose.yml`), or locally:

```bash
git clone https://github.com/cryptoskillz/shortformaudiobookshelf.git
cd shortformaudiobookshelf
LIBRARY_PATH=/path/in MEDIA_PATH=/path/out CONFIG_PATH=/path/config docker compose up -d
```

The image is 74 MB — Python plus seven source files, nothing to compile.
Change the **host** side of the port mapping rather than the port setting inside
the app; the container always listens on 7345.

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
| `--set-password` | create or reset an admin account |
| `--list-users` | print the accounts and their roles |
| `--remove-accounts` | delete every account, leaving the server open |
| `--state-dir` | where index, covers, accounts and progress live |
| `--host` / `--port` | bind address and port (default `0.0.0.0:7345`) |
| `--settings-file` | use a different settings file entirely |

Everything else lives in `~/.shortlistaudio/` too: `index.json` (the last scan),
`cache.json` (per-file metadata), `covers/` (art extracted from tags or fetched
from a provider), `metadata.json` (looked-up book details), `authors.json`
(author bios), `removed.json` (books you have hidden or deleted),
`users.json` (accounts, `0600`), and `progress.json` (resume points, per account). Put that directory somewhere else with
`--state-dir` or `$SHORTLIST_STATE_DIR` — which is how the container points it
at a mounted `/config`.

### Accounts

**⚙ → Accounts** manages who can do what. Two roles:

| Role | Can |
| --- | --- |
| **listener** | browse and play, and keep their own place in every book |
| **admin** | all of that, plus scan, add, remove, organise, look up details, change settings and manage accounts |

Signed out, the player shows a sign-in page and nothing else — no titles, no
covers, no library at all. The account menu (top right) holds your username,
Settings and Sign out.

With no accounts the server is open to anyone who can reach it and every
request is treated as an admin — the same behaviour as before accounts existed,
and the settings panel says so in a warning. Adding the first account turns
authentication on.

Resume positions are **per account**, so two people listening to the same book
do not overwrite each other. Deleting an account deletes its saved positions.

The UI hides what your role cannot do, and the server enforces it separately —
a listener posting straight at the API gets a 403.

Guards worth knowing: the last remaining admin cannot be demoted or deleted, so
you cannot lock yourself out; changing your own password requires the current
one, while an admin can reset anyone's; and account files are written `0600`
with PBKDF2-SHA256 hashes.

If you already had the older single login, it is adopted as the first admin on
startup, and any resume positions saved before accounts existed move to it.

```bash
python3 server.py --set-password     # still works; creates or updates an admin
```

### First run: admin / admin

A brand-new install (an empty state directory) seeds one account:

```
username: admin
password: admin
```

The sign-in page says so while that password still works, and the startup log
prints it. **Change it straight away** — until you do, the settings panel shows
a warning and anyone who can reach the server can sign in. Changing it clears
the warning; the default is never re-created once any account exists.

Usernames are matched case-insensitively, so `admin` and `Admin` are the same
account.

### Locked out

Everything lives in `users.json` in the state directory, and the command line
can rewrite it:

```bash
python3 server.py --list-users              # who exists, and their roles
python3 server.py --set-password            # create or reset an admin
python3 server.py --set-password media      # skip the username prompt
python3 server.py --remove-accounts         # last resort: delete them all
```

`--set-password` on an existing name resets that password **and** makes them an
admin, which is the way back in when the only admin password is lost. In Docker
run it inside the container so it edits the mounted `/config`:

```bash
docker exec -it shortform-audio-bookshelf python3 server.py --set-password
```

`--remove-accounts` moves `users.json` aside rather than deleting it, and leaves
the server open until you create an account again.

### How sign-in works

The browser gets an **HMAC-signed session cookie**, not HTTP Basic. That matters
because Basic has no sign-out: browsers cache the credentials and re-send them,
and the usual workaround (deliberately failing an auth request) can leave the
*wrong* credentials cached so you cannot sign back in at all. A cookie can
simply be expired, so Sign out really signs you out and signing back in works
immediately.

The signing key lives in `secret` in the state directory (`0600`, created on
first run). Sessions last 30 days. Deleting that file signs everyone out.

HTTP Basic is still accepted for API clients, so `curl -u user:pass` keeps
working. Either way the password crosses your network in the clear, so this is
a lock for home use, not for the open internet.

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

## Exporting for other apps

Setting the export folder only records *where*. Exporting is a separate
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

Copies go to a temporary name and are renamed into place, so a run cut short —
a container redeploy, a power cut — never leaves a half-written file behind.
A destination that cannot be read while its source can is treated as damaged
and replaced, which repairs anything left over from an interrupted run before
that was true.

Re-running is safe and cheap. A file already in place is left alone; one that
differs is replaced **only when the incoming copy is higher quality**, compared
by bitrate. So re-organising after re-ripping a book upgrades it in place:

```
Wrote: 8 books · 0 copied · 1 upgraded · 14 already there · 1 kept (incoming was not better)
```

Ticking **delete each original after its copy is verified** turns the copy into
a move: the source is removed only once its copy exists at a matching size, and
never for a file that was skipped — so a refused or failed copy can never lose
the original. Files the scan set aside as duplicates are not copied, and so are
not deleted either. The UI asks for confirmation before starting.

Pass `--overwrite` to replace regardless, or `--delete-originals` on the command
line for the same move behaviour. Organising into a directory inside
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
has no description yet — and fetches an author bio for each new author it meets
along the way — at about a second each, in the background with a progress
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

## Importing from a download folder

Set a **download folder** in Settings and new files can land there — from a
download client, a share, wherever. **Import** reads it, files each book into
the library as `Author/Title`, and rescans, so it appears ready to play.

**Preview** shows the plan and writes nothing. Ticking *delete each download
once its copy is verified* empties the download folder as it goes, and the
delete only happens after the copy exists at a matching size, so a failed copy
can never lose the original.

Files the scan set aside as duplicates are not imported, and so are not
deleted — they stay in the download folder for you to look at.

The library and the download folder must be separate directories; importing a
folder into itself is refused.

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
| `GET /api/users` | who you are, and the account list if you are an admin |
| `POST /api/users` | create an account |
| `POST /api/users/<name>/password` | set a password (own needs `current`) |
| `POST /api/users/<name>/role` | change a role |
| `POST /api/users/<name>/delete` | delete an account and its saved positions |
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
- JSON responses and the web assets are gzipped when the client asks. On a
  3,803-book library the listing goes from 958 KB to 162 KB, which is most of
  what a first load costs over a remote link. Audio and images are never
  compressed — they already are, and it would break range requests.
- The library grid renders every book at once. At 3,803 books that is about
  160ms and 26k DOM nodes on a laptop, with cover images loaded lazily as you
  scroll. Well past that, or on a slow phone, it would want windowing.

## Files

| File | Contents |
| --- | --- |
| [`server.py`](server.py) | HTTP server, range streaming, JSON API, CLI |
| [`library.py`](library.py) | directory scan, grouping, dedup, index |
| [`organise.py`](organise.py) | the tidy-copy tree, playlists, manifest |
| [`metadata.py`](metadata.py) | Audible / Apple / Google lookup and match ranking |
| [`settings.py`](settings.py) | settings file, password hashing |
| [`users.py`](users.py) | accounts, roles and the lockout guards |
| [`oggopus.py`](oggopus.py) | Ogg/Opus tag, duration and cover parsing |
| [`web/`](web) | the player (plain HTML/CSS/JS, no build step) |
