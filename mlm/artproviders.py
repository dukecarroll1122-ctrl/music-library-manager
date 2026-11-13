"""Independent artwork providers. A provider turns a search (artmatch.Sub) into raw candidate dicts; scoring,
fallback strategies and thumbnails are handled by artsearch. One provider failing never affects the others.

Terms of use respected here: official APIs only (no scraping), identifying User-Agent, per-host rate limits
(online.INTERVAL), a 30-day cache for API responses, and credentials only where a service requires them
(Discogs personal token, fanart.tv personal key).
"""
import json
import re
import threading
import urllib.parse

from . import artmatch as am
from . import db, online

DEFAULT_ORDER = ["musicbrainz", "itunes", "deezer", "discogs", "fanart"]


class Ctx:
    """Per-search context. HTTP responses are cached in SQLite through a connection owned by the calling thread."""

    def __init__(self, db_path, fresh=False):
        self.db_path, self.fresh = db_path, fresh
        self._local = threading.local()
        self.ua = online.user_agent(self.conn())

    @classmethod
    def from_conn(cls, conn, fresh=False):
        return cls(conn.execute("PRAGMA database_list").fetchone()[2], fresh)

    def conn(self):
        if not hasattr(self._local, "c"):
            self._local.c = db.connect(self.db_path)
        return self._local.c

    def setting(self, key, default=""):
        return db.get_setting(self.conn(), key, default)

    def json(self, url, headers=None):
        return online.cached_json(self.conn(), url, self.ua, headers, 0 if self.fresh else None)


def q(s):
    """Strip characters that break provider query syntax."""
    return re.sub(r"\s+", " ", re.sub(r'["\\]', " ", s or "")).strip()


class Provider:
    name, label, needs, home = "", "", "", ""
    state_note = ""

    def configured(self, ctx):
        return not self.needs or bool(ctx.setting(self.needs).strip())

    def headers(self, ctx):
        return {}

    def search(self, ctx, sub, want):
        raise NotImplementedError

    def thumb_candidates(self, cand):
        return [cand["thumb_url"]] if cand.get("thumb_url") else []

    def image_url(self, cand, target=0):
        return cand.get("full_url")

    def check(self, ctx):
        raise NotImplementedError


# ------------------------------------------------------------------ MusicBrainz + Cover Art Archive
def credit_names(ac):
    """Credited names plus MusicBrainz sort names in both orders: 山下達郎, Yamashita, Tatsuro, Tatsuro Yamashita."""
    names = [online.credit(ac)]
    for x in ac or []:
        names.append(x.get("name", ""))
        sort = (x.get("artist") or {}).get("sort-name") or ""
        if sort:
            names.append(sort)
            if ", " in sort:
                last, first = sort.split(", ", 1)
                names.append(f"{first} {last}")
    return [n for n in dict.fromkeys(names) if n]


