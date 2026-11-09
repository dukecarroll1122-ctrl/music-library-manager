"""Folder/file naming proposals. Planning is read-only; applying goes through repair.move_files."""
import os
import re
from collections import Counter

PRESETS = [
    "{albumartist}/{album}/{track:02d} - {title}",
    "{albumartist}/{year} - {album}/{track:02d} - {title}",
    "{albumartist}/{album}/{disc}-{track:02d} - {title}",
    "{genre}/{albumartist}/{album}/{track:02d} - {title}",
    "{artist}/{album}/{track:02d} {artist} - {title}",
]
FIELDS_HELP = "{artist} {albumartist} {album} {title} {track} {track:02d} {disc} {year} {genre}"


def clean(part, maxlen=120):
    # characters that are invalid on FAT/exFAT SD cards or confuse macOS
    part = re.sub(r'[/\\:*?"<>|\x00-\x1f]', "_", part).strip().rstrip(".")
    part = part.lstrip(".") or "_"
    return part[:maxlen].strip()


def render(template, t):
    ext = os.path.splitext(t["path"])[1].lower()
    artist = t["artist"] or t["albumartist"] or "Unknown Artist"
    values = dict(artist=artist, albumartist=t["albumartist"] or artist, album=t["album"] or "Unknown Album",
                  title=t["title"] or os.path.splitext(os.path.basename(t["path"]))[0],
                  track=t["track"] or 0, disc=t["disc"] or 1, year=(t["year"] or "")[:4] or "0000",
                  genre=t["genre"] or "Unknown Genre")
    values = {k: clean(v) if isinstance(v, str) else v for k, v in values.items()}  # "AC/DC" must not nest
    try:
        rel = template.format(**values)
    except (KeyError, ValueError, IndexError) as e:
        raise ValueError(f"Invalid naming template: {e}. Available fields: {FIELDS_HELP}")
    parts = [clean(p) for p in rel.split("/") if p.strip()]
    return os.path.join(*parts) + ext


def plan(tracks, dest_root, template):
    """Returns [{id, src, dst, status}] where status is ok | unchanged | conflict | duplicate target."""
    out = []
    for t in tracks:
        dst = os.path.join(dest_root, render(template, t))
        out.append(dict(id=t["id"], src=t["path"], dst=dst, status="ok"))
    counts = Counter(p["dst"].casefold() for p in out)  # macOS volumes are usually case-insensitive
    for p in out:
        if p["src"] == p["dst"]:
            p["status"] = "unchanged"
        elif counts[p["dst"].casefold()] > 1:
            p["status"] = "conflict: several files map here"
        elif os.path.exists(p["dst"]) and not (os.path.exists(p["src"]) and os.path.samefile(p["src"], p["dst"])):
            p["status"] = "conflict: file exists"
        elif not os.path.exists(p["src"]):
            p["status"] = "source missing"
    return out


def common_root(paths):
    try:
        return os.path.commonpath([os.path.dirname(p) for p in paths]) if paths else ""
    except ValueError:
        return ""
