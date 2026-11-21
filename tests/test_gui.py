"""Headless smoke test: build the window, scan, visit every page, exercise main actions."""
import os

import pytest

from conftest import needs_ffmpeg

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytestmark = needs_ffmpeg


@pytest.fixture
def win(conn, library, tmp_path):
    from PySide6.QtWidgets import QApplication, QMessageBox
    from mlm.gui import widgets as W
    from mlm.gui.app import MainWindow
    app = QApplication.instance() or QApplication([])
    app.setStyleSheet(W.STYLE)
    # dialogs would block a headless run
    QMessageBox.information = staticmethod(lambda *a, **k: None)
    QMessageBox.critical = staticmethod(lambda *a, **k: pytest.fail(f"error dialog: {a[1:]}"))
    w = MainWindow(str(tmp_path / "gui.db"))
    w.show()
    w.app_ = app
    yield w
    w.close()


def wait(win):
    while win.busy():
        win.app_.processEvents()
    win.worker.wait()
    win.app_.processEvents()


def test_every_page(win, library):
    win.scan_folder(str(library))
    wait(win)
    for key in win.keys:
        win.go(key)
        win.app_.processEvents()
    lib = win.pages["library"]
    win.go("library")
    assert len(lib.table.source_model.rows) == 6
    lib.search.setText("One")
    lib.reload()
    assert len(lib.table.source_model.rows) == 1
    win.go("dashboard")
    assert win.pages["dashboard"].cards["songs"].value.text() == "6"
    win.go("library", problem="corrupt")
    assert len(lib.table.source_model.rows) == 1
    insp = win.pages["inspector"]
    win.go("inspector", track_id=lib.table.source_model.rows[0]["id"])
    assert insp.table.selected_rows()
    dup = win.pages["duplicates"]
    win.go("duplicates")
    dup.find()
    wait(win)
    win.go("artwork")
    assert win.pages["artwork"].albums.count() >= 1
    org = win.pages["organize"]
    win.go("organize")
    org.dest.setText(str(library))
    org.preview()
    assert org.plan


def test_read_only_default_blocks_apply(win, library):
    from mlm import repair
    assert win.edit_mode is False
    win.scan_folder(str(library))
    wait(win)
    tid = win.conn.execute("SELECT id FROM tracks WHERE title='One'").fetchone()[0]
    repair.queue_manual(win.conn, [tid], {"genre": "Jazz"})
    with pytest.raises(repair.ReadOnlyError):
        repair.apply_suggestions(win.conn, [1], win.edit_mode)


def test_inspector_shows_album_match(win, library):
    import json
    win.scan_folder(str(library))
    wait(win)
    tid = win.conn.execute("SELECT id FROM tracks WHERE title='One'").fetchone()[0]
    cands = [dict(title="12 tracks", artist="Artist A", release="Album X", date="2001", country="GB", score=91,
                  length=0, id="rel", album=True)]
    win.conn.execute("INSERT OR REPLACE INTO mb_matches VALUES(?,?,?,?,?,?,?)",
                     (tid, "Verified", 91, "rec", "rel", json.dumps(cands), 0))
    win.conn.commit()
    win.go("inspector", track_id=tid)
    insp = win.pages["inspector"]
    insp.filter.setCurrentText("All songs")
    insp.select_id(tid)
    texts = [w.text() for w in insp.detail.findChildren(type(insp.count))]
    assert any("Matched as part of the whole album" in t for t in texts)


def test_saving_acoustid_key_refreshes_verification_banner(win):
    v = win.pages["verification"]
    win.go("verification")
    assert "No AcoustID API key" in v.banner.text()
    s = win.pages["settings"]
    win.go("settings")
    s.acoustid.setText("abc123")
    s.save_key()
    win.go("verification")
    assert not v.banner.isVisibleTo(win) or "No AcoustID API key" not in v.banner.text()


def test_missing_ffmpeg_banner(win, monkeypatch):
    from mlm import scanner
    win.go("dashboard")
    d = win.pages["dashboard"]
    d.refresh()
    assert d.deps.isVisibleTo(win) == (not scanner.tool("fpcalc"))      # only the optional-tool note, or nothing
    monkeypatch.setattr(scanner, "tool", lambda name: None)
    d.refresh()
    assert d.deps.isVisibleTo(win) and "brew install ffmpeg" in d.deps.text()
    monkeypatch.setattr(scanner, "tool", lambda name: "/x/" + name)
    d.refresh()
    assert not d.deps.isVisibleTo(win)


def test_sidebar_groups_icons_and_pinned_settings(win):
    from PySide6.QtWidgets import QLabel, QPushButton, QScrollArea
    from mlm.gui import icons
    labels = [l.text() for l in win.findChildren(QLabel, "navsection")]
    assert labels == ["LIBRARY", "FIX & IMPROVE", "TOOLS"]
    navs = [b for b in win.findChildren(QPushButton, "nav") if b.accessibleName()]
    assert len(navs) == len(win.keys) and all(not b.icon().isNull() for b in navs)
    assert all(b.accessibleName() and b.toolTip().startswith(b.accessibleName()) for b in navs)
    scroll = win.findChild(QScrollArea, "navscroll")
    settings = next(b for b in navs if b.accessibleName() == "Settings")
    assert not scroll.widget().isAncestorOf(settings)             # Settings stays reachable however small the window
    assert set(icons.PATHS) >= {"dashboard", "settings", "lock", "edit"}


def test_keyboard_shortcuts_and_toast(win):
    from PySide6.QtGui import QKeySequence
    keys = {a.shortcut().toString() for a in win.actions()}
    assert {"Ctrl+1", "Ctrl+9", "Ctrl+,", "Ctrl+F"} <= keys
    win.go("library")
    box = win.pages["library"].search
    calls = []
    box.setFocus = lambda *a: calls.append("focus")      # an offscreen window is never "active", so record the call
    win.focus_search()
    assert calls == ["focus"]
    win.toast("Added 2 songs", ms=50)
    assert "Added 2 songs" in win.statusBar().currentMessage()


def test_first_run_welcome_and_empty_library_hint(win, library):
    d = win.pages["dashboard"]
    win.go("dashboard")
    d.refresh()
    assert d.welcome.isVisibleTo(win) and not d.scroll.isVisibleTo(win)
    lib = win.pages["library"]
    win.go("library")
    lib.reload()
    assert "Scan Folder" in lib.count.text()
    win.scan_folder(str(library))
    wait(win)
    win.go("dashboard")
    assert not d.welcome.isVisibleTo(win) and d.scroll.isVisibleTo(win)
    win.go("library")
    lib.search.setText("zzz-no-such-song")
    lib.reload()
    assert "Clear Filters" in lib.count.text()


def test_focus_and_pressed_states_exist_in_theme():
    from mlm.gui import widgets as W
    for needle in ("QPushButton:focus", "QLineEdit:focus", "QPushButton:pressed", "QTableView::item:hover",
                   "QPushButton#nav:checked"):
        assert needle in W.STYLE
    assert "font-size: 11" not in W.STYLE and "font-size: 10" not in W.STYLE      # nothing below 12 px


def test_icon_only_buttons_have_names(win):
    from PySide6.QtWidgets import QPushButton
    win.go("playlists")
    names = {b.accessibleName() for b in win.pages["playlists"].findChildren(QPushButton)}
    assert {"Move up", "Move down"} <= names
