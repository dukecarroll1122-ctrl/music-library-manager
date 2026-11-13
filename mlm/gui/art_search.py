"""The artwork search dialog: query form (standard or video-game soundtrack), source picker, results gallery,
preview/compare, manual import, and apply."""
import os

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QGridLayout, QLabel, QLineEdit, QProgressBar,
                               QTabWidget, QVBoxLayout, QWidget)

from .. import artfetch, artmatch as am, artproviders as ap, artsearch, db
from . import widgets as W
from .art_widgets import (ApplyDialog, CompareDialog, Gallery, ImportPanel, PreviewDialog, apply_selection, describe,
                          summarize_apply)
from .pages_main import button

REGIONS = {"Any region": "", "Japan": "JP", "United States": "US", "United Kingdom": "GB"}
RESOLUTIONS = {"Best available (original)": 0, "At least 500 px": 500, "At least 1000 px": 1000, "At least 1500 px": 1500,
               "At least 2000 px": 2000}


def run_search(conn, g, want, names, mode, max_strategies, fresh, progress=None, cancel=None):
    """Worker: search the chosen providers, store the results as the job's candidates."""
    ctx = ap.Ctx.from_conn(conn, fresh=fresh)
    provs = [ap.PROVIDERS[n] for n in names]
    res = artsearch.search_album(ctx, want, provs, mode, progress=lambda m: progress(0, 0, m) if progress else None,
                                 cancel=cancel, max_strategies=max_strategies, target=artfetch.target_resolution(conn))
    return res, artfetch.store_results(conn, g, want, res, reset=True)


