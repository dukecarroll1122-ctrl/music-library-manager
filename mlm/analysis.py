"""Metadata health rules, dashboard stats, and the shared track query used by the GUI."""
import os
import re
import time
from collections import defaultdict

# kind -> (category, severity, label)
KINDS = {
    "unreadable": ("corrupt", "error", "Unreadable / corrupted file"),
    "zero_duration": ("corrupt", "error", "Zero duration"),
    "missing_title": ("missing", "warning", "Missing title"),
    "missing_artist": ("missing", "warning", "Missing artist"),
    "missing_album": ("missing", "warning", "Missing album"),
    "missing_track": ("missing", "warning", "Missing track number"),
    "missing_year": ("missing", "info", "Missing year"),
    "missing_genre": ("missing", "info", "Missing genre"),
    "missing_art": ("art", "info", "Missing artwork"),
    "placeholder": ("suspicious", "review", "Placeholder text"),
    "title_filename": ("suspicious", "review", "Title looks like a filename"),
    "bad_year": ("suspicious", "review", "Implausible release year"),
    "suspicious_genre": ("suspicious", "review", "Suspicious genre"),
    "inconsistent_albumartist": ("suspicious", "review", "Inconsistent album artist"),
    "inconsistent_year": ("suspicious", "review", "Inconsistent year within album"),
    "duplicate_tracknum": ("suspicious", "review", "Duplicate track number in album"),
    "track_over_total": ("suspicious", "review", "Track number exceeds total"),
}
SEVERITY_ORDER = {"error": 3, "warning": 2, "review": 1, "info": 0}
PROBLEMS = {"Missing metadata": "missing", "Suspicious metadata": "suspicious", "Missing artwork": "art",
            "Corrupted / unreadable": "corrupt", "Duplicates": "duplicate", "Any issue": "any"}

PLACEHOLDERS = {"unknown", "unknown artist", "unknown album", "untitled", "no title", "track", "audio track",
                "n/a", "none", "artist", "album", "title", "<unknown>", "unknown title", "new recording"}
RE_TRACK_TITLE = re.compile(r"^(track|audio track|piste|titel)\s*\d+$", re.I)
RE_FILENAME = re.compile(r"\.(mp3|flac|wav|m4a|aac|ogg|opus|aiff?|ape|wma)$|^\d{1,3}\s*[-_.]\s+\S", re.I)
RE_BAD_GENRE = re.compile(r"^\(?\d+\)?$|^(other|unknown|genre|none|misc|default|blah)$|https?:|www\.", re.I)


def year_of(s):
    m = re.match(r"\s*(\d{4})", s or "")
    return int(m.group(1)) if m else None


def track_issues(t):
    """Per-track rules. t is a dict/Row of a track."""
    out = []
    if not t["readable"]:
        return [("unreadable", t["error"] or "")]
    if not (t["duration"] or 0) > 0:
        out.append(("zero_duration", t["error"] or ""))
    for f in ("title", "artist", "album"):
        v = (t[f] or "").strip()
        if not v:
            out.append((f"missing_{f}", ""))
        elif v.casefold() in PLACEHOLDERS or (f == "title" and RE_TRACK_TITLE.match(v)):
            out.append(("placeholder", f"{f}: “{v}”"))
    title = (t["title"] or "").strip()
    if title and (RE_FILENAME.search(title) or ("_" in title and " " not in title)):
        out.append(("title_filename", title))
    if not t["track"]:
        out.append(("missing_track", ""))
    elif t["track_total"] and t["track"] > t["track_total"]:
        out.append(("track_over_total", f"{t['track']}/{t['track_total']}"))
    y = (t["year"] or "").strip()
    if not y:
        out.append(("missing_year", ""))
    else:
        yy = year_of(y)
        if yy is None or yy < 1900 or yy > time.localtime().tm_year + 1:
            out.append(("bad_year", y))
    g = (t["genre"] or "").strip()
    if not g:
        out.append(("missing_genre", ""))
    elif RE_BAD_GENRE.search(g) or len(g) > 40:
        out.append(("suspicious_genre", g))
    if not t["has_art"]:
        out.append(("missing_art", ""))
    return out


