"""Uniform tag + artwork access across mutagen's tag formats (ID3, MP4, Vorbis, APEv2, ASF)."""
import base64
import io

import mutagen
import mutagen.id3
from mutagen.aiff import AIFF
from mutagen.apev2 import BINARY, APEv2File, APEValue
from mutagen.asf import ASF
from mutagen.flac import FLAC, Picture
from mutagen.id3 import APIC
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4, MP4Cover
from mutagen.oggflac import OggFLAC
from mutagen.oggopus import OggOpus
from mutagen.oggvorbis import OggVorbis
from mutagen.wave import WAVE

FIELDS = ("title", "artist", "albumartist", "album", "genre", "year", "track", "disc")

MAPS = {
    "id3": dict(title="TIT2", artist="TPE1", albumartist="TPE2", album="TALB", genre="TCON",
                year="TDRC", track="TRCK", disc="TPOS"),
    "mp4": dict(title="\xa9nam", artist="\xa9ART", albumartist="aART", album="\xa9alb", genre="\xa9gen",
                year="\xa9day", track="trkn", disc="disk"),
    "vorbis": dict(title="title", artist="artist", albumartist="albumartist", album="album", genre="genre",
                   year="date", track="tracknumber", disc="discnumber"),
    "ape": dict(title="Title", artist="Artist", albumartist="Album Artist", album="Album", genre="Genre",
                year="Year", track="Track", disc="Disc"),
    "asf": dict(title="Title", artist="Author", albumartist="WM/AlbumArtist", album="WM/AlbumTitle",
                genre="WM/Genre", year="WM/Year", track="WM/TrackNumber", disc="WM/PartOfSet"),
}


class UnsupportedError(Exception):
    pass


def tag_kind(f):
    if isinstance(f, (MP3, WAVE, AIFF)):
        return "id3"
    if isinstance(f, MP4):
        return "mp4"
    if isinstance(f, (FLAC, OggVorbis, OggOpus, OggFLAC)):
        return "vorbis"
    if isinstance(f, APEv2File):
        return "ape"
    if isinstance(f, ASF):
        return "asf"
    return None


def split_num(s):
    """'3/12' -> (3, 12); '3' -> (3, None); '' -> (None, None)."""
    a, _, b = str(s or "").strip().partition("/")

    def to_int(x):
        try:
            return int(x.strip())
        except ValueError:
            return None
    return to_int(a), (to_int(b) if b else None)


def _delete(t, key):
    if key in t:
        del t[key]


def _get(kind, t, key):
    v = t.get(key)
    if v is None:
        return ""
    if kind == "id3":
        return str(v.text[0]) if getattr(v, "text", None) else ""
    if kind == "mp4":
        if not v:
            return ""
        v = v[0]
        if isinstance(v, tuple):
            n, tot = v
            return f"{n}/{tot}" if tot else str(n)
        return str(v)
    if kind == "vorbis":
        return str(v[0]) if v else ""
    if kind == "ape":
        return str(v)
    if kind == "asf":
        return str(v[0].value) if v else ""
    return ""


def read_tags(f) -> dict:
    out = {k: "" for k in FIELDS}
    kind = tag_kind(f)
    t = f.tags
    if kind is None or t is None:
        return out
    for field, key in MAPS[kind].items():
        out[field] = _get(kind, t, key).strip()
    if kind == "vorbis":
        if not out["year"] and t.get("year"):
            out["year"] = t["year"][0]
        for fld, totals in (("track", ("tracktotal", "totaltracks")), ("disc", ("disctotal", "totaldiscs"))):
            if out[fld] and "/" not in out[fld]:
                tot = next((t[k][0] for k in totals if t.get(k)), "")
                if tot:
                    out[fld] = f"{out[fld]}/{tot}"
    return out


def write_tags(f, changes: dict):
    """changes: {field: value}; empty value deletes the tag. Caller saves."""
    changes = {k: v for k, v in changes.items() if k != "artwork"}
    if not changes:
        return
    kind = tag_kind(f)
    if kind is None:
        raise UnsupportedError(f"Writing tags is not supported for {type(f).__name__} files")
    if f.tags is None:
        f.add_tags()
    t = f.tags
    for field, val in changes.items():
        key = MAPS[kind][field]
        val = (val or "").strip()
        if kind == "id3":
            t.delall(key)
            if val:
                t.add(getattr(mutagen.id3, key)(encoding=3, text=[val]))
        elif kind == "mp4":
            if not val:
                _delete(t, key)
            elif key in ("trkn", "disk"):
                n, tot = split_num(val)
                if tot is None:
                    old = t.get(key)
                    tot = old[0][1] if old else 0
                t[key] = [(n or 0, tot or 0)]
            else:
                t[key] = [val]
        elif kind == "vorbis":
            if not val:
                _delete(t, key)
            elif key in ("tracknumber", "discnumber") and "/" in val:
                n, tot = val.split("/", 1)
                t[key] = [n]
                t["tracktotal" if key == "tracknumber" else "disctotal"] = [tot]
            else:
                t[key] = [val]
        elif kind == "ape":
            if val:
                t[key] = val
            else:
                _delete(t, key)
        elif kind == "asf":
            if val:
                t[key] = [val]
            else:
                _delete(t, key)


