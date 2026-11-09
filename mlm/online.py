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
            if cur[:4] in (prop, (rec.get("first-release-date") or "")[:4]):
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


def lookup_many(conn, ids, progress=None, cancel=None):
    ua = user_agent(conn)
    counts = {c: 0 for c in CONFIDENCES}
    errors = []
    for i, tid in enumerate(ids):
        if cancel and cancel():
            break
        tr = conn.execute("SELECT * FROM tracks WHERE id=?", (tid,)).fetchone()
        if progress:
            progress(i, len(ids), f"MusicBrainz: {tr['artist']} – {tr['title'] or os.path.basename(tr['path'])}")
        try:
            counts[lookup_track(conn, tr, ua)] += 1
        except NetworkError as e:
            errors.append(str(e))
            break  # offline: stop instead of failing every remaining track
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
