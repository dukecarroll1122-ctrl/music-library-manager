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


def ffprobe(path):
    exe = tool("ffprobe")
    if not exe:
        return None
    try:
        r = subprocess.run([exe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", path],
                           capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            return None
        return json.loads(r.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def verify_audio(path) -> str:
    """Full decode with ffmpeg; returns '' if clean, else the decoder errors. Read-only."""
    exe = tool("ffmpeg")
    if not exe:
        return "ffmpeg not installed (brew install ffmpeg)"
    try:
        r = subprocess.run([exe, "-v", "error", "-nostdin", "-i", path, "-f", "null", "-"],
                           capture_output=True, text=True, timeout=600)
    except (OSError, subprocess.SubprocessError) as e:
        return str(e)
    return (r.stderr.strip() or ("ffmpeg exit code %d" % r.returncode if r.returncode else ""))[:2000]


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


def extract(path) -> dict:
    """Metadata + technical info for one file. Unreadable files come back with readable=0."""
    st = os.stat(path)
    row = dict(path=path, size=st.st_size, mtime=st.st_mtime, readable=1, error=None, art=None,
               **{k: "" for k in tags.FIELDS})
    f = None
    try:
        f = mutagen.File(path)
        if f is None:
            raise ValueError("Unrecognised or unsupported audio data")
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
        probe = ffprobe(path)
        if probe and probe.get("streams") and float((probe.get("format") or {}).get("duration") or 0) > 0:
            fmt, s = probe.get("format", {}), next((s for s in probe["streams"] if s.get("codec_type") == "audio"),
                                                   probe["streams"][0])
            t = {k.lower(): v for k, v in (fmt.get("tags") or {}).items()}
            row.update(duration=float(fmt.get("duration") or 0), bitrate=_int(fmt.get("bit_rate")),
                       sample_rate=_int(s.get("sample_rate")), channels=_int(s.get("channels")),
                       bits=_int(s.get("bits_per_raw_sample")), codec=s.get("codec_name", ""),
                       title=t.get("title", ""), artist=t.get("artist", ""),
                       albumartist=t.get("album_artist", ""), album=t.get("album", ""),
                       genre=t.get("genre", ""), year=t.get("date", ""), track=t.get("track", ""),
                       disc=t.get("disc", ""))
            row["error"] = f"Read via ffprobe (mutagen: {e})"
        else:
            row.update(readable=0, error=str(e) or type(e).__name__)
    row["format"] = _format_name(path, f)
    row["track"], row["track_total"] = tags.split_num(row["track"])
    row["disc"], row["disc_total"] = tags.split_num(row["disc"])
    if row["readable"] and not row.get("duration"):
        row["error"] = row["error"] or "Zero duration — file may be truncated or corrupted"
    return row


COLS = ("path", "root", "size", "mtime", "format", "codec", "title", "artist", "albumartist", "album", "genre",
        "year", "track", "track_total", "disc", "disc_total", "duration", "bitrate", "sample_rate", "channels",
        "bits", "has_art", "art_hash", "art_w", "art_h", "art_mime", "readable", "error", "scanned_at", "missing")


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
        except OSError as e:  # file vanished / drive ejected mid-scan
            st["errors"] += 1
            if not os.path.isdir(root):
                status = "drive disconnected"
                break
            row = dict({k: "" for k in tags.FIELDS}, path=path, size=0, mtime=0, readable=0, error=str(e),
                       format=_format_name(path, None), track=None, disc=None)
        if not row.get("readable"):
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
    if progress:
        progress(len(files), len(files), "Analysing metadata…")
    analysis.rebuild_issues(conn)
    st.update(status=status, root=root, walk_errors=walk_errors[:20])
    return st
