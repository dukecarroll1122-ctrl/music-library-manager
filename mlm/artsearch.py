"""Artwork search orchestration: every provider in parallel (or in priority order), fallback strategies, scoring,
thumbnails and real image resolutions. Also image download/cache, validation and preparation for embedding."""
import hashlib
import io
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from . import artmatch as am
from . import artproviders as ap
from . import db, online

MAX_IMAGE_BYTES = 40 * 1024 * 1024
FORMATS = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}
KIND_TIER = {"fan": 2, "promo": 1}


class ImageError(ValueError):
    pass


# ---------------------------------------------------------------- images
def inspect_image(data):
    """Validate image bytes. Returns dict(w, h, fmt, mime, warnings). Raises ImageError for unusable files.
    Non-square and low-resolution images are allowed but come with warnings."""
    from PIL import Image
    if not data:
        raise ImageError("The file is empty")
    try:
        im = Image.open(io.BytesIO(data))
        fmt = (im.format or "").upper()
        im.verify()
        im = Image.open(io.BytesIO(data))
        im.load()
    except Exception as e:
        raise ImageError(f"Not a readable image ({type(e).__name__}: {e})")
    if fmt not in FORMATS:
        raise ImageError(f"{fmt or 'This'} images aren't supported — use JPG, PNG or WebP")
    w, h = im.size
    warns = []
    if min(w, h) < 300:
        warns.append(f"Very small ({w}×{h}). It will look blurry in players; 500×500 or more is recommended.")
    elif min(w, h) < 500:
        warns.append(f"Small ({w}×{h}). 500×500 or more is recommended.")
    ratio = max(w, h) / max(1, min(w, h))
    if ratio > 1.4:
        warns.append(f"Not square ({w}×{h}) — probably a booklet scan, banner or poster rather than a cover.")
    elif ratio > 1.03:
        warns.append(f"Not exactly square ({w}×{h}). Players may crop it; that's normal for some original artwork.")
    return dict(w=w, h=h, fmt=fmt, mime=FORMATS[fmt], warnings=warns, mode=im.mode, bytes=len(data))


def prepare(data, max_side=0):
    """Image bytes ready to embed: (bytes, mime, (w, h)). JPEG and PNG are kept byte-for-byte; WebP becomes PNG
    (lossless) and CMYK becomes RGB so every player can show it. Larger than max_side is scaled down — never up."""
    from PIL import Image
    info = inspect_image(data)
    im = Image.open(io.BytesIO(data))
    im.load()
    keep = info["fmt"] in ("JPEG", "PNG") and info["mode"] in ("RGB", "RGBA", "L", "P", "LA") \
        and not (max_side and max(info["w"], info["h"]) > max_side)
    if keep:
        return data, info["mime"], (info["w"], info["h"])
    if max_side and max(im.size) > max_side:
        im.thumbnail((max_side, max_side), Image.LANCZOS)
    buf = io.BytesIO()
    if info["fmt"] == "PNG" or info["fmt"] == "WEBP" and "A" in im.mode:
        im.save(buf, "PNG", optimize=True)
        return buf.getvalue(), "image/png", im.size
    if info["fmt"] == "WEBP":
        im.save(buf, "PNG", optimize=True)  # lossless: WebP → JPEG would add another round of compression
        return buf.getvalue(), "image/png", im.size
    im.convert("RGB").save(buf, "JPEG", quality=95, subsampling=0)
    return buf.getvalue(), "image/jpeg", im.size


def to_jpeg(data):
    """JPEG bytes for folder.jpg / cover.jpg. JPEG sources are written unchanged."""
    from PIL import Image
    info = inspect_image(data)
    if info["fmt"] == "JPEG" and info["mode"] in ("RGB", "L"):
        return data
    im = Image.open(io.BytesIO(data))
    im.load()
    if "A" in im.mode or im.mode == "P":
        bg = Image.new("RGB", im.size, "white")
        bg.paste(im.convert("RGBA"), mask=im.convert("RGBA").split()[-1])
        im = bg
    buf = io.BytesIO()
    im.convert("RGB").save(buf, "JPEG", quality=95, subsampling=0)
    return buf.getvalue()


def cache_dir():
    d = db.data_dir() / "art_cache"
    d.mkdir(exist_ok=True)
    return d


def fetch_image(ctx, url, headers=None):
    """Download an image once; later requests read the cache. Returns bytes."""
    c = ctx.conn()
    row = c.execute("SELECT path FROM art_images WHERE url=?", (url,)).fetchone()
    if row and os.path.isfile(row["path"]) and not ctx.fresh:
        with open(row["path"], "rb") as fh:
            return fh.read()
    data = online.http(url, ctx.ua, headers=headers, timeout=60, max_bytes=MAX_IMAGE_BYTES)
    if not data:
        raise online.NetworkError("Image not available")
    info = inspect_image(data)
    path = str(cache_dir() / hashlib.sha1(url.encode()).hexdigest())
    with open(path, "wb") as fh:
        fh.write(data)
    c.execute("INSERT OR REPLACE INTO art_images VALUES(?,?,?,?,?,?,?)",
              (url, path, info["w"], info["h"], len(data), info["mime"], time.time()))
    c.commit()
    return data


