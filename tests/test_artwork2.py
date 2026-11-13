"""Artwork Manager 2.0: matching engine, providers, multi-source search, manual import, apply options, queue.
Everything runs on generated files and simulated services; no network and none of your music."""
import hashlib
import io
import json
import os

import mutagen
import pytest
from PIL import Image

from conftest import audio_md5, make_audio, needs_ffmpeg, png_bytes
from mlm import artfetch, artmatch as am, artproviders as ap, artsearch, db, online, repair, scanner, tags

JPEG = io.BytesIO()
Image.new("RGB", (1200, 1200), (30, 90, 160)).save(JPEG, "JPEG", quality=92)
JPEG = JPEG.getvalue()
WEBP = io.BytesIO()
Image.new("RGB", (800, 800), (160, 60, 20)).save(WEBP, "WEBP")
WEBP = WEBP.getvalue()


def img(fmt, size, color=(10, 10, 10)):
    b = io.BytesIO()
    Image.new("RGB", size, color).save(b, fmt)
    return b.getvalue()


# ======================================================================== normalisation & matching (pure)
def test_normalisation():
    assert am.fold("Beyoncé – Lemonade!") == "beyonce lemonade"
    assert am.fold("Wu‐Tang (36 Chambers)") == "wu tang 36 chambers"
    assert am.fold("ＰＥＲＳＯＮＡ　５") == "persona 5"                      # full-width characters
    assert am.fold("ペルソナ") == am.fold("ぺるそな")                          # katakana == hiragana
    assert am.fold("Ice Cube & Dr. Dre") == "ice cube and dr dre"
    assert am.romaji("ペルソナ3") == "perusona 3"


@pytest.mark.parametrize("a,b,expect_high", [
    ("Persona 5 (Original Soundtrack)", "Persona 5 Original Soundtrack", True),
    ("Illmatic", "Illmatic (Deluxe Edition)", True),
    ("AIZO - Single", "AIZO", True),
    ("Enter The Wu-Tang (36 Chambers) [Expanded Edition]", "Enter the Wu‐Tang (36 Chambers)", True),
    ("Persona 3", "Persona 4", False),                                     # different game
    ("Sonic Adventure (Original Soundtrack), Vol. 1", "Sonic Adventure Original Soundtrack Vol. 2", False),
    ("Born Like This", "Born This Way", False),                            # similar, not the same
    ("Thriller", "Thriller 40", False),
    ("Illmatic", "Illmatic XXL", False),
])
def test_similar_titles_are_not_matches(a, b, expect_high):
    s, how = am.title_sim(a, b)
    assert (s >= 0.93) == expect_high, (s, how)


def test_japanese_title_matching():
    s, how = am.title_sim("ペルソナ3 オリジナル・サウンドトラック", "Persona 3 Original Soundtrack")
    assert how == "translit" and 0.8 <= s <= 0.88                           # found, but never "exact"
    s, how = am.title_sim("ペルソナ3 オリジナル・サウンドトラック", "ペルソナ3 オリジナル･サウンドトラック")
    assert s >= 0.93                                                         # same Japanese title in another script form
    assert am.title_sim("ペルソナ3 オリジナル・サウンドトラック", "Persona 4 Original Soundtrack")[0] < 0.7
    assert am.artist_sim(["Tatsuro Yamashita"], ["山下達郎", "Yamashita, Tatsuro"])[0] == 0.9 or \
        am.artist_sim(["Tatsuro Yamashita"], ["山下達郎", "Yamashita, Tatsuro", "Tatsuro Yamashita"])[0] == 1.0


WANT = dict(artist="Nas", album="Illmatic", year="1994", n_tracks=10)


def judge(want, **cand):
    return am.judge(want, dict(dict(title="Illmatic", artist="Nas", year="1994", tracks=10, kind="original"), **cand))


def test_confidence_levels():
    assert judge(WANT)[1] == am.STRONG                                                     # right album
    assert judge(WANT, title="Illmatic", artist="Someone Else")[1] in (am.WEAK, None)    # wrong artist
    assert judge(WANT, title="Stillmatic")[1] in (am.WEAK, None)                          # similar name: weak at best
    assert judge(WANT, title="Completely Different")[1] is None                            # unrelated: not shown
    assert judge(WANT, title="", artist="")[1] == am.UNVERIFIED                           # nothing to verify
    assert judge(WANT, kind="deluxe", title="Illmatic (Deluxe)")[1] == am.POSSIBLE        # other edition: cap
    assert judge(WANT, kind="compilation")[1] == am.POSSIBLE
    assert judge(WANT, kind="fan")[1] == am.POSSIBLE                                       # fan art never > Possible
    assert judge(WANT, kind="promo")[1] == am.POSSIBLE
    assert judge(WANT, kind="remaster", title="Illmatic (Remastered)")[1] == am.STRONG    # same cover usually


def test_exact_release_by_musicbrainz_id_or_catalog():
    want = dict(WANT, mb_release_id="ABC-123")
    assert judge(want, release_id="abc-123")[1] == am.EXACT
    assert judge(dict(WANT, mb_rg_id="rg-1"), rg_id="RG-1")[1] == am.EXACT
    assert judge(dict(WANT, catalog="SVWC-7133"), catalog="svwc7133")[1] == am.EXACT
    assert judge(want, release_id="other")[1] == am.STRONG                                  # a different release ≠ exact


def test_weak_and_unverified_are_never_safe():
    assert am.WEAK not in am.SAFE and am.UNVERIFIED not in am.SAFE and am.POSSIBLE not in am.SAFE
    assert set(am.SAFE) == {am.EXACT, am.STRONG}


