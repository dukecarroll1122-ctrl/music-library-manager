"""Audio identity verification: does the audio match the tags?

Chromaprint fingerprint -> AcoustID -> MusicBrainz recording(s), compared with the file's title, artist and
album. Results are evidence for a human to review. Nothing here modifies files, and "no fingerprint match"
is never treated as proof that a file is wrong.
"""
import json
import os
import re
import subprocess
import time
import urllib.parse

from . import db, online, scanner

VERIFIED, LIKELY, POSSIBLE, STRONG, NO_MATCH, REVIEW = (
    "Verified match", "Likely match", "Possible mismatch", "Strong mismatch", "No fingerprint match",
    "Needs manual review")
STATUSES = (VERIFIED, LIKELY, POSSIBLE, STRONG, NO_MATCH, REVIEW)

# version words: cosmetic ones don't change the performance; vocal ones change what you hear completely
COSMETIC = {"remaster", "remastered", "mono", "stereo", "explicit", "clean", "single", "album", "lp", "original",
            "bonus", "deluxe", "digital", "anniversary", "expanded"}
VERSION = {"live", "remix", "mix", "edit", "extended", "radio", "club", "dub", "acoustic", "demo", "unplugged",
           "alternate", "alt", "take", "session", "rehearsal", "orchestral", "piano", "reprise", "version", "rework",
           "cover", "medley"}
VOCAL = {"instrumental", "karaoke", "acapella", "a cappella", "cappella", "inst", "off vocal", "backing"}


class VerifyError(Exception):
    pass


# ---------- fingerprinting ----------

def fingerprint(path, timeout=120):
    """(duration_seconds, fingerprint) via Chromaprint's fpcalc. Read-only."""
    exe = scanner.tool("fpcalc")
    if not exe:
        raise VerifyError("Chromaprint (fpcalc) is not installed. Install it with:  brew install chromaprint")
    r = None
    try:
        r = subprocess.run([exe, "-json", path], capture_output=True, text=True, timeout=timeout)
        j = json.loads(r.stdout)
        return float(j["duration"]), j["fingerprint"]
    except (OSError, subprocess.SubprocessError, ValueError, KeyError) as e:
        raise VerifyError(f"Could not fingerprint: {getattr(r, 'stderr', '') or e}".strip()[:300])


def cached_fingerprint(conn, track):
    """Uses tracks.fingerprint ('duration|fp'); the scanner clears it whenever the file changes."""
    if track["fingerprint"]:
        d, fp = track["fingerprint"].split("|", 1)
        return float(d), fp
    d, fp = fingerprint(track["path"])
    conn.execute("UPDATE tracks SET fingerprint=? WHERE id=?", (f"{d}|{fp}", track["id"]))
    conn.commit()
    return d, fp


# ---------- AcoustID ----------

def acoustid_lookup(key, duration, fp, ua=online.DEFAULT_UA):
    body = urllib.parse.urlencode(dict(client=key, meta="recordings releasegroups compress", duration=int(duration),
                                       fingerprint=fp)).encode()
    raw = online.http(online.ACOUSTID, ua, data=body)
    data = json.loads(raw or b"{}")
    if data.get("status") != "ok":
        msg = (data.get("error") or {}).get("message", "unknown error")
        raise online.NetworkError(f"AcoustID: {msg}")
    return data.get("results", [])


def mb_recording(conn, rid):
    try:
        return online.cached_json(conn, f"{online.MB}/recording/{rid}?inc=artist-credits+releases&fmt=json",
                                  online.user_agent(conn)) or {}
    except online.NetworkError:
        return {}


# ---------- comparison ----------

def _words(s):
    return set(re.findall(r"[a-z]+", (s or "").casefold().replace("a cappella", "acapella")))


