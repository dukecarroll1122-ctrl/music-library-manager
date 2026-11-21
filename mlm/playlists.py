"""Playlists: ordered lists of library songs, stored in the app's own database (music files are never touched).
Exports and imports extended M3U (.m3u8), which iTunes/Music, VLC, Plex and most players understand."""
import os
import sqlite3
import time
import urllib.parse


def create(conn, name):
    name = (name or "").strip()
    if not name:
        raise ValueError("Enter a playlist name")
    try:
        cur = conn.execute("INSERT INTO playlists(name, created) VALUES(?,?)", (name, time.time()))
    except sqlite3.IntegrityError:
        raise ValueError(f"A playlist named “{name}” already exists")
    conn.commit()
    return cur.lastrowid


def rename(conn, pid, name):
    name = (name or "").strip()
    if not name:
        raise ValueError("Enter a playlist name")
    try:
        conn.execute("UPDATE playlists SET name=? WHERE id=?", (name, pid))
    except sqlite3.IntegrityError:
        raise ValueError(f"A playlist named “{name}” already exists")
    conn.commit()


def delete(conn, pid):
    conn.execute("DELETE FROM playlist_tracks WHERE playlist_id=?", (pid,))
    conn.execute("DELETE FROM playlists WHERE id=?", (pid,))
    conn.commit()


def list_all(conn):
    return [dict(r) for r in conn.execute(
        "SELECT p.id, p.name, count(pt.track_id) AS n FROM playlists p LEFT JOIN playlist_tracks pt "
        "ON pt.playlist_id=p.id GROUP BY p.id ORDER BY p.name COLLATE NOCASE")]


def tracks(conn, pid):
    """Songs in order. A song whose file is gone from the library (drive unplugged) is kept and flagged."""
    out = []
    for r in conn.execute("SELECT t.*, pt.pos FROM playlist_tracks pt JOIN tracks t ON t.id=pt.track_id "
                          "WHERE pt.playlist_id=? ORDER BY pt.pos", (pid,)):
        d = dict(r)
        d["name"] = d["title"] or os.path.basename(d["path"])
        d["state"] = "not found" if d["missing"] else ""
        out.append(d)
    return out


def add_tracks(conn, pid, track_ids):
    """Append songs; ones already in the playlist are skipped. Returns how many were added."""
    have = {r[0] for r in conn.execute("SELECT track_id FROM playlist_tracks WHERE playlist_id=?", (pid,))}
    pos = (conn.execute("SELECT max(pos) FROM playlist_tracks WHERE playlist_id=?", (pid,)).fetchone()[0] or -1) + 1
    n = 0
    for tid in dict.fromkeys(track_ids):
        if tid in have or not conn.execute("SELECT 1 FROM tracks WHERE id=?", (tid,)).fetchone():
            continue
        conn.execute("INSERT INTO playlist_tracks VALUES(?,?,?)", (pid, tid, pos + n))
        n += 1
    conn.commit()
    return n


def _renumber(conn, pid):
    ids = [r[0] for r in conn.execute("SELECT track_id FROM playlist_tracks WHERE playlist_id=? ORDER BY pos", (pid,))]
    conn.executemany("UPDATE playlist_tracks SET pos=? WHERE playlist_id=? AND track_id=?",
                     [(i, pid, t) for i, t in enumerate(ids)])
    conn.commit()


def remove(conn, pid, track_ids):
    conn.executemany("DELETE FROM playlist_tracks WHERE playlist_id=? AND track_id=?", [(pid, t) for t in track_ids])
    _renumber(conn, pid)


def move(conn, pid, track_id, delta):
    """Swap a song with its neighbour (delta -1 = up, +1 = down). Returns False at either end."""
    row = conn.execute("SELECT pos FROM playlist_tracks WHERE playlist_id=? AND track_id=?", (pid, track_id)).fetchone()
    other = conn.execute("SELECT track_id FROM playlist_tracks WHERE playlist_id=? AND pos=?",
                         (pid, row[0] + delta)).fetchone() if row else None
    if not other:
        return False
    conn.execute("UPDATE playlist_tracks SET pos=? WHERE playlist_id=? AND track_id=?", (row[0] + delta, pid, track_id))
    conn.execute("UPDATE playlist_tracks SET pos=? WHERE playlist_id=? AND track_id=?", (row[0], pid, other[0]))
    conn.commit()
    return True


# ---------------------------------------------------------------- M3U
def export_m3u(conn, pid, path):
    """Write the playlist as UTF-8 extended M3U with absolute paths. Returns the number of songs written."""
    rows = tracks(conn, pid)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("#EXTM3U\n")
        for r in rows:
            label = " - ".join(x for x in (r["artist"], r["title"]) if x) or os.path.basename(r["path"])
            fh.write(f"#EXTINF:{int(r['duration'] or 0)},{label}\n{r['path']}\n")
    return len(rows)


def import_m3u(conn, path, name=None):
    """Create a playlist from an M3U/M3U8 file, matching entries to songs already in the library by path.
    Returns dict(id, name, added, missing=[entries not in the library])."""
    raw = open(path, "rb").read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")  # old .m3u files
    base = os.path.dirname(os.path.abspath(path))
    ids, missing = [], []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        p = urllib.parse.unquote(urllib.parse.urlparse(line).path) if line.startswith("file://") else line
        p = os.path.normpath(p if os.path.isabs(p) else os.path.join(base, p))
        r = conn.execute("SELECT id FROM tracks WHERE path=?", (p,)).fetchone() or \
            conn.execute("SELECT id FROM tracks WHERE lower(path)=lower(?)", (p,)).fetchone()  # macOS is case-insensitive
        (ids if r else missing).append(r[0] if r else line)
    stem = (name or os.path.splitext(os.path.basename(path))[0]).strip() or "Imported playlist"
    final, n = stem, 2
    while conn.execute("SELECT 1 FROM playlists WHERE name=?", (final,)).fetchone():
        final, n = f"{stem} ({n})", n + 1
    pid = create(conn, final)
    return dict(id=pid, name=final, added=add_tracks(conn, pid, ids), missing=missing)
