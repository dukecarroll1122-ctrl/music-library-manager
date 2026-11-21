"""Playlists page, plus helpers used by the Library and Player to add songs to a playlist."""
import os

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QFileDialog, QInputDialog, QListWidget, QListWidgetItem, QSplitter, QVBoxLayout,
                               QWidget)

from .. import playlists
from . import widgets as W
from .pages_main import Page, button


def pick_playlist(parent, app):
    """Ask which playlist to add to (or create one). Returns (id, name) or None."""
    existing = playlists.list_all(app.conn)
    new = "＋ New playlist…"
    item, ok = QInputDialog.getItem(parent, "Add to playlist", "Playlist:", [new] + [p["name"] for p in existing], 0,
                                    False)
    if not ok:
        return None
    if item != new:
        return next((p["id"], p["name"]) for p in existing if p["name"] == item)
    name, ok = QInputDialog.getText(parent, "New playlist", "Name:")
    if not ok:
        return None
    try:
        return playlists.create(app.conn, name), name.strip()
    except ValueError as e:
        W.error(parent, "Playlist", str(e))
        return None


def add_to_playlist(parent, app, track_ids):
    if not track_ids:
        return W.info(parent, "No songs", "Select one or more songs first.")
    picked = pick_playlist(parent, app)
    if not picked:
        return
    n = playlists.add_tracks(app.conn, picked[0], track_ids)
    app.pages["playlists"].dirty = True
    W.info(parent, "Added to playlist", f"Added {n} song(s) to “{picked[1]}”."
           + (f"\n{len(set(track_ids)) - n} were already in it." if n < len(set(track_ids)) else ""))


