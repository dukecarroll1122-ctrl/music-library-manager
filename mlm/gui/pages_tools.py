"""Artwork Manager, Duplicate Finder, Organization, Reports, Settings."""
import os
import subprocess

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QColor, QIcon, QPixmap
from PySide6.QtWidgets import (QCheckBox, QComboBox, QFileDialog, QGridLayout, QLabel, QLineEdit, QListView,
                               QListWidget, QListWidgetItem, QSplitter, QTreeWidget, QTreeWidgetItem, QVBoxLayout,
                               QWidget)

from .. import db, duplicates, online, organize, repair, reports, scanner
from . import widgets as W
from .pages_main import Page, button
from .pages_meta import clear_layout, section


def placeholder_icon(size=150):
    pm = QPixmap(size, size)
    pm.fill(QColor("#1d2027"))
    return QIcon(pm)


# ------------------------------------------------------------------ Artwork
class ArtworkPage(Page):
    def __init__(self, app):
        super().__init__(app)
        self.outer.addWidget(W.page_header("Artwork Manager", "Find albums without artwork, search the Cover Art "
                                                              "Archive, preview and apply covers to whole albums."))
        self.filter = QComboBox()
        self.filter.addItems(["Missing artwork", "Partial or mixed artwork", "All albums"])
        self.filter.currentTextChanged.connect(lambda _: self.reload())
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search albums…")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(lambda _: self.reload())
        self.albums = QListWidget()
        self.albums.setViewMode(QListView.IconMode)
        self.albums.setIconSize(QSize(132, 132))
        self.albums.setGridSize(QSize(158, 196))
        self.albums.setResizeMode(QListView.Adjust)
        self.albums.setMovement(QListView.Static)
        self.albums.setWordWrap(True)
        self.albums.setUniformItemSizes(True)
        self.albums.currentItemChanged.connect(lambda *_: self.show_album())
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.addWidget(W.hbox(self.filter, self.search))
        ll.addWidget(self.albums, 1)
        self.count = W.label("", "muted")
        ll.addWidget(self.count)

        right = QWidget()
        self.rl = QVBoxLayout(right)
        self.rl.setContentsMargins(8, 0, 0, 0)
        self.title = W.label("Select an album", "h2")
        self.title.setWordWrap(True)
        self.rl.addWidget(self.title)
        self.cur_img, self.new_img = QLabel(), QLabel()
        for lab in (self.cur_img, self.new_img):
            lab.setFixedSize(230, 230)
            lab.setAlignment(Qt.AlignCenter)
            lab.setStyleSheet(f"background: {W.CARD}; border: 1px solid {W.BORDER}; border-radius: 10px; "
                              f"color: {W.MUTED};")
        self.cur_info, self.new_info = W.label("", "muted", True), W.label("", "muted", True)
        cur_box = QWidget()
        cb = QVBoxLayout(cur_box)
        cb.setContentsMargins(0, 0, 0, 0)
        cb.addWidget(W.label("Current", "h2"))
        cb.addWidget(self.cur_img)
        cb.addWidget(self.cur_info)
        new_box = QWidget()
        nb = QVBoxLayout(new_box)
        nb.setContentsMargins(0, 0, 0, 0)
        nb.addWidget(W.label("Proposed", "h2"))
        nb.addWidget(self.new_img)
        nb.addWidget(self.new_info)
        self.rl.addWidget(W.hbox(cur_box, new_box, None, spacing=18))
        self.rl.addWidget(W.hbox(button("Search Cover Art Archive", self.search_online, "primary"),
                                 button("Choose Image File…", self.choose_file), None))
        self.cands = QListWidget()
        self.cands.setViewMode(QListView.IconMode)
        self.cands.setIconSize(QSize(110, 110))
        self.cands.setGridSize(QSize(132, 168))
        self.cands.setResizeMode(QListView.Adjust)
        self.cands.setMovement(QListView.Static)
        self.cands.setWordWrap(True)
        self.cands.setMinimumHeight(190)
        self.cands.itemClicked.connect(self.pick_candidate)
        self.rl.addWidget(self.cands, 1)
        self.only_missing = QCheckBox("Only add to songs that have no artwork")
        self.rl.addWidget(W.hbox(self.only_missing, None, button("Apply to Album…", self.apply, "primary")))
        split = QSplitter(Qt.Horizontal)
        split.addWidget(left)
        split.addWidget(right)
        split.setSizes([620, 560])
        self.outer.addWidget(split, 1)
        self.proposed = None  # (bytes, mime, (w, h))

    def refresh(self):
        self.reload()

    def reload(self):
        mode = self.filter.currentText()
        having = {"Missing artwork": "HAVING sum(has_art)=0",
                  "Partial or mixed artwork": "HAVING (sum(has_art)>0 AND sum(has_art)<count(*)) "
                                              "OR count(DISTINCT art_hash)>1",
                  "All albums": ""}[mode]
        q = self.search.text().strip()
        where = "missing=0 AND album<>'' AND readable=1" + (" AND (album LIKE ? OR artist LIKE ? OR albumartist "
                                                             "LIKE ?)" if q else "")
        rows = self.app.conn.execute(
            f"SELECT coalesce(nullif(albumartist,''), artist) AS aa, album, count(*) AS n, sum(has_art) AS arts, "
            f"count(DISTINCT art_hash) AS variants, max(art_hash) AS art_hash, group_concat(id) AS ids "
            f"FROM tracks WHERE {where} GROUP BY lower(aa), lower(album) {having} ORDER BY lower(aa), lower(album)",
            [f"%{q}%"] * 3 if q else []).fetchall()
        thumbs = {r[0]: r[1] for r in self.app.conn.execute("SELECT art_hash, data FROM thumbs")}
        self.albums.clear()
        blank = placeholder_icon()
        for r in rows:
            it = QListWidgetItem(f"{r['album']}\n{r['aa'] or 'Unknown artist'}")
            pm = W.pixmap(thumbs.get(r["art_hash"]), 132) if r["art_hash"] in thumbs else None
            it.setIcon(QIcon(pm) if pm and not pm.isNull() else blank)
            it.setData(Qt.UserRole, dict(r))
            it.setToolTip(f"{r['album']} — {r['aa']}\n{r['arts']}/{r['n']} songs have artwork")
            self.albums.addItem(it)
        self.count.setText(f"{len(rows):,} albums")
        if rows:
            self.albums.setCurrentRow(0)
        self.show_album()

    def album(self):
        it = self.albums.currentItem()
        return it.data(Qt.UserRole) if it else None

    def show_album(self):
        a = self.album()
        self.cands.clear()
        self.proposed = None
        self.new_img.clear()
        self.new_img.setText("Search or choose\nan image")
        self.new_info.setText("")
        if not a:
            self.title.setText("Select an album")
            self.cur_img.clear()
            self.cur_info.setText("")
            return
        self.title.setText(f"{a['album']}  —  {a['aa'] or 'Unknown artist'}")
        ids = [int(i) for i in a["ids"].split(",")]
        # load the real embedded image from the first track that has one
        r = self.app.conn.execute(f"SELECT path, art_w, art_h, art_mime FROM tracks WHERE has_art=1 AND id IN "
                                  f"({','.join('?' * len(ids))}) LIMIT 1", ids).fetchone()
        self.cur_img.clear()
        if r:
            import mutagen
            from .. import tags
            try:
                art = tags.get_art(mutagen.File(r["path"]))
                self.cur_img.setPixmap(W.pixmap(art[0], 226))
            except Exception:
                self.cur_img.setText("Artwork unreadable")
            self.cur_info.setText(f"{r['art_w']}×{r['art_h']} {r['art_mime'] or ''}\n"
                                  f"{a['arts']} of {a['n']} songs have artwork"
                                  + (f" ({a['variants']} different images)" if a["variants"] > 1 else ""))
        else:
            self.cur_img.setText("No artwork")
            self.cur_info.setText(f"0 of {a['n']} songs have artwork")

    def search_online(self):
        a = self.album()
        if not a:
            return W.info(self, "No album", "Select an album first.")

        def done(results):
            self.cands.clear()
            for c in results:
                it = QListWidgetItem(QIcon(W.pixmap(c["thumb_data"], 110)),
                                     f"{c['title']}\n{c['date'][:4]} {c['country']}".strip())
                it.setToolTip(f"{c['title']} — {c['artist']}\n{c['date']} {c['country']}\nRelease group {c['rg']}")
                it.setData(Qt.UserRole, c)
                self.cands.addItem(it)
            if not results:
                W.info(self, "No artwork found", "The Cover Art Archive has no front cover for matching releases. "
                                                 "You can choose an image file instead.")
        self.app.run_task("Searching artwork", online.search_artwork, a["aa"] or "", a["album"], on_done=done,
                          refresh=False)

    def pick_candidate(self, item):
        c = item.data(Qt.UserRole)

        def done(res):
            self.set_proposed(*res, source=f"Cover Art Archive: {c['title']} ({c['date'][:4]})")
        self.app.run_task("Downloading full-size artwork", online.download_image, c["full"], on_done=done,
                          refresh=False)

    def choose_file(self):
        if not self.album():
            return W.info(self, "No album", "Select an album first.")
        path, _ = QFileDialog.getOpenFileName(self, "Choose cover image", os.path.expanduser("~/Pictures"),
                                              "Images (*.jpg *.jpeg *.png *.webp *.bmp *.gif *.tif *.tiff)")
        if path:
            try:
                with open(path, "rb") as fh:
                    self.set_proposed(*online.prepare_image(fh.read()), source=os.path.basename(path))
            except (OSError, ValueError) as e:
                W.error(self, "Cannot use image", str(e))

    def set_proposed(self, data, mime, size, source=""):
        self.proposed = (data, mime, size)
        self.new_img.setPixmap(W.pixmap(data, 226))
        warn = "  ⚠ low resolution" if max(size) < 500 else ""
        self.new_info.setText(f"{size[0]}×{size[1]} {mime} · {len(data) / 1024:.0f} KB{warn}\n{source}")

    def apply(self):
        a = self.album()
        if not a or not self.proposed:
            return W.info(self, "Nothing to apply", "Select an album and a proposed image first.")
        ids = [int(i) for i in a["ids"].split(",")]
        if self.only_missing.isChecked():
            ids = [r[0] for r in self.app.conn.execute(
                f"SELECT id FROM tracks WHERE has_art=0 AND id IN ({','.join('?' * len(ids))})", ids)]
        if not ids:
            return W.info(self, "Nothing to do", "Every song in this album already has artwork.")
        if not self.app.require_edit():
            return
        data, mime, size = self.proposed
        if not W.confirm(self, "Replace album artwork?",
                         f"Embed the {size[0]}×{size[1]} image in {len(ids)} song(s) of “{a['album']}”?\n\n"
                         "Existing artwork is backed up and can be restored from Repair history."):
            return
        self.app.run_task("Applying artwork", repair.apply_changes, {i: {"artwork": (data, mime)} for i in ids},
                          self.app.edit_mode, note=f"artwork: {a['album']}",
                          on_done=lambda res: W.info(self, "Artwork applied", W.summarize(res)))