def test_video_game_matching():
    want = dict(mode="game", album="Persona 5 (Original Soundtrack)", game="Persona 5", artist="ATLUS Sound Team",
                n_tracks=110)
    assert am.judge(want, dict(title="Persona 5 Original Soundtrack", artist="ATLUS Sound Team", tracks=110))[1] == am.STRONG
    # the album needn't be named after the game: matched on the game's title, but only as a candidate
    alt = dict(want, album="Joker's Theme Collection")
    assert am.judge(alt, dict(title="Persona 5 Original Soundtrack", artist="ATLUS Sound Team"))[1] == am.POSSIBLE
    # same name as the game, different artist, not a soundtrack: a different record
    assert am.judge(want, dict(title="Persona 5", artist="None Like Joshua"))[1] == am.WEAK
    # a different game version
    assert am.judge(want, dict(title="Persona 5 Royal Original Soundtrack", artist="ATLUS Sound Team"))[1] in (am.WEAK, None)
    # Various Artists compilation: title + track count/year is enough for Strong, title alone isn't
    va = dict(mode="game", album="Sonic Adventure (Original Soundtrack), Vol. 1", artist="Various Artists", n_tracks=37)
    t = "Sonic Adventure (Original Soundtrack), Vol. 1"
    assert am.judge(va, dict(title=t, artist="Various Artists", tracks=37))[1] == am.STRONG
    assert am.judge(va, dict(title=t, artist="Various Artists", tracks=12))[1] == am.POSSIBLE
    assert am.judge(va, dict(title="Sonic Adventure Original Soundtrack Vol. 2", artist="Various Artists",
                             tracks=37))[1] is None


def test_edition_classification():
    assert am.edition_of("Illmatic (Deluxe Edition)")[0] == "deluxe"
    assert am.edition_of("Thriller (25th Anniversary Edition)")[0] == "remaster"
    assert am.edition_of("Hits", ["Compilation"])[0] == "compilation"
    assert am.edition_of("Off the Wall", note="Japan")[0] == "regional"
    assert am.edition_of("X", note="Promo CD")[0] == "promo"
    assert am.edition_of("X", provider="fanart")[0] == "fan"
    assert am.edition_of("Illmatic")[0] == "original"


def test_fallback_strategies():
    names = [s.name for s in am.strategies(dict(artist="Nas", album="Illmatic (Deluxe)", year="1994"))]
    assert names[0] == "exact artist + album"
    assert {"album without special characters", "album + year", "album + soundtrack", "album only"} <= set(names)
    assert any(n.startswith("regional edition") for n in names)
    game = am.strategies(dict(mode="game", album="Persona 5 (Original Soundtrack)", game="Persona 5",
                              artist="ATLUS Sound Team", composer="Shoji Meguro", catalog="SVWC-7133",
                              publisher="Atlus"))
    gnames = [s.name for s in game]
    assert gnames[0] == "catalog number"
    assert "game title + composer/artist" in gnames and "game title + original soundtrack" in gnames
    assert "game title + publisher" in gnames
    comp = next(s for s in game if s.name == "game title + composer/artist")
    assert (comp.artist, comp.album) == ("Shoji Meguro", "Persona 5")
    jp = [s.name for s in am.strategies(dict(mode="game", album="ペルソナ3 オリジナル・サウンドトラック", game="ペルソナ3"))]
    assert "romanised title" in jp
    assert len({s.key() for s in am.strategies(WANT)}) == len(am.strategies(WANT))        # no duplicates


# ======================================================================== fake providers / orchestration
class Fake(ap.Provider):
    def __init__(self, name, fn=None, fail=False, needs=""):
        self.name, self.label, self.needs, self.fn, self.fail, self.calls = name, name.title(), needs, fn, fail, []

    def search(self, ctx, sub, want):
        self.calls.append(sub.name)
        if self.fail:
            raise online.NetworkError("Cannot reach fake.example — check your internet connection")
        return [dict(c, provider=self.name) for c in (self.fn(sub, want) if self.fn else [])]

    def thumb_candidates(self, cand):
        return [f"fake://{self.name}/{cand['title']}/{cand.get('release_id', '')}/thumb"]

    def image_url(self, cand, target=0):
        return f"fake://{self.name}/{cand['title']}/{cand.get('release_id', '')}/full{target}"

    def headers(self, ctx):
        return {"Authorization": "token"}


SIZES = {}


@pytest.fixture
def fake_net(monkeypatch):
    monkeypatch.setattr(artsearch, "fetch_image", lambda ctx, url, headers=None: png_bytes((9, 99, 199), (250, 250)))
    monkeypatch.setattr(artsearch, "probe_size", lambda ctx, url, headers=None: SIZES.get(url.split("/full")[0], (1000, 1000)))
    SIZES.clear()


def cand(title, artist="Nas", **kw):
    return dict(dict(title=title, artist=artist, names=[artist], year="1994", tracks=10, kind="original", edition="",
                     release_id=title.lower().replace(" ", "-")), **kw)


def run(conn, want, providers, **kw):
    return artsearch.search_album(ap.Ctx.from_conn(conn), want, providers, **kw)


def test_one_provider_failing_does_not_break_the_search(conn, fake_net):
    good = Fake("good", lambda s, w: [cand("Illmatic")])
    r = run(conn, WANT, [Fake("down", fail=True), good, Fake("keyed", needs="x")])
    assert [c["provider"] for c in r["cands"]] == ["good"]
    st = r["status"]
    assert st["down"]["state"] == "error" and "internet" in st["down"]["msg"]
    assert st["keyed"]["state"] == "needs-key"                       # a missing key is reported, not a failure
    assert st["good"]["state"] == "ok"


def test_offline_every_provider(conn, fake_net):
    r = run(conn, WANT, [Fake("a", fail=True), Fake("b", fail=True)])
    assert r["cands"] == [] and {v["state"] for v in r["status"].values()} == {"error"}


def test_fallback_strategies_find_what_the_first_query_misses(conn, fake_net):
    # the service only knows the album when the search has no special characters
    def fn(sub, want):
        return [cand("MM FOOD", "MF DOOM", year="2004", tracks=13)] if sub.album == "MM FOOD" else []
    p = Fake("p", fn)
    want = dict(artist="MF DOOM", album="MM..FOOD", year="2004", n_tracks=13)
    r = run(conn, want, [p])
    assert p.calls[0] == "exact artist + album" and "album without special characters" in p.calls
    assert r["cands"][0]["confidence"] == am.STRONG and r["cands"][0]["strategy"] == "album without special characters"
    assert p.calls[-1] == "album without special characters"          # stopped at the first strong match


def test_game_soundtrack_found_by_game_title(conn, fake_net):
    # the soundtrack is catalogued under a different name; only "<game> + original soundtrack" finds it
    def fn(sub, want):
        return ([cand("Persona 5 Original Soundtrack", "ATLUS Sound Team", year="2017", tracks=110)]
                if sub.album == "Persona 5 Original Soundtrack" else [])
    want = dict(mode="game", album="P5 OST (disc set)", game="Persona 5", artist="ATLUS Sound Team", n_tracks=110)
    r = run(conn, want, [Fake("p", fn)])
    assert r["cands"] and r["cands"][0]["strategy"] == "game title + original soundtrack"
    assert r["cands"][0]["confidence"] in (am.STRONG, am.POSSIBLE)