class PlaylistsPage(Page):
    COLS = [("pos", "#", lambda v: str(v + 1)), ("name", "Title", None), ("artist", "Artist", None),
            ("album", "Album", None), ("duration", "Time", W.fmt_dur), ("state", "", None)]

    def __init__(self, app):
        super().__init__(app)
        self.outer.addWidget(W.page_header("Playlists", "Ordered lists of your songs. They live in this app only — "
                                           "your music files are never changed. Export to M3U for other players."))
        self.lists = QListWidget()
        self.lists.currentItemChanged.connect(lambda *_: self.show_tracks())
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.addWidget(self.lists, 1)
        ll.addWidget(W.hbox(button("New…", self.new), button("Rename…", self.rename), button("Delete", self.delete)))
        ll.addWidget(W.hbox(button("Import M3U…", self.import_m3u), None))
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(12, 0, 0, 0)
        self.table = W.make_table(self.COLS, [40, 260, 170, 190, 60])
        self.table.setSortingEnabled(False)  # the order IS the playlist
        self.table.doubleClicked.connect(lambda _: self.play(selected=True))
        rl.addWidget(self.table, 1)
        self.count = W.label("", "muted")
        rl.addWidget(self.count)
        rl.addWidget(W.hbox(button("▶ Play", lambda: self.play(selected=False), "primary"),
                            button("Play Selected", lambda: self.play(selected=True)), None,
                            button("↑", lambda: self.move(-1)), button("↓", lambda: self.move(1)),
                            button("Remove", self.remove), button("Export M3U…", self.export)))
        split = QSplitter(Qt.Horizontal)
        split.addWidget(left)
        split.addWidget(right)
        split.setSizes([280, 800])
        self.outer.addWidget(split, 1)

    # ---------- lists
    def pid(self):
        it = self.lists.currentItem()
        return it.data(Qt.UserRole) if it else None

    def refresh(self, select=None):
        select = select or self.pid()
        self.lists.blockSignals(True)
        self.lists.clear()
        for p in playlists.list_all(self.app.conn):
            it = QListWidgetItem(f"{p['name']}   ·   {p['n']}")
            it.setData(Qt.UserRole, p["id"])
            self.lists.addItem(it)
            if p["id"] == select:
                self.lists.setCurrentItem(it)
        if self.lists.currentItem() is None and self.lists.count():
            self.lists.setCurrentRow(0)
        self.lists.blockSignals(False)
        self.show_tracks()

    def show_tracks(self, reselect=None):
        pid = self.pid()
        rows = playlists.tracks(self.app.conn, pid) if pid else []
        for r in rows:
            r["_color"] = {"state": W.AMBER}
        self.table.source_model.set_rows(rows)
        gone = sum(1 for r in rows if r["state"])
        self.count.setText(("Select or create a playlist." if not pid else f"{len(rows)} songs · "
                            f"{W.fmt_dur(sum(r['duration'] or 0 for r in rows))}")
                           + (f" · {gone} not found (drive disconnected or removed from the library)" if gone else ""))
        if reselect is not None:
            for i, r in enumerate(rows):
                if r["id"] == reselect:
                    self.table.selectRow(i)

    def new(self):
        name, ok = QInputDialog.getText(self, "New playlist", "Name:")
        if ok:
            try:
                self.refresh(select=playlists.create(self.app.conn, name))
            except ValueError as e:
                W.error(self, "Playlist", str(e))

    def rename(self):
        pid = self.pid()
        if pid:
            cur = self.lists.currentItem().text().rsplit("   ·   ", 1)[0]
            name, ok = QInputDialog.getText(self, "Rename playlist", "Name:", text=cur)
            if ok:
                try:
                    playlists.rename(self.app.conn, pid, name)
                    self.refresh()
                except ValueError as e:
                    W.error(self, "Playlist", str(e))

    def delete(self):
        pid = self.pid()
        if pid and W.confirm(self, "Delete this playlist?", "The playlist is deleted. Your songs and files are not "
                                                              "touched.", ok="Delete"):
            playlists.delete(self.app.conn, pid)
            self.refresh()

    # ---------- songs
    def play(self, selected):
        rows = self.table.selected_rows() if selected else self.table.source_model.rows
        if selected and not rows:
            return W.info(self, "No selection", "Select songs first.")
        rows = [r for r in rows if not r["state"]]
        if not rows:
            return W.info(self, "Nothing to play", "These songs aren't available right now.")
        self.app.go("player", rows=rows, replace=True)

    def remove(self):
        rows = self.table.selected_rows()
        if self.pid() and rows:
            playlists.remove(self.app.conn, self.pid(), [r["id"] for r in rows])
            self.refresh()

    def move(self, delta):
        rows = self.table.selected_rows()
        if self.pid() and len(rows) == 1 and playlists.move(self.app.conn, self.pid(), rows[0]["id"], delta):
            self.show_tracks(reselect=rows[0]["id"])

    # ---------- M3U
    def export(self):
        pid = self.pid()
        if not pid:
            return W.info(self, "No playlist", "Select a playlist first.")
        name = self.lists.currentItem().text().rsplit("   ·   ", 1)[0]
        path, _ = QFileDialog.getSaveFileName(self, "Export playlist", os.path.join(os.path.expanduser("~/Desktop"),
                                                                                    f"{name}.m3u8"), "Playlist (*.m3u8 *.m3u)")
        if path:
            try:
                n = playlists.export_m3u(self.app.conn, pid, path)
            except OSError as e:
                return W.error(self, "Export failed", str(e))
            W.info(self, "Exported", f"Saved {n} song(s) to\n{path}")

    def import_m3u(self):
        path, _ = QFileDialog.getOpenFileName(self, "Import playlist", os.path.expanduser("~/Desktop"),
                                              "Playlist (*.m3u8 *.m3u)")
        if not path:
            return
        try:
            res = playlists.import_m3u(self.app.conn, path)
        except (OSError, ValueError) as e:
            return W.error(self, "Import failed", str(e))
        self.refresh(select=res["id"])
        msg = f"Created “{res['name']}” with {res['added']} song(s)."
        if res["missing"]:
            msg += f"\n\n{len(res['missing'])} entries aren't in your library (scan their folder first):\n" + \
                "\n".join(os.path.basename(m) for m in res["missing"][:10])
        W.info(self, "Playlist imported", msg)
