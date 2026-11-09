"""Network: rate-limited HTTP, MusicBrainz matching, Cover Art Archive search, AcoustID lookup.

A MusicBrainz result is evidence, not truth: matches are graded and every proposed change is
left 'pending' for the user to approve.
"""
import difflib
import io
import json
import os
import re
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import db

MB = "https://musicbrainz.org/ws/2"
CAA = "https://coverartarchive.org"
ACOUSTID = "https://api.acoustid.org/v2/lookup"
DEFAULT_UA = "MusicLibraryManager/1.0 ( local macOS desktop app )"
CACHE_DAYS = 30
# Minimum seconds between requests per host (MusicBrainz allows ~1/s; AcoustID 3/s)
INTERVAL = {"musicbrainz.org": 1.1, "api.acoustid.org": 0.35, "coverartarchive.org": 0.25}

VERIFIED, HIGH, POSSIBLE, REVIEW, NONE = ("Verified", "High-confidence match", "Possible match",
                                          "Needs manual review", "No match found")
CONFIDENCES = (VERIFIED, HIGH, POSSIBLE, REVIEW, NONE)

_locks = {h: threading.Lock() for h in INTERVAL}
_last = {h: 0.0 for h in INTERVAL}


class NetworkError(Exception):
    pass


def _ssl_ctx():
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


_CTX = _ssl_ctx()


def user_agent(conn):
    contact = db.get_setting(conn, "mb_contact", "").strip()
    return f"MusicLibraryManager/1.0 ( {contact} )" if contact else DEFAULT_UA


