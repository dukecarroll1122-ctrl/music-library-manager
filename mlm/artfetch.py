"""Artwork jobs: which albums/folders lack artwork, the candidates found for each, review decisions, and applying
a chosen cover (embed / folder.jpg / cover.jpg / save separately) with provenance.

Groups:
* album  — songs sharing an album tag (and album artist)
* folder — songs with no album tag, grouped by folder; artist/album are guessed from folder names
           *only to search for a cover* (tags are never changed here)

Search results come from mlm.artsearch (all providers, fallback strategies, confidence levels). Nothing is written
to files until the user approves a cover and applies it; embedding goes through repair.apply_changes, so it needs
Edit Mode and is backed up, logged and undoable.
"""
import hashlib
import json
import os
import re
import shutil
import time
import uuid
from dataclasses import dataclass

import mutagen

from . import artmatch as am
from . import artproviders as ap
from . import artsearch, db, online, repair, tags

PENDING, APPROVED, REJECTED, APPLIED, NONE, FAILED = "pending", "approved", "rejected", "applied", "none", "failed"
DECIDED = (APPROVED, REJECTED, APPLIED)

SCHEMA = """
CREATE TABLE IF NOT EXISTS art_jobs(id INTEGER PRIMARY KEY, key TEXT UNIQUE, kind TEXT, label TEXT, artist TEXT,
  album TEXT, track_ids TEXT, confidence TEXT, note TEXT, status TEXT, chosen INTEGER DEFAULT 0, searched REAL,
  applied REAL, error TEXT);
CREATE TABLE IF NOT EXISTS art_cands(job_id INTEGER, idx INTEGER, source TEXT, rg TEXT, release TEXT, title TEXT,
  artist TEXT, date TEXT, country TEXT, rtype TEXT, score REAL, thumb BLOB, full_url TEXT, src_track INTEGER);
"""
JOB_COLS = {"mode": "TEXT", "want": "TEXT", "strategies": "TEXT", "provider_status": "TEXT"}
CAND_COLS = {"width": "INTEGER", "height": "INTEGER", "edition": "TEXT", "kind": "TEXT", "confidence": "TEXT",
             "catalog": "TEXT", "reasons": "TEXT", "extra": "TEXT", "rejected": "INTEGER DEFAULT 0", "names": "TEXT",
             "tracks": "INTEGER", "strategy": "TEXT"}


def ensure(conn):
    """Create the tables and add any missing columns. Existing rows are kept (additive migration)."""
    conn.executescript(SCHEMA)
    for table, cols in (("art_jobs", JOB_COLS), ("art_cands", CAND_COLS)):
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for col, typ in cols.items():
            if col not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
    conn.commit()


# ---------------------------------------------------------------- settings
def target_resolution(conn):
    v = db.get_setting(conn, "art_resolution", "0")
    return int(v) if v.isdigit() else 0


def embed_max_side(conn):
    v = db.get_setting(conn, "art_max_side", "3000")
    return int(v) if v.isdigit() else 3000


def search_mode(conn):
    return "priority" if db.get_setting(conn, "art_search_mode", "all") == "priority" else "all"


# ---------------------------------------------------------------- grouping
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


def game_name(album):
    """'Persona 5 (Original Soundtrack)' -> 'Persona 5'; 'Sonic Adventure (Original Soundtrack), Vol. 1' ->
    'Sonic Adventure'. The soundtrack's own title is kept separately; the game's name is only a search hint."""
    g = am.strip_ost(am.split_edition(album or "")[0])
    g = re.sub(r"[,\s]*(?:vol(?:ume)?\.?|disc|cd)\s*\d+\s*$", "", g, flags=re.I)
    return re.sub(r"[\s,:\-–]+$", "", g).strip()


def detect_mode(album, genre, path):
    if am.OST_RE.search(album or "") or am.OST_RE.search(genre or ""):
        return "game"
    return "game" if re.search(r"\bgames?\b|video ?game|\bost\b|soundtrack", f"{genre} {path}", re.I) else "music"