def album_issues(tracks):
    """Cross-track rules for tracks sharing an album name and folder."""
    out = defaultdict(list)
    if len(tracks) < 2:
        return out
    aas = {t["albumartist"].strip() for t in tracks if t["albumartist"].strip()}
    artists = {t["artist"].strip() for t in tracks if t["artist"].strip()}
    if len(aas) > 1:
        for t in tracks:
            out[t["id"]].append(("inconsistent_albumartist", "Album artists: " + " / ".join(sorted(aas))[:200]))
    elif not aas and len(artists) >= 3:
        for t in tracks:
            out[t["id"]].append(("inconsistent_albumartist",
                                 "Several artists but no album artist (compilation?)"))
    years = {year_of(t["year"]) for t in tracks if year_of(t["year"])}
    if len(years) > 1:
        for t in tracks:
            out[t["id"]].append(("inconsistent_year", "Years: " + ", ".join(map(str, sorted(years)))
                                 + " (can be legitimate on compilations)"))
    seen = defaultdict(list)
    for t in tracks:
        if t["track"]:
            seen[(t["disc"] or 1, t["track"])].append(t["id"])
    for (disc, n), ids in seen.items():
        if len(ids) > 1:
            for i in ids:
                out[i].append(("duplicate_tracknum", f"Disc {disc} track {n} appears {len(ids)}×"))
    return out


def rebuild_issues(conn):
    rows = [dict(r) for r in conn.execute("SELECT * FROM tracks WHERE missing=0")]
    albums = defaultdict(list)
    issues = []
    for t in rows:
        for kind, detail in track_issues(t):
            issues.append((t["id"], kind, detail))
        if t["readable"] and t["album"].strip():
            albums[(t["album"].strip().casefold(), os.path.dirname(t["path"]))].append(t)
    for tracks in albums.values():
        for tid, lst in album_issues(tracks).items():
            issues.extend((tid, k, d) for k, d in lst)
    conn.execute("DELETE FROM issues")
    conn.executemany("INSERT INTO issues(track_id, kind, category, severity, detail) VALUES(?,?,?,?,?)",
                     [(tid, k, KINDS[k][0], KINDS[k][1], d) for tid, k, d in issues])
    conn.commit()
    return len(issues)


def health(issue_kinds):
    """Worst severity for a list of kinds -> 'error'|'warning'|'review'|'ok' (info alone counts as ok)."""
    worst = max((SEVERITY_ORDER[KINDS[k][1]] for k in issue_kinds if k in KINDS), default=0)
    return {3: "error", 2: "warning", 1: "review", 0: "ok"}[worst]


def duplicate_extra_ids(conn):
    """Track ids that are 'extra copies' (all but the first member of each group)."""
    extra = set()
    for g in conn.execute("SELECT id FROM dup_groups"):
        ids = [r[0] for r in conn.execute("SELECT track_id FROM dup_members WHERE group_id=? ORDER BY track_id",
                                          (g[0],))]
        extra.update(ids[1:])
    return extra


def stats(conn):
    q = lambda sql, *a: conn.execute(sql, a).fetchone()[0] or 0  # noqa: E731
    live = "FROM tracks WHERE missing=0"
    cat = lambda c: q("SELECT count(DISTINCT i.track_id) FROM issues i JOIN tracks t ON t.id=i.track_id "  # noqa
                      "WHERE t.missing=0 AND i.category=? AND i.severity IN ('error','warning','review')", c)
    s = dict(
        songs=q(f"SELECT count(*) {live}"),
        artists=q(f"SELECT count(DISTINCT lower(artist)) {live} AND artist<>''"),
        albums=q(f"SELECT count(*) FROM (SELECT 1 {live} AND album<>'' "
                 "GROUP BY lower(coalesce(nullif(albumartist,''), artist)), lower(album))"),
        size=q(f"SELECT sum(size) {live}"),
        missing_meta=cat("missing"),
        suspicious=cat("suspicious"),
        duplicates=len(duplicate_extra_ids(conn)),
        missing_art=q(f"SELECT count(*) {live} AND has_art=0 AND readable=1"),
        corrupt=cat("corrupt"),
        offline=q("SELECT count(*) FROM tracks WHERE missing=1"),
    )
    top = lambda col: [(r[0] or "(blank)", r[1]) for r in conn.execute(  # noqa: E731
        f"SELECT {col} AS k, count(*) n {live} GROUP BY lower(k) ORDER BY n DESC LIMIT 10")]
    s["by_artist"] = top("artist")
    s["by_album"] = top("album")
    s["by_genre"] = top("genre")
    s["by_format"] = [(r[0], r[1]) for r in conn.execute(
        f"SELECT format, count(*) n {live} GROUP BY format ORDER BY n DESC")]
    # metadata health: worst severity per track
    sev = {r[0]: r[1] for r in conn.execute(
        "SELECT track_id, max(CASE severity WHEN 'error' THEN 3 WHEN 'warning' THEN 2 WHEN 'review' THEN 1 "
        "ELSE 0 END) FROM issues GROUP BY track_id")}
    ids = [r[0] for r in conn.execute(f"SELECT id {live}")]
    counts = defaultdict(int)
    for i in ids:
        counts[sev.get(i, 0)] += 1
    s["by_health"] = [("Healthy", counts[0]), ("Needs review", counts[1]), ("Missing metadata", counts[2]),
                      ("Corrupted", counts[3])]
    s["scans"] = [dict(r) for r in conn.execute("SELECT * FROM scans ORDER BY id DESC LIMIT 8")]
    return s


