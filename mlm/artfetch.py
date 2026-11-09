"""Bulk artwork: find covers for every song without artwork, let the user review, then embed approved ones.

Groups:
* album  — songs sharing an album tag (and album artist) that lack artwork
* folder — songs with no album tag, grouped by folder; artist/album are guessed from the folder names
           *only to search for a cover* (tags are never changed here)

Candidates come from the user's own files (another song in the same album/folder that already has a cover)
or from the Cover Art Archive via MusicBrainz release search. Applying only adds artwork to songs that still
have none, through repair.apply_changes (edit mode, backups, history, undo).
"""
import json
import os
import re
import time

import mutagen

from . import online, repair, tags

PENDING, APPROVED, REJECTED, APPLIED, NONE, FAILED = "pending", "approved", "rejected", "applied", "none", "failed"
HIGH, POSSIBLE = "High", "Possible"
MAX_CANDS = 4

SCHEMA = """
CREATE TABLE IF NOT EXISTS art_jobs(id INTEGER PRIMARY KEY, key TEXT UNIQUE, kind TEXT, label TEXT, artist TEXT,
  album TEXT, track_ids TEXT, confidence TEXT, note TEXT, status TEXT, chosen INTEGER DEFAULT 0, searched REAL,
  applied REAL, error TEXT);
CREATE TABLE IF NOT EXISTS art_cands(job_id INTEGER, idx INTEGER, source TEXT, rg TEXT, release TEXT, title TEXT,
  artist TEXT, date TEXT, country TEXT, rtype TEXT, score REAL, thumb BLOB, full_url TEXT, src_track INTEGER);
"""


def ensure(conn):
    conn.executescript(SCHEMA)


def _aa(t):
    return (t["albumartist"] or t["artist"] or "").strip()


def guess_from_folder(folder):
    """'…/MF DOOM/The Mouse & the Mask' -> ('MF DOOM', 'The Mouse & the Mask'); strips '1988 - ' prefixes."""
    album = re.sub(r"^\s*(19|20)\d\d\s*[-–.]\s*", "", os.path.basename(folder)).strip()
    if re.fullmatch(r"(various( artists)?|unknown( album| artist)?|music|misc|downloads?|new folder|untitled( folder)?"
                    r"|singles?|tracks?)", album, re.I):
        album = ""  # a generic folder name says nothing about which album this is
    parent = os.path.basename(os.path.dirname(folder)).strip()
    artist = "" if not parent or re.search(r"[:/]|soundtrack|genre|music$|^various", parent, re.I) else parent
    return artist, album


def plan(conn):
    """Groups of songs without artwork, each with an optional cover found in the user's own files."""
    rows = [dict(r) for r in conn.execute("SELECT * FROM tracks WHERE missing=0 AND readable=1")]
    groups = {}
    for t in rows:
        if t["album"].strip():
            key = f"album|{_aa(t).casefold()}|{t['album'].strip().casefold()}"
            g = groups.setdefault(key, dict(key=key, kind="album", artist=_aa(t), album=t["album"].strip(),
                                            members=[]))
        else:
            folder = os.path.dirname(t["path"])
            key = f"folder|{folder}"
            artist, album = guess_from_folder(folder)
            g = groups.setdefault(key, dict(key=key, kind="folder", artist=artist, album=album, members=[]))
        g["members"].append(t)
    out = []
    for g in groups.values():
        missing = [t for t in g["members"] if not t["has_art"]]
        if not missing:
            continue
        with_art = [t for t in g["members"] if t["has_art"]]
        hashes = {t["art_hash"] for t in with_art}
        g["sibling"] = with_art[0]["id"] if len(hashes) == 1 else None  # only if the others agree on one cover
        g["track_ids"] = [t["id"] for t in missing]
        g["total"] = len(g["members"])
        g["label"] = (f"{g['album']} — {g['artist'] or 'Unknown artist'}" if g["kind"] == "album" else
                      f"Folder: {os.path.relpath(os.path.dirname(missing[0]['path']), missing[0]['root'] or '/')}")
        del g["members"]
        out.append(g)
    return sorted(out, key=lambda g: (g["kind"] != "album", g["label"].casefold()))


def _type(rg):
    t = rg.get("primary-type") or ""
    sec = rg.get("secondary-types") or []
    return " + ".join([t] + sec) if t else ", ".join(sec)


