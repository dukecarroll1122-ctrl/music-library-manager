"""Artwork Manager → "Find Missing Artwork" tab: bulk search, review grid, apply approved covers."""
from collections import Counter

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (QAbstractItemView, QButtonGroup, QComboBox, QHBoxLayout, QLabel, QTableWidget,
                               QTableWidgetItem, QToolButton, QVBoxLayout, QWidget)

from .. import artfetch
from . import widgets as W
from .pages_main import button

FILTERS = {"To review": ["pending"], "Approved": ["approved"], "Rejected": ["rejected"], "Applied": ["applied"],
           "No cover found": ["none"], "Failed": ["failed"], "All": None}
STATUS_TEXT = {"pending": ("To review", W.AMBER), "approved": ("Approved", W.GREEN), "rejected": ("Rejected", W.RED),
               "applied": ("Applied ✓", W.BLUE), "none": ("No cover found", W.MUTED), "failed": ("Failed", W.RED)}
SOURCE = {"caa": "Cover Art Archive", "itunes": "iTunes", "library": "Your files"}


class BulkArtTab(QWidget):
    HEAD = ["Album / folder", "Songs", "Confidence", "Status", "Choose a cover (click to approve)"]

    def __init__(self, app):
        super().__init__()
        self.app = app
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 10, 0, 0)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        self.summary.setTextFormat(Qt.RichText)
        lay.addWidget(self.summary)
        lay.addWidget(W.hbox(None, button("Search Again", lambda: self.find(redo=True),
                                                         tip="Search every group again (keeps your approvals)"),
                             button("Find Covers", self.find, "primary",
                                    tip="Search the Cover Art Archive and iTunes for every album or folder without "
                                        "artwork. Nothing is changed.")))
        self.filter = QComboBox()
        self.filter.addItems(list(FILTERS))
        self.conf = QComboBox()
        self.conf.addItems(["Any confidence", "High", "Possible"])
        for w in (self.filter, self.conf):
            w.currentIndexChanged.connect(lambda _: self.reload())
        lay.addWidget(W.hbox(self.filter, self.conf, None))
        self.table = QTableWidget(0, len(self.HEAD))
        self.table.setHorizontalHeaderLabels(self.HEAD)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(150)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setShowGrid(False)
        self.table.setWordWrap(True)
        self.table.horizontalHeader().setStretchLastSection(True)
        for i, w in enumerate([300, 60, 90, 110]):
            self.table.setColumnWidth(i, w)
        lay.addWidget(self.table, 1)
        lay.addWidget(W.hbox(W.label("Covers are only added to songs that have none — existing artwork is never "
                                     "replaced. Each album is backed up and can be undone in Repair history.",
                                     "muted", True), None,
                             button("Approve", lambda: self.mark("approved")),
                             button("Reject", lambda: self.mark("rejected")),
                             button("Reset", lambda: self.mark("pending")),
                             button("Approve All High-Confidence", self.approve_high),
                             button("Apply Approved…", self.apply, "primary")))
        self.groups = []

    def refresh(self):
        artfetch.ensure(self.app.conn)
        self.reload()

    def reload(self):
        c = self.app.conn
        all_jobs = artfetch.jobs(c)
        counts = Counter(j["status"] for j in all_jobs)
        missing = c.execute("SELECT count(*) FROM tracks WHERE missing=0 AND readable=1 AND has_art=0").fetchone()[0]
        if all_jobs:
            self.summary.setText(
                f"<b>{missing:,}</b> songs still without artwork · {len(all_jobs)} albums/folders searched: "
                f"<span style='color:{W.AMBER}'>{counts['pending']} to review</span> · "
                f"<span style='color:{W.GREEN}'>{counts['approved']} approved</span> · {counts['applied']} applied · "
                f"{counts['none']} with no cover found")
        else:
            self.summary.setText(f"<b>{missing:,}</b> songs have no artwork. Click <b>Find Covers</b> to search for "
                                 "all of them at once; you review every cover before anything is changed.")
        want = FILTERS[self.filter.currentText()]
        conf = self.conf.currentText()
        jobs = [j for j in all_jobs if (want is None or j["status"] in want)
                and (conf == "Any confidence" or j["confidence"] == conf)]
        for r in range(self.table.rowCount()):  # hide old row widgets now; Qt deletes them later
            for col in (0, 4):
                w = self.table.cellWidget(r, col)
                if w:
                    w.hide()
        self.table.setRowCount(0)
        self.table.setRowCount(len(jobs))
        self.groups = []
        for row, j in enumerate(jobs):
            label = QLabel(f"<b>{j['label']}</b><br><span style='color:{W.MUTED}'>{j['note'] or ''}</span>"
                           + (f"<br><span style='color:{W.RED}'>{j['error']}</span>" if j["error"] else ""))
            label.setWordWrap(True)
            label.setTextFormat(Qt.RichText)
            label.setContentsMargins(6, 4, 6, 4)
            self.table.setCellWidget(row, 0, label)
            it = QTableWidgetItem(str(len(j["track_ids"])))
            it.setData(Qt.UserRole, j["id"])
            self.table.setItem(row, 1, it)
            ci = QTableWidgetItem(j["confidence"] or "—")
            ci.setForeground(W.QColor(W.GREEN if j["confidence"] == "High" else W.AMBER if j["confidence"] else
                                      W.MUTED))
            self.table.setItem(row, 2, ci)
            text, color = STATUS_TEXT.get(j["status"], (j["status"], W.TEXT))
            si = QTableWidgetItem(text)
            si.setForeground(W.QColor(color))
            self.table.setItem(row, 3, si)
            self.table.setCellWidget(row, 4, self._cands(j))

    def _cands(self, j):
        box = QWidget()
        h = QHBoxLayout(box)
        h.setContentsMargins(4, 4, 4, 4)
        h.setSpacing(8)
        group = QButtonGroup(box)
        group.setExclusive(True)
        editable = j["status"] in ("pending", "approved", "rejected", "failed")
        for c in j["cands"]:
            b = QToolButton()
            b.setCheckable(True)
            b.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
            b.setIconSize(QSize(100, 100))
            pm = W.pixmap(c["thumb"], 100)
            if not pm.isNull():
                b.setIcon(QIcon(pm))
            b.setText(f"{SOURCE.get(c['source'], c['source'])}\n{(c['date'] or '')[:4]} {c['rtype'][:14]}".strip())
            b.setToolTip(f"{c['title']}\n{c['artist']}\n{c['rtype']} {c['date']} {c['country']}\n"
                         f"Source: {SOURCE.get(c['source'], c['source'])} · match {c['score']:.0%}")
            b.setChecked(c["idx"] == j["chosen"] and j["status"] in ("approved", "applied"))
            b.setEnabled(editable or b.isChecked())
            b.setStyleSheet(f"QToolButton {{ border: 2px solid transparent; border-radius: 8px; padding: 3px; "
                            f"font-size: 11px; color: {W.MUTED}; }} QToolButton:checked {{ border-color: {W.GREEN}; "
                            f"background: #15301f; color: {W.TEXT}; }} QToolButton:hover {{ background: #1f232b; }}")
            b.clicked.connect(lambda _=False, jid=j["id"], idx=c["idx"]: self.choose(jid, idx))
            group.addButton(b)
            h.addWidget(b)
        if not j["cands"]:
            h.addWidget(W.label("No cover found. Use the Browse Albums tab to pick an image file.", "muted"))
        h.addStretch(1)
        return box

    def _selected(self):
        rows = sorted({i.row() for i in self.table.selectionModel().selectedRows()})
        return [self.table.item(r, 1).data(Qt.UserRole) for r in rows]

    def choose(self, jid, idx):
        artfetch.set_status(self.app.conn, [jid], "approved", chosen=idx)
        self.reload()

    def mark(self, status):
        ids = self._selected()
        if not ids:
            return W.info(self, "No selection", "Select one or more rows first.")
        if status == "approved":
            ids = [i for i in ids if self.app.conn.execute("SELECT count(*) FROM art_cands WHERE job_id=?",
                                                           (i,)).fetchone()[0]]
        artfetch.set_status(self.app.conn, ids, status)
        self.reload()

    def approve_high(self):
        n = artfetch.approve_high(self.app.conn)
        self.reload()
        W.info(self, "Approved", f"Approved the top cover for {n} high-confidence album(s). Review them, then click "
                                 "Apply Approved.")

    def find(self, redo=False):
        def done(st):
            msg = (f"Searched {st['searched']} album(s)/folder(s): {st['found']} with covers, {st['none']} without."
                   + (f" {st['skipped']} already searched were kept." if st["skipped"] else ""))
            if st.get("error"):
                msg += f"\n\nStopped early: {st['error']}\nClick Find Covers again to continue where it stopped."
            W.info(self, "Cover search finished", msg + "\n\nNothing has been changed. Review the covers, then apply.")
            self.reload()
        self.app.run_task("Finding covers", artfetch.search, redo=redo, on_done=done, refresh=False)

    def apply(self):
        ids = [j["id"] for j in artfetch.jobs(self.app.conn, "approved")]
        if not ids:
            return W.info(self, "Nothing approved", "Click a cover (or use Approve) to approve it first.")
        songs = sum(len(j["track_ids"]) for j in artfetch.jobs(self.app.conn, "approved"))
        if not self.app.require_edit():
            return
        if not W.confirm(self, "Add artwork?", f"Embed the chosen cover in up to {songs:,} song(s) across {len(ids)} "
                                               "album(s)/folder(s).\n\nOnly songs without artwork are changed. Full-"
                                               "size images are downloaded now. Every album is backed up and can be "
                                               "undone from Metadata Repair → Repair history."):
            return

        def done(res):
            msg = f"Added artwork to {res['songs']:,} song(s) in {res['albums']} album(s)/folder(s)."
            if res["errors"]:
                msg += f"\n\n{len(res['errors'])} problem(s):\n" + "\n".join(res["errors"][:10])
            W.info(self, "Artwork applied", msg)
            self.reload()
        self.app.run_task("Adding artwork", artfetch.apply, ids, self.app.edit_mode, on_done=done)
