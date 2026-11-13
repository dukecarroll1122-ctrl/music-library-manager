"""Artwork Manager → Missing Artwork: queue of albums/folders without artwork, batch search across every source,
review, and apply approved covers."""
from collections import Counter

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QHBoxLayout, QLabel, QTableWidget, QTableWidgetItem,
                               QToolButton, QVBoxLayout, QWidget)

from .. import artfetch, artmatch as am
from . import widgets as W
from .art_search import SearchDialog
from .art_widgets import CONF_COLOR, SOURCE, ApplyDialog, apply_selection, res_text, summarize_apply
from .pages_main import button

STATUS_TEXT = {"pending": ("Awaiting approval", W.AMBER), "approved": ("Approved", W.GREEN),
               "rejected": ("Rejected", W.RED), "applied": ("Applied ✓", W.BLUE), "none": ("No results", W.MUTED),
               "failed": ("Failed", W.RED)}
FILTERS = ["Awaiting approval", "Multiple candidates", "Matched (approved / applied)", "No results", "Rejected",
           "Failed", "All"]


def category(j):
    """Queue category for filtering. 'Multiple candidates' = two or more Strong/Exact covers to choose between."""
    safe = [c for c in j["cands"] if not c["rejected"] and c["confidence"] in am.SAFE]
    if j["status"] == "pending" and len(safe) >= 2:
        return "Multiple candidates"
    return {"pending": "Awaiting approval", "approved": "Matched (approved / applied)",
            "applied": "Matched (approved / applied)", "none": "No results", "rejected": "Rejected",
            "failed": "Failed"}.get(j["status"], "All")


