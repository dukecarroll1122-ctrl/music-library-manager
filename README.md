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
| **Audio Verification** | Checks whether the audio actually matches the tags, using a Chromaprint fingerprint looked up in AcoustID/MusicBrainz. Statuses: Verified match, Likely match, Possible mismatch, Strong mismatch, No fingerprint match, Needs manual review. Runs in the background with pause, resume and cancel. **Play** and **Compare…** send files to the player. |
| **Music Player** | Plays files from where they are (Qt Multimedia with its FFmpeg backend): MP3, AAC/M4A, ALAC, FLAC, WAV, AIFF, Opus, OGG and more. Queue, seek, volume and artwork. **Compare mode** keeps the playback position when you switch songs, so two versions can be A/B'd. |
| **Audio Converter** | Makes converted **copies** with FFmpeg. Outputs: MP3 (320/256/192/128 CBR, custom bitrate or VBR V0–V9), AAC, FLAC, WAV, ALAC, OGG, Opus. Sample rate and mono/stereo options. Tags, track/disc numbers, album artist, year and artwork are kept. Batch queue with estimated sizes, per-file progress, cancel, and conversion history. |
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

### File health classes
Every song is put into exactly one class. A missing field, a tag-parsing problem or an unsupported codec is never
reported as corruption.

| Class | Meaning |
|---|---|
| Healthy | Audio inspected fine, no metadata problems |
| Metadata issues | Audio fine. Missing or suspicious tags, or tags that only ffprobe could read |
| Unsupported format or codec | No audio stream, or a codec that can't be decoded (e.g. DRM-protected `.m4p`) |
| Partial decoding warning | A full decode reached the end, but some packets were damaged (possible brief glitches) |
| Potentially corrupted | Neither the tag reader nor ffprobe could read the audio. Not yet confirmed |
| Confirmed decoding failure | A full ffmpeg decode failed (**Verify Audio**) |
| File unavailable | The file couldn't be opened (permissions, I/O error, removed mid-scan) |

Scans check headers only (fast). **Metadata Inspector → Verify Audio** runs a full decode and saves the result, which
is the only way to get "Partial decoding warning" or "Confirmed decoding failure".

**Fragmented MP4 (.m4a):** many downloaded or streamed `.m4a` files store their audio in `moof`/`mdat` fragments.
Mutagen, the tag reader, only counts audio inside the `moov` header, so it reports 0 or a too-short length. For
these files the duration comes from ffprobe, and the Inspector notes "Fragmented MP4". Tag edits on them are safe:
the tests check that the audio checksum is unchanged.

**Reports → Audio diagnostics report** lists the path, format, error message and inspection result for every file
that isn't plainly healthy or needed a fallback reader.

Databases created by earlier versions are migrated in place: new columns are added and no rows are removed. Old
rows get an audio status on the next scan.

### Audio verification
1. `fpcalc` (Chromaprint) fingerprints each file. Fingerprints are cached and recalculated only when a file
   changes.
2. AcoustID returns the recordings that match the fingerprint, with MusicBrainz details.
3. The identified recording is compared with your title, artist and album. The comparison is version-aware:
   "(2008 Remaster)" counts as the same recording; live, remix, edit and acoustic versions are "Likely match"
   with a note; instrumental or karaoke versus vocal is "Possible mismatch"; same title by another artist (a
   cover?) is "Possible mismatch"; a different song is "Strong mismatch". An album that isn't among the
   recording's releases only matters if it isn't a compilation. Weak fingerprint matches (<70%) are never rated
   above "Likely" or "Possible".
4. **No fingerprint match is never treated as evidence that a file is wrong.**

Nothing is modified or replaced automatically. Use **Compare…** to listen, then fix tags yourself in
Metadata Repair if needed. Without an API key the page still generates and caches fingerprints.

### Audio conversion safety
* Originals are only read. Output must go to a folder other than the original's own folder.
* Existing files are never overwritten. Each output is written to a hidden `.mlmpart` temp file, checked, then
  published with a no-overwrite link.
* Each output is verified with ffprobe (audio stream, codec, length within 1% of the source), both before and
  after tags are copied.
* Cancelling or quitting removes unfinished temp files; leftovers from a crash are cleaned up on the next run.
  The same file with the same settings is queued only once.
