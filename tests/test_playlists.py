import os

import pytest

from conftest import make_audio, needs_ffmpeg
from mlm import playlists, scanner

pytestmark = needs_ffmpeg


@pytest.fixture
def lib(conn, tmp_path):
    L = tmp_path / "Music"
    names = ["Artist/Album/01 One.mp3", "Artist/Album/02 Two.mp3", "Artist/Album/03 Three.mp3",
             "ペルソナ/サウンドトラック/01 曲 (live).mp3"]
    for i, n in enumerate(names):
        make_audio(L / n, freq=300 + 80 * i, title=os.path.basename(n)[3:-4], artist="Artist")
    scanner.scan(conn, str(L))
    ids = {os.path.basename(r["path"]): r["id"] for r in conn.execute("SELECT id, path FROM tracks")}
    return L, ids


def titles(conn, pid):
    return [t["title"] for t in playlists.tracks(conn, pid)]


def test_create_rename_delete_and_unique_names(conn):
    a = playlists.create(conn, "  Road trip ")
    with pytest.raises(ValueError):
        playlists.create(conn, "road trip")                       # same name, any case
    with pytest.raises(ValueError):
        playlists.create(conn, "   ")
    b = playlists.create(conn, "Study")
    with pytest.raises(ValueError):
        playlists.rename(conn, b, "ROAD TRIP")
    playlists.rename(conn, b, "Focus")
    assert [p["name"] for p in playlists.list_all(conn)] == ["Focus", "Road trip"]
    playlists.delete(conn, a)
    assert [p["name"] for p in playlists.list_all(conn)] == ["Focus"]
    assert conn.execute("SELECT count(*) FROM playlist_tracks WHERE playlist_id=?", (a,)).fetchone()[0] == 0


def test_add_order_duplicates_remove_and_move(conn, lib):
    _, ids = lib
    pid = playlists.create(conn, "P")
    assert playlists.add_tracks(conn, pid, [ids["02 Two.mp3"], ids["01 One.mp3"], ids["02 Two.mp3"], 99999]) == 2
    assert titles(conn, pid) == ["Two", "One"]                    # insertion order; duplicate and unknown id skipped
    assert playlists.add_tracks(conn, pid, [ids["01 One.mp3"], ids["03 Three.mp3"]]) == 1
    assert playlists.move(conn, pid, ids["03 Three.mp3"], -1)
    assert titles(conn, pid) == ["Two", "Three", "One"]
    assert not playlists.move(conn, pid, ids["02 Two.mp3"], -1)   # already first
    assert not playlists.move(conn, pid, ids["01 One.mp3"], 1)                # already last
    playlists.remove(conn, pid, [ids["03 Three.mp3"]])
    assert titles(conn, pid) == ["Two", "One"]
    assert [t["pos"] for t in playlists.tracks(conn, pid)] == [0, 1]         # positions stay contiguous
    assert playlists.list_all(conn)[0]["n"] == 2


def test_song_on_a_disconnected_drive_stays_in_the_playlist(conn, lib):
    L, ids = lib
    pid = playlists.create(conn, "P")
    playlists.add_tracks(conn, pid, [ids["01 One.mp3"], ids["02 Two.mp3"]])
    os.remove(L / "Artist/Album/02 Two.mp3")
    scanner.scan(conn, str(L))                                    # the scanner marks it missing, not deleted
    rows = playlists.tracks(conn, pid)
    assert [r["state"] for r in rows] == ["", "not found"] and len(rows) == 2


def test_export_import_round_trip(conn, lib, tmp_path):
    L, ids = lib
    pid = playlists.create(conn, "Mix")
    playlists.add_tracks(conn, pid, [ids["01 曲 (live).mp3"], ids["03 Three.mp3"], ids["01 One.mp3"]])
    out = tmp_path / "Mix.m3u8"
    assert playlists.export_m3u(conn, pid, str(out)) == 3
    text = out.read_text(encoding="utf-8")
    assert text.startswith("#EXTM3U\n#EXTINF:") and str(L / "ペルソナ/サウンドトラック/01 曲 (live).mp3") in text
    res = playlists.import_m3u(conn, str(out))
    assert res["name"] == "Mix (2)" and res["added"] == 3 and res["missing"] == []   # name clash handled, nothing overwritten
    assert titles(conn, res["id"]) == titles(conn, pid) == ["曲 (live)", "Three", "One"]