def _group_dict(key, kind, artist, album, members):
    missing = [t for t in members if not t["has_art"]]
    with_art = [t for t in members if t["has_art"]]
    sample = members[0]
    years = sorted(y[:4] for y in (t["year"] for t in members) if y and y[:4].isdigit())
    genre = next((t["genre"] for t in members if t["genre"]), "")
    return dict(
        key=key, kind=kind, artist=artist, album=album, total=len(members), track_ids=[t["id"] for t in missing],
        all_ids=[t["id"] for t in members],
        sibling=with_art[0]["id"] if with_art and len({t["art_hash"] for t in with_art}) == 1 else None,
        year=years[0] if years else "", genre=genre, sample=sample["path"],
        mode=detect_mode(album, genre, sample["path"]),
        label=(f"{album} — {artist or 'Unknown artist'}" if kind == "album" else
               f"Folder: {os.path.relpath(os.path.dirname(sample['path']), sample['root'] or '/')}"))


def plan(conn, only_missing=True):
    """Groups of songs (albums, or folders for untagged songs). only_missing: just those lacking artwork."""
    rows = [dict(r) for r in conn.execute("SELECT * FROM tracks WHERE missing=0 AND readable=1")]
    groups = {}
    for t in rows:
        if t["album"].strip():
            key = f"album|{_aa(t).casefold()}|{t['album'].strip().casefold()}"
            groups.setdefault(key, ("album", _aa(t), t["album"].strip(), []))[3].append(t)
        else:
            folder = os.path.dirname(t["path"])
            artist, album = guess_from_folder(folder)
            groups.setdefault(f"folder|{folder}", ("folder", artist, album, []))[3].append(t)
    out = []
    for key, (kind, artist, album, members) in groups.items():
        if only_missing and all(t["has_art"] for t in members):
            continue
        out.append(_group_dict(key, kind, artist, album, members))
    return sorted(out, key=lambda g: (g["kind"] != "album", g["label"].casefold()))


def group_for_album(conn, artist, album):
    """One album (all its songs) as a group, for searching from the album browser."""
    members = [dict(r) for r in conn.execute(
        "SELECT * FROM tracks WHERE missing=0 AND readable=1 AND lower(album)=? AND "
        "lower(coalesce(nullif(albumartist,''),artist))=? ORDER BY path", ((album or "").lower(), (artist or "").lower()))]
    if not members:
        return None
    return _group_dict(f"album|{(artist or '').casefold()}|{album.strip().casefold()}", "album", artist, album, members)


def want_for(conn, g, overrides=None):
    """What the user's files say about this album, in the form artmatch/providers expect."""
    extra = dict(mb_release_id="", mb_rg_id="", catalog="", label="", composer="")
    try:
        if g.get("sample") and os.path.isfile(g["sample"]):
            f = mutagen.File(g["sample"])
            extra = tags.read_extra(f) if f else extra
    except Exception:
        pass
    want = dict(artist=g["artist"], albumartist="", album=g["album"], year=g.get("year", ""), n_tracks=g["total"],
                mb_release_id=extra["mb_release_id"], mb_rg_id=extra["mb_rg_id"], catalog=extra["catalog"],
                publisher=extra["label"], composer=extra["composer"], mode=g.get("mode", "music"),
                game=game_name(g["album"]) if g.get("mode") == "game" else "", soundtrack_title="", region="",
                edition_kind=am.edition_of(g["album"])[0])
    want.update({k: v for k, v in (overrides or {}).items() if v is not None})
    if want["mode"] == "game" and not want.get("game"):
        want["game"] = game_name(want["album"])
    want["edition_kind"] = am.edition_of(want["album"])[0] if not (overrides or {}).get("edition_kind") \
        else overrides["edition_kind"]
    return want


# ---------------------------------------------------------------- results storage
def _library_cand(conn, g):
    r = conn.execute("SELECT t.art_w, t.art_h, th.data FROM tracks t LEFT JOIN thumbs th ON th.art_hash=t.art_hash "
                     "WHERE t.id=?", (g["sibling"],)).fetchone()
    return dict(provider="library", title="Cover already embedded in other songs of this album", artist="", names=[],
                year="", tracks=0, kind="original", edition="Your own files", rtype="", country="", catalog="",
                confidence=am.STRONG, score=1.0, reasons=["Other songs in this album/folder already use this cover"],
                thumb=r["data"] if r else None, width=r["art_w"] if r else None, height=r["art_h"] if r else None,
                src_track=g["sibling"], strategy="your files")


EXTRA_KEYS = ("art_base", "cover_xl", "cover_big", "art_scope", "art_scope_found")