def test_japanese_soundtrack_never_auto_approved_via_transliteration(conn, fake_net):
    fn = lambda s, w: [cand("Persona 3 Original Soundtrack", "ATLUS Sound Team", year="2006", tracks=59)]
    want = dict(mode="game", album="ペルソナ3 オリジナル・サウンドトラック", game="ペルソナ3", artist="ATLUS Sound Team",
                n_tracks=59)
    r = run(conn, want, [Fake("p", fn)])
    top = r["cands"][0]
    assert top["confidence"] == am.POSSIBLE and any("transliteration" in x for x in top["reasons"])
    # the same title in Japanese is a Strong match
    fn2 = lambda s, w: [cand("ペルソナ3 オリジナル･サウンドトラック", "ATLUS Sound Team", year="2006", tracks=59)]
    assert run(conn, want, [Fake("q", fn2)])["cands"][0]["confidence"] == am.STRONG


def test_wrong_album_is_not_substituted(conn, fake_net):
    fn = lambda s, w: [cand("Illmatic", "Nas"), cand("Illmatic Remixed", "DJ Someone"), cand("Stillmatic", "Nas")]
    r = run(conn, WANT, [Fake("p", fn)])
    titles = {c["title"]: c["confidence"] for c in r["cands"]}
    assert titles["Illmatic"] == am.STRONG
    assert titles.get("Stillmatic", am.WEAK) == am.WEAK and titles.get("Illmatic Remixed", am.WEAK) == am.WEAK
    assert am.STRONG not in [v for k, v in titles.items() if k != "Illmatic"]


def test_multiple_candidates_ranked_by_confidence_then_resolution(conn, fake_net):
    SIZES.update({"fake://a/Illmatic/illmatic": (600, 600), "fake://b/Illmatic/illmatic": (3000, 3000),
                  "fake://c/Illmatic/illmatic": (1000, 1000)})
    one = lambda s, w: [cand("Illmatic")]
    r = run(conn, WANT, [Fake("a", one), Fake("b", one), Fake("c", one),
                         Fake("fan", lambda s, w: [cand("Illmatic", kind="fan")])])
    order = [(c["provider"], c["confidence"]) for c in r["cands"]]
    assert [p for p, _ in order[:3]] == ["b", "c", "a"]               # 3000 › 1000 › 600 at equal confidence
    assert order[-1] == ("fan", am.POSSIBLE)                          # fan artwork is an alternative, listed last
    assert r["cands"][0]["width"] == 3000


def test_priority_mode_stops_at_first_strong_match(conn, fake_net):
    a, b = Fake("a", lambda s, w: [cand("Illmatic")]), Fake("b", lambda s, w: [cand("Illmatic")])
    r = run(conn, WANT, [a, b], mode="priority")
    assert a.calls and not b.calls and r["status"]["b"]["state"] == "skipped"
    r = run(conn, WANT, [a, b], mode="all")
    assert b.calls


# ======================================================================== real providers against canned JSON
@pytest.fixture
def canned(monkeypatch):
    seen = []

    def install(mapping):
        def fake_json(self, url, headers=None):
            seen.append((url, headers))
            for key, body in mapping.items():
                if key in url:
                    return body
            return {}
        monkeypatch.setattr(ap.Ctx, "json", fake_json)
        return seen
    return install


def test_musicbrainz_provider_parsing(conn, canned):
    rel = {"id": "rel-1", "title": "Illmatic", "date": "1994-04-19", "country": "US", "status": "Official",
           "track-count": 10, "artist-credit": [{"name": "Nas", "artist": {"sort-name": "Nas"}}],
           "label-info": [{"catalog-number": "CK 57684"}],
           "release-group": {"id": "rg-1", "primary-type": "Album", "secondary-types": []}}
    seen = canned({"/release?query=": {"releases": [rel, dict(rel, id="rel-2", date="2014-01-01", status="Bootleg",
                                                                  title="Illmatic (Deluxe)")]}})
    out = ap.MusicBrainz().search(ap.Ctx.from_conn(conn), am.Sub("x", artist="Nas", album="Illmatic", year="1994",
                                                                 region="US", terms="soundtrack"), dict(WANT))
    assert len(out) == 1 and out[0]["rg_id"] == "rg-1" and out[0]["catalog"] == "CK 57684"   # one per release group
    assert out[0]["release_id"] == "rel-1" and out[0]["year"] == "1994"                       # the original edition
    url = seen[0][0]
    assert "release%3A%22Illmatic%22" in url and "country%3AUS" in url and "secondarytype%3Asoundtrack" in url
    # the user's own release wins when the files carry its MusicBrainz ID
    out = ap.MusicBrainz().search(ap.Ctx.from_conn(conn), am.Sub("x", album="Illmatic"),
                                  dict(WANT, mb_release_id="REL-2"))
    assert out[0]["release_id"] == "rel-2" and out[0]["art_scope"] == "release"


def test_caa_image_sizes():
    p = ap.MusicBrainz()
    c = dict(release_id="r", rg_id="g", art_scope="release-group")
    assert p.image_url(c, 0).endswith("/release-group/g/front")          # best available = the original
    assert p.image_url(c, 500).endswith("/front-500")
    assert p.image_url(c, 1000).endswith("/front-1200")                  # smallest size that is at least the target
    assert p.image_url(c, 3000).endswith("/front")                       # nothing big enough published: original