def artist_ok(local, found, score):
    """Similar album titles aren't enough: the artist must match too (unless it's a various-artists album)."""
    if not local or re.search(r"various|soundtrack|ost|original", local, re.I):
        return True
    if score >= 0.5:
        return True
    from .verify import artists_of
    return bool(artists_of(local) & artists_of(found))


def credit_names(ac):
    """Credited names plus MusicBrainz sort names, both orders: '山下達郎' + 'Yamashita, Tatsuro' + 'Tatsuro Yamashita'."""
    names = [online.credit(ac)]
    for x in ac or []:
        sort = (x.get("artist") or {}).get("sort-name") or ""
        if sort:
            names.append(sort)
            if ", " in sort:
                last, first = sort.split(", ", 1)
                names.append(f"{first} {last}")
    return [n for n in names if n]


def find_online(conn, artist, album, n_songs, ua):
    """Cover Art Archive candidates for an album: [(score, cand dict)] best first."""
    releases = online.search_releases(conn, album, artist, n_songs, ua)
    by_artist = bool(artist)  # MusicBrainz matched the artist itself, including aliases (山下達郎 = Tatsuro Yamashita)
    if not releases and artist:
        releases = online.search_releases(conn, album, "", n_songs, ua)
        by_artist = False
    cands, seen = [], set()
    for r in releases[:15]:
        rg = r.get("release-group") or {}
        if not rg.get("id") or rg["id"] in seen:
            continue
        seen.add(rg["id"])
        al = online.album_sim(album, r.get("title"))
        if al < 0.5:
            continue
        names = credit_names(r.get("artist-credit"))
        a = max((online.sim(artist, n) for n in names), default=0.0) if artist else 0.6
        if not by_artist and not any(artist_ok(artist, n, a) for n in names):
            continue
        if by_artist:
            a = max(a, 0.85)
        n = r.get("track-count") or 0
        closeness = 1 - abs(n - n_songs) / max(n, n_songs, 1) if n else 0.5
        score = round(0.6 * al + 0.25 * a + 0.15 * closeness, 3)
        cands.append((score, dict(source="caa", rg=rg["id"], release=r.get("id"), title=r.get("title", ""),
                                  artist=online.credit(r.get("artist-credit")), date=r.get("date", ""),
                                  country=r.get("country", ""), rtype=_type(rg), al=al, a=a,
                                  thumb_url=f"{online.CAA}/release-group/{rg['id']}/front-250",
                                  full_url=f"{online.CAA}/release-group/{rg['id']}/front")))
    cands.sort(key=lambda x: -x[0])
    found = []
    for score, c in cands:
        if len(found) >= MAX_CANDS:
            break
        try:
            thumb = online.http(c["thumb_url"], ua)  # None when the release group has no front cover
        except online.NetworkError:
            thumb = None  # one unreachable image shouldn't stop the whole run
        if thumb:
            c["thumb"] = thumb
            found.append((score, c))
    return found


ITUNES = "https://itunes.apple.com/search"


def find_itunes(conn, artist, album, n_songs, ua, countries=("US", "JP")):
    """Fallback source: Apple's free iTunes Search API (no key). [(score, cand dict)] best first."""
    import urllib.parse
    base = online._base(album) or album
    term = f"{artist} {base}".strip() if artist else base
    found = []
    for country in countries:
        data = online.cached_json(conn, f"{ITUNES}?{urllib.parse.urlencode(dict(term=term, entity='album', limit=10, country=country))}", ua)
        for r in data.get("results", []):
            art = r.get("artworkUrl100") or ""
            if not art:
                continue
            al = online.album_sim(album, r.get("collectionName"))
            if al < 0.6:
                continue
            a = online.sim(artist, r.get("artistName")) if artist else 0.6
            if not artist_ok(artist, r.get("artistName"), a):
                continue  # e.g. "Born This Way" (Lady Gaga) must not match "Born Like This" (MF DOOM)
            n = r.get("trackCount") or 0
            closeness = 1 - abs(n - n_songs) / max(n, n_songs, 1) if n else 0.5
            found.append((round(0.6 * al + 0.25 * a + 0.15 * closeness, 3),
                          dict(source="itunes", rg=None, release=str(r.get("collectionId", "")),
                               title=r.get("collectionName", ""), artist=r.get("artistName", ""),
                               date=(r.get("releaseDate") or "")[:10], country=country,
                               rtype="iTunes " + ("Single" if " - Single" in r.get("collectionName", "") else "Album"),
                               al=al, a=a, thumb_url=art.replace("100x100bb", "300x300bb"),
                               full_url=art.replace("100x100bb", "1400x1400bb"))))
        if found:
            break
    found.sort(key=lambda x: -x[0])
    out, seen = [], set()
    for score, c in found:
        if c["release"] in seen or len(out) >= MAX_CANDS:
            continue
        seen.add(c["release"])
        try:
            c["thumb"] = online.http(c["thumb_url"], ua)
        except online.NetworkError:
            c["thumb"] = None
        if c["thumb"]:
            out.append((score, c))
    return out


