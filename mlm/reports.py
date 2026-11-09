"""CSV / JSON exports."""
import csv
import json

from . import analysis, duplicates

TRACK_COLS = ["path", "title", "artist", "albumartist", "album", "genre", "year", "track", "track_total", "disc",
              "duration", "format", "codec", "bitrate", "sample_rate", "bits", "channels", "size", "has_art",
              "art_w", "art_h", "readable", "error"]


def _issue_rows(conn, categories):
    return [dict(r) for r in conn.execute(
        f"SELECT t.path, t.artist, t.album, t.title, i.kind, i.category, i.severity, i.detail FROM issues i "
        f"JOIN tracks t ON t.id=i.track_id WHERE t.missing=0 AND i.category IN "
        f"({','.join('?' * len(categories))}) ORDER BY t.artist, t.album, t.track", categories)]


def _quality(conn):
    rows = []
    for r in conn.execute("SELECT path, artist, album, title, format, codec, bitrate, sample_rate, bits, channels, "
                          "duration FROM tracks WHERE missing=0 ORDER BY artist, album, track"):
        d = dict(r)
        br = (d["bitrate"] or 0) // 1000
        if d["format"] in duplicates.LOSSLESS:
            d["quality"] = "Hi-res lossless" if (d["sample_rate"] or 0) > 48000 or (d["bits"] or 0) > 16 \
                else "Lossless"
        elif br >= 256:
            d["quality"] = "High (lossy)"
        elif br >= 160:
            d["quality"] = "Good (lossy)"
        else:
            d["quality"] = "Low (lossy)" if br else "Unknown"
        d["kbps"] = br
        rows.append(d)
    return rows


def _dups(conn):
    rows = []
    for g in duplicates.load_groups(conn):
        for m in g["members"]:
            rows.append(dict(group=g["id"], method=g["method"], filename=m["path"].rsplit("/", 1)[-1],
                             artist=m["artist"], title=m["title"], album=m["album"], format=m["format"],
                             bitrate=m["bitrate"], size=m["size"], path=m["path"], keep=m["keep"]))
    return rows


REPORTS = {
    "Complete music inventory": lambda c: [{k: r[k] for k in TRACK_COLS} for r in analysis.query_tracks(c)],
    "Artist list": lambda c: [dict(r) for r in c.execute("SELECT * FROM artists ORDER BY name")],
    "Album list": lambda c: [dict(r) for r in c.execute("SELECT * FROM albums ORDER BY albumartist, album")],
    "Missing metadata report": lambda c: _issue_rows(c, ["missing", "art"]),
    "Metadata errors report": lambda c: _issue_rows(c, ["suspicious", "corrupt"]),
    "Duplicate report": _dups,
    "Audio quality report": _quality,
}


def export(conn, name, path, fmt=None):
    rows = REPORTS[name](conn)
    fmt = fmt or ("json" if path.lower().endswith(".json") else "csv")
    with open(path, "w", encoding="utf-8", newline="") as fh:
        if fmt == "json":
            json.dump(rows, fh, indent=2, ensure_ascii=False, default=str)
        else:
            cols = list(rows[0].keys()) if rows else ["(no rows)"]
            w = csv.DictWriter(fh, fieldnames=cols)
            w.writeheader()
            w.writerows(rows)
    return len(rows)