# ------------------------------------------------------------------ Duplicates
class DuplicatesPage(Page):
    HEAD = ["Keep", "File", "Artist", "Title", "Album", "Format", "Bitrate", "Size", "Location"]

    def __init__(self, app):
        super().__init__(app)
        self.outer.addWidget(W.page_header(
            "Duplicate Finder", "Groups of likely duplicates. Nothing is ever deleted automatically — tick the copies "
            "to keep; unticked copies can be moved to a review folder you choose."))
        self.m_exact = QCheckBox("Identical files (hash)")
        self.m_exact.setChecked(True)
        self.m_meta = QCheckBox("Same artist + title + length")
        self.m_meta.setChecked(True)
        self.m_ac = QCheckBox("Audio fingerprint (AcoustID)")
        self.outer.addWidget(W.hbox(self.m_exact, self.m_meta, self.m_ac, None,
                                    button("Find Duplicates", self.find, "primary")))
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(self.HEAD)
        self.tree.setAlternatingRowColors(True)
        for i, w in enumerate([60, 200, 140, 160, 150, 60, 70, 70]):
            self.tree.setColumnWidth(i, w)
        self.tree.itemChanged.connect(self.keep_changed)
        self.tree.itemDoubleClicked.connect(lambda it, _c: W.reveal(it.data(0, Qt.UserRole + 1) or ""))
        self.outer.addWidget(self.tree, 1)
        self.count = W.label("", "muted")
        self.outer.addWidget(W.hbox(self.count, None,
                                    button("Auto-Select Best Quality", self.auto_keep,
                                           tip="Tick the best copy in each group (lossless › bitrate › sample "
                                               "rate › artwork) and untick the rest. Nothing is moved."),
                                    button("Keep All", self.keep_all),
                                    button("Move Unticked Copies…", self.move_unkept, "danger")))

    def refresh(self):
        self.m_ac.setEnabled(bool(db.get_setting(self.app.conn, "acoustid_key")))
        self.m_ac.setToolTip("" if self.m_ac.isEnabled() else "Add an AcoustID API key in Settings to enable")
        self.reload()

    def reload(self):
        self.tree.blockSignals(True)
        self.tree.clear()
        groups = duplicates.load_groups(self.app.conn)
        names = {"exact": "Identical files", "metadata": "Same song (metadata)", "acoustid": "Same recording "
                 "(AcoustID)"}
        extra = 0
        for g in groups:
            ms = g["members"]
            top = QTreeWidgetItem([f"{names.get(g['method'], g['method'])}  ·  {ms[0]['artist']} – {ms[0]['title']}"
                                   f"  ·  {len(ms)} copies"])
            top.setForeground(0, QColor(W.AMBER))
            self.tree.addTopLevelItem(top)
            top.setFirstColumnSpanned(True)
            for m in ms:
                it = QTreeWidgetItem(["", os.path.basename(m["path"]), m["artist"], m["title"], m["album"],
                                      m["format"], W.fmt_kbps(m["bitrate"]), W.fmt_size(m["size"]),
                                      os.path.dirname(m["path"])])
                it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
                it.setCheckState(0, Qt.Checked if m["keep"] else Qt.Unchecked)
                it.setData(0, Qt.UserRole, (g["id"], m["id"]))
                it.setData(0, Qt.UserRole + 1, m["path"])
                it.setToolTip(8, m["path"])
                top.addChild(it)
            extra += len(ms) - 1
            top.setExpanded(True)
        self.tree.blockSignals(False)
        self.count.setText(f"{len(groups):,} groups · {extra:,} extra copies" if groups else
                           "No duplicate groups. Click Find Duplicates.")

    def keep_changed(self, item, col):
        d = item.data(0, Qt.UserRole)
        if d and col == 0:
            self.app.conn.execute("UPDATE dup_members SET keep=? WHERE group_id=? AND track_id=?",
                                  (int(item.checkState(0) == Qt.Checked), d[0], d[1]))
            self.app.conn.commit()

    def find(self):
        methods = [m for m, cb in (("exact", self.m_exact), ("metadata", self.m_meta), ("acoustid", self.m_ac))
                   if cb.isChecked() and cb.isEnabled()]
        if not methods:
            return W.info(self, "No method", "Choose at least one detection method.")

        def run(conn, methods, progress=None, cancel=None):
            fns = {"exact": duplicates.find_exact, "metadata": duplicates.find_metadata,
                   "acoustid": duplicates.find_acoustic}
            return {m: fns[m](conn, progress=progress, cancel=cancel) for m in methods}

        def done(res):
            W.info(self, "Duplicate search finished",
                   "\n".join(f"{k}: {v if v is not None else 'cancelled'} group(s)" for k, v in res.items()))
        self.app.run_task("Finding duplicates", run, methods, on_done=done)

    def _each_group(self):
        for i in range(self.tree.topLevelItemCount()):
            top = self.tree.topLevelItem(i)
            yield [top.child(j) for j in range(top.childCount())]

    def auto_keep(self):
        groups = {g["id"]: g for g in duplicates.load_groups(self.app.conn)}
        for items in self._each_group():
            gid = items[0].data(0, Qt.UserRole)[0]
            best = duplicates.best_copy(groups[gid]["members"])
            for it in items:
                it.setCheckState(0, Qt.Checked if it.data(0, Qt.UserRole)[1] == best else Qt.Unchecked)

    def keep_all(self):
        for items in self._each_group():
            for it in items:
                it.setCheckState(0, Qt.Checked)

    def move_unkept(self):
        unkept, seen = [], set()
        for items in self._each_group():
            if not any(it.checkState(0) == Qt.Checked for it in items):
                continue  # never move every copy of a song
            for it in items:
                tid = it.data(0, Qt.UserRole)[1]
                if it.checkState(0) != Qt.Checked and tid not in seen:
                    seen.add(tid)
                    unkept.append((tid, it.data(0, Qt.UserRole + 1)))
        kept = {it.data(0, Qt.UserRole)[1] for items in self._each_group() for it in items
                if it.checkState(0) == Qt.Checked}
        unkept = [u for u in unkept if u[0] not in kept]  # a file kept in any group is never moved
        if not unkept:
            return W.info(self, "Nothing to move", "Untick the copies you don't want to keep (each group must keep "
                                                   "at least one ticked copy).")
        if not self.app.require_edit():
            return
        dest = QFileDialog.getExistingDirectory(self, "Choose a review folder for unticked copies",
                                                os.path.expanduser("~/Music"))
        if not dest:
            return
        moves, used = [], set()
        for tid, path in unkept:
            base, ext = os.path.splitext(os.path.basename(path))
            cand, n = os.path.join(dest, base + ext), 2
            while os.path.exists(cand) or cand in used:
                cand, n = os.path.join(dest, f"{base} ({n}){ext}"), n + 1
            used.add(cand)
            moves.append((tid, cand))
        if not W.confirm(self, "Move unticked copies?",
                         f"Move {len(moves)} file(s) to:\n{dest}\n\nFiles are moved, not deleted, and the move can "
                         "be undone from Repair history. Delete them yourself later once you're sure.", ok="Move"):
            return
        self.app.run_task("Moving duplicate copies", repair.move_files, moves, self.app.edit_mode,
                          note="duplicates → review folder",
                          on_done=lambda res: W.info(self, "Moved", W.summarize(res, "Moved")))