class MusicBrainz(Provider):
    name, label, home = "musicbrainz", "MusicBrainz / Cover Art Archive", "https://musicbrainz.org"

    @staticmethod
    def query(sub):
        parts = []
        if sub.album:
            parts.append(f'release:"{online.esc(q(sub.album))}"')
        if sub.artist:
            parts.append(f'artist:"{online.esc(q(sub.artist))}"')
        if sub.year:
            parts.append(f"date:{sub.year[:4]}")
        if sub.catalog:
            parts.append(f'catno:"{online.esc(q(sub.catalog))}"')
        if sub.region:
            parts.append(f"country:{sub.region}")
        if sub.terms == "soundtrack":
            parts.append("secondarytype:soundtrack")
        elif sub.terms:
            parts.append(online.esc(q(sub.terms)))
        return " AND ".join(parts)

    def release_groups(self, ctx, sub, want):
        """Search releases and keep one per release group, preferring the user's own release, then Official, then
        the earliest date (the original edition)."""
        query = self.query(sub)
        if not query:
            return []
        data = ctx.json(f"{online.MB}/release?query={urllib.parse.quote(query)}&fmt=json&limit=25")
        best = {}
        mine = str(want.get("mb_release_id") or "").lower()
        for r in data.get("releases", []):
            rg = r.get("release-group") or {}
            if not rg.get("id"):
                continue
            key = (r.get("id", "").lower() == mine, r.get("status") == "Official",
                   -(int((r.get("date") or "9999")[:4]) if (r.get("date") or "")[:4].isdigit() else 9999))
            if rg["id"] not in best or key > best[rg["id"]][0]:
                best[rg["id"]] = (key, r)
        return [r for _, r in sorted(best.values(), key=lambda x: x[0], reverse=True)]

    def search(self, ctx, sub, want):
        out = []
        for r in self.release_groups(ctx, sub, want)[:12]:
            rg = r["release-group"]
            sec = rg.get("secondary-types") or []
            li = (r.get("label-info") or [{}])[0]
            title = r.get("title", "")
            kind, edition = am.edition_of(title, sec, r.get("disambiguation", ""))
            exact = r.get("id", "").lower() == str(want.get("mb_release_id") or "").lower()
            out.append(dict(
                provider=self.name, title=title, artist=online.credit(r.get("artist-credit")),
                names=credit_names(r.get("artist-credit")), year=(r.get("date") or "")[:4],
                tracks=r.get("track-count") or 0, country=r.get("country", ""), catalog=li.get("catalog-number") or "",
                release_id=r.get("id"), rg_id=rg["id"], kind=kind, edition=edition,
                rtype=" + ".join(x for x in [rg.get("primary-type") or ""] + sec if x),
                art_scope="release" if exact else "release-group"))
        return out

    def _base(self, cand, scope=None):
        scope = scope or cand.get("art_scope", "release-group")
        return f"{online.CAA}/{scope}/{cand['release_id'] if scope == 'release' else cand['rg_id']}"

    def thumb_candidates(self, cand):
        first = cand.get("art_scope", "release-group")
        other = "release" if first == "release-group" else "release-group"
        return [self._base(cand, first) + "/front-250", self._base(cand, other) + "/front-250"]

    def image_url(self, cand, target=0):
        # the smallest published size that is at least the target; the original when none is big enough
        size = next((s for s in (250, 500, 1200) if target and s >= target), None)
        scope = cand.get("art_scope_found") or cand.get("art_scope", "release-group")
        return self._base(cand, scope) + (f"/front-{size}" if size else "/front")

    def check(self, ctx):
        try:
            ctx.json(f"{online.MB}/release?query=release:test&fmt=json&limit=1", None)
            online.http(f"{online.CAA}/release-group/6b0a0b88-fd3b-3a1c-a8b0-eb0ae9d3a2f3", ctx.ua)
        except online.NetworkError as e:
            return "error", str(e)
        return "ok", "MusicBrainz and Cover Art Archive reachable (no key needed)"


# ------------------------------------------------------------------ iTunes Search
class ITunes(Provider):
    name, label, home = "itunes", "iTunes Search", "https://performance-partners.apple.com/search-api"
    URL = "https://itunes.apple.com/search"

    def search(self, ctx, sub, want):
        term = " ".join(x for x in (q(sub.artist), q(sub.album), q(sub.terms)) if x)
        if not term:
            return []
        country = sub.region or ("JP" if am.has_japanese(term) else "US")
        data = ctx.json(f"{self.URL}?{urllib.parse.urlencode(dict(term=term, entity='album', limit=12, country=country))}")
        out = []
        for r in data.get("results", []):
            art = r.get("artworkUrl100") or ""
            if not art:
                continue
            title = r.get("collectionName", "")
            kind, edition = am.edition_of(title, ["Soundtrack"] if "soundtrack" in (r.get("primaryGenreName") or "").lower()
                                          else [], "")
            out.append(dict(provider=self.name, title=title, artist=r.get("artistName", ""),
                            names=[r.get("artistName", "")], year=(r.get("releaseDate") or "")[:4],
                            tracks=r.get("trackCount") or 0, country=country, release_id=str(r.get("collectionId", "")),
                            kind=kind, edition=edition, rtype=r.get("primaryGenreName", ""),
                            thumb_url=art.replace("100x100bb", "300x300bb"), art_base=art.replace("100x100bb", "{n}x{n}bb")))
        return out

    def image_url(self, cand, target=0):
        # Apple serves at most the original size, so asking for more than exists returns the original unscaled
        return cand["art_base"].format(n=target or 5000)

    def check(self, ctx):
        try:
            ctx.json(f"{self.URL}?term=test&entity=album&limit=1")
        except online.NetworkError as e:
            return "error", str(e)
        return "ok", "iTunes Search reachable (no key needed; about 20 requests per minute)"