def sniff_mime(data: bytes) -> str:
    if data[:4] == b"\x89PNG":
        return "image/png"
    if data[:3] == b"GIF":
        return "image/gif"
    return "image/jpeg"


def _utf16z(raw: bytes, pos: int):
    end = pos
    while end + 1 < len(raw) and raw[end:end + 2] != b"\x00\x00":
        end += 2
    return raw[pos:end].decode("utf-16-le", "replace"), end + 2


def get_art(f):
    """Return (bytes, mime) of the front cover (or first picture), else None."""
    if isinstance(f, FLAC) and f.pictures:
        p = sorted(f.pictures, key=lambda p: p.type != 3)[0]
        return p.data, p.mime or sniff_mime(p.data)
    kind, t = tag_kind(f), f.tags
    if t is None:
        return None
    if kind == "id3":
        aps = t.getall("APIC")
        if aps:
            a = sorted(aps, key=lambda a: a.type != 3)[0]
            return a.data, a.mime if "/" in (a.mime or "") else sniff_mime(a.data)
    elif kind == "mp4":
        c = t.get("covr")
        if c:
            return bytes(c[0]), "image/png" if c[0].imageformat == MP4Cover.FORMAT_PNG else "image/jpeg"
    elif kind == "vorbis":
        for b in t.get("metadata_block_picture", []):
            try:
                p = Picture(base64.b64decode(b))
                return p.data, p.mime or sniff_mime(p.data)
            except Exception:
                continue
    elif kind == "ape":
        v = t.get("Cover Art (Front)")
        if v is not None:
            img = v.value.partition(b"\x00")[2]
            return img, sniff_mime(img)
    elif kind == "asf":
        for v in t.get("WM/Picture", []):
            raw = v.value
            size = int.from_bytes(raw[1:5], "little")
            mime, pos = _utf16z(raw, 5)
            _, pos = _utf16z(raw, pos)
            return raw[pos:pos + size], mime or sniff_mime(raw[pos:pos + size])
    return None


def _picture(data, mime):
    from PIL import Image
    p = Picture()
    p.type, p.mime, p.desc, p.data = 3, mime, "Cover", data
    try:
        im = Image.open(io.BytesIO(data))
        p.width, p.height = im.size
        p.depth = 24
    except Exception:
        pass
    return p


def set_art(f, data, mime):
    """Replace embedded cover with (data, mime); data=None removes it. Caller saves."""
    kind = tag_kind(f)
    if kind in (None, "asf"):
        raise UnsupportedError(f"Writing artwork is not supported for {type(f).__name__} files")
    if isinstance(f, FLAC):
        f.clear_pictures()
        if data:
            f.add_picture(_picture(data, mime))
        return
    if f.tags is None:
        f.add_tags()
    t = f.tags
    if kind == "id3":
        t.delall("APIC")
        if data:
            t.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=data))
    elif kind == "mp4":
        if data:
            fmt = MP4Cover.FORMAT_PNG if mime == "image/png" else MP4Cover.FORMAT_JPEG
            t["covr"] = [MP4Cover(data, imageformat=fmt)]
        else:
            _delete(t, "covr")
    elif kind == "vorbis":
        if data:
            t["metadata_block_picture"] = [base64.b64encode(_picture(data, mime).write()).decode("ascii")]
        else:
            _delete(t, "metadata_block_picture")
    elif kind == "ape":
        if data:
            t["Cover Art (Front)"] = APEValue(b"Cover Art (Front).jpg\x00" + data, BINARY)
        else:
            _delete(t, "Cover Art (Front)")


def save(f):
    if isinstance(f, MP3) and f.tags is not None and getattr(f.tags, "version", (2, 4))[1] == 3:
        f.save(v2_version=3)  # keep files that were ID3v2.3 as v2.3 (best compatibility with old players)
    else:
        f.save()


def raw_tags(f) -> list:
    """Every tag key/value for the inspector, binary values summarised."""
    out = []
    if isinstance(f, FLAC):
        for p in f.pictures:
            out.append(("PICTURE", f"{p.mime} {p.width}x{p.height} ({len(p.data):,} bytes)"))
    if f.tags is None:
        return out
    try:
        items = list(f.tags.items())
    except Exception:
        return out
    for k, v in items:
        if isinstance(v, (list, tuple)):
            v = ", ".join(_short(x) for x in v)
        out.append((str(k), _short(v)))
    return sorted(out)


def _short(v):
    data = getattr(v, "data", None)
    if isinstance(v, (bytes, bytearray)) or isinstance(data, (bytes, bytearray)):
        n = len(v if isinstance(v, (bytes, bytearray)) else data)
        return f"<binary {n:,} bytes>"
    s = str(v)
    if len(s) > 300:
        s = s[:300] + "…"
    return s