# ------------------------------------------------------------------ Organization
class OrganizePage(Page):
    COLS = [("status", "Status", None), ("src_rel", "Current location", None), ("dst_rel", "Proposed location", None)]

    def __init__(self, app):
        super().__init__(app)
        self.outer.addWidget(W.page_header(
            "Organization", "Propose a clean folder structure and file names. Preview every move first; existing "
            "files are never overwritten."))
        self.source = QLineEdit()
        self.source.setPlaceholderText("Only songs inside this folder (empty = whole library)")
        self.dest = QLineEdit()
        self.dest.setPlaceholderText("Destination root folder")
        self.template = QComboBox()
        self.template.setEditable(True)
        self.template.addItems(organize.PRESETS)
        self.template.setMinimumWidth(420)
        grid = QGridLayout()
        grid.addWidget(W.label("Songs from", "muted"), 0, 0)
        grid.addWidget(self.source, 0, 1)
        grid.addWidget(button("Browse…", lambda: self._browse(self.source)), 0, 2)
        grid.addWidget(W.label("Organize into", "muted"), 1, 0)
        grid.addWidget(self.dest, 1, 1)
        grid.addWidget(button("Browse…", lambda: self._browse(self.dest)), 1, 2)
        grid.addWidget(W.label("Naming", "muted"), 2, 0)
        grid.addWidget(self.template, 2, 1)
        grid.addWidget(button("Preview", self.preview, "primary"), 2, 2)
        grid.addWidget(W.label(f"Fields: {organize.FIELDS_HELP}. Use / for folders. The file extension is "
                               "kept automatically.", "hint", True), 3, 1)
        w = QWidget()
        w.setLayout(grid)
        self.outer.addWidget(w)
        self.table = W.make_table(self.COLS, [210, 440])
        self.outer.addWidget(self.table, 1)
        self.count = W.label("", "muted")
        self.outer.addWidget(W.hbox(self.count, None, button("Apply Moves…", self.apply, "primary")))
        self.plan = []

    def _browse(self, edit):
        d = QFileDialog.getExistingDirectory(self, "Choose folder", edit.text() or os.path.expanduser("~/Music"))
        if d:
            edit.setText(d)

    def refresh(self):
        tmpl = db.get_setting(self.app.conn, "org_template")
        if tmpl and self.template.currentText() == organize.PRESETS[0]:
            self.template.setCurrentText(tmpl)
        if not self.dest.text():
            last = db.get_setting(self.app.conn, "last_root")
            self.dest.setText(last)

    def _tracks(self):
        src = self.source.text().strip().rstrip("/")
        q = "SELECT * FROM tracks WHERE missing=0 AND readable=1"
        args = []
        if src:
            q += " AND substr(path,1,?)=?"
            args = [len(src) + 1, src + "/"]
        return [dict(r) for r in self.app.conn.execute(q + " ORDER BY path", args)]

    def preview(self):
        dest = self.dest.text().strip()
        if not dest or not os.path.isdir(dest):
            return W.info(self, "Destination", "Choose an existing destination folder.")
        try:
            self.plan = organize.plan(self._tracks(), dest, self.template.currentText())
        except ValueError as e:
            return W.error(self, "Naming template", str(e))
        db.set_setting(self.app.conn, "org_template", self.template.currentText())
        for p in self.plan:
            p["src_rel"] = p["src"]
            p["dst_rel"] = os.path.relpath(p["dst"], dest)
            col = {"ok": W.GREEN, "unchanged": W.MUTED}.get(p["status"], W.RED)
            p["_color"] = {"status": col}
        self.plan.sort(key=lambda p: (p["status"] == "unchanged", p["status"] == "ok", p["dst_rel"]))
        self.table.source_model.set_rows(self.plan)
        n = {s: sum(1 for p in self.plan if p["status"] == s) for s in ("ok", "unchanged")}
        bad = len(self.plan) - n["ok"] - n["unchanged"]
        self.count.setText(f"{n['ok']:,} to move · {n['unchanged']:,} already in place · {bad:,} skipped "
                           "(conflicts or missing)")

    def apply(self):
        ok = [(p["id"], p["dst"]) for p in self.plan if p["status"] == "ok"]
        if not ok:
            return W.info(self, "Nothing to move", "Preview first. Only rows with status “ok” are moved.")
        if not self.app.require_edit():
            return
        if not W.confirm(self, "Move and rename files?",
                         f"{len(ok):,} file(s) will be moved/renamed as shown in the preview. Conflicting files are "
                         "skipped, nothing is overwritten, and the batch can be undone from Repair history.",
                         ok="Move files"):
            return

        def done(res):
            self.plan = []
            self.table.source_model.set_rows([])
            W.info(self, "Organization finished", W.summarize(res, "Moved"))
        self.app.run_task("Organizing files", repair.move_files, ok, self.app.edit_mode, note="organize",
                          on_done=done)