def probe_size(ctx, url, headers=None):
    """(w, h) of a remote image from its first 64 KB (HTTP Range), cached. None when unknown."""
    from PIL import ImageFile
    c = ctx.conn()
    row = c.execute("SELECT w, h FROM art_probe WHERE url=?", (url,)).fetchone()
    if row and row["w"]:
        return row["w"], row["h"]
    try:
        data = online.http(url, ctx.ua, headers={**(headers or {}), "Range": "bytes=0-65535"}, timeout=30,
                           max_bytes=65536, truncate=True)
    except online.NetworkError:
        return None
    parser = ImageFile.Parser()
    try:
        parser.feed(data or b"")
    except Exception:
        pass
    if not parser.image:
        return None
    w, h = parser.image.size
    c.execute("INSERT OR REPLACE INTO art_probe VALUES(?,?,?,?)", (url, w, h, time.time()))
    c.commit()
    return w, h


# ---------------------------------------------------------------- search
def _key(c):
    return (c["provider"], c.get("release_id") or am.fold(f"{c['title']} {c['artist']} {c.get('year', '')}"),
            c.get("full_url") or "")


def _tier(c):
    w, h = c.get("width") or 0, c.get("height") or 0
    if not w:
        return 2
    return 0 if min(w, h) >= 1000 else 1 if min(w, h) >= 500 else 3


def sort_key(c, order):
    return (am.RANK[c["confidence"]], KIND_TIER.get(c.get("kind"), 0), _tier(c), order.get(c["provider"], 9),
            -c.get("score", 0))


def _resolve_thumb(ctx, p, c):
    for url in p.thumb_candidates(c):
        try:
            c["thumb"] = fetch_image(ctx, url, p.headers(ctx) if p.needs else None)
        except (online.NetworkError, ImageError):
            continue
        if p.name == "musicbrainz":
            c["art_scope_found"] = "release" if "/release/" in url else "release-group"
        return True
    return False


def search_album(ctx, want, providers=None, mode="all", progress=None, cancel=None, max_strategies=10,
                 target=0, keep=6):
    """Find cover candidates for one album. mode 'all' queries every provider at once; 'priority' goes through
    them in order and stops at the first provider with a Strong/Exact match (fewer API calls).
    Returns dict(cands, status, strategies). status[provider] = dict(state, msg, tried, found)."""
    provs = providers if providers is not None else ap.active(ctx.conn())
    subs = am.strategies(want, max_strategies)
    order = {p.name: i for i, p in enumerate(provs)}
    status = {p.name: dict(state="pending", msg="", tried=0, found=0, label=p.label) for p in provs}
    found, lock = [], threading.Lock()
    used = []

    def run(p):
        st = status[p.name]
        if not p.configured(ctx):
            st.update(state="needs-key", msg="Needs credentials — add them in Settings")
            return
        mine = {}
        try:
            for sub in subs:
                if cancel and cancel():
                    break
                st["tried"] += 1
                if sub.name not in used:
                    used.append(sub.name)
                if progress:
                    progress(f"{p.label}: {sub.name}")
                scored = []
                for c in p.search(ctx, sub, want):
                    score, conf, reasons = am.judge(want, c)
                    if conf is None:
                        continue
                    c.update(score=score, confidence=conf, reasons=reasons, strategy=sub.name)
                    if _key(c) not in mine or sort_key(c, order) < sort_key(mine[_key(c)], order):
                        scored.append(c)
                scored.sort(key=lambda c: sort_key(c, order))
                for c in scored[:keep]:
                    if "thumb" not in c and not _resolve_thumb(ctx, p, c):
                        continue  # no artwork behind this record
                    mine[_key(c)] = c
                if any(c["confidence"] in am.SAFE for c in mine.values()):
                    break
            st.update(state="ok" if mine else "empty", found=len(mine),
                      msg="" if mine else f"No results after {st['tried']} search(es)")
        except online.NetworkError as e:
            invalid = e.code in (401, 403)
            st.update(state="invalid-key" if invalid else "error", msg=str(e), found=len(mine))
        with lock:
            found.extend(mine.values())

    if mode == "priority":
        for p in provs:
            run(p)
            if any(c["confidence"] in am.SAFE for c in found) or (cancel and cancel()):
                break
        for p in provs:
            if status[p.name]["state"] == "pending":
                status[p.name].update(state="skipped", msg="Not needed: an earlier source already matched")
    elif provs:
        with ThreadPoolExecutor(len(provs)) as ex:
            list(ex.map(run, provs))

    found.sort(key=lambda c: sort_key(c, order))
    prov_by = {p.name: p for p in provs}
    for c in found[:16]:  # real resolutions for the best candidates
        p = prov_by[c["provider"]]
        c["full_url"] = p.image_url(c, target)
        size = probe_size(ctx, c["full_url"], p.headers(ctx) if p.needs else None)
        if size:
            c["width"], c["height"] = size
    found.sort(key=lambda c: sort_key(c, order))
    for c in found[16:]:
        c["full_url"] = prov_by[c["provider"]].image_url(c, target)
    return dict(cands=found, status=status, strategies=used)