def split_title(t):
    """'Billie Jean (Single Version) [Remastered 2008]' -> ('billie jean', {'single','version','remastered'})."""
    t = t or ""
    tagged = re.findall(r"[\(\[]([^\)\]]*)[\)\]]", t)
    base = re.sub(r"\s*[\(\[][^\)\]]*[\)\]]", "", t)
    m = re.search(r"\s+[-–—]\s+(.*)$", base)  # 'Song - Live at Wembley' / 'Song - 2011 Remaster'
    if m and (_words(m.group(1)) & (COSMETIC | VERSION | VOCAL) or re.search(r"\b(19|20)\d\d\b", m.group(1))):
        tagged.append(m.group(1))
        base = base[:m.start()]
    base = re.sub(r"\s+(feat\.?|ft\.?|featuring)\s+.*$", "", base, flags=re.I)
    words = set()
    for x in tagged:
        words |= _words(x)
    return online.norm(base), words


def artists_of(s):
    parts = re.split(r"\s*(?:,|&|\+|/|;|\band\b|\bx\b|\bwith\b|\bfeat\.?|\bft\.?|\bfeaturing\b|\bvs\.?)\s*",
                     (s or "").casefold())
    return {online.norm(p) for p in parts if online.norm(p)}


def artist_sim(local, matched_names):
    """Best of whole-string similarity and 'a credited artist matches a local artist'."""
    if not local:
        return 0.5
    whole = max((online.sim(local, m) for m in matched_names), default=0.0)
    la = artists_of(local)
    ma = set()
    for m in matched_names:
        ma |= artists_of(m)
    part = max((online.sim(a, b) for a in la for b in ma), default=0.0)
    return max(whole, part if part >= 0.92 else 0.0)


def _rec_artist(rec):
    return "".join(a.get("name", "") + a.get("joinphrase", "") for a in rec.get("artists") or [])


def _classes(words):
    return dict(version=words & VERSION, vocal=words & VOCAL, cosmetic=words & COSMETIC)