# ------------------------------------------------------------------ Deezer
class Deezer(Provider):
    name, label, home = "deezer", "Deezer", "https://developers.deezer.com/api"
    URL = "https://api.deezer.com/search/album"

    def search(self, ctx, sub, want):
        if sub.region:
            return []  # Deezer's catalogue isn't region-specific in search; skip the repeat
        if sub.artist and sub.album:
            query = f'artist:"{q(sub.artist)}" album:"{q(sub.album)}"'
        else:
            query = " ".join(x for x in (q(sub.artist), q(sub.album), q(sub.terms)) if x)
        if not query:
            return []
        data = ctx.json(f"{self.URL}?{urllib.parse.urlencode(dict(q=query, limit=12))}")
        out = []
        for r in data.get("data", []):
            title = r.get("title", "")
            kind, edition = am.edition_of(title, [], "Single" if r.get("record_type") == "single" else "")
            out.append(dict(provider=self.name, title=title, artist=(r.get("artist") or {}).get("name", ""),
                            names=[(r.get("artist") or {}).get("name", "")], year="", tracks=r.get("nb_tracks") or 0,
                            release_id=str(r.get("id", "")), kind=kind, edition=edition,
                            rtype=(r.get("record_type") or "").title(), thumb_url=r.get("cover_medium"),
                            cover_big=r.get("cover_big"), cover_xl=r.get("cover_xl")))
        return out

    def image_url(self, cand, target=0):
        return (cand.get("cover_big") if target and target <= 500 else cand.get("cover_xl")) or cand.get("cover_xl")

    def check(self, ctx):
        try:
            ctx.json(f"{self.URL}?q=test&limit=1")
        except online.NetworkError as e:
            return "error", str(e)
        return "ok", "Deezer reachable (no key needed; covers are served up to 1000×1000)"


# ------------------------------------------------------------------ Discogs
class Discogs(Provider):
    name, label, needs, home = "discogs", "Discogs", "discogs_token", "https://www.discogs.com/settings/developers"
    URL = "https://api.discogs.com"
    COUNTRY = {"JP": "Japan", "US": "US", "GB": "UK"}

    def headers(self, ctx):
        return {"Authorization": f"Discogs token={ctx.setting('discogs_token').strip()}"}

    def search(self, ctx, sub, want):
        params = dict(type="release", per_page=12)
        if sub.artist and sub.album:
            params.update(artist=q(sub.artist), release_title=q(sub.album))
        else:
            params["q"] = " ".join(x for x in (q(sub.artist), q(sub.album)) if x)
        if sub.year:
            params["year"] = sub.year[:4]
        if sub.catalog:
            params["catno"] = sub.catalog
        if sub.region:
            params["country"] = self.COUNTRY.get(sub.region, sub.region)
        if want.get("mode") == "game" or sub.terms == "soundtrack":
            params["style"] = "Video Game Music" if want.get("mode") == "game" else "Soundtrack"
        elif sub.terms:
            params["q"] = (params.get("q", "") + " " + q(sub.terms)).strip()
        if not (params.get("q") or params.get("artist") or params.get("release_title") or params.get("catno")):
            return []
        data = ctx.json(f"{self.URL}/database/search?{urllib.parse.urlencode(params)}", self.headers(ctx))
        out = []
        for r in data.get("results", []):
            img = r.get("cover_image") or ""
            if not img or "spacer.gif" in img:
                continue
            artist, _, title = (r.get("title") or "").partition(" - ")
            if not title:
                artist, title = "", r.get("title", "")
            fmt = " ".join(r.get("format") or [])
            kind, edition = am.edition_of(title, [], fmt + " " + " ".join(r.get("style") or []))
            out.append(dict(provider=self.name, title=title, artist=artist, names=[re.sub(r"\s*\(\d+\)$", "", artist)],
                            year=str(r.get("year") or ""), country=r.get("country", ""),
                            catalog=r.get("catno") or "", release_id=str(r.get("id", "")), kind=kind,
                            edition=edition or fmt, rtype=", ".join(r.get("format") or [])[:60],
                            thumb_url=img, full_url=img))
        return out

    def check(self, ctx):
        if not self.configured(ctx):
            return "needs-key", "Needs a Discogs personal access token (Settings)"
        try:
            ctx.json(f"{self.URL}/oauth/identity", self.headers(ctx))  # fresh, not cached: the token may have changed
        except online.NetworkError as e:
            return ("invalid-key", "Discogs rejected the token") if e.code in (401, 403) else ("error", str(e))
        return "ok", "Discogs token accepted (about 60 requests per minute)"


