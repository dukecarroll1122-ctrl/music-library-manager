"""Duplicate detection: exact hash, metadata, AcoustID. Only reports groups; never deletes."""
import hashlib
import json
import os
import re
import subprocess
from collections import defaultdict

from . import db, online, scanner


def file_hash(path):
    h = hashlib.sha1()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _save(conn, method, groups):
    old = [r[0] for r in conn.execute("SELECT id FROM dup_groups WHERE method=?", (method,))]
    conn.executemany("DELETE FROM dup_members WHERE group_id=?", [(g,) for g in old])
    conn.execute("DELETE FROM dup_groups WHERE method=?", (method,))
    for key, ids in groups.items():
        gid = conn.execute("INSERT INTO dup_groups(method, key) VALUES(?,?)", (method, str(key))).lastrowid
        conn.executemany("INSERT INTO dup_members(group_id, track_id) VALUES(?,?)", [(gid, i) for i in ids])
    conn.commit()
    return len(groups)


def find_exact(conn, progress=None, cancel=None):
    rows = conn.execute("SELECT id, path, size, mtime, hash, hash_key FROM tracks WHERE missing=0 AND size>0 AND "
                        "size IN (SELECT size FROM tracks WHERE missing=0 GROUP BY size HAVING count(*)>1)").fetchall()
    by_hash = defaultdict(list)
    for i, r in enumerate(rows):
        if cancel and cancel():
            return None
        if progress:
            progress(i, len(rows), f"Hashing {os.path.basename(r['path'])}")
        key = f"{r['size']}:{r['mtime']}"
        h = r["hash"] if r["hash_key"] == key else None
        if not h:
            try:
                h = file_hash(r["path"])
            except OSError:
                continue
            conn.execute("UPDATE tracks SET hash=?, hash_key=? WHERE id=?", (h, key, r["id"]))
        by_hash[h].append(r["id"])
    return _save(conn, "exact", {h: ids for h, ids in by_hash.items() if len(ids) > 1})


def _norm(s):
    s = re.sub(r"[^\w\s]", " ", (s or "").casefold())
    s = re.sub(r"\s+", " ", s).strip()
    return re.sub(r"^the ", "", s)


def find_metadata(conn, tolerance=3.0, progress=None, cancel=None):
    """Same normalised artist + title and duration within `tolerance` seconds.
    'Song (Live)' and 'Song' are NOT grouped, so live/remix versions stay separate."""
    keyed = defaultdict(list)
    for r in conn.execute("SELECT id, artist, albumartist, title, duration FROM tracks WHERE missing=0 AND readable=1"):
        a, t = _norm(r["artist"] or r["albumartist"]), _norm(r["title"])
        if a and t:
            keyed[(a, t)].append((r["duration"] or 0, r["id"]))
    groups = {}
    for key, items in keyed.items():
        if len(items) < 2:
            continue
        items.sort()
        cluster = [items[0]]
        for d, tid in items[1:]:
            if d - cluster[-1][0] <= tolerance or not d or not cluster[-1][0]:
                cluster.append((d, tid))
            else:
                if len(cluster) > 1:
                    groups[f"{key[0]} – {key[1]} ({len(groups)})"] = [c[1] for c in cluster]
                cluster = [(d, tid)]
        if len(cluster) > 1:
            groups[f"{key[0]} – {key[1]} ({len(groups)})"] = [c[1] for c in cluster]
    return _save(conn, "metadata", groups)


def find_acoustic(conn, progress=None, cancel=None):
    key = db.get_setting(conn, "acoustid_key").strip()
    if not key:
        raise ValueError("AcoustID is not configured. Add a free API key in Settings "
                         "(get one at https://acoustid.org/new-application).")
    exe = scanner.tool("fpcalc")
    if not exe:
        raise ValueError("fpcalc (Chromaprint) is not installed. Install it with:  brew install chromaprint")
    ua = online.user_agent(conn)
    rows = conn.execute("SELECT id, path, fingerprint, acoustid_rec FROM tracks WHERE missing=0 AND readable=1"
                        ).fetchall()
    for i, r in enumerate(rows):
        if cancel and cancel():
            return None
        if progress:
            progress(i, len(rows), f"Fingerprinting {os.path.basename(r['path'])}")
        if r["acoustid_rec"]:
            continue
        fp = r["fingerprint"]
        if not fp:
            try:
                out = subprocess.run([exe, "-json", r["path"]], capture_output=True, text=True, timeout=120)
                j = json.loads(out.stdout)
                fp = f"{j['duration']}|{j['fingerprint']}"
            except Exception:
                continue
            conn.execute("UPDATE tracks SET fingerprint=? WHERE id=?", (fp, r["id"]))
        dur, fingerprint = fp.split("|", 1)
        rec = online.acoustid_lookup(key, float(dur), fingerprint, ua)
        conn.execute("UPDATE tracks SET acoustid_rec=? WHERE id=?", (rec, r["id"]))
        conn.commit()
    groups = defaultdict(list)
    for r in conn.execute("SELECT id, acoustid_rec FROM tracks WHERE missing=0 AND acoustid_rec IS NOT NULL "
                          "AND acoustid_rec<>'-'"):
        groups[r["acoustid_rec"]].append(r["id"])
    return _save(conn, "acoustid", {k: v for k, v in groups.items() if len(v) > 1})


def load_groups(conn):
    out = []
    for g in conn.execute("SELECT * FROM dup_groups ORDER BY method, id"):
        members = [dict(r) for r in conn.execute(
            "SELECT t.*, d.keep FROM dup_members d JOIN tracks t ON t.id=d.track_id "
            "WHERE d.group_id=? AND t.missing=0", (g["id"],))]
        if len(members) > 1:
            out.append(dict(id=g["id"], method=g["method"], key=g["key"], members=members))
    return out


LOSSLESS = {"FLAC", "ALAC", "WAV", "AIFF", "APE"}


def best_copy(members):
    """Highest quality copy: lossless first, then bitrate, sample rate, has artwork."""
    return max(members, key=lambda m: (m["format"] in LOSSLESS, m["bitrate"] or 0, m["sample_rate"] or 0,
                                       m["has_art"], -len(m["path"])))["id"]