def compare(local, results, min_score=0.5):
    """local: dict(title, artist, album, duration). results: AcoustID results with recordings.
    Returns dict(status, score, recording, notes, candidates)."""
    cands = []
    for res in results or []:
        for rec in res.get("recordings") or []:
            cands.append((float(res.get("score") or 0), rec))
    titled = [(s, r) for s, r in cands if r.get("title") and s >= min_score]
    summary = [dict(score=round(s, 3), id=r.get("id"), title=r.get("title", ""), artist=_rec_artist(r),
                    duration=r.get("duration"), albums=[g.get("title") for g in r.get("releasegroups") or []][:8])
               for s, r in sorted(cands, key=lambda x: -x[0])[:10]]
    out = dict(status=NO_MATCH, score=0.0, recording=None, notes=[], candidates=summary)
    if not cands or max(s for s, _ in cands) < min_score:
        out["notes"].append("AcoustID has no fingerprint match. This is not evidence the file is wrong — many "
                            "recordings, especially rare, regional or very new ones, are simply not in the database.")
        return out
    if not titled:
        out.update(status=REVIEW, score=max(s for s, _ in cands))
        out["notes"].append("The fingerprint is known to AcoustID but has no linked recording details to compare.")
        return out

    lt_base, lt_words = split_title(local.get("title"))
    scored = []
    for s, r in titled:
        mt_base, mt_words = split_title(r["title"])
        ts = online.sim(lt_base, mt_base) if lt_base else 0.0
        a_s = artist_sim(local.get("artist"), [_rec_artist(r)] + [a.get("name", "") for a in r.get("artists") or []])
        scored.append(dict(fp=s, rec=r, ts=ts, as_=a_s, mt_words=mt_words,
                           rank=(s >= 0.8, round(0.6 * ts + 0.4 * a_s, 2), s)))
    best = max(scored, key=lambda x: x["rank"])
    r, fp, ts, a_s = best["rec"], best["fp"], best["ts"], best["as_"]
    ident = f"“{r['title']}” by {_rec_artist(r) or 'unknown artist'}"
    out.update(score=fp, recording=dict(id=r.get("id"), title=r["title"], artist=_rec_artist(r),
                                        albums=[g.get("title") for g in r.get("releasegroups") or []],
                                        duration=r.get("duration")))
    notes = out["notes"]

    if not local.get("title"):
        out["status"] = REVIEW
        notes.append(f"Your file has no title. The audio was identified as {ident}.")
        return out

    lc, mc = _classes(lt_words), _classes(best["mt_words"])
    if ts >= 0.9 and a_s >= 0.85:
        status = VERIFIED
        if lc["vocal"] != mc["vocal"]:
            status = POSSIBLE
            which = "an instrumental/karaoke version" if mc["vocal"] else "the vocal version"
            notes.append(f"Same song, but the audio was identified as {which} ({r['title']}) while your title "
                         "says otherwise.")
        elif lc["version"] != mc["version"]:
            status = LIKELY
            notes.append(f"Same song, different version: your title has {sorted(lc['version']) or 'no version tag'}, "
                         f"the identified recording has {sorted(mc['version']) or 'no version tag'} "
                         "(live, remix, edit, acoustic…). Both can be correct.")
        local_d, rec_d = local.get("duration") or 0, r.get("duration") or 0
        if status == VERIFIED and local_d and rec_d and abs(local_d - rec_d) > 15:
            status = LIKELY
            notes.append(f"Length differs by {abs(local_d - rec_d):.0f}s (edit or alternate version?).")
        album = local.get("album")
        groups = r.get("releasegroups") or []
        if album and groups and status == VERIFIED:
            al = max((online.album_sim(album, g.get("title")) for g in groups), default=0)
            compilation = any("Compilation" in (g.get("secondarytypes") or []) for g in groups)
            if al < 0.8:
                if compilation:
                    notes.append("Your album isn't among this recording's releases; it also appears on "
                                 "compilations, so this is common.")
                else:
                    status = LIKELY
                    notes.append(f"Your album “{album}” isn't among this recording's known releases "
                                 "(compilation, regional or alternate release?).")
        if lc["cosmetic"] != mc["cosmetic"] and status == VERIFIED:
            notes.append("Only remaster/edition wording differs — same recording.")
    elif ts >= 0.9:
        status = POSSIBLE if local.get("artist") else LIKELY
        notes.append(f"Same title, but the audio was identified as {ident}. Could be a cover version, a "
                     "mislabeled artist, or a different credit for the same act." if local.get("artist") else
                     f"Your file has no artist; the audio was identified as {ident}.")
    elif ts >= 0.6:
        status = LIKELY if a_s >= 0.85 else POSSIBLE
        notes.append(f"Title is spelled differently: identified as {ident}.")
    else:
        status = STRONG if fp >= 0.85 else POSSIBLE
        notes.append(f"The audio was identified as {ident}, but your tags say “{local.get('title')}” by "
                     f"{local.get('artist') or 'unknown artist'}.")
        if a_s >= 0.85:
            notes.append("Same artist, different song — often a track-numbering or file-naming mix-up within an "
                         "album.")
    if fp < 0.7:  # weak fingerprint match: never more certain than 'likely' / 'possible'
        status = {VERIFIED: LIKELY, STRONG: POSSIBLE}.get(status, status)
        notes.append(f"Fingerprint match is weak ({fp:.0%}).")
    other = [c for c in scored if c is not best and c["fp"] >= 0.8 and c["ts"] < 0.6]
    if status == VERIFIED and other:
        notes.append("AcoustID also links this fingerprint to other recordings — usually the same audio "
                     "released under different titles.")
    out["status"] = status
    return out