def store_results(conn, g, want, result, reset=True):
    """Save a search result as the job's candidates. Returns the job id."""
    ensure(conn)
    cands = list(result["cands"])
    if g.get("sibling") and not any(c["provider"] == "library" for c in cands):
        cands.insert(0, _library_cand(conn, g))
    best = cands[0] if cands else None
    status = NONE if not cands else PENDING
    if best:
        note = (f"{best['provider'].title()}: “{best['title']}”" + (f" by {best['artist']}" if best["artist"] else "")
                + (f", {best['year']}" if best.get("year") else ""))
        if best.get("reasons"):
            note += " — " + "; ".join(best["reasons"][:2])
    else:
        tried = ", ".join(f"{v['label']}: {v['state']}" for v in result["status"].values())
        note = f"No cover found after {len(result['strategies'])} search strategies ({tried})"
    job = conn.execute("SELECT * FROM art_jobs WHERE key=?", (g["key"],)).fetchone()
    want_json = json.dumps(dict(want, _all_ids=g["all_ids"]))
    vals = [g["kind"], g["label"], g["artist"], g["album"], json.dumps(g["track_ids"]),
            best["confidence"] if best else "", note, status if (reset or not job) else job["status"], time.time(),
            g["mode"], want_json, json.dumps(result["strategies"]), json.dumps(result["status"])]
    if job:
        jid = job["id"]
        conn.execute("UPDATE art_jobs SET kind=?, label=?, artist=?, album=?, track_ids=?, confidence=?, note=?, "
                     "status=?, searched=?, mode=?, want=?, strategies=?, provider_status=?, chosen=0, error=NULL "
                     "WHERE id=?", vals + [jid])
        conn.execute("DELETE FROM art_cands WHERE job_id=?", (jid,))
    else:
        jid = conn.execute("INSERT INTO art_jobs(kind, label, artist, album, track_ids, confidence, note, status, "
                           "searched, mode, want, strategies, provider_status, key) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                           vals + [g["key"]]).lastrowid
    for idx, c in enumerate(cands):
        extra = {k: c[k] for k in EXTRA_KEYS if c.get(k)}
        conn.execute(
            "INSERT INTO art_cands(job_id, idx, source, rg, release, title, artist, date, country, rtype, score, thumb, "
            "full_url, src_track, width, height, edition, kind, confidence, catalog, reasons, extra, rejected, names, "
            "tracks, strategy) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?,?,?)",
            (jid, idx, c["provider"], c.get("rg_id"), c.get("release_id"), c["title"], c["artist"], c.get("year", ""),
             c.get("country", ""), c.get("rtype", ""), c.get("score", 0), c.get("thumb"), c.get("full_url"),
             c.get("src_track"), c.get("width"), c.get("height"), c.get("edition", ""), c.get("kind", "original"),
             c["confidence"], c.get("catalog", ""), json.dumps(c.get("reasons", [])), json.dumps(extra),
             json.dumps(c.get("names", [])), c.get("tracks", 0), c.get("strategy", "")))
    conn.commit()
    return jid


def search(conn, progress=None, cancel=None, redo=False, retry_none=False, mode=None, max_strategies=6, groups=None,
           providers=None):
    """Search every group missing artwork. Resumable: groups already searched are kept unless redo; groups with
    no result are searched again (network cache bypassed) when retry_none. Decisions are never overwritten."""
    ensure(conn)
    groups = groups if groups is not None else plan(conn)
    mode = mode or search_mode(conn)
    target = target_resolution(conn)
    stats = dict(groups=len(groups), searched=0, found=0, none=0, skipped=0, providers={})
    for i, g in enumerate(groups):
        if cancel and cancel():
            break
        job = conn.execute("SELECT * FROM art_jobs WHERE key=?", (g["key"],)).fetchone()
        if job and (job["status"] in DECIDED or (job["searched"] and not redo and not (
                retry_none and job["status"] == NONE))):
            conn.execute("UPDATE art_jobs SET track_ids=? WHERE id=?", (json.dumps(g["track_ids"]), job["id"]))
            stats["skipped"] += 1
            continue
        if progress:
            progress(i, len(groups), g["label"])
        want = want_for(conn, g)
        ctx = ap.Ctx.from_conn(conn, fresh=bool(job and job["status"] == NONE and retry_none) or redo)
        res = artsearch.search_album(ctx, want, providers, mode, cancel=cancel, max_strategies=max_strategies,
                                     target=target)
        store_results(conn, g, want, res, reset=True)
        stats["searched"] += 1
        stats["found" if res["cands"] else "none"] += 1
        for name, st in res["status"].items():
            stats["providers"].setdefault(name, {}).setdefault(st["state"], 0)
            stats["providers"][name][st["state"]] += 1
    conn.commit()
    return stats


# ---------------------------------------------------------------- reading jobs / decisions
def jobs(conn, status=None):
    ensure(conn)
    out = []
    for j in conn.execute("SELECT * FROM art_jobs" + (" WHERE status=?" if status else "") + " ORDER BY kind, label",
                          (status,) if status else ()):
        d = dict(j)
        d["track_ids"] = json.loads(d["track_ids"] or "[]")
        d["confidence"] = am.level(d["confidence"]) if d["confidence"] else ""
        d["provider_status"] = json.loads(d.get("provider_status") or "{}")
        d["want"] = json.loads(d.get("want") or "{}")
        d["cands"] = []
        for c in conn.execute("SELECT * FROM art_cands WHERE job_id=? ORDER BY idx", (j["id"],)):
            cd = dict(c)
            cd["confidence"] = am.level(cd["confidence"] or (d["confidence"] if c["idx"] == 0 else am.POSSIBLE))
            cd["reasons"] = json.loads(cd.get("reasons") or "[]")
            cd["extra"] = json.loads(cd.get("extra") or "{}")
            cd["provider"] = cd["source"]
            cd["year"] = cd["date"]
            cd["kind"] = cd.get("kind") or "original"
            d["cands"].append(cd)
        out.append(d)
    return out


def set_status(conn, job_ids, status, chosen=None):
    for jid in job_ids:
        sql, args = "UPDATE art_jobs SET status=?", [status]
        if chosen is not None:
            sql, args = sql + ", chosen=?", args + [chosen]
        conn.execute(sql + " WHERE id=? AND status IN ('pending','approved','rejected','failed','none')", args + [jid])
    conn.commit()


def reject_candidate(conn, job_id, idx, rejected=True):
    conn.execute("UPDATE art_cands SET rejected=? WHERE job_id=? AND idx=?", (int(rejected), job_id, idx))
    conn.commit()


def safe_candidates(job):
    return [c for c in job["cands"] if not c["rejected"] and c["confidence"] in am.SAFE
            and c["kind"] not in ("fan", "promo")]


def approve_safe(conn):
    """Approve the best Exact/Strong official cover of every pending job. Weak, Unverified, fan and promotional
    results are never approved this way."""
    n = 0
    for j in jobs(conn, PENDING):
        best = safe_candidates(j)
        if best:
            set_status(conn, [j["id"]], APPROVED, chosen=best[0]["idx"])
            n += 1
    return n


# ---------------------------------------------------------------- images
def candidate_image(conn, cand, target=None, ctx=None):
    """Download (cached) the full-size image for a stored candidate. Returns bytes."""
    ctx = ctx or ap.Ctx.from_conn(conn)
    target = target_resolution(conn) if target is None else target
    if cand["provider"] == "library":
        r = conn.execute("SELECT path FROM tracks WHERE id=?", (cand["src_track"],)).fetchone()
        art = tags.get_art(mutagen.File(r["path"])) if r and os.path.isfile(r["path"]) else None
        if not art:
            raise ValueError("The cover in your other songs is no longer available")
        return art[0]
    p = ap.PROVIDERS[cand["provider"]]
    c = dict(cand, **cand.get("extra", {}), release_id=cand.get("release") or cand.get("release_id"),
             rg_id=cand.get("rg") or cand.get("rg_id"))
    last = None
    for u in dict.fromkeys(x for x in (p.image_url(c, target), cand.get("full_url")) if x):
        try:
            return artsearch.fetch_image(ctx, u, p.headers(ctx) if p.needs else None)
        except (online.NetworkError, artsearch.ImageError) as e:
            last = e
    raise last or online.NetworkError("Image not available")


def import_file(path):
    """Read a user-chosen image file. Returns (bytes, info). Raises ImageError."""
    try:
        if os.path.getsize(path) > artsearch.MAX_IMAGE_BYTES:
            raise artsearch.ImageError("The file is larger than 40 MB")
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError as e:
        raise artsearch.ImageError(f"Cannot read the file: {e}")
    return data, artsearch.inspect_image(data)


def import_url(conn, url):
    """Download an image from a web address (http/https only). Returns (bytes, info)."""
    if not re.match(r"https?://\S+$", (url or "").strip(), re.I):
        raise artsearch.ImageError("Enter a web address starting with http:// or https://")
    try:
        data = online.http(url.strip(), online.user_agent(conn), timeout=45, max_bytes=artsearch.MAX_IMAGE_BYTES,
                           headers={"Accept": "image/*"})
    except online.NetworkError as e:
        raise artsearch.ImageError(str(e))
    if not data:
        raise artsearch.ImageError("Nothing was found at that address")
    return data, artsearch.inspect_image(data)


# ---------------------------------------------------------------- applying
@dataclass
class ApplyOptions:
    embed: bool = True            # embed in the songs' tags
    only_missing: bool = True     # embed only into songs without artwork; False replaces embedded art (backed up)
    folder_jpg: bool = False      # save folder.jpg next to the songs
    cover_jpg: bool = False       # save cover.jpg next to the songs
    replace_files: bool = False   # replace an existing folder.jpg/cover.jpg (the old file is backed up)
    save_to: str = ""             # save a copy here; does not touch the library at all
    max_side: int = 0             # 0: keep the image as downloaded; else scale down (never up) when embedding

    def writes_library(self):
        return self.embed or self.folder_jpg or self.cover_jpg


def _publish(data, dst):
    """Write bytes to dst without ever overwriting an existing file."""
    from . import convert
    tmp = os.path.join(os.path.dirname(dst), f".{os.path.basename(dst)}.{uuid.uuid4().hex[:8]}{convert.PART}")
    with open(tmp, "wb") as fh:
        fh.write(data)
    try:
        convert.publish(tmp, dst)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _provenance(conn, batch, job_id, source, info, applied_as, dest, track_ids):
    conn.execute("INSERT INTO art_provenance(ts, batch, job_id, provider, source_url, release_id, rg_id, title, artist, "
                 "confidence, w, h, bytes, applied_as, dest, track_ids) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (time.time(), batch, job_id, source.get("provider", "manual"), source.get("url", ""),
                  source.get("release_id", ""), source.get("rg_id", ""), source.get("title", ""),
                  source.get("artist", ""), source.get("confidence", am.UNVERIFIED), info["w"], info["h"],
                  info["bytes"], applied_as, dest, json.dumps(track_ids)))
    conn.commit()