def test_itunes_and_deezer_providers(conn, canned):
    canned({"itunes.apple.com": {"results": [{"collectionName": "Persona 5 (Original Soundtrack)", "collectionId": 7,
                                              "artistName": "ATLUS Sound Team", "trackCount": 110,
                                              "releaseDate": "2017-01-01T00:00:00Z", "primaryGenreName": "Soundtrack",
                                              "artworkUrl100": "https://x.mzstatic.com/a/100x100bb.jpg"}]},
            "api.deezer.com": {"data": [{"id": 9, "title": "Persona 5", "artist": {"name": "ATLUS"}, "nb_tracks": 12,
                                         "record_type": "single", "cover_medium": "m", "cover_big": "b",
                                         "cover_xl": "xl"}]}})
    ctx = ap.Ctx.from_conn(conn)
    it = ap.ITunes().search(ctx, am.Sub("x", album="Persona 5"), {})
    assert it[0]["year"] == "2017" and it[0]["tracks"] == 110
    assert ap.ITunes().image_url(it[0], 0).endswith("/5000x5000bb.jpg")     # asks for the original; never upscaled
    assert ap.ITunes().image_url(it[0], 1200).endswith("/1200x1200bb.jpg")
    dz = ap.Deezer().search(ctx, am.Sub("x", artist="ATLUS", album="Persona 5"), {})
    assert dz[0]["tracks"] == 12 and ap.Deezer().image_url(dz[0], 0) == "xl" and ap.Deezer().image_url(dz[0], 400) == "b"
    assert ap.Deezer().search(ctx, am.Sub("x", album="Persona 5", region="JP"), {}) == []    # no pointless repeats


def test_discogs_needs_token_and_sends_it(conn, canned):
    seen = canned({"api.discogs.com/database/search": {"results": [
        {"id": 5, "title": "Shoji Meguro - Persona 5 Original Soundtrack", "year": "2016", "country": "Japan",
         "catno": "SVWC-7133", "format": ["CD", "Album", "Limited Edition"], "cover_image": "https://i.discogs.com/x",
         "thumb": "t"},
        {"id": 6, "title": "X - No Image", "cover_image": "https://st.discogs.com/spacer.gif"}]}})
    d = ap.Discogs()
    ctx = ap.Ctx.from_conn(conn)
    assert not d.configured(ctx) and d.check(ctx)[0] == "needs-key"
    db.set_setting(conn, "discogs_token", "SECRET")
    out = d.search(ap.Ctx.from_conn(conn), am.Sub("x", artist="Shoji Meguro", album="Persona 5"),
                   dict(mode="game"))
    assert len(out) == 1 and out[0]["catalog"] == "SVWC-7133" and out[0]["country"] == "Japan"
    assert out[0]["artist"] == "Shoji Meguro" and out[0]["title"] == "Persona 5 Original Soundtrack"
    url, headers = seen[0]
    assert "SECRET" not in url and headers["Authorization"] == "Discogs token=SECRET"   # token only in a header
    assert "style=Video+Game+Music" in url


def test_fanart_uses_musicbrainz_ids_and_labels_results_as_fan_artwork(conn, canned):
    rel = {"id": "r1", "title": "Illmatic", "date": "1994", "artist-credit": [{"name": "Nas"}], "track-count": 10,
           "release-group": {"id": "rg-9", "primary-type": "Album"}}
    seen = canned({"/release?query=": {"releases": [rel]},
                   "fanart.tv": {"albums": {"rg-9": {"albumcover": [
                       {"id": "1", "url": "https://assets.fanart.tv/fanart/music/rg-9/albumcover/a.jpg", "likes": "5"},
                       {"id": "2", "url": "https://assets.fanart.tv/fanart/music/rg-9/albumcover/b.jpg", "likes": "9"}]}}}})
    db.set_setting(conn, "fanart_key", "KEY")
    out = ap.Fanart().search(ap.Ctx.from_conn(conn), am.Sub("x", artist="Nas", album="Illmatic"), WANT)
    assert [c["kind"] for c in out] == ["fan", "fan"] and out[0]["rtype"] == "9 likes"      # most liked first
    assert out[0]["thumb_url"].count("/preview/") == 1
    assert am.judge(WANT, out[0])[1] == am.POSSIBLE                                           # never Strong


def test_provider_settings_priority_and_disable(conn):
    assert ap.load_config(conn)["order"] == ap.DEFAULT_ORDER
    ap.save_config(conn, ["deezer", "itunes", "musicbrainz", "discogs", "fanart"], ["discogs"])
    assert [p.name for p in ap.active(conn)] == ["deezer", "itunes", "musicbrainz", "fanart"]
    assert [p.name for p in ap.active(conn, only={"itunes"})] == ["itunes"]
    db.set_setting(conn, "art_providers", "{not json")                                       # corrupt setting: defaults
    assert ap.load_config(conn)["order"] == ap.DEFAULT_ORDER


def test_provider_status_checks(conn, monkeypatch):
    monkeypatch.setattr(online, "http", lambda *a, **k: (_ for _ in ()).throw(online.NetworkError("bad", 401)))
    monkeypatch.setattr(ap.Ctx, "json", lambda self, url, headers=None: (_ for _ in ()).throw(online.NetworkError("bad", 401)))
    db.set_setting(conn, "discogs_token", "x")
    db.set_setting(conn, "fanart_key", "x")
    ctx = ap.Ctx.from_conn(conn)
    assert ap.Discogs().check(ctx)[0] == "invalid-key" and ap.Fanart().check(ctx)[0] == "invalid-key"
    monkeypatch.setattr(ap.Ctx, "json", lambda self, url, headers=None: (_ for _ in ()).throw(
        online.NetworkError("Cannot reach itunes", None)))
    assert ap.ITunes().check(ctx)[0] == "error"


# ======================================================================== manual import & image rules
def test_image_validation():
    assert artsearch.inspect_image(JPEG)["fmt"] == "JPEG" and artsearch.inspect_image(png_bytes())["fmt"] == "PNG"
    assert artsearch.inspect_image(WEBP)["mime"] == "image/webp"
    for bad in (img("GIF", (600, 600)), img("BMP", (600, 600)), b"not an image", b"", JPEG[:300]):
        with pytest.raises(artsearch.ImageError):
            artsearch.inspect_image(bad)


def test_image_warnings_without_rejection():
    assert artsearch.inspect_image(img("PNG", (1000, 1000)))["warnings"] == []
    small = artsearch.inspect_image(img("PNG", (400, 400)))
    assert any("Small" in w for w in small["warnings"])
    tiny = artsearch.inspect_image(img("PNG", (200, 200)))
    assert any("Very small" in w for w in tiny["warnings"])
    wide = artsearch.inspect_image(img("JPEG", (1500, 1000)))             # legitimate non-square original: warn, allow
    assert any("square" in w for w in wide["warnings"]) and wide["w"] == 1500
    banner = artsearch.inspect_image(img("JPEG", (2000, 600)))
    assert any("booklet" in w or "banner" in w for w in banner["warnings"])


