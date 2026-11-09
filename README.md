# Music Library Manager

A macOS desktop app to scan a music collection (internal disk, external drives, SD cards), inspect metadata,
find missing or suspicious tags, check them against MusicBrainz, repair them safely, manage album artwork,
find duplicates, organise folders, and export reports.

**Read-only by default.** Nothing on disk changes until you switch to Edit Mode, approve specific changes, and
confirm. Every write is backed up, logged and undoable.

## Install & run

Requirements: macOS, Python 3.12+ (tested on 3.13), and optionally `ffmpeg` (`brew install ffmpeg`) for
deeper audio inspection and corruption checks.

```bash
./run.sh
```

The first run creates `.venv` and installs the dependencies. To do it by hand:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m mlm
```

### Standalone .app

```bash
./build_mac.sh        # → dist/Music Library Manager.app
```

## Using it

| Page | What it does |
|---|---|
| **Dashboard** | Totals (songs, artists, albums, storage) plus problem counts. Click a problem card to see those songs. Charts show artists, albums, genres, formats and metadata health. |
| **Music Library** | Every song in a sortable table. Global search, plus filters for artist, album, genre, year, format, problem type, minimum bitrate and sample rate. Select songs and use **Edit Selected…** to batch edit. |
| **Metadata Inspector** | Songs with issues, a detail panel with all tags, technical info, the issue list and MusicBrainz results. **Check with MusicBrainz** grades each song as Verified / High-confidence / Possible / Needs manual review / No match. **Verify Audio** fully decodes files with ffmpeg to detect corruption. |
| **Metadata Repair** | Proposed changes from MusicBrainz and your own edits, shown as BEFORE → AFTER. Approve or reject each one, then **Apply Approved Changes**. The **Repair history** tab lists every batch with **Undo**. |
| **Artwork Manager** | Albums with missing or mixed artwork. Search the Cover Art Archive or choose a file, preview it, then apply to the whole album (optionally only songs without art). Full resolution is kept up to 3000 px. |
| **Duplicate Finder** | Three methods: identical files (SHA-1), same artist + title + length, and AcoustID fingerprint. Tick the copies to keep; **Move Unticked Copies…** moves the rest to a review folder you choose. Files are never deleted. |
| **Organization** | Pick a naming template, e.g. `{albumartist}/{album}/{track:02d} - {title}`, preview every move, then apply. Conflicts are skipped and nothing is overwritten. |
| **Reports** | Inventory, artists, albums, missing metadata, metadata errors, duplicates and audio quality, as CSV or JSON. |
| **Settings** | Mode, MusicBrainz contact, AcoustID key, scanned folders (rescan or forget), data folder, cache. |

Shortcuts: ⌘1 … ⌘9 switch pages.

### Scanning SD cards and external drives
Click **Scan Folder…**. The picker opens at `/Volumes`, where removable drives appear. Scans are recursive and
read-only. They skip the macOS `._*` AppleDouble files that show up on FAT/exFAT cards. Rescans only re-read
files whose size or modification time changed. If a drive is unplugged, its songs are kept and marked as not
found instead of being deleted, and scanning a disconnected drive shows a clear message.

### How MusicBrainz results are treated
A database result is evidence, not truth. Matching scores title, artist, album and duration. Proposals that
differ from your tags carry a note that remasters, deluxe, regional and compilation editions can legitimately
differ. Year, track and disc changes to fields that already have a value are never rated higher than "Possible
match". Release-level fields (album, album artist, year, track) are only proposed when the match is on your
album. Nothing is applied automatically. Requests are rate-limited to 1/second and cached for 30 days. If you're
offline you get a clear message and the batch stops.

## Optional API keys

* **MusicBrainz:** no key needed. You can enter a contact email or URL in Settings; it's sent in the User-Agent,
  as MusicBrainz asks.
* **AcoustID (audio fingerprint duplicates):**
  1. Register a free application at <https://acoustid.org/new-application> and copy its API key.
  2. Install Chromaprint: `brew install chromaprint` (provides `fpcalc`).
  3. Paste the key in **Settings → AcoustID API key**. The AcoustID option in Duplicate Finder becomes active.

## Safety model

* Read-Only Mode is the default on every launch. The write functions themselves (`mlm/repair.py`) refuse to run
  without edit mode, not just the UI.
* Before any tag write, the original tags (and original artwork, if artwork changes) go to
  `~/Library/Application Support/MusicLibraryManager/backups/<batch>/`.
* Every change is recorded in the `history` table and appended to `changes.log` in the same folder.
* Only tags are rewritten. The tests check that the decoded audio checksum is identical before and after edits.
  Embedded artwork is kept unless you replace it.
* Moves and renames never overwrite. Cross-drive moves copy, then remove the source. Duplicates are moved, never
  deleted.
* Undo restores tags, artwork and file locations per batch.

## Data location

`~/Library/Application Support/MusicLibraryManager/` holds `library.db` (SQLite), `backups/` and `changes.log`.
Set `MLM_DATA_DIR` to use a different folder.

## Tests

```bash
.venv/bin/python -m pytest
```

There are 23 tests. They generate real MP3/FLAC/AAC/Opus/WAV files with ffmpeg and cover: scanning and
extraction, incremental rescans, disconnected drives, cancelling, issue rules, all three duplicate methods'
logic, read-only enforcement, tag edits that keep the audio bytes and artwork, artwork replacement and undo,
input validation, the suggestion workflow, organisation without overwriting, MusicBrainz grading (offline
fixtures), report exports, and a headless GUI smoke test of every page.

## Project layout

```
mlm/
  db.py          SQLite schema (tracks, issues, scans, suggestions, history, duplicates, cache, settings)
  tags.py        uniform tag/artwork read & write for ID3, MP4, Vorbis/FLAC/Opus, APEv2, ASF
  scanner.py     recursive read-only scanner, ffprobe fallback, ffmpeg integrity check
  analysis.py    issue rules, dashboard stats, track queries
  online.py      rate-limited HTTP, MusicBrainz matching, Cover Art Archive, AcoustID
  repair.py      the only code that writes: apply, backup, log, move, undo
  duplicates.py  hash / metadata / AcoustID duplicate groups
  organize.py    naming templates and move plans
  reports.py     CSV/JSON exports
  gui/           PySide6 app (app.py window + pages_*.py)
tests/
```

## Known limitations

* WMA (ASF) and raw `.aac` (ADTS) files are scanned and inspected, but their tags/artwork are read-only. Writing
  is supported for MP3, FLAC, M4A (AAC/ALAC), OGG, Opus, WAV, AIFF and APE.
* WAV files are read and written using ID3 tags. RIFF INFO tags written by some Windows tools are not shown.
* Genre suggestions come from MusicBrainz community tags and are always rated "Possible match".
* The `.app` built by PyInstaller is unsigned. On first launch, right-click it and choose **Open**.
