"""Read-only recursive scanner. Never writes to the scanned files."""
import hashlib
import io
import json
import os
import shutil
import subprocess
import time

import mutagen

from . import analysis, tags

AUDIO_EXT = {".mp3", ".flac", ".wav", ".m4a", ".aac", ".alac", ".ogg", ".oga", ".opus",
             ".aiff", ".aif", ".aifc", ".ape", ".wma"}
SKIP_DIRS = {".Trashes", ".Spotlight-V100", ".fseventsd", ".TemporaryItems", "$RECYCLE.BIN",
             "System Volume Information", ".DocumentRevisions-V100"}


class ScanError(Exception):
    pass


def tool(name):
    return shutil.which(name) or next((p for p in (f"/opt/homebrew/bin/{name}", f"/usr/local/bin/{name}")
                                       if os.path.exists(p)), None)


# Audio inspection status stored in tracks.status. Metadata problems are tracked separately (issues table).
OK, PARTIAL, UNSUPPORTED, SUSPECT, FAILED, UNAVAILABLE = (
    "ok", "partial", "unsupported", "potentially_corrupted", "decode_failure", "unavailable")
UNDECODABLE_CODECS = {"none", "unknown", "drms"}  # 'drms' = Apple FairPlay DRM (iTunes .m4p)