def test_prepare_never_upscales_and_preserves_quality():
    data, mime, size = artsearch.prepare(JPEG)
    assert data == JPEG and mime == "image/jpeg" and size == (1200, 1200)             # byte-for-byte
    png = img("PNG", (300, 300))
    assert artsearch.prepare(png, 3000)[0] == png and artsearch.prepare(png, 3000)[2] == (300, 300)   # not enlarged
    d, m, s = artsearch.prepare(img("PNG", (4000, 4000)), 3000)
    assert s == (3000, 3000) and m == "image/png"                                      # scaled down only when asked
    d, m, s = artsearch.prepare(WEBP)
    assert m == "image/png" and Image.open(io.BytesIO(d)).size == (800, 800)          # WebP → lossless PNG
    cmyk = io.BytesIO()
    Image.new("CMYK", (600, 600)).save(cmyk, "JPEG")
    assert artsearch.prepare(cmyk.getvalue())[1] == "image/jpeg"
    assert Image.open(io.BytesIO(artsearch.prepare(cmyk.getvalue())[0])).mode == "RGB"


def test_import_file_and_url(conn, tmp_path, monkeypatch):
    p = tmp_path / "cover.webp"
    p.write_bytes(WEBP)
    data, info = artfetch.import_file(str(p))
    assert info["fmt"] == "WEBP" and info["w"] == 800
    (tmp_path / "x.gif").write_bytes(img("GIF", (500, 500)))
    with pytest.raises(artsearch.ImageError):
        artfetch.import_file(str(tmp_path / "x.gif"))
    with pytest.raises(artsearch.ImageError):
        artfetch.import_file(str(tmp_path / "missing.jpg"))
    monkeypatch.setattr(online, "http", lambda url, ua, **k: JPEG)
    assert artfetch.import_url(conn, "https://example.com/cover.jpg")[1]["fmt"] == "JPEG"
    for bad in ("file:///etc/passwd", "ftp://x/y.jpg", "javascript:alert(1)", "not a url", ""):
        with pytest.raises(artsearch.ImageError):
            artfetch.import_url(conn, bad)
    monkeypatch.setattr(online, "http", lambda url, ua, **k: (_ for _ in ()).throw(online.NetworkError("offline")))
    with pytest.raises(artsearch.ImageError):
        artfetch.import_url(conn, "https://example.com/a.jpg")
    monkeypatch.setattr(online, "http", lambda url, ua, **k: b"<html>not an image</html>")
    with pytest.raises(artsearch.ImageError):
        artfetch.import_url(conn, "https://example.com/a.jpg")


def test_import_panel_clipboard_drop_and_url(conn, tmp_path, monkeypatch):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QMimeData, QUrl
    from PySide6.QtGui import QGuiApplication, QImage
    from PySide6.QtWidgets import QApplication
    from mlm.gui.art_widgets import ImportPanel
    app = QApplication.instance() or QApplication([])

    class FakeApp:
        pass
    fa = FakeApp()
    fa.conn = conn
    panel = ImportPanel(fa)
    got = []
    panel.loaded.connect(lambda d, i, label: got.append((i["w"], i["h"], label)))
    # clipboard
    qimg = QImage(640, 640, QImage.Format_RGB32)
    qimg.fill(0x3366CC)
    QGuiApplication.clipboard().setImage(qimg)
    panel.paste()
    assert got[-1][:2] == (640, 640) and "clipboard" in got[-1][2] and panel.data
    # drag & drop of a file
    f = tmp_path / "dropped.png"
    f.write_bytes(img("PNG", (700, 700)))
    md = QMimeData()
    md.setUrls([QUrl.fromLocalFile(str(f))])

    class Ev:
        def mimeData(self):
            return md
    panel.dropEvent(Ev())
    assert got[-1][:2] == (700, 700)
    # a dropped file that isn't an image is refused with a message
    bad = tmp_path / "notes.png"
    bad.write_bytes(b"hello")
    md2 = QMimeData()
    md2.setUrls([QUrl.fromLocalFile(str(bad))])
    Ev.mimeData = lambda self: md2
    panel.dropEvent(Ev())
    assert panel.data is None and "Not a readable image" in panel.msg.text()
    # URL
    monkeypatch.setattr(online, "http", lambda url, ua, **k: JPEG)
    panel.url.setText("https://example.com/c.jpg")
    panel.from_url()
    assert got[-1][:2] == (1200, 1200)


# ======================================================================== library fixtures
@pytest.fixture
def lib(tmp_path):
    L = tmp_path / "Music"
    for n in range(1, 4):
        make_audio(L / f"Nas/Illmatic/{n:02d}.mp3", freq=300 + 40 * n, title=f"S{n}", artist="Nas", album="Illmatic",
                   album_artist="Nas", track=str(n), date="1994", genre="Hip-Hop")
    make_audio(L / "Game/Persona 5/01.mp3", title="Aria", artist="ATLUS Sound Team", album="Persona 5 (Original Soundtrack)",
               genre="Soundtrack", date="2017")
    make_audio(L / "Loose/Untagged Album/01.mp3")
    for n, art in ((1, True), (2, False)):
        p = make_audio(L / f"Band/Half/{n:02d}.mp3", freq=900 + n, title=f"H{n}", artist="Band", album="Half")
        if art:
            f = mutagen.File(p)
            tags.set_art(f, png_bytes((200, 0, 0)), "image/png")
            tags.save(f)
    return L


def tids(conn, like):
    return [r[0] for r in conn.execute("SELECT id FROM tracks WHERE path LIKE ? ORDER BY path", (f"%{like}%",))]


@pytest.fixture
def scanned(conn, lib):
    scanner.scan(conn, str(lib))
    return lib