class BulkArtTab(QWidget):
    HEAD = ["Album / folder", "Songs", "Best match", "Status", "Candidates (click a cover to approve it)"]

    def __init__(self, app):
        super().__init__()
        self.app = app
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 10, 0, 0)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        self.summary.setTextFormat(Qt.RichText)
        lay.addWidget(self.summary)
        self.filter = QComboBox()
        self.filter.addItems(FILTERS)
        self.conf = QComboBox()
        self.conf.addItems(["Any confidence"] + list(am.LEVELS))
        for w in (self.filter, self.conf):
            w.currentIndexChanged.connect(lambda _: self.reload())
        lay.addWidget(W.hbox(self.filter, self.conf, None,
                             button("Search This Album…", self.open_selected,
                                    tip="Open the full search window for the selected row"),
                             button("Search Selected", self.search_selected,
                                    tip="Search every source again for the selected rows"),
                             button("Retry No-Result Searches", lambda: self.find(retry=True),
                                    tip="Search again (bypassing the cache) for albums that found nothing"),
                             button("Search All Missing Artwork", self.find, "primary",
                                    tip="Search all sources for every album and folder without artwork. Nothing is "
                                        "changed; you review every cover.")))
        self.table = QTableWidget(0, len(self.HEAD))
        self.table.setHorizontalHeaderLabels(self.HEAD)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(150)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setShowGrid(False)
        self.table.horizontalHeader().setStretchLastSection(True)
        for i, w in enumerate([300, 60, 130, 120]):
            self.table.setColumnWidth(i, w)
        self.table.doubleClicked.connect(lambda _: self.open_selected())
        lay.addWidget(self.table, 1)
        note = W.label("Weak and unverified matches are never approved automatically.", "muted", True)
        note.setMinimumWidth(300)
        lay.addWidget(W.hbox(note, None,
                             button("Approve", lambda: self.mark("approved")), button("Reject", lambda: self.mark("rejected")),
                             button("Reset", lambda: self.mark("pending")),
                             button("Approve All Exact / Strong", self.approve_safe,
                                    tip="Approve the best official Exact or Strong cover of every pending album"),
                             button("Apply Approved…", self.apply, "primary")))

    def refresh(self):
        artfetch.ensure(self.app.conn)
        self.reload()

    # ---------- table
    def reload(self):
        c = self.app.conn
        all_jobs = artfetch.jobs(c)
        cats = Counter(category(j) for j in all_jobs)
        missing = c.execute("SELECT count(*) FROM tracks WHERE missing=0 AND readable=1 AND has_art=0").fetchone()[0]
        if all_jobs:
            self.summary.setText(
                f"<b>{missing:,}</b> songs still without artwork · {len(all_jobs)} albums/folders searched: "
                f"<span style='color:{W.AMBER}'>{cats['Awaiting approval'] + cats['Multiple candidates']} awaiting "
                f"approval</span> ({cats['Multiple candidates']} with several good candidates) · "
                f"<span style='color:{W.GREEN}'>{cats['Matched (approved / applied)']} matched</span> · "
                f"{cats['No results']} with no results")
        else:
            self.summary.setText(f"<b>{missing:,}</b> songs have no artwork. Click <b>Search Every Album Missing "
                                 "Artwork</b> to search MusicBrainz, iTunes, Deezer, Discogs and fanart.tv at once. "
                                 "You review every cover before anything is changed.")
        f = self.filter.currentText()
        want = self.conf.currentText()
        jobs = [j for j in all_jobs if (f == "All" or category(j) == f or (f == "Awaiting approval" and
                                                                           category(j) == "Multiple candidates"))
                and (want == "Any confidence" or j["confidence"] == want)]
        for r in range(self.table.rowCount()):
            for col in (0, 4):
                w = self.table.cellWidget(r, col)
                if w:
                    w.hide()
        self.table.setRowCount(0)
        self.table.setRowCount(len(jobs))
        for row, j in enumerate(jobs):
            label = QLabel(f"<b>{j['label']}</b>"
                           + (f" <span style='color:{W.VIOLET}'>🎮 soundtrack</span>" if j["mode"] == "game" else "")
                           + f"<br><span style='color:{W.MUTED}'>{j['note'] or ''}</span>"
                           + (f"<br><span style='color:{W.RED}'>{j['error']}</span>" if j["error"] else ""))
            label.setWordWrap(True)
            label.setTextFormat(Qt.RichText)
            label.setContentsMargins(6, 4, 6, 4)
            self.table.setCellWidget(row, 0, label)
            it = QTableWidgetItem(str(len(j["track_ids"])))
            it.setData(Qt.UserRole, j["id"])
            self.table.setItem(row, 1, it)
            ci = QTableWidgetItem(j["confidence"] or "—")
            ci.setForeground(W.QColor(CONF_COLOR.get(j["confidence"], W.MUTED)))
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
        editable = j["status"] in ("pending", "approved", "rejected", "failed", "none")
        for c in [c for c in j["cands"] if not c["rejected"]][:5]:
            b = QToolButton()
            b.setCheckable(True)
            b.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
            b.setIconSize(QSize(88, 88))
            pm = W.pixmap(c["thumb"], 88)
            if not pm.isNull():
                b.setIcon(QIcon(pm))
            b.setText(f"{c['confidence'].split()[0]}\n{res_text(c).split('  ')[0]}")
            b.setToolTip(f"{c['title']}\n{c['artist']} {c['year']}\n{SOURCE.get(c['provider'], c['provider'])}\n"
                         f"{c['confidence']}" + "".join(f"\n• {r}" for r in c["reasons"][:3]))
            b.setChecked(c["idx"] == j["chosen"] and j["status"] in ("approved", "applied"))
            b.setEnabled(editable or b.isChecked())
            col = CONF_COLOR.get(c["confidence"], W.MUTED)
            b.setStyleSheet(f"QToolButton {{ border: 2px solid transparent; border-radius: 8px; padding: 3px; "
                            f"font-size: 10.5px; color: {col}; }} QToolButton:checked {{ border-color: {W.GREEN}; "
                            f"background: #15301f; }} QToolButton:hover {{ background: #1f232b; }}")
            b.clicked.connect(lambda _=False, jid=j["id"], idx=c["idx"]: self.choose(jid, idx))
            h.addWidget(b)
        if not j["cands"]:
            h.addWidget(W.label("No cover found. Double-click to search with other terms or import your own image.",
                                "muted", True))
        h.addStretch(1)
        return box

    def _selected_ids(self):
        rows = sorted({i.row() for i in self.table.selectionModel().selectedRows()})
        return [self.table.item(r, 1).data(Qt.UserRole) for r in rows]

    # ---------- decisions
    def choose(self, jid, idx):
        c = next(c for j in artfetch.jobs(self.app.conn) if j["id"] == jid for c in j["cands"] if c["idx"] == idx)
        if c["confidence"] in (am.WEAK, am.UNVERIFIED) and not W.confirm(
                self, "Approve a weak match?", f"This is a {c['confidence'].lower()}: “{c['title']}” by "
                f"{c['artist'] or 'unknown'}.\n\nIt may not be the right album. Approve it anyway?", ok="Approve"):
            return self.reload()
        artfetch.set_status(self.app.conn, [jid], "approved", chosen=idx)
        self.reload()

    def mark(self, status):
        ids = self._selected_ids()
        if not ids:
            return W.info(self, "No selection", "Select one or more rows first.")
        jobs = {j["id"]: j for j in artfetch.jobs(self.app.conn)}
        if status == "approved":
            ids = [i for i in ids if any(not c["rejected"] for c in jobs[i]["cands"])]
            for i in ids:  # keep an already chosen cover, else the best candidate
                j = jobs[i]
                keep = next((c["idx"] for c in j["cands"] if c["idx"] == j["chosen"] and not c["rejected"]),
                            next(c["idx"] for c in j["cands"] if not c["rejected"]))
                best = next(c for c in j["cands"] if c["idx"] == keep)
                if best["confidence"] in (am.WEAK, am.UNVERIFIED) and not W.confirm(
                        self, "Approve a weak match?", f"{j['label']}: the best candidate is a "
                        f"{best['confidence'].lower()}. Approve it anyway?", ok="Approve"):
                    continue
                artfetch.set_status(self.app.conn, [i], "approved", chosen=keep)
        else:
            artfetch.set_status(self.app.conn, ids, status)
        self.reload()

    def approve_safe(self):
        n = artfetch.approve_safe(self.app.conn)
        self.reload()
        W.info(self, "Approved", f"Approved the best official Exact/Strong cover for {n} album(s). Weak, unverified, "
                                 "fan and promotional results were left for you to review.")

    # ---------- search
    def open_selected(self):
        ids = self._selected_ids()
        if not ids:
            return W.info(self, "No selection", "Select a row first.")
        j = next(j for j in artfetch.jobs(self.app.conn) if j["id"] == ids[0])
        g = next((g for g in artfetch.plan(self.app.conn, only_missing=False) if g["key"] == j["key"]), None)
        if not g:
            return W.info(self, "Not available", "These songs are no longer in the library.")
        SearchDialog(self.app, g).exec()
        self.reload()

    def find(self, retry=False, keys=None):
        def run(conn, retry, keys, progress=None, cancel=None):
            groups = [g for g in artfetch.plan(conn) if keys is None or g["key"] in keys]
            return artfetch.search(conn, progress, cancel, redo=keys is not None, retry_none=retry,
                                   max_strategies=6, groups=groups)

        def done(st):
            msg = (f"Searched {st['searched']} album(s)/folder(s): {st['found']} with candidates, {st['none']} without."
                   + (f" {st['skipped']} earlier result(s) were kept." if st["skipped"] else ""))
            bad = [f"{n}: {', '.join(f'{k} ×{v}' for k, v in s.items() if k in ('error', 'invalid-key', 'needs-key'))}"
                   for n, s in st["providers"].items() if set(s) & {"error", "invalid-key", "needs-key"}]
            if bad:
                msg += "\n\nSources with problems:\n" + "\n".join(bad) + "\n(Add credentials in Settings → Artwork sources.)"
            W.info(self, "Search finished", msg + "\n\nNothing has been changed. Review the covers, then apply.")
            self.reload()
        self.app.run_task("Searching all artwork sources", run, retry, keys, on_done=done, refresh=False)

    def search_selected(self):
        ids = self._selected_ids()
        if not ids:
            return W.info(self, "No selection", "Select one or more rows first.")
        keys = {j["key"] for j in artfetch.jobs(self.app.conn) if j["id"] in ids}
        self.find(keys=keys)

    # ---------- apply
    def apply(self):
        approved = artfetch.jobs(self.app.conn, "approved")
        if not approved:
            return W.info(self, "Nothing approved", "Click a cover (or use Approve) to approve it first.")
        c = self.app.conn
        ids = sorted({t for j in approved for t in j["want"].get("_all_ids", j["track_ids"])})
        rows = [dict(r) for r in c.execute(f"SELECT id, path, has_art FROM tracks WHERE id IN ({','.join('?' * len(ids))}) "
                                           f"ORDER BY path", ids)]
        dlg = ApplyDialog(self.app, rows, f"{len(approved)} approved album(s)/folder(s)",
                          "Each album gets its own chosen cover. Tick the songs to include.")
        if not dlg.exec():
            return
        opts = dlg.options(c)
        if opts.writes_library() and not self.app.require_edit():
            return
        keep = set(dlg.ids())
        if not W.confirm(self, "Apply approved artwork?",
                         f"Apply {len(approved)} cover(s) to up to {len(keep):,} song(s).\n\nFull-size images are "
                         "downloaded now. Existing embedded artwork is only replaced if you chose that, and is backed "
                         "up first. Each album is a separate batch you can undo in Metadata Repair → Repair history.",
                         ok="Apply"):
            return

        def run(conn, job_ids, opts, keep, progress=None, cancel=None):
            return artfetch.apply(conn, job_ids, self.app.edit_mode, opts, progress, cancel, only_ids=keep)

        def done(res):
            msg = f"Artwork added to {res['songs']:,} song(s) in {res['albums']} album(s)/folder(s)."
            if res["files"]:
                msg += f"\n{res['files']} image file(s) saved."
            if res["skipped"]:
                msg += "\n\n" + "\n".join(res["skipped"][:6])
            if res["errors"]:
                msg += f"\n\n{len(res['errors'])} problem(s):\n" + "\n".join(res["errors"][:8])
            W.info(self, "Artwork applied", msg)
            self.reload()
        self.app.run_task("Adding artwork", run, [j["id"] for j in approved], opts, keep, on_done=done)