def _sibling_thumb(conn, track_id):
    r = conn.execute("SELECT t.art_hash, th.data FROM tracks t LEFT JOIN thumbs th ON th.art_hash=t.art_hash "
                     "WHERE t.id=?", (track_id,)).fetchone()
    return r["data"] if r else None


def search(conn, progress=None, cancel=None, redo=False):
    """Find candidates for every group (resumable: groups already searched are skipped unless redo)."""
    ensure(conn)
    ua = online.user_agent(conn)
    groups = plan(conn)
    stats = dict(groups=len(groups), searched=0, found=0, none=0, skipped=0, error=None)
    for i, g in enumerate(groups):
        if cancel and cancel():
            break
        job = conn.execute("SELECT * FROM art_jobs WHERE key=?", (g["key"],)).fetchone()
        if job and job["status"] in (APPROVED, REJECTED):
            # keep the user's decision; just refresh which songs still need art
            conn.execute("UPDATE art_jobs SET track_ids=? WHERE id=?", (json.dumps(g["track_ids"]), job["id"]))
            stats["skipped"] += 1
            continue
        if job and job["searched"] and job["status"] != APPLIED and not redo:
            conn.execute("UPDATE art_jobs SET track_ids=? WHERE id=?", (json.dumps(g["track_ids"]), job["id"]))
            stats["skipped"] += 1
            continue
        if progress:
            progress(i, len(groups), g["label"])
        cands, note = [], ""
        if g["sibling"]:
            cands.append((1.0, dict(source="library", title="Cover already in your other songs", artist="",
                                    date="", country="", rtype="", thumb=_sibling_thumb(conn, g["sibling"]),
                                    src_track=g["sibling"], al=1, a=1)))
        try:
            if g["album"]:
                caa = find_online(conn, g["artist"], g["album"], g["total"], ua)
                cands += caa
                if not caa or caa[0][1]["al"] < 0.9:  # iTunes only when the archive has no good match
                    cands += find_itunes(conn, g["artist"], g["album"], g["total"], ua)[:MAX_CANDS - len(caa)]
        except online.NetworkError as e:
            stats["error"] = str(e)
            break
        if cands and cands[0][1]["source"] != "library":
            cands.sort(key=lambda x: -x[0])
        best = cands[0][1] if cands else None
        if not best:
            status, conf, note = NONE, "", "No cover found on the Cover Art Archive or iTunes for this title."
        elif best["source"] == "library":
            status, conf, note = PENDING, HIGH, "Same cover as your other songs in this album/folder."
        else:
            high = best["al"] >= 0.9 and (best["a"] >= 0.85 or not g["artist"]) and g["kind"] == "album"
            src = "Cover Art Archive" if best["source"] == "caa" else "iTunes"
            status, conf = PENDING, HIGH if high else POSSIBLE
            note = (f"{src}: {best['rtype'] or 'Release'} “{best['title']}”"
                    + (f" by {best['artist']}" if best["artist"] else "") + (f", {best['date'][:4]}" if best["date"]
                                                                            else ""))
            if g["kind"] == "folder":
                note += " — searched by folder name because these songs have no album tag."
        if job:
            jid = job["id"]
            conn.execute("UPDATE art_jobs SET track_ids=?, label=?, artist=?, album=?, confidence=?, note=?, status=?, "
                         "chosen=0, searched=?, error=NULL WHERE id=?",
                         (json.dumps(g["track_ids"]), g["label"], g["artist"], g["album"], conf, note, status,
                          time.time(), jid))
            conn.execute("DELETE FROM art_cands WHERE job_id=?", (jid,))
        else:
            jid = conn.execute("INSERT INTO art_jobs(key, kind, label, artist, album, track_ids, confidence, note, "
                               "status, searched) VALUES(?,?,?,?,?,?,?,?,?,?)",
                               (g["key"], g["kind"], g["label"], g["artist"], g["album"], json.dumps(g["track_ids"]),
                                conf, note, status, time.time())).lastrowid
        for idx, (score, c) in enumerate(cands):
            conn.execute("INSERT INTO art_cands VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (jid, idx, c["source"], c.get("rg"), c.get("release"), c["title"], c["artist"], c["date"],
                          c["country"], c["rtype"], score, c.get("thumb"), c.get("full_url"), c.get("src_track")))
        conn.commit()
        stats["searched"] += 1
        stats["found" if cands else "none"] += 1
    conn.commit()
    return stats