# ======================================================================== missing artwork queue & batch
@needs_ffmpeg
def test_plan_modes_and_sibling_cover(conn, scanned):
    groups = {g["label"]: g for g in artfetch.plan(conn)}
    assert len(groups["Illmatic — Nas"]["track_ids"]) == 3 and groups["Illmatic — Nas"]["mode"] == "music"
    assert groups["Persona 5 (Original Soundtrack) — ATLUS Sound Team"]["mode"] == "game"
    assert groups["Half — Band"]["sibling"] and len(groups["Half — Band"]["track_ids"]) == 1
    f = groups["Folder: Loose/Untagged Album"]
    assert (f["album"], f["artist"]) == ("Untagged Album", "Loose") and f["kind"] == "folder"   # guessed from the path
    want = artfetch.want_for(conn, groups["Persona 5 (Original Soundtrack) — ATLUS Sound Team"])
    assert want["mode"] == "game" and want["game"] == "Persona 5" and want["year"] == "2017"
    assert artfetch.game_name("Sonic Adventure (Original Soundtrack), Vol. 1") == "Sonic Adventure"


@needs_ffmpeg
def test_batch_search_resume_retry_and_decisions(conn, scanned, fake_net):
    calls = {"n": 0}

    def fn(sub, want):
        calls["n"] += 1
        return [cand("Illmatic")] if want["album"] == "Illmatic" else []
    p = Fake("p", fn)
    st = artfetch.search(conn, providers=[p], max_strategies=3)
    j = {x["label"]: x for x in artfetch.jobs(conn)}
    assert j["Illmatic — Nas"]["status"] == "pending" and j["Illmatic — Nas"]["confidence"] == am.STRONG
    assert j["Illmatic — Nas"]["cands"][0]["width"] == 1000                              # real resolution stored
    assert j["Persona 5 (Original Soundtrack) — ATLUS Sound Team"]["status"] == "none"   # no results
    assert j["Half — Band"]["cands"][0]["provider"] == "library"                          # the cover you already have
    assert "“Illmatic” by Nas" in j["Illmatic — Nas"]["note"]
    # decisions survive and finished searches are not repeated
    jid = j["Illmatic — Nas"]["id"]
    artfetch.set_status(conn, [jid], "approved", chosen=0)
    n = calls["n"]
    st = artfetch.search(conn, providers=[p], max_strategies=3)
    assert st["searched"] == 0 and st["skipped"] == 4 and calls["n"] == n
    # "retry no-result searches" goes back to the empty ones only
    st = artfetch.search(conn, providers=[p], max_strategies=3, retry_none=True)
    assert st["searched"] == 2 and calls["n"] > n                      # Persona 5 and the untagged folder
    assert {x["label"]: x["status"] for x in artfetch.jobs(conn)}["Illmatic — Nas"] == "approved"


@needs_ffmpeg
def test_approve_safe_skips_weak_possible_fan_and_unverified(conn, scanned, fake_net):
    def fn(sub, want):
        if want["album"] == "Illmatic":
            return [cand("Illmatic", kind="fan"), cand("Illmatic (Deluxe)", release_id="dx", kind="deluxe")]
        return []
    artfetch.search(conn, providers=[Fake("p", fn)], max_strategies=2)
    j = next(x for x in artfetch.jobs(conn) if x["label"] == "Illmatic — Nas")
    assert {c["confidence"] for c in j["cands"]} == {am.POSSIBLE}
    approved = artfetch.approve_safe(conn)
    assert next(x for x in artfetch.jobs(conn) if x["label"] == "Illmatic — Nas")["status"] == "pending"
    assert approved == 1                                  # only the "Half" album, whose cover is in your own files


@needs_ffmpeg
def test_candidates_can_be_rejected_and_restored(conn, scanned, fake_net):
    artfetch.search(conn, providers=[Fake("p", lambda s, w: [cand("Illmatic")] if w["album"] == "Illmatic" else [])],
                    max_strategies=2)
    j = next(x for x in artfetch.jobs(conn) if x["label"] == "Illmatic — Nas")
    artfetch.reject_candidate(conn, j["id"], 0)
    j = next(x for x in artfetch.jobs(conn) if x["label"] == "Illmatic — Nas")
    assert j["cands"][0]["rejected"] == 1 and artfetch.safe_candidates(j) == []
    artfetch.reject_candidate(conn, j["id"], 0, rejected=False)
    assert artfetch.safe_candidates(next(x for x in artfetch.jobs(conn) if x["label"] == "Illmatic — Nas"))


def test_additive_migration_keeps_first_version_data(tmp_path, monkeypatch):
    monkeypatch.setenv("MLM_DATA_DIR", str(tmp_path / "data"))
    c = db.connect(str(tmp_path / "old.db"))
    c.executescript(artfetch.SCHEMA)                                          # the original two-table layout
    c.execute("INSERT INTO art_jobs(key, kind, label, artist, album, track_ids, confidence, note, status, chosen) "
              "VALUES('album|a|b','album','B — A','A','B','[1]','High','old note','approved',0)")
    c.execute("INSERT INTO art_cands(job_id, idx, source, title, artist, date, score, thumb) "
              "VALUES(1,0,'caa','B','A','1999',0.9,x'00')")
    c.commit()
    artfetch.ensure(c)
    cols = {r[1] for r in c.execute("PRAGMA table_info(art_cands)")}
    assert set(artfetch.CAND_COLS) <= cols
    j = artfetch.jobs(c)[0]
    assert j["status"] == "approved" and j["note"] == "old note"
    assert j["confidence"] == am.STRONG and j["cands"][0]["confidence"] == am.STRONG    # "High" mapped to new levels
    assert j["cands"][0]["title"] == "B"