def apply_image(conn, track_ids, data, opts, edit_mode, source=None, note="", job_id=None):
    """Apply one image to songs per opts. Embedding and folder files need Edit Mode; saving a separate copy
    doesn't. Returns dict(batch, embedded, files, skipped, errors)."""
    if opts.writes_library() and not edit_mode:
        raise repair.ReadOnlyError("Read-only mode is on. Switch to Edit Mode to change your music folders.")
    source = source or {}
    info = artsearch.inspect_image(data)
    res = dict(batch=repair.new_batch(), embedded=0, files=[], skipped=[], errors=[])
    rows = [dict(r) for r in conn.execute(f"SELECT id, path, has_art FROM tracks WHERE id IN "
                                          f"({','.join('?' * len(track_ids))})", list(track_ids))] if track_ids else []
    label = note or "artwork"
    if opts.embed and rows:
        targets = [r["id"] for r in rows if not (opts.only_missing and r["has_art"])]
        if targets:
            emb, mime, size = artsearch.prepare(data, opts.max_side)
            r = repair.apply_changes(conn, {t: {"artwork": (emb, mime)} for t in targets}, edit_mode,
                                     note=f"artwork: {label}")  # backs up any existing embedded artwork
            res["embedded"], res["batch"] = len(r["applied"]), r["batch"]
            res["errors"] += r["errors"]
            if r["applied"]:
                _provenance(conn, r["batch"], job_id, source, dict(info, w=size[0], h=size[1], bytes=len(emb)),
                            "embedded", "", r["applied"])
        else:
            res["skipped"].append("Every selected song already has embedded artwork")
    names = [n for n, on in (("folder.jpg", opts.folder_jpg), ("cover.jpg", opts.cover_jpg)) if on]
    if names and rows:
        jpg = artsearch.to_jpeg(data)
        sha = hashlib.sha1(jpg).hexdigest()
        bdir = db.data_dir() / "backups" / "art" / res["batch"]
        for folder in sorted({os.path.dirname(r["path"]) for r in rows}):
            first = next(r["id"] for r in rows if os.path.dirname(r["path"]) == folder)
            for name in names:
                dst, backup = os.path.join(folder, name), ""
                try:
                    if os.path.exists(dst):
                        if not opts.replace_files:
                            res["skipped"].append(f"{dst} already exists — not replaced")
                            continue
                        bdir.mkdir(parents=True, exist_ok=True)
                        backup = str(bdir / f"{hashlib.sha1(dst.encode()).hexdigest()[:10]}-{name}")
                        shutil.copy2(dst, backup)
                        os.remove(dst)  # only after the backup exists; the new file is published without overwrite
                    _publish(jpg, dst)
                    repair._hist(conn, res["batch"], first, dst, "folder_art", "", sha, backup, f"artwork: {label}")
                    _provenance(conn, res["batch"], job_id, source, dict(info, bytes=len(jpg)), name, dst, [first])
                    res["files"].append(dst)
                except Exception as e:
                    res["errors"].append(f"{dst}: {e}")
        conn.commit()
    if opts.save_to:
        try:
            os.makedirs(opts.save_to, exist_ok=True)
            base = re.sub(r'[/\\:*?"<>|]', "_", f"{source.get('artist', '')} - {source.get('title', '') or label}".strip(" -"))
            ext = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}[info["mime"]]
            dst, n = os.path.join(opts.save_to, base + ext), 2
            while os.path.exists(dst):
                dst, n = os.path.join(opts.save_to, f"{base} ({n}){ext}"), n + 1
            _publish(data, dst)  # the original bytes, untouched
            res["files"].append(dst)
            _provenance(conn, res["batch"], job_id, source, info, "separate copy", dst, [])
        except OSError as e:
            res["errors"].append(f"Could not save a copy: {e}")
    return res