def jobs(conn, status=None):
    ensure(conn)
    q = "SELECT * FROM art_jobs" + (" WHERE status=?" if status else "") + " ORDER BY kind, label"
    out = []
    for j in conn.execute(q, (status,) if status else ()):
        d = dict(j)
        d["track_ids"] = json.loads(d["track_ids"] or "[]")
        d["cands"] = [dict(c) for c in conn.execute("SELECT * FROM art_cands WHERE job_id=? ORDER BY idx", (j["id"],))]
        out.append(d)
    return out


def set_status(conn, job_ids, status, chosen=None):
    for jid in job_ids:
        if chosen is None:
            conn.execute("UPDATE art_jobs SET status=? WHERE id=? AND status IN ('pending','approved','rejected',"
                         "'failed')", (status, jid))
        else:
            conn.execute("UPDATE art_jobs SET status=?, chosen=? WHERE id=? AND status IN "
                         "('pending','approved','rejected','failed')", (status, chosen, jid))
    conn.commit()


def approve_high(conn):
    n = conn.execute("UPDATE art_jobs SET status='approved' WHERE status='pending' AND confidence='High'").rowcount
    conn.commit()
    return n


def _image_for(conn, cand):
    if cand["source"] == "library":
        r = conn.execute("SELECT path FROM tracks WHERE id=?", (cand["src_track"],)).fetchone()
        art = tags.get_art(mutagen.File(r["path"])) if r and os.path.isfile(r["path"]) else None
        if not art:
            raise ValueError("The source song's cover is no longer available")
        return online.prepare_image(art[0])
    return online.download_image(conn, cand["full_url"])


def apply(conn, job_ids, edit_mode, progress=None, cancel=None):
    """Embed the chosen cover for approved jobs, only into songs that still have no artwork."""
    if not edit_mode:
        raise repair.ReadOnlyError("Read-only mode is on. Switch to Edit Mode to add artwork to files.")
    ensure(conn)
    todo = [j for j in jobs(conn, APPROVED) if j["id"] in set(job_ids)]
    done = dict(albums=0, songs=0, errors=[])
    for i, j in enumerate(todo):
        if cancel and cancel():
            break
        if progress:
            progress(i, len(todo), j["label"])
        ids = j["track_ids"]
        still = [r[0] for r in conn.execute(f"SELECT id FROM tracks WHERE has_art=0 AND id IN "
                                            f"({','.join('?' * len(ids))})", ids)] if ids else []
        try:
            cand = next(c for c in j["cands"] if c["idx"] == j["chosen"])
            if not still:
                raise ValueError("All songs already have artwork now")
            data, mime, size = _image_for(conn, cand)
            res = repair.apply_changes(conn, {t: {"artwork": (data, mime)} for t in still}, edit_mode,
                                       note=f"bulk artwork: {j['label']}")
            done["songs"] += len(res["applied"])
            done["errors"] += res["errors"]
            ok = bool(res["applied"])
            conn.execute("UPDATE art_jobs SET status=?, applied=?, error=? WHERE id=?",
                         (APPLIED if ok else FAILED, time.time(), "; ".join(res["errors"])[:500] or None, j["id"]))
            done["albums"] += ok
        except (online.NetworkError, ValueError, StopIteration) as e:
            done["errors"].append(f"{j['label']}: {e}")
            conn.execute("UPDATE art_jobs SET status=?, error=? WHERE id=?", (FAILED, str(e)[:500], j["id"]))
        conn.commit()
    return done