# ------------------------------------------------------------------ Reports
class ReportsPage(Page):
    DESC = {"Complete music inventory": "Every song with all metadata and technical details.",
            "Artist list": "Artists with song and album counts and storage used.",
            "Album list": "Albums with year, song count, artwork coverage and formats.",
            "Missing metadata report": "Songs missing title, artist, album, track, year, genre or artwork.",
            "Metadata errors report": "Suspicious metadata and unreadable/corrupted files.",
            "Duplicate report": "All duplicate groups from the Duplicate Finder.",
            "Audio quality report": "Format, bitrate, sample rate and a quality grade for every song.",
            "Audio diagnostics report": "Path, format, error message and inspection result for every file that "
                                        "is not plainly healthy, or needed a fallback reader."}

    def __init__(self, app):
        super().__init__(app)
        self.outer.addWidget(W.page_header("Reports", "Export your library data as CSV (Excel/Numbers) or JSON."))
        grid = QGridLayout()
        grid.setSpacing(12)
        for i, name in enumerate(reports.REPORTS):
            fr, lay = section(name)
            lay.addWidget(W.label(self.DESC[name], "muted", True))
            lay.addWidget(W.hbox(None, button("CSV", lambda _=False, n=name: self.export(n, "csv")),
                                 button("JSON", lambda _=False, n=name: self.export(n, "json"))))
            grid.addWidget(fr, i // 2, i % 2)
        w = QWidget()
        w.setLayout(grid)
        self.outer.addWidget(w)
        self.outer.addStretch(1)

    def export(self, name, fmt):
        default = os.path.join(os.path.expanduser("~/Desktop"), name.lower().replace(" ", "_") + "." + fmt)
        path, _ = QFileDialog.getSaveFileName(self, f"Export {name}", default, f"{fmt.upper()} (*.{fmt})")
        if not path:
            return
        try:
            n = reports.export(self.app.conn, name, path, fmt)
        except OSError as e:
            return W.error(self, "Export failed", str(e))
        W.info(self, "Exported", f"Saved {n:,} rows to\n{path}")


# ------------------------------------------------------------------ Settings
class SettingsPage(Page):
    def __init__(self, app):
        super().__init__(app)
        self.outer.addWidget(W.page_header("Settings"))
        fr, lay = section("Operating mode")
        self.mode_label = W.label("", wrap=True)
        lay.addWidget(self.mode_label)
        lay.addWidget(W.hbox(button("Toggle Mode", app.toggle_mode), None))
        app.mode_changed.connect(lambda _: self.refresh())
        self.outer.addWidget(fr)

        fr, lay = section("Online services")
        self.contact = QLineEdit()
        self.contact.setPlaceholderText("optional: your email or website, sent in the MusicBrainz User-Agent")
        self.contact.editingFinished.connect(lambda: db.set_setting(app.conn, "mb_contact", self.contact.text()))
        self.acoustid = QLineEdit()
        self.acoustid.setPlaceholderText("optional AcoustID application API key")
        self.acoustid.editingFinished.connect(self.save_key)
        g = QGridLayout()
        g.addWidget(W.label("MusicBrainz contact", "muted"), 0, 0)
        g.addWidget(self.contact, 0, 1)
        g.addWidget(W.label("AcoustID API key", "muted"), 1, 0)
        g.addWidget(self.acoustid, 1, 1)
        lay.addLayout(g)
        self.fp_status = W.label("", "muted", True)
        lay.addWidget(self.fp_status)
        lay.addWidget(W.label("MusicBrainz needs no key; requests are limited to one per second and cached for 30 "
                              "days. For audio fingerprinting, register a free application at "
                              "<a style='color:#5b8cff' href='https://acoustid.org/new-application'>"
                              "acoustid.org/new-application</a>, paste the key above, and install Chromaprint: "
                              "<code>brew install chromaprint</code>.", "muted", True))
        lay.itemAt(lay.count() - 1).widget().setOpenExternalLinks(True)
        self.outer.addWidget(fr)

        fr, lay = section("Library & data")
        self.roots = QListWidget()
        self.roots.setMaximumHeight(110)
        lay.addWidget(W.label("Scanned folders", "muted"))
        lay.addWidget(self.roots)
        lay.addWidget(W.hbox(button("Scan Folder…", lambda: app.scan_folder()),
                             button("Rescan Selected", self.rescan),
                             button("Remove Selected from Library", self.remove_root), None))
        self.data_label = W.label("", "muted", True)
        lay.addWidget(self.data_label)
        lay.addWidget(W.hbox(button("Open Data Folder", lambda: subprocess.Popen(["open", str(db.data_dir())])),
                             button("Clear MusicBrainz Cache", self.clear_cache), None))
        self.outer.addWidget(fr)
        self.outer.addStretch(1)

    def save_key(self):
        db.set_setting(self.app.conn, "acoustid_key", self.acoustid.text().strip())
        self.app.pages["duplicates"].dirty = True

    def refresh(self):
        c = self.app.conn
        self.mode_label.setText(
            "<b style='color:#f5a524'>Edit Mode</b> — approved changes can be written to files." if self.app.edit_mode
            else "<b style='color:#3ecf8e'>Read-Only Mode</b> (default) — scanning, inspection and reports only. "
                 "Your files cannot be modified.")
        self.contact.setText(db.get_setting(c, "mb_contact"))
        self.acoustid.setText(db.get_setting(c, "acoustid_key"))
        fp = scanner.tool("fpcalc")
        ff = scanner.tool("ffprobe")
        self.fp_status.setText(f"fpcalc (Chromaprint): {'found at ' + fp if fp else 'not installed'}   ·   "
                               f"FFmpeg/ffprobe: {'found' if ff else 'not installed (brew install ffmpeg)'}")
        self.roots.clear()
        for r in c.execute("SELECT root, count(*) n, sum(missing) m FROM tracks GROUP BY root ORDER BY root"):
            avail = "" if os.path.isdir(r["root"]) else "   (not connected)"
            it = QListWidgetItem(f"{r['root']}   —   {r['n']:,} songs{avail}")
            it.setData(Qt.UserRole, r["root"])
            self.roots.addItem(it)
        n = c.execute("SELECT count(*) FROM http_cache").fetchone()[0]
        self.data_label.setText(f"Data folder: {db.data_dir()}\nDatabase: {self.app.db_path}\n"
                                f"Backups: {db.data_dir() / 'backups'}  ·  Change log: "
                                f"{db.data_dir() / 'changes.log'}\nCached web responses: {n:,}")

    def _root(self):
        it = self.roots.currentItem()
        if not it:
            W.info(self, "No folder", "Select a scanned folder first.")
        return it.data(Qt.UserRole) if it else None

    def rescan(self):
        r = self._root()
        if r:
            self.app.scan_folder(r)

    def remove_root(self):
        r = self._root()
        if not r or not W.confirm(self, "Remove from library?",
                                  f"Forget all songs under\n{r}\n\nThis only removes entries from this app's "
                                  "database. Your music files are not touched.", ok="Remove"):
            return
        c = self.app.conn
        ids = [x[0] for x in c.execute("SELECT id FROM tracks WHERE root=?", (r,))]
        for t in ("issues", "suggestions", "mb_matches", "dup_members"):
            c.executemany(f"DELETE FROM {t} WHERE track_id=?", [(i,) for i in ids])
        c.execute("DELETE FROM tracks WHERE root=?", (r,))
        c.commit()
        self.app.library_changed.emit()

    def clear_cache(self):
        self.app.conn.execute("DELETE FROM http_cache")
        self.app.conn.commit()
        self.refresh()