class SearchDialog(QDialog):
    def __init__(self, app, group, start_tab=0):
        super().__init__(app)
        self.app, self.g = app, group
        self.job = None
        self.cand = None           # selected search candidate
        self.manual = None         # (data, info, label) from the import tab
        self.worker = None
        self.setWindowTitle(f"Artwork — {group['label']}")
        self.resize(1180, 860)
        lay = QVBoxLayout(self)
        want = artfetch.want_for(app.conn, group)

        # ----- query form
        self.mode = QComboBox()
        self.mode.addItems(["Standard album search", "Video Game Soundtrack search"])
        self.mode.setCurrentIndex(1 if want["mode"] == "game" else 0)
        self.mode.currentIndexChanged.connect(self.mode_changed)
        self.f = {}
        grid = QGridLayout()
        grid.setHorizontalSpacing(8)

        self.game_labels = []

        def field(key, label, row, col, value="", placeholder=""):
            lab = W.label(label, "muted")
            grid.addWidget(lab, row, col * 2)
            e = QLineEdit(str(value or ""))
            e.setPlaceholderText(placeholder)
            e.returnPressed.connect(lambda: self.search(all_sources=True))
            grid.addWidget(e, row, col * 2 + 1)
            self.f[key] = e
            if row == 1:
                self.game_labels.append(lab)
            return e
        field("artist", "Artist", 0, 0, want["artist"], "album artist or performer")
        field("album", "Album", 0, 1, want["album"], "album title as tagged")
        field("year", "Year", 0, 2, want["year"])
        field("catalog", "Catalog no.", 0, 3, want["catalog"], "e.g. SVWC-7133")
        self.game_fields = []
        for key, label, r, c, ph in (("game", "Game title", 1, 0, "can differ from the album title"),
                                     ("soundtrack_title", "Soundtrack title", 1, 1, "official album name"),
                                     ("composer", "Composer", 1, 2, ""), ("publisher", "Publisher", 1, 3, "")):
            self.game_fields.append(field(key, label, r, c, want.get(key, ""), ph))
        self.region = QComboBox()
        self.region.addItems(list(REGIONS))
        self.edition = QLineEdit()
        self.edition.setPlaceholderText("edition: deluxe, anniversary, remastered, multi-disc…")
        self.edition.returnPressed.connect(lambda: self.search(all_sources=True))
        self.game_extra = [W.label("Region", "muted"), self.region, W.label("Edition", "muted"), self.edition]
        grid.addWidget(self.game_extra[0], 2, 0)
        grid.addWidget(self.region, 2, 1)
        grid.addWidget(self.game_extra[2], 2, 2)
        grid.addWidget(self.edition, 2, 3, 1, 1)
        qw = QWidget()
        qw.setLayout(grid)

        # ----- sources
        cfg = ap.load_config(app.conn)
        self.src = {}
        row = []
        for n in cfg["order"]:
            p = ap.PROVIDERS[n]
            cb = QCheckBox(p.label)
            cb.setChecked(n not in cfg["disabled"])
            cb.setToolTip("Needs credentials (Settings)" if p.needs and not p.configured(ap.Ctx.from_conn(app.conn))
                          else "")
            self.src[n] = cb
            row.append(cb)
        self.size = QComboBox()
        self.size.addItems(list(RESOLUTIONS))
        cur = artfetch.target_resolution(app.conn)
        self.size.setCurrentIndex(max(0, list(RESOLUTIONS.values()).index(cur)) if cur in RESOLUTIONS.values() else 0)
        self.size.setToolTip("Download size. Never upscaled: if the original is smaller you get the original.")
        self.btn_all = button("Search All Sources", lambda: self.search(all_sources=True), "primary")
        self.btn_sel = button("Search Checked Sources", lambda: self.search(all_sources=False),
                              tip="Only the ticked sources — use this to try another provider")
        self.cancel_btn = button("Cancel", self.cancel_search)
        self.cancel_btn.setEnabled(False)

        tabs = QTabWidget()
        results = QWidget()
        rl = QVBoxLayout(results)
        rl.setContentsMargins(0, 8, 0, 0)
        self.gallery = Gallery()
        self.gallery.preview.connect(self.preview)
        self.gallery.chosen.connect(self.choose)
        self.gallery.rejected.connect(self.reject_cand)
        self.gallery.compare_changed.connect(self.compare_changed)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.RichText)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setFixedHeight(10)
        self.progress.hide()
        self.compare_btn = button("Compare Selected Two", self.compare)
        self.compare_btn.setEnabled(False)
        rl.addWidget(self.status)
        rl.addWidget(self.progress)
        rl.addWidget(self.gallery, 1)
        tabs.addTab(results, "Search results")
        self.importer = ImportPanel(app)
        self.importer.loaded.connect(self.imported)
        tabs.addTab(self.importer, "Import your own artwork")
        self.tabs = tabs

        lay.addWidget(W.hbox(W.label("Mode", "muted"), self.mode, None))
        lay.addWidget(qw)
        lay.addWidget(W.hbox(W.label("Sources", "muted"), *row, None, W.label("Size", "muted"), self.size))
        lay.addWidget(W.hbox(self.btn_all, self.btn_sel, self.cancel_btn, None, self.compare_btn))
        lay.addWidget(tabs, 1)

        # ----- bottom bar
        self.sel_label = QLabel("Nothing selected yet. Click Select on a cover, or import your own.")
        self.sel_label.setTextFormat(Qt.RichText)
        self.sel_label.setWordWrap(True)
        self.sel_label.setMinimumWidth(620)
        self.approve_btn = button("Approve for Queue", self.approve, tip="Remember this choice; apply it later together "
                                                                       "with the other approved albums")
        self.apply_btn = button("Apply…", self.apply, "primary")
        for b in (self.approve_btn, self.apply_btn):
            b.setEnabled(False)
        lay.addWidget(W.hbox(self.sel_label, None, self.approve_btn, self.apply_btn, button("Close", self.accept)))

        self.mode_changed()
        existing = next((j for j in artfetch.jobs(app.conn) if j["key"] == group["key"]), None)
        if existing and existing["cands"]:
            self.show_job(existing, "Previous results")
        tabs.setCurrentIndex(start_tab)

    # ---------- form
    def mode_changed(self):
        game = self.mode.currentIndex() == 1
        for w in self.game_fields + self.game_extra + self.game_labels:
            w.setVisible(game)

    def current_want(self):
        w = artfetch.want_for(self.app.conn, self.g, overrides={
            k: e.text().strip() for k, e in self.f.items()})
        w["mode"] = "game" if self.mode.currentIndex() == 1 else "music"
        w["region"] = REGIONS[self.region.currentText()]
        if self.edition.text().strip():
            w["edition_kind"] = am.edition_of(self.edition.text() + " edition")[0]
        if w["mode"] == "music":
            w["game"] = ""
        return w

    # ---------- search
    def search(self, all_sources):
        if self.worker and self.worker.isRunning():
            return
        names = [n for n, cb in self.src.items() if all_sources or cb.isChecked()]
        if not names:
            return W.info(self, "No sources", "Tick at least one source.")
        if all_sources:
            for cb in self.src.values():
                cb.setChecked(True)
        db.set_setting(self.app.conn, "art_resolution", str(RESOLUTIONS[self.size.currentText()]))
        want = self.current_want()
        if not (want["album"] or want.get("game") or want.get("soundtrack_title")):
            return W.info(self, "Nothing to search for", "Enter an album title or a game title.")
        self.g.update(artist=want["artist"], album=want["album"] or want.get("game", ""))
        self.progress.show()
        for b in (self.btn_all, self.btn_sel):
            b.setEnabled(False)
        self.cancel_btn.setEnabled(True)
        self.status.setText(f"<span style='color:{W.MUTED}'>Searching {len(names)} source(s) with up to 10 "
                            "fallback strategies each…</span>")
        mode = "all" if all_sources else artfetch.search_mode(self.app.conn)
        self.worker = W.Worker(self.app.db_path, run_search, (self.g, want, names, mode, 10, True), {})
        self.worker.progressed.connect(lambda i, n, m: self.status.setText(
            f"<span style='color:{W.MUTED}'>{m}</span>"))
        self.worker.succeeded.connect(self.searched)
        self.worker.failed.connect(self.search_failed)
        self.worker.start()

    def cancel_search(self):
        if self.worker:
            self.worker.cancelled = True

    def search_failed(self, msg):
        self.search_done()
        self.status.setText(f"<span style='color:{W.RED}'>Search failed: {msg}</span>")

    def search_done(self):
        self.progress.hide()
        for b in (self.btn_all, self.btn_sel):
            b.setEnabled(True)
        self.cancel_btn.setEnabled(False)

    def searched(self, res):
        self.search_done()
        result, jid = res
        self.app.library_changed.emit()
        job = next(j for j in artfetch.jobs(self.app.conn) if j["id"] == jid)
        self.show_job(job, f"{len(result['strategies'])} search strategies tried")

    def show_job(self, job, note=""):
        self.job = job
        self.cand = None
        self.gallery.set_cands(job["cands"], empty="No cover found by any source. Try the other mode, edit the "
                                                    "fields, or import your own artwork.")
        icon = {"ok": ("✓", W.GREEN), "empty": ("—", W.MUTED), "error": ("✕", W.RED), "needs-key": ("🔑", W.AMBER),
                "invalid-key": ("✕", W.RED), "skipped": ("·", W.MUTED), "pending": ("·", W.MUTED)}
        bits = []
        for n, st in job["provider_status"].items():
            sym, col = icon.get(st["state"], ("·", W.MUTED))
            extra = f" {st['found']}" if st["state"] == "ok" else ""
            tip = st["msg"].replace("'", "")
            bits.append(f"<span style='color:{col}' title='{tip}'>{sym} {st['label']}{extra}</span>"
                        + (f" <span style='color:{W.MUTED}'>({st['msg']})</span>" if st["state"] in ("error", "needs-key",
                                                                                              "invalid-key") else ""))
        self.status.setText(" &nbsp;·&nbsp; ".join(bits) + (f"<br><span style='color:{W.MUTED}'>{note}</span>"
                                                           if note else ""))
        self.update_selection()

    # ---------- selection
    def cand_by_idx(self, idx):
        return next((c for c in self.job["cands"] if c["idx"] == idx), None) if self.job else None

    def choose(self, cand):
        if cand.get("rejected"):
            return W.info(self, "Rejected", "Restore this result first if you want to use it.")
        self.cand, self.manual = cand, None
        self.gallery.sel = cand["idx"]
        self.gallery.layout_cards()
        self.update_selection()

    def update_selection(self):
        if self.manual:
            data, info, label = self.manual
            self.sel_label.setText(f"<b>Selected: your own image</b> — {label}, {info['w']}×{info['h']} {info['fmt']}")
            self.approve_btn.setEnabled(False)
            self.apply_btn.setEnabled(True)
        elif self.cand:
            self.sel_label.setText("<b>Selected:</b><br>" + describe(self.cand).replace("<br>", " · ", 3))
            self.approve_btn.setEnabled(bool(self.g["track_ids"]))
            self.apply_btn.setEnabled(True)
        else:
            self.sel_label.setText("Nothing selected yet. Click Select on a cover, or import your own.")
            self.approve_btn.setEnabled(False)
            self.apply_btn.setEnabled(False)

    def imported(self, data, info, label):
        self.manual, self.cand = (data, info, label), None
        self.update_selection()

    def reject_cand(self, cand):
        if not self.job:
            return
        artfetch.reject_candidate(self.app.conn, self.job["id"], cand["idx"], not cand.get("rejected"))
        job = next(j for j in artfetch.jobs(self.app.conn) if j["id"] == self.job["id"])
        if self.cand and self.cand["idx"] == cand["idx"]:
            self.cand = None
        self.job = job
        self.gallery.set_cands(job["cands"], selected=self.cand["idx"] if self.cand else None)
        self.update_selection()

    def preview(self, cand):
        PreviewDialog(self, cand, on_select=self.choose).exec()

    def compare_changed(self, two):
        self.compare_btn.setEnabled(len(two) == 2)
        self._two = two

    def compare(self):
        if len(getattr(self, "_two", [])) == 2:
            CompareDialog(self, *self._two, on_select=self.choose).exec()

    # ---------- approve / apply
    def approve(self):
        if self.cand and self.job:
            artfetch.set_status(self.app.conn, [self.job["id"]], artfetch.APPROVED, chosen=self.cand["idx"])
            self.app.library_changed.emit()
            W.info(self, "Approved", "This cover is approved. Apply it later from Artwork Manager → Missing Artwork.")
            self.accept()

    def apply(self):
        if not (self.cand or self.manual):
            return
        c = self.app.conn
        rows = [dict(r) for r in c.execute(f"SELECT id, path, has_art FROM tracks WHERE id IN "
                                           f"({','.join('?' * len(self.g['all_ids']))}) ORDER BY path",
                                           self.g["all_ids"])]
        if self.manual:
            data, info, label = self.manual
            title, desc = "Your own image", f"{label} — {info['w']}×{info['h']} {info['fmt']}"
        else:
            title = self.cand["title"]
            desc = (f"{self.cand['confidence']} · {self.cand['provider']}"
                    + (f" · {self.cand['width']}×{self.cand['height']}" if self.cand.get("width") else ""))
        weak = self.cand and self.cand["confidence"] in (am.WEAK, am.UNVERIFIED)
        dlg = ApplyDialog(self.app, rows, title, desc, default_missing_only=any(not r["has_art"] for r in rows))
        if not dlg.exec():
            return
        opts = dlg.options(c)
        if opts.writes_library() and not self.app.require_edit():
            return
        msg = (f"Apply “{title}” to {len(dlg.ids())} song(s)?\n\nYour original audio and all other tags are preserved. "
               "Existing embedded artwork is backed up before it is replaced, and the change can be undone from "
               "Metadata Repair → Repair history.")
        if weak:
            msg = f"⚠ This is a {self.cand['confidence'].lower()} — it may not be the right album.\n\n" + msg
        if not W.confirm(self, "Apply artwork?", msg, ok="Apply"):
            return
        if self.manual:
            args = dict(data=self.manual[0], source=dict(provider="manual", url=self.manual[1].get("source", ""),
                                                          title=label, artist=self.g["artist"], confidence=am.UNVERIFIED))
        else:
            args = dict(cand=self.cand)
        job_id = self.job["id"] if self.job else None
        app = self.app

        def applied(res):
            W.info(app, "Artwork applied", summarize_apply(res))
            if job_id and (res["embedded"] or res["files"]):
                app.conn.execute("UPDATE art_jobs SET status='applied', applied=strftime('%s','now') WHERE id=?", (job_id,))
                app.conn.commit()
                app.library_changed.emit()
        if app.run_task("Applying artwork", apply_selection, dlg.ids(), opts, app.edit_mode, label=self.g["label"],
                        job_id=job_id, on_done=applied, **args):
            self.accept()

    def done(self, r):
        if self.worker and self.worker.isRunning():
            self.worker.cancelled = True
            self.worker.wait(5000)
        super().done(r)