# ------------------------------------------------------------------ fanart.tv
class Fanart(Provider):
    name, label, needs, home = "fanart", "fanart.tv (fan artwork)", "fanart_key", "https://fanart.tv/get-an-api-key/"
    URL = "https://webservice.fanart.tv/v3/music/albums"
    _mb = MusicBrainz()

    def search(self, ctx, sub, want):
        if sub.region:
            return []
        out = []
        for r in self._mb.release_groups(ctx, sub, want)[:3]:
            rg = r["release-group"]
            if am.title_sim(sub.album or want.get("album") or "", r.get("title", ""))[0] < 0.6 and want.get("mode") != "game":
                continue
            data = ctx.json(f"{self.URL}/{rg['id']}?{urllib.parse.urlencode(dict(api_key=ctx.setting('fanart_key').strip()))}")
            albums = (data.get("albums") or {}).get(rg["id"]) or {}
            for img in sorted(albums.get("albumcover") or [], key=lambda i: -int(i.get("likes") or 0))[:3]:
                url = img.get("url") or ""
                if not url:
                    continue
                out.append(dict(provider=self.name, title=r.get("title", ""), artist=online.credit(r.get("artist-credit")),
                                names=credit_names(r.get("artist-credit")), year=(r.get("date") or "")[:4],
                                tracks=r.get("track-count") or 0, release_id=r.get("id"), rg_id=rg["id"],
                                kind="fan", edition="Fan-uploaded cover", rtype=f"{img.get('likes', 0)} likes",
                                thumb_url=url.replace("/fanart/", "/preview/"), full_url=url))
        return out

    def check(self, ctx):
        if not self.configured(ctx):
            return "needs-key", "Needs a fanart.tv personal API key (Settings)"
        try:
            online.http(f"{self.URL}/6b0a0b88-fd3b-3a1c-a8b0-eb0ae9d3a2f3?api_key="
                        f"{urllib.parse.quote(ctx.setting('fanart_key').strip())}", ctx.ua)
        except online.NetworkError as e:
            return ("invalid-key", "fanart.tv rejected the key") if e.code in (401, 403) else ("error", str(e))
        return "ok", "fanart.tv key accepted"


PROVIDERS = {p.name: p for p in (MusicBrainz(), ITunes(), Deezer(), Discogs(), Fanart())}


def load_config(conn):
    """{'order': [names], 'disabled': [names]} from Settings, always containing every provider once."""
    try:
        cfg = json.loads(db.get_setting(conn, "art_providers", "") or "{}")
    except ValueError:
        cfg = {}
    order = [n for n in cfg.get("order", []) if n in PROVIDERS]
    order += [n for n in DEFAULT_ORDER if n not in order]
    return {"order": order, "disabled": [n for n in cfg.get("disabled", []) if n in PROVIDERS]}


def save_config(conn, order, disabled):
    db.set_setting(conn, "art_providers", json.dumps({"order": order, "disabled": list(disabled)}))


def active(conn, only=None):
    """Providers to search, in priority order: enabled and configured (a missing key is reported, not an error)."""
    cfg = load_config(conn)
    return [PROVIDERS[n] for n in cfg["order"] if n not in cfg["disabled"] and (not only or n in only)]