def test_import_handles_relative_urls_case_and_unknown_entries(conn, lib, tmp_path):
    L, ids = lib
    m3u = L / "Artist" / "mine.m3u"
    m3u.write_bytes("﻿#EXTM3U\n# a comment\nAlbum/01 One.mp3\n"
                    f"file://{str(L / 'Artist/Album/02 Two.mp3').replace(' ', '%20')}\n"
                    f"{str(L / 'ARTIST/ALBUM/03 THREE.MP3')}\n"          # wrong case: macOS volumes are case-insensitive
                    "/Volumes/Gone/ghost.mp3\n\n".encode("utf-8"))
    res = playlists.import_m3u(conn, str(m3u), name="From file")
    assert res["added"] == 3 and res["missing"] == ["/Volumes/Gone/ghost.mp3"]
    assert titles(conn, res["id"]) == ["One", "Two", "Three"]
    latin = tmp_path / "old.m3u"
    latin.write_bytes(b"/nowhere/caf\xe9.mp3\n")                          # not valid UTF-8
    assert playlists.import_m3u(conn, str(latin))["missing"] == ["/nowhere/café.mp3"]


def test_playlist_gui_flow(conn, lib, monkeypatch, tmp_path):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication, QInputDialog
    from mlm.gui import app as A, pages_playlists as pp, widgets as W
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(W, "info", lambda *a, **k: None)
    monkeypatch.setattr(W, "confirm", lambda *a, **k: True)
    w = A.MainWindow(conn.execute("PRAGMA database_list").fetchone()[2])
    _, ids = lib
    # Library → Add to Playlist… creating a new list
    answers = iter([("＋ New playlist…", True), ("Favourites", True)])
    monkeypatch.setattr(QInputDialog, "getItem", staticmethod(lambda *a, **k: next(answers)))
    monkeypatch.setattr(QInputDialog, "getText", staticmethod(lambda *a, **k: next(answers)))
    pp.add_to_playlist(w, w, [ids["01 One.mp3"], ids["02 Two.mp3"]])
    # ...then into the existing one
    monkeypatch.setattr(QInputDialog, "getItem", staticmethod(lambda *a, **k: ("Favourites", True)))
    pp.add_to_playlist(w, w, [ids["02 Two.mp3"], ids["03 Three.mp3"]])
    w.go("playlists")
    page = w.pages["playlists"]
    assert page.lists.count() == 1 and "Favourites" in page.lists.item(0).text()
    assert [r["title"] for r in page.table.source_model.rows] == ["One", "Two", "Three"]
    # reorder, remove
    page.table.selectRow(2)
    page.move(-1)
    assert [r["title"] for r in page.table.source_model.rows] == ["One", "Three", "Two"]
    page.table.selectRow(0)
    page.remove()
    assert [r["title"] for r in page.table.source_model.rows] == ["Three", "Two"]
    # play sends the queue to the player
    page.play(selected=False)
    assert [q["title"] for q in w.pages["player"].queue] == ["Three", "Two"]
    w.pages["player"].player.stop()
    # Player → Save as Playlist…
    monkeypatch.setattr(QInputDialog, "getItem", staticmethod(lambda *a, **k: ("＋ New playlist…", True)))
    monkeypatch.setattr(QInputDialog, "getText", staticmethod(lambda *a, **k: ("From queue", True)))
    w.pages["player"].save_playlist()
    assert {p["name"] for p in playlists.list_all(w.conn)} == {"Favourites", "From queue"}
    # delete leaves the songs alone
    page.delete()
    assert w.conn.execute("SELECT count(*) FROM tracks WHERE missing=0").fetchone()[0] == 4
    w.close()
