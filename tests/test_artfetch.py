"""Bulk artwork: planning, searching (simulated services), review decisions, safe apply and undo."""
import os

import mutagen
import pytest

from conftest import audio_md5, make_audio, needs_ffmpeg, png_bytes
from mlm import artfetch, online, repair, scanner, tags

pytestmark = needs_ffmpeg

THUMB = png_bytes((10, 120, 200), (250, 250))
FULL = png_bytes((10, 120, 200), (1200, 1200))


def release(rid, title, artist, n, date="1994-04-19", rtype="Album", status="Official"):
    return {"id": rid, "title": title, "artist-credit": [{"name": artist}], "track-count": n, "date": date,
            "status": status, "release-group": {"id": f"rg-{rid}", "primary-type": rtype}}


@pytest.fixture
def services(monkeypatch):
    """Fake MusicBrainz release search, Cover Art Archive and iTunes."""
    calls = {"caa": [], "itunes": []}
    releases = {"Illmatic": [release("illmatic", "Illmatic", "Nas", 10)],
                "AIZO": [release("aizo", "AIZO", "King Gnu", 2, "2024-01-01", "Single")],
                "Rare Album": [release("rare", "Rare Album", "Someone", 3)],  # in MusicBrainz but no CAA cover
                }

    def search_releases(conn, album, artist, n, ua):
        return releases.get(online._base(album), [])

    def http(url, ua, data=None, timeout=25):
        if "coverartarchive" in url:
            calls["caa"].append(url)
            if "rg-rare" in url:
                return None
            return FULL if url.endswith("/front") else THUMB
        if "mzstatic" in url:
            return FULL if "1400x1400" in url else THUMB
        raise AssertionError(url)

    def cached_json(conn, url, ua):
        if "itunes.apple.com" in url:
            calls["itunes"].append(url)
            if "Rare+Album" in url:
                return {"results": [{"collectionId": 7, "collectionName": "Rare Album", "artistName": "Someone",
                                     "trackCount": 3, "releaseDate": "2001-01-01T00:00:00Z",
                                     "artworkUrl100": "https://is1.mzstatic.com/x/100x100bb.jpg"}]}
            return {"results": []}
        raise AssertionError(url)
    monkeypatch.setattr(online, "search_releases", search_releases)
    monkeypatch.setattr(online, "http", http)
    monkeypatch.setattr(online, "cached_json", cached_json)
    return calls


@pytest.fixture
def lib(tmp_path):
    L = tmp_path / "Music"
    for n in range(1, 4):
        make_audio(L / f"Nas/Illmatic/{n:02d}.mp3", freq=300 + 40 * n, title=f"S{n}", artist="Nas", album="Illmatic",
                   album_artist="Nas", track=str(n))
    make_audio(L / "King Gnu/AIZO/01.mp3", title="AIZO", artist="King Gnu", album="AIZO - Single")
    make_audio(L / "Someone/Rare Album/01.mp3", title="R", artist="Someone", album="Rare Album")
    make_audio(L / "MF DOOM/The Mouse & the Mask/01 Mince Meat.mp3")   # no tags at all
    make_audio(L / "Various Artists/x.mp3")                              # generic folder name
    # partly covered album: one song has a cover, the other doesn't
    for n, art in ((1, True), (2, False)):
        p = make_audio(L / f"Band/Half/{n:02d}.mp3", freq=900 + n, title=f"H{n}", artist="Band", album="Half")
        if art:
            f = mutagen.File(p)
            tags.set_art(f, png_bytes((200, 0, 0)), "image/png")
            tags.save(f)
    return L


def by_label(conn):
    return {j["label"]: j for j in artfetch.jobs(conn)}


def test_plan_groups(conn, lib):
    scanner.scan(conn, str(lib))
    groups = {g["label"]: g for g in artfetch.plan(conn)}
    assert len(groups["Illmatic — Nas"]["track_ids"]) == 3
    assert groups["Half — Band"]["sibling"] and len(groups["Half — Band"]["track_ids"]) == 1
    doom = groups["Folder: MF DOOM/The Mouse & the Mask"]
    assert (doom["artist"], doom["album"]) == ("MF DOOM", "The Mouse & the Mask")
    assert groups["Folder: Various Artists"]["album"] == ""  # generic folder: never searched as an album


def test_search_finds_and_grades_covers(conn, lib, services):
    scanner.scan(conn, str(lib))
    st = artfetch.search(conn)
    assert st["error"] is None
    j = by_label(conn)
    assert j["Illmatic — Nas"]["confidence"] == "High" and j["Illmatic — Nas"]["cands"][0]["source"] == "caa"
    assert j["AIZO - Single — King Gnu"]["confidence"] == "High"  # " - Single" suffix ignored when matching
    rare = j["Rare Album — Someone"]
    assert rare["cands"][0]["source"] == "itunes" and "iTunes" in rare["note"]  # fallback when CAA has no cover
    assert j["Half — Band"]["cands"][0]["source"] == "library"
    assert j["Folder: Various Artists"]["status"] == "none"
    assert not services["itunes"] or all("Illmatic" not in u for u in services["itunes"])  # CAA was enough


def test_search_is_resumable_and_keeps_decisions(conn, lib, services):
    scanner.scan(conn, str(lib))
    artfetch.search(conn)
    jid = by_label(conn)["Illmatic — Nas"]["id"]
    artfetch.set_status(conn, [jid], "rejected")
    n = len(services["caa"])
    st = artfetch.search(conn)
    assert st["searched"] == 0 and len(services["caa"]) == n  # nothing re-fetched
    assert by_label(conn)["Illmatic — Nas"]["status"] == "rejected"