def source_of(cand, url=""):
    return dict(provider=cand["provider"], url=url or cand.get("full_url") or "", release_id=cand.get("release") or
                cand.get("release_id") or "", rg_id=cand.get("rg") or cand.get("rg_id") or "", title=cand["title"],
                artist=cand["artist"], confidence=cand["confidence"])


def apply(conn, job_ids, edit_mode, opts=None, progress=None, cancel=None, only_ids=None):
    """Apply the chosen cover of each approved job. By default embedding only touches songs that still have no
    artwork; opts.only_missing=False replaces embedded artwork (backed up) in all of the album's songs."""
    opts = opts or ApplyOptions(max_side=embed_max_side(conn))
    if opts.writes_library() and not edit_mode:
        raise repair.ReadOnlyError("Read-only mode is on. Switch to Edit Mode to add artwork to files.")
    ensure(conn)
    ctx = ap.Ctx.from_conn(conn)
    todo = [j for j in jobs(conn, APPROVED) if j["id"] in set(job_ids)]
    done = dict(albums=0, songs=0, files=0, errors=[], skipped=[])
    for i, j in enumerate(todo):
        if cancel and cancel():
            break
        if progress:
            progress(i, len(todo), j["label"])
        try:
            cand = next(c for c in j["cands"] if c["idx"] == j["chosen"])
            ids = j["want"].get("_all_ids", j["track_ids"]) if (not opts.only_missing or opts.folder_jpg
                                                                or opts.cover_jpg) else j["track_ids"]
            ids = [i for i in ids if only_ids is None or i in only_ids]
            if not ids:
                raise ValueError("No songs to apply to")
            res = apply_image(conn, ids, candidate_image(conn, cand, ctx=ctx), opts, edit_mode, source_of(cand),
                              j["label"], j["id"])
            done["songs"] += res["embedded"]
            done["files"] += len(res["files"])
            done["errors"] += res["errors"]
            done["skipped"] += res["skipped"]
            ok = bool(res["embedded"] or res["files"])
            conn.execute("UPDATE art_jobs SET status=?, applied=?, error=? WHERE id=?",
                         (APPLIED if ok else FAILED, time.time(), "; ".join(res["errors"] + res["skipped"])[:500] or None,
                          j["id"]))
            done["albums"] += ok
        except (online.NetworkError, ValueError, StopIteration, artsearch.ImageError) as e:
            done["errors"].append(f"{j['label']}: {e}")
            conn.execute("UPDATE art_jobs SET status=?, error=? WHERE id=?", (FAILED, str(e)[:500], j["id"]))
        conn.commit()
    return done