def http(url, ua, data=None, timeout=25):
    host = urllib.parse.urlparse(url).hostname or ""
    lock = _locks.get(host)
    if lock:
        with lock:
            wait = _last[host] + INTERVAL[host] - time.time()
            if wait > 0:
                time.sleep(wait)
            _last[host] = time.time()
    req = urllib.request.Request(url, data=data, headers={"User-Agent": ua, "Accept": "application/json"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if e.code in (429, 503) and attempt < 2:
                time.sleep(2 * (attempt + 1))
                continue
            raise NetworkError(f"{host} returned HTTP {e.code}")
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise NetworkError(f"Cannot reach {host} — check your internet connection ({getattr(e, 'reason', e)})")


def cached_json(conn, url, ua):
    r = conn.execute("SELECT body, fetched FROM http_cache WHERE url=?", (url,)).fetchone()
    if r and time.time() - r["fetched"] < CACHE_DAYS * 86400:
        return json.loads(r["body"])
    body = http(url, ua)
    if body is None:
        return {}
    conn.execute("INSERT OR REPLACE INTO http_cache VALUES(?,?,?)", (url, body, time.time()))
    conn.commit()
    return json.loads(body)


# ---------- matching ----------

def esc(s):
    return re.sub(r'([+\-&|!(){}\[\]^"~*?:\\/])', r"\\\1", s)


def norm(s):
    s = (s or "").casefold().replace("&", " and ")
    s = re.sub(r"[^\w\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def sim(a, b):
    a, b = norm(a), norm(b)
    if not a or not b:
        return 0.0
    return 1.0 if a == b else difflib.SequenceMatcher(None, a, b).ratio()


def credit(ac):
    return "".join(c.get("name", "") + c.get("joinphrase", "") for c in ac or [])


def guess_title(path):
    stem = os.path.splitext(os.path.basename(path))[0]
    return re.sub(r"^\s*\d{1,3}\s*[-_. ]+\s*", "", stem).replace("_", " ").strip()


def score(tr, title, artist, rec):
    t = sim(title, rec.get("title"))
    a = sim(artist, credit(rec.get("artist-credit"))) if artist else 0.5
    best = None
    for rel in rec.get("releases") or []:
        s = sim(tr["album"], rel.get("title")) if tr["album"] else 0.0
        y = (rel.get("date") or "")[:4]
        pos = ((rel.get("media") or [{}])[0].get("position") or 1)
        # prefer: album match, official, the user's disc (else disc 1), earliest date — the original edition
        key = (round(s, 2), rel.get("status") == "Official", pos == (tr["disc"] or 1), -(int(y) if y.isdigit() else 9999))
        if best is None or key > best[0]:
            best = (key, rel)
    al = best[0][0] if best else 0.0
    rel = best[1] if best else {}
    d = None
    if rec.get("length") and tr["duration"]:
        d = abs(rec["length"] / 1000 - tr["duration"])
    dscore = 0.5 if d is None else max(0.0, 1 - d / 10)
    total = 0.4 * t + 0.3 * a + 0.15 * (al if tr["album"] else 0.5) + 0.15 * dscore
    media = (rel.get("media") or [{}])[0]
    trk = (media.get("track") or [{}])[0]
    y = (rel.get("date") or "")[:4]
    return dict(t=t, a=a, al=al, d=d, total=total, rec=rec, rel=rel, media=media, trk=trk,
                year=int(y) if y.isdigit() else 9999)


def classify(s, from_tags):
    if s is None:
        return NONE
    dur_ok = s["d"] is not None and s["d"] <= 3
    if from_tags and s["t"] >= 0.97 and s["a"] >= 0.97 and dur_ok and (s["al"] >= 0.97):
        return VERIFIED
    if s["t"] >= 0.9 and s["a"] >= 0.85 and (dur_ok or s["al"] >= 0.9):
        return HIGH
    if s["t"] >= 0.75 and s["a"] >= 0.6:
        return POSSIBLE
    return REVIEW


def proposals(tr, s, conf):
    """Field proposals from the best match. Differences are flagged for review, never assumed wrong."""
    rec, rel = s["rec"], s["rel"]
    same_release = s["al"] >= 0.85
    out = {
        "title": rec.get("title", ""),
        "artist": credit(rec.get("artist-credit")),
        "album": rel.get("title", ""),
        "albumartist": credit(rel.get("artist-credit")) or credit(rec.get("artist-credit")),
        "year": (rel.get("date") or "")[:4],
        "track": str(s["trk"].get("number") or ""),
        "disc": str(s["media"].get("position") or ""),
    }
    tags_ = sorted(rec.get("tags") or [], key=lambda x: -x.get("count", 0))
    if tags_ and not tr["genre"]:
        out["genre"] = tags_[0]["name"].title()
    res = []
    for field, prop in out.items():
        cur = str(tr[field] or "")
        if not prop or not prop.strip():
            continue
        if field in ("track", "disc"):
            if not prop.isdigit() or (cur and int(cur) == int(prop)):
                continue
            if field == "disc" and not cur and prop == "1":
                continue
        elif field == "year":
            if cur and cur[:4] in (prop, (rec.get("first-release-date") or "")[:4]):
                continue  # matches this release or the recording's original release year
        elif cur == prop:
            continue
        release_field = field in ("album", "albumartist", "year", "track", "disc")
        c = conf
        if release_field and not same_release:
            if tr["album"]:
                continue  # the match is on a different release than the user's album: leave release fields alone
            c = POSSIBLE  # no album in the file: a release is only a guess
        if not cur:
            note = "Field is empty in your file."
        elif cur.casefold() == prop.casefold():
            note = "Only capitalisation differs."
        elif field in ("album", "title") and norm(_base(cur)) == norm(_base(prop)):
            note = "Only an edition suffix differs (e.g. “Remastered”, “Deluxe”). Keeping yours is usually fine."
            c = POSSIBLE
        elif field == "year":
            note = ("Year differs from this MusicBrainz release. Your file may carry the original release "
                    "year or a remaster/reissue year — both can be valid.")
        else:
            note = ("Differs from MusicBrainz. May be a legitimate variant (remaster, deluxe, regional or "
                    "compilation release). Review before approving.")
        if field == "genre":
            note = "Genre from MusicBrainz community tags."
        if field == "genre" or (cur and (field in ("year", "track", "disc") or cur.casefold() == prop.casefold())):
            c = POSSIBLE  # edition-dependent or cosmetic: never more than a possibility
        res.append((field, cur, prop, c, note))
    return res


def lookup_track(conn, tr, ua=None):
    ua = ua or user_agent(conn)
    from_tags = bool(tr["title"])
    title = tr["title"] or guess_title(tr["path"])
    artist = tr["artist"] or tr["albumartist"]
    if not title:
        conf, best, cands = NONE, None, []
    else:
        q = f'recording:"{esc(title)}"' + (f' AND artist:"{esc(artist)}"' if artist else "")
        data = {}
        if tr["album"]:  # prefer recordings on the user's album; fall back to any release
            qa = q + f' AND release:"{esc(tr["album"])}"'
            data = cached_json(conn, f"{MB}/recording?query={urllib.parse.quote(qa)}&fmt=json&limit=10", ua)
        if not data.get("recordings"):
            data = cached_json(conn, f"{MB}/recording?query={urllib.parse.quote(q)}&fmt=json&limit=10", ua)
        scored = sorted((score(tr, title, artist, r) for r in data.get("recordings", [])),
                        key=lambda s: (-round(s["total"], 1), -round(s["al"], 2), s["year"]))
        best = scored[0] if scored else None
        conf = classify(best, from_tags)
        cands = [dict(title=s["rec"].get("title"), artist=credit(s["rec"].get("artist-credit")),
                      release=s["rel"].get("title", ""), date=s["rel"].get("date", ""),
                      country=s["rel"].get("country", ""), score=round(s["total"] * 100),
                      length=round((s["rec"].get("length") or 0) / 1000), id=s["rec"].get("id"))
                 for s in scored[:6]]
    return _save(conn, tr, conf, best, cands)


def _save(conn, tr, conf, best, cands):
    """Replace this track's pending MusicBrainz proposals and match record."""
    conn.execute("DELETE FROM suggestions WHERE track_id=? AND source='musicbrainz' AND status='pending'",
                 (tr["id"],))
    if best and conf in (VERIFIED, HIGH, POSSIBLE):
        now = time.time()
        for field, cur, prop, c, note in proposals(tr, best, conf):
            conn.execute("INSERT INTO suggestions(track_id, field, current, proposed, confidence, source, note, "
                         "status, created) VALUES(?,?,?,?,?,?,?,?,?)",
                         (tr["id"], field, cur, prop, c, "musicbrainz", note, "pending", now))
    conn.execute("INSERT OR REPLACE INTO mb_matches VALUES(?,?,?,?,?,?,?)",
                 (tr["id"], conf, round(best["total"] * 100) if best else 0,
                  best["rec"].get("id") if best else None, best["rel"].get("id") if best else None,
                  json.dumps(cands), time.time()))
    conn.commit()
    return conf


# ---------- album-level matching ----------
# One release search + up to 3 release fetches per album (instead of one search per song), and every song in
# the album is matched against the same release, so track numbers, years and album artist stay consistent.

RELEASE_CANDIDATES = 3


def _base(s):
    """Album title without edition suffixes: 'OK Computer (Remastered)' -> 'OK Computer'."""
    return re.sub(r"\s*[\(\[][^\)\]]*[\)\]]\s*", " ", s or "").strip()


def album_sim(a, b):
    return max(sim(a, b), sim(_base(a), _base(b)))


def _year(date):
    y = (date or "")[:4]
    return int(y) if y.isdigit() else 9999


def search_releases(conn, album, artist, n_local, ua):
    """Release search results ranked by title, artist, track-count closeness, officialness, then earliest date."""
    q = f'release:"{esc(_base(album) or album)}"' + (f' AND artist:"{esc(artist)}"' if artist else "")
    data = cached_json(conn, f"{MB}/release?query={urllib.parse.quote(q)}&fmt=json&limit=25", ua)
    ranked = []
    for r in data.get("releases", []):
        n = r.get("track-count") or sum(m.get("track-count", 0) for m in r.get("media") or [])
        s = (0.4 * album_sim(album, r.get("title")) +
             0.3 * (sim(artist, credit(r.get("artist-credit"))) if artist else 0.5) +
             0.2 * (1 - abs(n - n_local) / max(n, n_local, 1)) + 0.1 * (r.get("status") == "Official"))
        ranked.append((round(s, 2), _year(r.get("date")), r))
    ranked.sort(key=lambda x: (-x[0], x[1]))
    return [r for _, _, r in ranked]


def release_tracks(release):
    return [(m.get("position") or 1, t) for m in release.get("media") or [] for t in m.get("tracks") or []]


def match_tracks(local, release):
    """Greedy one-to-one assignment of local songs to release tracks.
    Returns ({local_index: (release_track_index, score, title_sim, duration_diff, same_position)}, score 0..1)."""
    rts = release_tracks(release)
    pairs = []
    for i, t in enumerate(local):
        title = t["title"] or guess_title(t["path"])
        for j, (pos, rt) in enumerate(rts):
            rec = rt.get("recording") or {}
            ts = sim(title, rt.get("title") or rec.get("title"))
            length = rt.get("length") or rec.get("length")
            d = abs(length / 1000 - t["duration"]) if length and t["duration"] else None
            ds = 0.5 if d is None else max(0.0, 1 - d / 10)
            same_pos = bool(t["track"]) and str(t["track"]) == str(rt.get("number")) and (t["disc"] or 1) == pos
            sc = 0.65 * ts + 0.25 * ds + 0.10 * same_pos
            # untitled songs can still match by disc/track position + duration
            if ts >= 0.6 or (same_pos and d is not None and d <= 3):
                pairs.append((sc, i, j, ts, d, same_pos))
    pairs.sort(key=lambda p: -p[0])
    used_i, used_j, out = set(), set(), {}
    for sc, i, j, ts, d, same_pos in pairs:
        if i not in used_i and j not in used_j:
            used_i.add(i)
            used_j.add(j)
            out[i] = (j, sc, ts, d, same_pos)
    if not local or not rts:
        return out, 0.0
    extra = max(0, len(rts) - len(local)) / len(rts)  # deluxe/box editions with many extra tracks score lower
    return out, sum(m[1] for m in out.values()) / len(local) - 0.15 * extra


def _album_score(t, release, pos, rt, ts, d):
    """Build the same match dict score() produces, so classify() and proposals() work unchanged."""
    rec = dict(rt.get("recording") or {})
    rec["title"] = rt.get("title") or rec.get("title", "")
    rec["artist-credit"] = rt.get("artist-credit") or rec.get("artist-credit") or release.get("artist-credit")
    rec["tags"] = [{"name": g["name"], "count": g.get("count", 0)} for g in release.get("genres") or []]
    rel = {k: release.get(k) for k in ("id", "title", "artist-credit", "date", "country")}
    artist = t["artist"] or t["albumartist"]
    a = sim(artist, credit(rec["artist-credit"])) if artist else 0.5
    al = album_sim(t["album"], release.get("title"))
    dscore = 0.5 if d is None else max(0.0, 1 - d / 10)
    return dict(t=ts, a=a, al=al, d=d, total=0.4 * ts + 0.3 * a + 0.15 * al + 0.15 * dscore, rec=rec, rel=rel,
                media={"position": pos}, trk={"number": rt.get("number")}, year=_year(release.get("date")))


def lookup_album(conn, local, wanted, ua):
    """Match one album (all its local songs) to a single release; save results for songs in `wanted`.
    Returns {track_id: confidence} for songs it handled; unmatched songs are left for per-song lookup."""
    first = local[0]
    album = first["album"]
    artist = first["albumartist"] or first["artist"]
    best_rel, best_score, best_map = None, 0.0, {}
    considered = []
    for r in search_releases(conn, album, artist, len(local), ua)[:RELEASE_CANDIDATES]:
        rel = cached_json(conn, f"{MB}/release/{r['id']}?inc=recordings+artist-credits+genres&fmt=json", ua)
        if not rel:
            continue
        m, sc = match_tracks(local, rel)
        considered.append((sc, rel))
        if sc > best_score:
            best_rel, best_score, best_map = rel, sc, m
    if not best_rel or best_score < 0.4:
        return {}
    cands = [dict(title=f"{len(release_tracks(rel))} tracks", artist=credit(rel.get("artist-credit")),
                  release=rel.get("title", ""), date=rel.get("date", ""), country=rel.get("country", ""),
                  score=round(sc * 100), length=0, id=rel.get("id"), album=True)
             for sc, rel in sorted(considered, key=lambda x: -x[0])]
    rts = release_tracks(best_rel)
    out = {}
    for i, t in enumerate(local):
        if t["id"] not in wanted or i not in best_map:
            continue
        j, _, ts, d, same_pos = best_map[i]
        pos, rt = rts[j]
        s = _album_score(t, best_rel, pos, rt, ts, d)
        conf = classify(s, bool(t["title"]))
        if conf == REVIEW and not t["title"] and same_pos and d is not None and d <= 3:
            conf = POSSIBLE  # untitled song in the right slot with the right length
        out[t["id"]] = _save(conn, t, conf, s, cands)
    return out


def estimate_seconds(conn, ids):
    """Rough MusicBrainz time: ~4 requests per album, 1-2 per loose song, 1.1 s each."""
    albums, singles = _group(conn, ids)
    return 1.1 * (4 * len(albums) + 1.5 * len(singles))


def _group(conn, ids):
    """Split requested songs into album groups (with all their album-mates) and loose songs."""
    rows = [conn.execute("SELECT * FROM tracks WHERE id=?", (i,)).fetchone() for i in ids]
    keys, singles = {}, []
    for r in rows:
        if r is None:
            continue
        if r["album"].strip():
            keys.setdefault((r["album"].strip().casefold(), os.path.dirname(r["path"])), []).append(r)
        else:
            singles.append(r)
    albums = []
    for (album, folder), wanted in keys.items():
        mates = [r for r in conn.execute("SELECT * FROM tracks WHERE missing=0 AND readable=1 AND lower(trim(album))=? "
                                         "AND substr(path, 1, ?)=? ORDER BY disc, track, path",
                                         (album, len(folder) + 1, folder + "/"))
                 if os.path.dirname(r["path"]) == folder]
        if len(mates) >= 2:
            albums.append((mates, wanted))
        else:
            singles += wanted
    return albums, singles


def lookup_many(conn, ids, progress=None, cancel=None):
    ua = user_agent(conn)
    counts = {c: 0 for c in CONFIDENCES}
    errors = []
    albums, singles = _group(conn, ids)
    total = len(albums) + len(singles)
    step = 0
    try:
        for local, wanted in albums:
            if cancel and cancel():
                break
            if progress:
                progress(step, total, f"MusicBrainz album: {local[0]['albumartist'] or local[0]['artist']} – "
                                      f"{local[0]['album']}")
            step += 1
            done = lookup_album(conn, local, {w["id"] for w in wanted}, ua)
            for c in done.values():
                counts[c] += 1
            singles += [w for w in wanted if w["id"] not in done]  # songs the album match couldn't place
            total = len(albums) + len(singles)
        for tr in singles:
            if cancel and cancel():
                break
            if progress:
                progress(step, total, f"MusicBrainz: {tr['artist']} – {tr['title'] or os.path.basename(tr['path'])}")
            step += 1
            counts[lookup_track(conn, tr, ua)] += 1
    except NetworkError as e:
        errors.append(str(e))  # offline: stop instead of failing every remaining song
    return dict(counts=counts, errors=errors)


# ---------- artwork ----------

def search_artwork(conn, artist, album, progress=None, cancel=None):
    ua = user_agent(conn)
    q = f'release:"{esc(album)}"' + (f' AND artist:"{esc(artist)}"' if artist else "")
    data = cached_json(conn, f"{MB}/release?query={urllib.parse.quote(q)}&fmt=json&limit=15", ua)
    out, seen = [], set()
    for r in data.get("releases", []):
        rg = (r.get("release-group") or {}).get("id")
        if not rg or rg in seen:
            continue
        seen.add(rg)
        out.append(dict(title=r.get("title"), artist=credit(r.get("artist-credit")), date=r.get("date", ""),
                        country=r.get("country", ""), release=r.get("id"), rg=rg,
                        thumb=f"{CAA}/release-group/{rg}/front-250", full=f"{CAA}/release-group/{rg}/front"))
    results = []
    for i, c in enumerate(out[:8]):
        if cancel and cancel():
            break
        if progress:
            progress(i, min(len(out), 8), f"Cover Art Archive: {c['title']}")
        try:
            img = http(c["thumb"], ua)
        except NetworkError:
            img = None
        if img:
            c["thumb_data"] = img
            results.append(c)
    return results


def download_image(conn, url, progress=None, cancel=None):
    data = http(url, user_agent(conn), timeout=60)
    if not data:
        raise NetworkError("Image not available")
    return prepare_image(data)


def prepare_image(data, max_side=3000):
    """Validate image bytes; keep JPEG/PNG as-is (full resolution) unless huge; convert others to JPEG."""
    from PIL import Image
    try:
        im = Image.open(io.BytesIO(data))
        im.load()
    except Exception as e:
        raise ValueError(f"Not a valid image: {e}")
    fmt = (im.format or "").upper()
    if fmt in ("JPEG", "PNG") and max(im.size) <= max_side:
        return data, "image/" + fmt.lower(), im.size
    im = im.convert("RGB")
    im.thumbnail((max_side, max_side))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=92)
    return buf.getvalue(), "image/jpeg", im.size


# ---------- AcoustID ----------

def acoustid_lookup(key, duration, fingerprint, ua=DEFAULT_UA):
    body = urllib.parse.urlencode(dict(client=key, meta="recordingids", duration=int(duration),
                                       fingerprint=fingerprint)).encode()
    data = json.loads(http(ACOUSTID, ua, data=body) or b"{}")
    if data.get("status") != "ok":
        raise NetworkError(f"AcoustID error: {(data.get('error') or {}).get('message', 'unknown')}")
    for r in sorted(data.get("results", []), key=lambda r: -r.get("score", 0)):
        if r.get("score", 0) >= 0.8 and r.get("recordings"):
            return r["recordings"][0]["id"]
    return "-"