def test_apply_requires_edit_mode(conn, lib, services):
    scanner.scan(conn, str(lib))
    artfetch.search(conn)
    artfetch.approve_high(conn)
    with pytest.raises(repair.ReadOnlyError):
        artfetch.apply(conn, [j["id"] for j in artfetch.jobs(conn)], edit_mode=False)


def test_apply_adds_only_missing_art_and_can_be_undone(conn, lib, services):
    scanner.scan(conn, str(lib))
    artfetch.search(conn)
    j = by_label(conn)
    covered = str(lib / "Band/Half/01.mp3")
    before_cover = tags.get_art(mutagen.File(covered))[0]
    audio = {p: audio_md5(p) for p in scanner.collect(str(lib))[0]}
    artfetch.set_status(conn, [j["Illmatic — Nas"]["id"]], "approved", chosen=0)
    artfetch.set_status(conn, [j["Half — Band"]["id"]], "approved", chosen=0)
    res = artfetch.apply(conn, [j["Illmatic — Nas"]["id"], j["Half — Band"]["id"]], edit_mode=True)
    assert res["errors"] == [] and res["songs"] == 4 and res["albums"] == 2
    for n in (1, 2, 3):
        assert tags.get_art(mutagen.File(str(lib / f"Nas/Illmatic/{n:02d}.mp3")))[0] == FULL
    assert tags.get_art(mutagen.File(str(lib / "Band/Half/02.mp3")))[0] == before_cover  # copied from album-mate
    assert tags.get_art(mutagen.File(covered))[0] == before_cover  # existing cover untouched
    assert {p: audio_md5(p) for p in audio} == audio  # audio never changes
    assert by_label(conn)["Illmatic — Nas"]["status"] == "applied"
    assert conn.execute("SELECT count(*) FROM tracks WHERE has_art=0 AND path LIKE '%Illmatic%'").fetchone()[0] == 0
    # one undoable batch per album
    batch = conn.execute("SELECT batch FROM history WHERE note LIKE 'bulk artwork: Illmatic%' LIMIT 1").fetchone()[0]
    repair.undo_batch(conn, batch, edit_mode=True)
    assert tags.get_art(mutagen.File(str(lib / "Nas/Illmatic/01.mp3"))) is None


def test_never_replaces_art_added_after_review(conn, lib, services):
    scanner.scan(conn, str(lib))
    artfetch.search(conn)
    jid = by_label(conn)["Illmatic — Nas"]["id"]
    artfetch.set_status(conn, [jid], "approved", chosen=0)
    p = str(lib / "Nas/Illmatic/02.mp3")  # the user adds their own cover before applying
    mine = png_bytes((0, 200, 0))
    f = mutagen.File(p)
    tags.set_art(f, mine, "image/png")
    tags.save(f)
    scanner.scan(conn, str(lib))
    res = artfetch.apply(conn, [jid], edit_mode=True)
    assert res["songs"] == 2
    assert tags.get_art(mutagen.File(p))[0] == mine


def test_network_error_stops_and_keeps_progress(conn, lib, services, monkeypatch):
    scanner.scan(conn, str(lib))
    real = online.search_releases
    third = [g for g in artfetch.plan(conn) if g["album"]][2]["album"]

    def flaky(conn_, album, *a, **k):
        if album == third:
            raise online.NetworkError("Cannot reach musicbrainz.org")
        return real(conn_, album, *a, **k)
    monkeypatch.setattr(online, "search_releases", flaky)
    st = artfetch.search(conn)
    assert st["error"] and st["searched"] == 2
    monkeypatch.setattr(online, "search_releases", real)
    st = artfetch.search(conn)
    assert st["error"] is None and st["skipped"] == 2  # resumes where it stopped


def test_bulk_tab(conn, lib, services, tmp_path):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from mlm.gui.app import MainWindow
    app = QApplication.instance() or QApplication([])
    scanner.scan(conn, str(lib))
    artfetch.search(conn)
    conn.commit()
    w = MainWindow(conn.execute("PRAGMA database_list").fetchone()[2])
    w.go("artwork")
    tab = w.pages["artwork"].bulk
    assert tab.table.rowCount() >= 4
    jid = by_label(w.conn)["Illmatic — Nas"]["id"]
    tab.choose(jid, 0)
    assert by_label(w.conn)["Illmatic — Nas"]["status"] == "approved"
    w.close()


def test_similar_title_by_another_artist_is_rejected():
    assert not artfetch.artist_ok("MF DOOM", "Lady Gaga", 0.1)
    assert artfetch.artist_ok("MF DOOM", "MF DOOM", 1.0)
    assert artfetch.artist_ok("Various Artists", "SEGA", 0.0)
    assert artfetch.artist_ok("Madvillain", "Madlib & MF DOOM", 0.3) is False
    assert artfetch.artist_ok("SEGA/Richard Jacques", "Richard Jacques", 0.4)


def test_romanised_sort_name_counts_as_same_artist():
    ac = [{"name": "山下達郎", "artist": {"sort-name": "Yamashita, Tatsuro"}}]
    names = artfetch.credit_names(ac)
    assert "Tatsuro Yamashita" in names
    assert max(online.sim("Tatsuro Yamashita", n) for n in names) == 1.0