* The app warns honestly: lossless → lossy loses information; lossy → lossy always loses some quality and
  never improves it; a higher bitrate than the source doesn't restore quality; lossy → lossless only makes
  files bigger.
* Every conversion is stored in the database (settings, output path, status, errors), and failures are also
  written to `conversion_errors.log`.
* **Convert Albums to MP3…** in Music Library queues whole albums and keeps their `Artist/Album/` folders under
  the output folder.

### How MusicBrainz results are treated
Songs are matched **album by album**: one release search per album folder, then up to three candidate releases
are compared track-by-track (title, duration, track position). The release that fits your album best wins, and
editions with many extra tracks (deluxe, box sets) are penalised, so every song in an album gets the same
release, year and album artist. This takes about four requests per album instead of one per song. Untitled songs
can still be identified by track position and length. Songs without an album, or ones the release doesn't
contain, fall back to a per-song search.

A database result is evidence, not truth. Matching scores title, artist, album and duration. Proposals that
differ from your tags carry a note that remasters, deluxe, regional and compilation editions can legitimately
differ. Differences that are only an edition suffix ("Remastered", "Deluxe") or capitalisation, and year, track and disc changes to fields that already have a value are never rated higher than "Possible
match". Release-level fields (album, album artist, year, track) are only proposed when the match is on your
album. Nothing is applied automatically. Requests are rate-limited to 1/second and cached for 30 days. If you're
offline you get a clear message and the batch stops.

## Optional API keys

* **MusicBrainz:** no key needed. You can enter a contact email or URL in Settings; it's sent in the User-Agent,
  as MusicBrainz asks.
* **AcoustID (Audio Verification and fingerprint duplicates):**
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

There are 80 tests. They generate real MP3/FLAC/AAC/Opus/WAV files with ffmpeg and cover: scanning and
extraction, incremental rescans, disconnected drives, cancelling, issue rules, all three duplicate methods'
logic, read-only enforcement, tag edits that keep the audio bytes and artwork, artwork replacement and undo,
input validation, conversion to every output format (metadata, artwork, CBR/VBR, batch, cancel, integrity,
overwrite protection), playback of seven formats through Qt Multimedia, fingerprint identification with a
simulated AcoustID (correct, mislabeled and unknown recordings, pause/resume, caching), the version-aware
comparison rules, the suggestion workflow, every file health class (including fragmented MP4 and old-database
migration), organisation without overwriting, MusicBrainz grading and album-level
release matching (offline fixtures), report exports, and a headless GUI smoke test of every page.

## Project layout

```
mlm/
  db.py          SQLite schema (tracks, issues, scans, suggestions, history, duplicates, cache, settings)
  tags.py        uniform tag/artwork read & write for ID3, MP4, Vorbis/FLAC/Opus, APEv2, ASF
  scanner.py     recursive read-only scanner, ffprobe fallback, ffmpeg integrity check
  analysis.py    issue rules, dashboard stats, track queries
  online.py      rate-limited HTTP, MusicBrainz matching, Cover Art Archive, AcoustID
  repair.py      the only code that changes library files: apply, backup, log, move, undo
  convert.py     FFmpeg conversion (writes new copies only), verification, history
  verify.py      Chromaprint + AcoustID identity verification and comparison rules
  duplicates.py  hash / metadata / AcoustID duplicate groups
  organize.py    naming templates and move plans
  reports.py     CSV/JSON exports
  gui/           PySide6 app (app.py window + pages_*.py; pages_audio.py = verification, player, converter)
tests/
```

## Known limitations

* WMA (ASF) and raw `.aac` (ADTS) files are scanned and inspected, but their tags/artwork are read-only. Writing
  is supported for MP3, FLAC, M4A (AAC/ALAC), OGG, Opus, WAV, AIFF and APE.
* WAV files are read and written using ID3 tags. RIFF INFO tags written by some Windows tools are not shown.
* Genre suggestions come from MusicBrainz community tags and are always rated "Possible match".
* Audio Verification needs a free AcoustID API key. With about 3 lookups a second, a first pass over 8,000
  songs takes around 45–60 minutes. It can be paused, resumed, or stopped and continued later, since finished
  songs are skipped.
* OGG output uses FFmpeg's experimental Vorbis encoder (stereo only). Opus is the better choice.
* The player plays one file at a time; there's no gapless playback or crossfade.
* The `.app` built by PyInstaller is unsigned. On first launch, right-click it and choose **Open**.