def verify_track(conn, track, key, ua=None):
    """Fingerprint (cached), identify and store the result for one library track. Returns the result dict."""
    duration, fp = cached_fingerprint(conn, track)
    results = acoustid_lookup(key, duration, fp, ua or online.user_agent(conn))
    local = dict(title=track["title"], artist=track["artist"] or track["albumartist"], album=track["album"],
                 duration=track["duration"] or duration)
    res = compare(local, results)
    rec = res["recording"]
    if rec and res["status"] in (POSSIBLE, STRONG, LIKELY) and rec.get("id"):
        detail = mb_recording(conn, rec["id"])  # MusicBrainz: disambiguation (e.g. "live", "cover") for the note
        if detail.get("disambiguation"):
            res["notes"].append(f"MusicBrainz describes the identified recording as: {detail['disambiguation']}.")
    if rec and rec.get("id"):
        conn.execute("UPDATE tracks SET acoustid_rec=? WHERE id=?", (rec["id"], track["id"]))
    conn.execute("INSERT OR REPLACE INTO verifications VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                 (track["id"], res["status"], res["score"], rec and rec.get("id"), rec and rec["title"],
                  rec and rec["artist"], json.dumps(rec["albums"] if rec else []), json.dumps(res["notes"]),
                  json.dumps(res["candidates"]), track["size"], track["mtime"], time.time()))
    conn.commit()
    return res


def pending_ids(conn, ids=None):
    """Tracks never verified, or changed since their last verification."""
    q = ("SELECT t.id FROM tracks t LEFT JOIN verifications v ON v.track_id=t.id WHERE t.missing=0 AND t.readable=1 "
         "AND (v.track_id IS NULL OR v.size IS NOT t.size OR v.mtime IS NOT t.mtime)")
    found = [r[0] for r in conn.execute(q + " ORDER BY t.path")]
    return [i for i in found if i in set(ids)] if ids is not None else found


def run_batch(conn, ids, key, progress=None, cancel=None, paused=None, workers=4):
    """Verify tracks in order. Fingerprints are computed ahead in parallel; AcoustID calls are rate-limited.
    paused() is polled between tracks (pause/resume). Returns counts per status."""
    from concurrent.futures import ThreadPoolExecutor
    counts = {s: 0 for s in STATUSES}
    errors = []
    ua = online.user_agent(conn)
    rows = [dict(r) for r in conn.execute(f"SELECT * FROM tracks WHERE id IN ({','.join('?' * len(ids))})",
                                          list(ids))] if ids else []
    order = {i: n for n, i in enumerate(ids)}
    rows.sort(key=lambda r: order[r["id"]])

    def fp_only(r):
        if r["fingerprint"]:
            return r["id"], r["fingerprint"], None
        try:
            d, f = fingerprint(r["path"])
            return r["id"], f"{d}|{f}", None
        except VerifyError as e:
            return r["id"], None, str(e)

    done = 0
    with ThreadPoolExecutor(workers) as ex:
        for start in range(0, len(rows), 16):
            chunk = rows[start:start + 16]
            for (tid, fpstr, err), r in zip(ex.map(fp_only, chunk), chunk):
                while paused and paused() and not (cancel and cancel()):
                    time.sleep(0.2)
                if cancel and cancel():
                    return dict(counts=counts, errors=errors, done=done, cancelled=True)
                if progress:
                    progress(done, len(rows), f"{r['artist']} – {r['title'] or os.path.basename(r['path'])}")
                if err:
                    errors.append(f"{os.path.basename(r['path'])}: {err}")
                    done += 1
                    continue
                if not r["fingerprint"]:
                    conn.execute("UPDATE tracks SET fingerprint=? WHERE id=?", (fpstr, tid))
                    r["fingerprint"] = fpstr
                if not key:
                    done += 1
                    continue  # fingerprints only (no API key yet) — still cached for later
                try:
                    res = verify_track(conn, r, key, ua)
                    counts[res["status"]] += 1
                except online.NetworkError as e:
                    errors.append(str(e))
                    conn.commit()
                    return dict(counts=counts, errors=errors, done=done, cancelled=False)
                done += 1
            conn.commit()
    return dict(counts=counts, errors=errors, done=done, cancelled=False)