# ======================================================================== applying artwork
def provenance(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM art_provenance ORDER BY id")]


@needs_ffmpeg
def test_apply_embeds_only_where_missing_preserves_metadata_and_logs_provenance(conn, scanned):
    ids = tids(conn, "Nas/Illmatic")
    paths = [conn.execute("SELECT path FROM tracks WHERE id=?", (i,)).fetchone()[0] for i in ids]
    before = {p: (audio_md5(p), tags.read_tags(mutagen.File(p))) for p in paths}
    src = dict(provider="itunes", url="https://x/art.jpg", release_id="77", title="Illmatic", artist="Nas",
               confidence=am.STRONG)
    res = artfetch.apply_image(conn, ids, JPEG, artfetch.ApplyOptions(), True, src, "Illmatic", job_id=1)
    assert res["embedded"] == 3 and res["errors"] == []
    for p in paths:
        f = mutagen.File(p)
        assert tags.get_art(f)[0] == JPEG                                   # embedded byte-for-byte
        assert (audio_md5(p), tags.read_tags(f)) == before[p]               # audio and every tag preserved
    pr = provenance(conn)
    assert pr[-1]["provider"] == "itunes" and pr[-1]["source_url"] == "https://x/art.jpg" and pr[-1]["release_id"] == "77"
    assert (pr[-1]["w"], pr[-1]["h"]) == (1200, 1200) and pr[-1]["confidence"] == am.STRONG and pr[-1]["ts"]
    assert pr[-1]["applied_as"] == "embedded" and sorted(json.loads(pr[-1]["track_ids"])) == sorted(ids)


@needs_ffmpeg
def test_apply_replace_backs_up_the_old_cover_and_can_be_undone(conn, scanned):
    ids = tids(conn, "Band/Half")                                            # 01 has a red cover, 02 has none
    p1 = conn.execute("SELECT path FROM tracks WHERE id=?", (ids[0],)).fetchone()[0]
    old = tags.get_art(mutagen.File(p1))[0]
    res = artfetch.apply_image(conn, ids, JPEG, artfetch.ApplyOptions(only_missing=True), True)
    assert res["embedded"] == 1 and tags.get_art(mutagen.File(p1))[0] == old    # existing cover untouched
    res = artfetch.apply_image(conn, ids, JPEG, artfetch.ApplyOptions(only_missing=False), True)
    assert res["embedded"] == 2 and tags.get_art(mutagen.File(p1))[0] == JPEG
    h = conn.execute("SELECT * FROM history WHERE batch=? AND field='artwork' AND track_id=?",
                     (res["batch"], ids[0])).fetchone()
    assert h and os.path.isfile(h["backup"]) and open(h["backup"], "rb").read() == old   # backup of the replaced cover
    repair.undo_batch(conn, res["batch"], True)
    assert tags.get_art(mutagen.File(p1))[0] == old


@needs_ffmpeg
def test_apply_selected_tracks_only(conn, scanned):
    ids = tids(conn, "Nas/Illmatic")
    res = artfetch.apply_image(conn, ids[:1], JPEG, artfetch.ApplyOptions(), True)
    got = [bool(tags.get_art(mutagen.File(conn.execute("SELECT path FROM tracks WHERE id=?", (i,)).fetchone()[0])))
           for i in ids]
    assert res["embedded"] == 1 and got == [True, False, False]


@needs_ffmpeg
def test_apply_folder_and_cover_jpg_without_overwriting(conn, scanned):
    ids = tids(conn, "Nas/Illmatic")
    folder = os.path.dirname(conn.execute("SELECT path FROM tracks WHERE id=?", (ids[0],)).fetchone()[0])
    opts = artfetch.ApplyOptions(embed=False, folder_jpg=True, cover_jpg=True)
    res = artfetch.apply_image(conn, ids, png_bytes((1, 2, 3), (900, 900)), opts, True)
    assert sorted(os.path.basename(f) for f in res["files"]) == ["cover.jpg", "folder.jpg"]
    jpg = open(os.path.join(folder, "folder.jpg"), "rb").read()
    assert Image.open(io.BytesIO(jpg)).format == "JPEG"                       # PNG source converted for a .jpg file
    for t in ids:                                                              # audio files untouched: no embedding
        assert not tags.get_art(mutagen.File(conn.execute("SELECT path FROM tracks WHERE id=?", (t,)).fetchone()[0]))
    # a second run neither overwrites nor errors
    res2 = artfetch.apply_image(conn, ids, JPEG, opts, True)
    assert res2["files"] == [] and len(res2["skipped"]) == 2 and open(os.path.join(folder, "folder.jpg"), "rb").read() == jpg
    # replacing is opt-in and backs up the old file first
    res3 = artfetch.apply_image(conn, ids, JPEG, artfetch.ApplyOptions(embed=False, folder_jpg=True, replace_files=True), True)
    assert open(os.path.join(folder, "folder.jpg"), "rb").read() == JPEG
    h = conn.execute("SELECT * FROM history WHERE batch=? AND field='folder_art'", (res3["batch"],)).fetchone()
    assert open(h["backup"], "rb").read() == jpg
    repair.undo_batch(conn, res3["batch"], True)
    assert open(os.path.join(folder, "folder.jpg"), "rb").read() == jpg      # the earlier image is back
    repair.undo_batch(conn, res["batch"], True)
    assert not os.path.exists(os.path.join(folder, "cover.jpg"))              # created by us, removed by undo


@needs_ffmpeg
def test_save_separately_needs_no_edit_mode_and_touches_nothing(conn, scanned, tmp_path):
    ids = tids(conn, "Nas/Illmatic")
    paths = [r[0] for r in conn.execute("SELECT path FROM tracks WHERE id IN (%s)" % ",".join("?" * len(ids)), ids)]
    snap = {p: (audio_md5(p), os.path.getmtime(p)) for p in paths}
    out = tmp_path / "Covers"
    opts = artfetch.ApplyOptions(embed=False, save_to=str(out))
    res = artfetch.apply_image(conn, ids, WEBP, opts, edit_mode=False, source=dict(artist="Nas", title="Illmatic"))
    assert os.listdir(out) == ["Nas - Illmatic.webp"] and open(out / "Nas - Illmatic.webp", "rb").read() == WEBP
    res = artfetch.apply_image(conn, ids, WEBP, opts, False, dict(artist="Nas", title="Illmatic"))
    assert sorted(os.listdir(out)) == ["Nas - Illmatic (2).webp", "Nas - Illmatic.webp"]          # never overwrites
    assert {p: (audio_md5(p), os.path.getmtime(p)) for p in paths} == snap
    assert provenance(conn)[-1]["applied_as"] == "separate copy"


@needs_ffmpeg
def test_read_only_mode_blocks_library_writes(conn, scanned):
    ids = tids(conn, "Nas/Illmatic")
    for opts in (artfetch.ApplyOptions(), artfetch.ApplyOptions(embed=False, folder_jpg=True)):
        with pytest.raises(repair.ReadOnlyError):
            artfetch.apply_image(conn, ids, JPEG, opts, edit_mode=False)


@needs_ffmpeg
def test_webp_and_non_square_art_embeds(conn, scanned):
    ids = tids(conn, "Nas/Illmatic")
    wide = img("JPEG", (1600, 1000))
    res = artfetch.apply_image(conn, ids[:1], wide, artfetch.ApplyOptions(), True)
    assert res["embedded"] == 1
    p = conn.execute("SELECT path FROM tracks WHERE id=?", (ids[0],)).fetchone()[0]
    assert tags.get_art(mutagen.File(p))[0] == wide                           # legitimate non-square art isn't altered
    res = artfetch.apply_image(conn, ids[1:2], WEBP, artfetch.ApplyOptions(), True)
    p = conn.execute("SELECT path FROM tracks WHERE id=?", (ids[1],)).fetchone()[0]
    assert res["errors"] == [] and tags.get_art(mutagen.File(p))[1] == "image/png"


@needs_ffmpeg
def test_embed_size_limit_scales_down_only(conn, scanned):
    ids = tids(conn, "Nas/Illmatic")
    big = img("PNG", (2400, 2400))
    artfetch.apply_image(conn, ids[:1], big, artfetch.ApplyOptions(max_side=1500), True)
    artfetch.apply_image(conn, ids[1:2], img("PNG", (600, 600)), artfetch.ApplyOptions(max_side=1500), True)
    sizes = []
    for i in ids[:2]:
        p = conn.execute("SELECT path FROM tracks WHERE id=?", (i,)).fetchone()[0]
        sizes.append(Image.open(io.BytesIO(tags.get_art(mutagen.File(p))[0])).size)
    assert sizes == [(1500, 1500), (600, 600)]


@needs_ffmpeg
def test_queue_apply_end_to_end(conn, scanned, fake_net, monkeypatch):
    monkeypatch.setattr(artfetch, "candidate_image", lambda conn, cand, target=None, ctx=None: JPEG)
    artfetch.search(conn, providers=[Fake("p", lambda s, w: [cand("Illmatic")] if w["album"] == "Illmatic" else [])],
                    max_strategies=2)
    assert artfetch.approve_safe(conn) >= 1
    with pytest.raises(repair.ReadOnlyError):
        artfetch.apply(conn, [j["id"] for j in artfetch.jobs(conn, "approved")], edit_mode=False)
    res = artfetch.apply(conn, [j["id"] for j in artfetch.jobs(conn, "approved")], True,
                         artfetch.ApplyOptions(folder_jpg=True))
    assert res["songs"] >= 3 and res["files"] >= 1 and res["errors"] == []
    jobs = {j["label"]: j for j in artfetch.jobs(conn)}
    assert jobs["Illmatic — Nas"]["status"] == "applied"
    assert conn.execute("SELECT count(*) FROM tracks WHERE has_art=0 AND path LIKE '%Illmatic%'").fetchone()[0] == 0
    assert provenance(conn) and {r["provider"] for r in provenance(conn)} >= {"p"}


# ======================================================================== GUI
@needs_ffmpeg
def test_search_dialog_gallery_select_reject_and_import(conn, scanned, fake_net, monkeypatch, tmp_path):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from mlm.gui import app as A, widgets as W
    from mlm.gui.art_search import SearchDialog
    from mlm.gui.art_widgets import CandidateCard
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(W, "info", lambda *a, **k: None)          # modal boxes would block a headless run
    w = A.MainWindow(conn.execute("PRAGMA database_list").fetchone()[2])
    monkeypatch.setattr(artfetch, "candidate_image", lambda conn, cand, target=None, ctx=None: JPEG)
    artfetch.search(conn, providers=[Fake("p", lambda s, w: [cand("Illmatic"), cand("Illmatic", "Nas", kind="fan",
                                                                                   release_id="fan1")]
                                        if w["album"] == "Illmatic" else [])], max_strategies=2)
    g = next(g for g in artfetch.plan(conn, only_missing=False) if g["label"] == "Illmatic — Nas")
    d = SearchDialog(w, g)
    d.show()
    app.processEvents()
    cards = d.gallery.body.findChildren(CandidateCard)
    assert len(cards) == 2 and {c.cand["kind"] for c in cards} == {"original", "fan"}
    assert [c.cand["confidence"] for c in cards][0] == am.STRONG
    d.choose(cards[0].cand)
    assert d.apply_btn.isEnabled() and d.approve_btn.isEnabled()
    d.reject_cand(cards[1].cand)
    assert d.job["cands"][1]["rejected"] == 1
    d.approve()
    assert next(j for j in artfetch.jobs(conn) if j["label"] == "Illmatic — Nas")["status"] == "approved"
    # manual import path
    d2 = SearchDialog(w, g, start_tab=1)
    f = tmp_path / "mine.png"
    f.write_bytes(png_bytes((5, 5, 5), (700, 700)))
    d2.importer.from_path(str(f))
    assert d2.manual and not d2.approve_btn.isEnabled() and d2.apply_btn.isEnabled()
    d2.close()
    w.close()


@needs_ffmpeg
def test_artwork_page_queue_filters_and_settings(conn, scanned, fake_net):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from mlm.gui import app as A
    app = QApplication.instance() or QApplication([])
    path = conn.execute("PRAGMA database_list").fetchone()[2]
    artfetch.search(conn, providers=[Fake("p", lambda s, w: [cand("Illmatic")] if w["album"] == "Illmatic" else [])],
                    max_strategies=2)
    w = A.MainWindow(path)
    w.go("artwork")
    tab = w.pages["artwork"].bulk
    tab.filter.setCurrentText("All")
    total = tab.table.rowCount()
    assert total == len(artfetch.jobs(w.conn)) >= 4
    tab.filter.setCurrentText("No results")
    assert 0 < tab.table.rowCount() < total
    tab.filter.setCurrentText("All")
    tab.conf.setCurrentText(am.STRONG)
    assert 0 < tab.table.rowCount() < total
    tab.conf.setCurrentIndex(0)
    # Settings → Artwork sources: priority, enable switches, keys
    w.go("settings")
    art = w.pages["settings"].art
    art.refresh()
    art.move(1, -1)
    art.toggle("fanart", False)
    art.discogs.setText("TOKEN123")
    art.save_key("discogs_token", art.discogs)
    cfg = ap.load_config(w.conn)
    assert cfg["order"][0] == "itunes" and "fanart" in cfg["disabled"]
    assert db.get_setting(w.conn, "discogs_token") == "TOKEN123"
    art.res.setCurrentIndex(2)
    art.mode.setCurrentIndex(1)
    assert artfetch.target_resolution(w.conn) == 1000 and artfetch.search_mode(w.conn) == "priority"
    w.close()