def query_tracks(conn, f=None, limit=None):
    """Tracks + issue kinds + MB confidence, filtered by dict f (see keys below)."""
    f = f or {}
    where, args = ["t.missing=0"], []
    if f.get("search"):
        where.append("(t.title LIKE ? OR t.artist LIKE ? OR t.album LIKE ? OR t.albumartist LIKE ? OR t.path LIKE ?)")
        args += [f"%{f['search']}%"] * 5
    for key in ("artist", "album", "genre", "format"):
        if f.get(key):
            where.append(f"t.{key}=?")
            args.append(f[key])
    if f.get("year"):
        where.append("substr(t.year,1,4)=?")
        args.append(str(f["year"]))
    if f.get("min_bitrate"):
        where.append("t.bitrate>=?")
        args.append(int(f["min_bitrate"]) * 1000)
    if f.get("sample_rate"):
        where.append("t.sample_rate=?")
        args.append(int(f["sample_rate"]))
    if f.get("ids") is not None:
        where.append(f"t.id IN ({','.join('?' * len(f['ids']))})")
        args += list(f["ids"])
    if f.get("issue_kind"):
        where.append("EXISTS(SELECT 1 FROM issues i WHERE i.track_id=t.id AND i.kind=?)")
        args.append(f["issue_kind"])
    p = f.get("problem")
    if p in ("missing", "suspicious", "corrupt"):
        where.append("EXISTS(SELECT 1 FROM issues i WHERE i.track_id=t.id AND i.category=? "
                     "AND i.severity<>'info')")
        args.append(p)
    elif p == "art":
        where.append("t.has_art=0 AND t.readable=1")
    elif p == "duplicate":
        where.append("EXISTS(SELECT 1 FROM dup_members d WHERE d.track_id=t.id)")
    elif p == "any":
        where.append("EXISTS(SELECT 1 FROM issues i WHERE i.track_id=t.id AND i.severity<>'info')")
    sql = (f"SELECT t.*, (SELECT group_concat(kind) FROM issues i WHERE i.track_id=t.id) AS issue_kinds, "
           f"m.confidence AS mb, EXISTS(SELECT 1 FROM dup_members d WHERE d.track_id=t.id) AS dup "
           f"FROM tracks t LEFT JOIN mb_matches m ON m.track_id=t.id WHERE {' AND '.join(where)} "
           f"ORDER BY lower(coalesce(nullif(t.albumartist,''), t.artist)), lower(t.album), t.disc, t.track, t.path")
    if limit:
        sql += f" LIMIT {int(limit)}"
    out = []
    for r in conn.execute(sql, args):
        d = dict(r)
        kinds = [k for k in (d["issue_kinds"] or "").split(",") if k]
        d["health"] = health(kinds)
        d["name"] = d["title"] or os.path.basename(d["path"])
        d["issues_text"] = ", ".join(KINDS[k][2] for k in kinds if KINDS[k][1] != "info") or \
            ("Missing artwork" if "missing_art" in kinds else "")
        out.append(d)
    return out


def distinct(conn, col):
    return [r[0] for r in conn.execute(
        f"SELECT DISTINCT {col} FROM tracks WHERE missing=0 AND {col} IS NOT NULL AND {col}<>'' ORDER BY {col}")]