def ffprobe(path):
    """{'ok': True, 'json': ...} | {'ok': False, 'error': msg} | None when ffprobe is not installed."""
    exe = tool("ffprobe")
    if not exe:
        return None
    try:
        r = subprocess.run([exe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", path],
                           capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        return {"ok": False, "error": str(e)}
    if r.returncode != 0:
        return {"ok": False, "error": r.stderr.strip()[:500] or f"ffprobe exit code {r.returncode}"}
    try:
        return {"ok": True, "json": json.loads(r.stdout)}
    except ValueError as e:
        return {"ok": False, "error": f"ffprobe output unreadable: {e}"}


def is_fragmented_mp4(path):
    """True if the file stores audio in moof fragments (common for streamed/downloaded .m4a)."""
    try:
        with open(path, "rb") as fh:
            pos = 0
            for _ in range(8):
                fh.seek(pos)
                h = fh.read(8)
                if len(h) < 8:
                    return False
                size, typ = int.from_bytes(h[:4], "big"), h[4:]
                if typ == b"moof":
                    return True
                if size == 1:
                    size = int.from_bytes(fh.read(8), "big")
                if size < 8:
                    return False
                pos += size
    except OSError:
        return False
    return False


def verify_audio(path):
    """Full decode with ffmpeg (read-only). Returns (status, detail); status None if ffmpeg is missing."""
    if not os.path.isfile(path):
        return UNAVAILABLE, "File not found (moved, deleted or drive disconnected)"
    exe = tool("ffmpeg")
    if not exe:
        return None, "ffmpeg not installed (brew install ffmpeg)"
    try:
        r = subprocess.run([exe, "-v", "error", "-nostdin", "-i", path, "-f", "null", "-"],
                           capture_output=True, text=True, timeout=900)
    except (OSError, subprocess.SubprocessError) as e:
        return None, f"Could not run ffmpeg: {e}"
    err = r.stderr.strip()
    low = err.lower()
    if r.returncode != 0:
        if ("decoder" in low and "not found" in low) or "unsupported codec" in low or "no decoder" in low:
            return UNSUPPORTED, f"ffmpeg has no decoder for this audio: {err[:300]}"
        return FAILED, f"Full decode failed (exit {r.returncode}): {err[:500]}"
    if err:
        bad = sum(1 for line in err.splitlines() if "error submitting packet" in line.lower()) or len(err.splitlines())
        return PARTIAL, f"Decoded to the end with {bad} damaged packet(s) — may cause brief glitches: {err[:300]}"
    return OK, "Full decode clean"


def _format_name(path, f):
    ext = os.path.splitext(path)[1].lower()
    if ext in (".m4a", ".alac", ".mp4"):
        codec = getattr(f.info, "codec", "") if f else ""
        return "ALAC" if codec == "alac" else "AAC"
    return {".aif": "AIFF", ".aifc": "AIFF", ".oga": "OGG"}.get(ext, ext[1:].upper())


def _int(x):
    try:
        return int(x)
    except (TypeError, ValueError):
        return None


def _apply_probe(row, j, take_tags):
    fmt = j.get("format") or {}
    audio = [s for s in j.get("streams") or [] if s.get("codec_type") == "audio"]
    if not audio:
        return None, fmt
    s = audio[0]
    row.update(duration=float(s.get("duration") or fmt.get("duration") or 0),
               bitrate=row.get("bitrate") or _int(s.get("bit_rate") or fmt.get("bit_rate")),
               sample_rate=row.get("sample_rate") or _int(s.get("sample_rate")),
               channels=row.get("channels") or _int(s.get("channels")),
               bits=row.get("bits") or _int(s.get("bits_per_raw_sample")),
               codec=row.get("codec") or s.get("codec_name", ""))
    if take_tags:
        t = {k.lower(): v for k, v in (fmt.get("tags") or {}).items()}
        row.update(title=t.get("title", ""), artist=t.get("artist", ""), albumartist=t.get("album_artist", ""),
                   album=t.get("album", ""), genre=t.get("genre", ""), year=t.get("date", ""),
                   track=t.get("track", ""), disc=t.get("disc", ""))
    return s, fmt


def inspect_audio(path, row, mutagen_error):
    """Decide the audio status after the tag reader ran. Fills duration etc. from ffprobe when needed.
    A missing field or a tag-parsing failure is never treated as corruption on its own."""
    has_audio_props = bool(row.get("sample_rate") or row.get("channels"))
    fragmented = os.path.splitext(path)[1].lower() in (".m4a", ".mp4", ".m4b") and is_fragmented_mp4(path)
    if not mutagen_error and (row.get("duration") or 0) > 0 and has_audio_props and not fragmented:
        return OK, "", ""
    if fragmented and not mutagen_error:
        row["duration"] = 0  # the tag reader only counts samples in moov, not in the fragments
    probe = ffprobe(path)
    if mutagen_error:
        why = f"Tag reader: {mutagen_error}. "
    elif not has_audio_props:
        why = "Tag reader found no audio track properties. "
    else:
        why = "Tag reader reported zero length. "
    if probe is None:
        return SUSPECT, why + "ffprobe is not installed, so the audio could not be checked.", ""
    if not probe["ok"]:
        return SUSPECT, why + f"ffprobe could not open the file: {probe['error']}", ""
    s, _ = _apply_probe(row, probe["json"], take_tags=bool(mutagen_error))
    if s is None:
        return UNSUPPORTED, why + "The file has no audio stream ffprobe can identify.", ""
    codec = (s.get("codec_name") or "").lower()
    if codec in UNDECODABLE_CODECS or (s.get("codec_tag_string") or "").lower() == "drms":
        return UNSUPPORTED, why + f"Audio codec '{s.get('codec_tag_string') or codec}' cannot be decoded "\
                                  "(DRM-protected or unknown).", ""
    if (row.get("duration") or 0) <= 0:
        return SUSPECT, why + f"ffprobe sees a '{codec}' stream but cannot determine its length — the file "\
                              "header may be damaged. Run Verify Audio to confirm.", ""
    diag = ("Fragmented MP4 (moof/mdat): " if fragmented else "") + \
        f"duration {row['duration']:.1f}s read by ffprobe"
    return OK, "", diag


def extract(path) -> dict:
    """Metadata + technical info + audio status for one file. Read-only."""
    row = dict(path=path, readable=1, error=None, art=None, status=OK, status_detail="", diag="",
               **{k: "" for k in tags.FIELDS})
    try:
        st = os.stat(path)
        with open(path, "rb"):
            pass  # permission / I/O check
    except OSError as e:
        row.update(size=0, mtime=0, readable=0, status=UNAVAILABLE, status_detail=str(e),
                   format=_format_name(path, None), track=None, disc=None)
        return row
    row.update(size=st.st_size, mtime=st.st_mtime)
    f = mutagen_error = None
    try:
        f = mutagen.File(path)
        if f is None:
            raise ValueError("format not recognised by the tag reader")
        info = f.info
        row.update(duration=float(getattr(info, "length", 0) or 0),
                   bitrate=_int(getattr(info, "bitrate", 0)),
                   sample_rate=_int(getattr(info, "sample_rate", 0)),
                   channels=_int(getattr(info, "channels", 0)),
                   bits=_int(getattr(info, "bits_per_sample", 0)),
                   codec=str(getattr(info, "codec", "") or type(f).__name__))
        row.update(tags.read_tags(f))
        row["art"] = tags.get_art(f)
    except Exception as e:
        mutagen_error = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
    row["status"], row["status_detail"], row["diag"] = inspect_audio(path, row, mutagen_error)
    if mutagen_error and row["status"] in (OK, PARTIAL, UNSUPPORTED):
        row["error"] = f"Tags could not be parsed ({mutagen_error}); values shown were read by ffprobe"
    row["readable"] = int(row["status"] in (OK, PARTIAL, UNSUPPORTED))
    row["format"] = _format_name(path, f)
    row["track"], row["track_total"] = tags.split_num(row["track"])
    row["disc"], row["disc_total"] = tags.split_num(row["disc"])
    return row


TECH_COLS = ("duration", "bitrate", "sample_rate", "channels", "bits", "codec", "readable",
             "status", "status_detail", "diag")
OLD_AUDIO_NOTES = "error LIKE 'Zero duration%' OR error LIKE 'Read via ffprobe%'"  # written by the old scanner


def reclassify(conn, progress=None, cancel=None, where="status IS NULL", workers=8):
    """Fill audio status for rows scanned by older versions. Only technical/status columns are updated;
    tags, artwork, history and every other scan result are left as they are."""
    from concurrent.futures import ThreadPoolExecutor
    rows = conn.execute(f"SELECT id, path, duration, readable, sample_rate, channels FROM tracks "
                        f"WHERE missing=0 AND ({where})").fetchall()

    def trusted(r):  # old result is fine as-is; MP4s are re-checked because fragments can hide length
        return (r["readable"] and (r["duration"] or 0) > 0 and (r["sample_rate"] or r["channels"])
                and os.path.splitext(r["path"])[1].lower() not in (".m4a", ".mp4", ".m4b"))
    quick = [r for r in rows if trusted(r)]
    conn.executemany(f"UPDATE tracks SET status='ok', status_detail='', diag='', "
                     f"error=CASE WHEN {OLD_AUDIO_NOTES} THEN NULL ELSE error END WHERE id=?",
                     [(r["id"],) for r in quick])
    slow = [r for r in rows if not trusted(r)]
    done = 0
    with ThreadPoolExecutor(workers) as ex:
        for i in range(0, len(slow), 64):
            if cancel and cancel():
                break
            chunk = slow[i:i + 64]
            for r, new in zip(chunk, ex.map(lambda r: extract(r["path"]), chunk)):
                conn.execute(f"UPDATE tracks SET {', '.join(c + '=?' for c in TECH_COLS)}, "
                             f"error=CASE WHEN ? IS NOT NULL THEN ? WHEN {OLD_AUDIO_NOTES} THEN NULL ELSE error END "
                             f"WHERE id=?", [new.get(c) for c in TECH_COLS] + [new["error"], new["error"], r["id"]])
            done += len(chunk)
            conn.commit()
            if progress:
                progress(done, len(slow), "Re-inspecting audio")
    conn.commit()
    return dict(checked=len(rows), reinspected=done)


def verify_track(conn, track_id):
    """Full decode of one library file; stores the result. Returns (status, detail)."""
    r = conn.execute("SELECT path, diag FROM tracks WHERE id=?", (track_id,)).fetchone()
    status, detail = verify_audio(r["path"])
    if status:
        diag = "; ".join(x for x in (r["diag"] or "").split("; ") if x and not x.startswith("Full decode"))
        diag = "; ".join(x for x in (diag, f"Full decode {time.strftime('%Y-%m-%d')}: {status}") if x)
        conn.execute("UPDATE tracks SET status=?, status_detail=?, diag=?, readable=? WHERE id=?",
                     (status, "" if status == OK else detail, diag, int(status in (OK, PARTIAL, UNSUPPORTED)),
                      track_id))
        conn.commit()
    return status, detail


COLS = ("path", "root", "size", "mtime", "format", "codec", "title", "artist", "albumartist", "album", "genre",
        "year", "track", "track_total", "disc", "disc_total", "duration", "bitrate", "sample_rate", "channels",
        "bits", "has_art", "art_hash", "art_w", "art_h", "art_mime", "readable", "error", "scanned_at", "missing",
        "status", "status_detail", "diag")


def _store(conn, row, root, known_thumbs):
    art = row.pop("art", None)
    row.update(root=root, has_art=0, art_hash=None, art_w=None, art_h=None, art_mime=None,
               scanned_at=time.time(), missing=0)
    if art and art[0]:
        data, mime = art
        h = hashlib.sha1(data).hexdigest()[:20]
        row.update(has_art=1, art_hash=h, art_mime=mime)
        try:
            from PIL import Image
            im = Image.open(io.BytesIO(data))
            row["art_w"], row["art_h"] = im.size
            if h not in known_thumbs:
                im = im.convert("RGB")
                im.thumbnail((180, 180))
                buf = io.BytesIO()
                im.save(buf, "JPEG", quality=85)
                conn.execute("INSERT OR REPLACE INTO thumbs VALUES(?,?)", (h, buf.getvalue()))
                known_thumbs.add(h)
        except Exception:
            row["error"] = row.get("error") or "Embedded artwork is not a readable image"
    vals = [row.get(c) for c in COLS]
    conn.execute(f"INSERT INTO tracks({','.join(COLS)}) VALUES({','.join('?' * len(COLS))}) "
                 f"ON CONFLICT(path) DO UPDATE SET {','.join(f'{c}=excluded.{c}' for c in COLS[1:])}, "
                 "hash=NULL, hash_key=NULL, fingerprint=NULL, acoustid_rec=NULL", vals)


def refresh_track(conn, track_id, path):
    """Re-read one file after we modified it."""
    root = conn.execute("SELECT root FROM tracks WHERE id=?", (track_id,)).fetchone()
    known = {r[0] for r in conn.execute("SELECT art_hash FROM thumbs")}
    _store(conn, extract(path), root[0] if root else os.path.dirname(path), known)


def collect(root, cancel=None):
    files, errors = [], []
    for dirpath, dirnames, filenames in os.walk(root, onerror=lambda e: errors.append(str(e))):
        if cancel and cancel():
            break
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for fn in filenames:
            # "._x.mp3" are macOS AppleDouble metadata files that appear on FAT/exFAT SD cards
            if not fn.startswith(".") and os.path.splitext(fn)[1].lower() in AUDIO_EXT:
                files.append(os.path.join(dirpath, fn))
    return files, errors


def scan(conn, root, progress=None, cancel=None, force=False):
    root = os.path.abspath(os.path.expanduser(root))
    if not os.path.isdir(root):
        raise ScanError(f"Folder is not available: {root}\nIf this is an external drive or SD card, "
                        "check that it is connected and mounted.")
    started = time.time()
    if progress:
        progress(0, 0, "Finding audio files…")
    files, walk_errors = collect(root, cancel)
    prefix = root.rstrip("/") + "/"
    existing = {r["path"]: r for r in conn.execute(
        "SELECT id, path, size, mtime, missing FROM tracks WHERE substr(path, 1, ?) = ?", (len(prefix), prefix))}
    known_thumbs = {r[0] for r in conn.execute("SELECT art_hash FROM thumbs")}
    st = dict(files=len(files), new=0, updated=0, unchanged=0, errors=len(walk_errors), missing=0)
    status = "cancelled" if cancel and cancel() else "completed"
    for i, path in enumerate(files if status == "completed" else []):
        if cancel and cancel():
            status = "cancelled"
            break
        if progress:
            progress(i, len(files), path)
        old = existing.get(path)
        try:
            s = os.stat(path)
            if old and not force and not old["missing"] and old["size"] == s.st_size and old["mtime"] == s.st_mtime:
                st["unchanged"] += 1
                continue
            row = extract(path)
        except OSError:  # file vanished / drive ejected mid-scan
            if not os.path.isdir(root):
                status = "drive disconnected"
                break
            row = extract(path)  # records it as unavailable with the OS error
        if row["status"] in (SUSPECT, FAILED, UNAVAILABLE):
            st["errors"] += 1
        st["updated" if old else "new"] += 1
        _store(conn, row, root, known_thumbs)
        if i % 200 == 0:
            conn.commit()
    if status == "completed":
        gone = set(existing) - set(files)
        st["missing"] = len(gone)
        conn.executemany("UPDATE tracks SET missing=1 WHERE path=?", [(p,) for p in gone])
    conn.execute("INSERT INTO scans(root, started, finished, files, new, updated, unchanged, errors, missing, status) "
                 "VALUES(?,?,?,?,?,?,?,?,?,?)", (root, started, time.time(), st["files"], st["new"], st["updated"],
                                                 st["unchanged"], st["errors"], st["missing"], status))
    conn.commit()
    reclassify(conn, progress, cancel)  # rows from scans made before audio statuses existed
    if progress:
        progress(len(files), len(files), "Analysing metadata…")
    analysis.rebuild_issues(conn)
    st.update(status=status, root=root, walk_errors=walk_errors[:20])
    return st
