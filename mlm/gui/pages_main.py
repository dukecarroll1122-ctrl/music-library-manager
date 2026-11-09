"""Dashboard, Music Library browser, batch edit dialog."""
import time

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QGridLayout, QLabel, QLineEdit,
                               QScrollArea, QSpinBox, QVBoxLayout, QWidget)

from .. import analysis, repair, tags
from . import widgets as W


class Page(QWidget):
    def __init__(self, app):
        super().__init__()
        self.app = app
        self.dirty = True
        self.outer = QVBoxLayout(self)
        self.outer.setContentsMargins(28, 22, 28, 16)
        self.outer.setSpacing(12)

    def refresh(self):
        pass

    def open_with(self, **kw):
        pass


def button(text, slot=None, kind=None, tip=""):
    from PySide6.QtWidgets import QPushButton as B
    b = B(text)
    if kind:
        b.setObjectName(kind)
    if slot:
        b.clicked.connect(slot)
    if tip:
        b.setToolTip(tip)
    return b


# ------------------------------------------------------------------ Dashboard
class DashboardPage(Page):
    def __init__(self, app):
        super().__init__(app)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        body = QWidget()
        lay = QVBoxLayout(body)
        lay.setContentsMargins(0, 0, 8, 0)
        lay.setSpacing(14)
        scroll.setWidget(body)
        self.outer.addWidget(W.hbox(W.page_header("Dashboard", "An overview of your music collection's size and "
                                                  "metadata health."), None,
                                    button("Rescan Library", self.rescan),
                                    button("Scan Folder…", lambda: app.scan_folder(), "primary")))
        self.outer.addWidget(scroll, 1)

        specs = [("songs", "Total songs", W.BLUE, None), ("artists", "Artists", W.VIOLET, None),
                 ("albums", "Albums", "#38bdf8", None), ("size", "Storage used", "#94a3b8", None),
                 ("missing_meta", "Missing metadata", "#ff8a3d", "missing"),
                 ("suspicious", "Suspicious metadata", W.AMBER, "suspicious"),
                 ("duplicates", "Duplicate songs", "#f472b6", "duplicate"),
                 ("missing_art", "Missing artwork", "#facc15", "art"),
                 ("corrupt", "Damaged files", W.RED, "corrupt")]
        grid = QGridLayout()
        grid.setSpacing(12)
        self.cards = {}
        for i, (key, title, color, problem) in enumerate(specs):
            c = W.Card(title, color, "Click to view" if problem else "")
            if problem:
                c.setCursor(Qt.PointingHandCursor)
                c.mousePressEvent = lambda _e, p=problem: app.go("library", problem=p)
            self.cards[key] = c
            grid.addWidget(c, i // 5, i % 5)
        lay.addLayout(grid)

        self.charts = {"by_artist": W.BarChart(W.VIOLET), "by_album": W.BarChart("#38bdf8"),
                       "by_genre": W.BarChart("#f472b6"), "by_format": W.BarChart(W.BLUE),
                       "by_health": W.BarChart(colors=[W.GREEN, W.AMBER, W.VIOLET, "#facc15", "#ff8a3d", W.RED,
                                                       W.MUTED])}
        titles = {"by_artist": "Top artists", "by_album": "Top albums", "by_genre": "Genres",
                  "by_format": "Audio formats", "by_health": "File health"}
        cg_items = [("by_health", 0, 0), ("by_format", 0, 1), ("by_artist", 1, 0), ("by_album", 1, 1),
                    ("by_genre", 2, 0)]
        cg = QGridLayout()
        cg.setSpacing(12)
        for k, r, c in cg_items:
            cg.addWidget(W.chart_panel(titles[k], self.charts[k]), r, c)
        self.scans_label = QLabel()
        self.scans_label.setTextFormat(Qt.RichText)
        self.scans_label.setWordWrap(True)
        cg.addWidget(W.chart_panel("Recent scans", self.scans_label), 2, 1)
        lay.addLayout(cg)
        lay.addStretch(1)

    def refresh(self):
        s = analysis.stats(self.app.conn)
        n = s["songs"] or 1
        pct = lambda v: f"{v / n:.0%} of songs" if s["songs"] else ""  # noqa: E731
        self.cards["songs"].set(f"{s['songs']:,}", f"{s['offline']:,} offline / not found" if s["offline"] else "")
        self.cards["artists"].set(f"{s['artists']:,}")
        self.cards["albums"].set(f"{s['albums']:,}")
        self.cards["size"].set(W.fmt_size(s["size"]))
        for k in ("missing_meta", "suspicious", "duplicates", "missing_art", "corrupt"):
            self.cards[k].set(f"{s[k]:,}", pct(s[k]) if s[k] else "None found")
        extra = [f"{s[k]:,} {n}" for k, n in (("unsupported", "unsupported"), ("partial", "decode warnings"),
                                                ("unavailable", "unavailable")) if s[k]]
        self.cards["corrupt"].set(f"{s['corrupt']:,}", " · ".join(extra) or ("Potentially corrupted or failed "
                                                                             "decode" if s["corrupt"] else
                                                                             "None found"))
        for k, ch in self.charts.items():
            ch.set_data(s[k] if s["songs"] else [])
        rows = []
        for sc in s["scans"]:
            when = time.strftime("%b %d %H:%M", time.localtime(sc["started"]))
            rows.append(f"<b>{when}</b> &nbsp; <span style='color:{W.MUTED}'>{sc['root']}</span><br>"
                        f"{sc['files']:,} files · {sc['new']:,} new · {sc['updated']:,} updated · "
                        f"{sc['errors']:,} damaged/unavailable · <i>{sc['status']}</i>")
        self.scans_label.setText("<br><br>".join(rows) or f"<span style='color:{W.MUTED}'>No scans yet. "
                                 "Click <b>Scan Folder…</b> to add your music folder, external drive or SD card."
                                 "</span>")

    def rescan(self):
        roots = [r[0] for r in self.app.conn.execute("SELECT DISTINCT root FROM scans ORDER BY id DESC")]
        if not roots:
            return self.app.scan_folder()
        from .. import scanner

        def scan_all(conn, roots, progress=None, cancel=None):
            out = []
            for r in roots:
                if cancel and cancel():
                    break
                try:
                    out.append(scanner.scan(conn, r, progress, cancel))
                except scanner.ScanError as e:
                    out.append(dict(root=r, error=str(e).split("\n")[0]))
            return out

        def done(res):
            lines = [f"{r['root']}: " + (r["error"] if "error" in r else
                                          f"{r['files']:,} files, {r['new'] + r['updated']:,} new/changed")
                     for r in res]
            W.info(self, "Rescan finished", "\n".join(lines))
        self.app.run_task("Rescanning library", scan_all, roots, on_done=done)


# ------------------------------------------------------------------ Library
TRACK_COLS = [("health", "", None), ("name", "Title", None), ("artist", "Artist", None),
              ("album", "Album", None), ("albumartist", "Album Artist", None), ("genre", "Genre", None),
              ("year", "Year", None), ("track", "#", None), ("duration", "Time", W.fmt_dur),
              ("format", "Format", None), ("bitrate", "Bitrate", W.fmt_kbps), ("sample_rate", "Rate", W.fmt_hz),
              ("size", "Size", W.fmt_size), ("has_art", "Art", None), ("class", "Status", None),
              ("path", "Location", None)]
TRACK_WIDTHS = [28, 220, 160, 180, 140, 100, 52, 36, 56, 60, 76, 70, 74, 40, 150]


class LibraryPage(Page):
    def __init__(self, app):
        super().__init__(app)
        self.outer.addWidget(W.hbox(W.page_header("Music Library", "Every scanned song. Search, filter, and "
                                                  "select songs to inspect or edit."), None,
                                    button("Scan Folder…", lambda: app.scan_folder(), "primary")))
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search title, artist, album or file path…")
        self.search.setClearButtonEnabled(True)
        self.timer = QTimer(self, singleShot=True, interval=250, timeout=self.reload)
        self.search.textChanged.connect(lambda: self.timer.start())
        self.outer.addWidget(self.search)

        self.filters = {}
        row = []
        for key, ph in (("artist", "All artists"), ("album", "All albums"), ("genre", "All genres"),
                        ("year", "All years"), ("format", "All formats")):
            cb = QComboBox()
            cb.setEditable(key in ("artist", "album"))
            cb.setMinimumWidth(120 if key not in ("artist", "album") else 170)
            cb.setMaxVisibleItems(25)
            cb.placeholder = ph
            cb.currentTextChanged.connect(lambda _=None: self.timer.start())
            self.filters[key] = cb
            row.append(cb)
        self.problem = QComboBox()
        self.problem.addItems(["Any status"] + list(analysis.PROBLEMS))
        self.problem.currentTextChanged.connect(lambda _: self.reload())
        self.min_br = QSpinBox()
        self.min_br.setRange(0, 5000)
        self.min_br.setSingleStep(64)
        self.min_br.setSuffix(" kbps min")
        self.min_br.setSpecialValueText("Any bitrate")
        self.min_br.valueChanged.connect(lambda _: self.timer.start())
        self.rate = QComboBox()
        self.rate.setMinimumWidth(140)
        self.rate.currentTextChanged.connect(lambda _: self.timer.start())
        self.outer.addWidget(W.hbox(*row, self.problem, self.min_br, self.rate,
                                    button("Clear", self.clear_filters), None))

        self.table = W.make_table(TRACK_COLS, TRACK_WIDTHS)
        self.table.doubleClicked.connect(lambda _: self.inspect())
        self.outer.addWidget(self.table, 1)
        self.count = W.label("", "muted")
        self.outer.addWidget(W.hbox(self.count, None,
                                    button("▶ Play", self.play),
                                    button("Reveal in Finder", self.reveal),
                                    button("Inspect", self.inspect),
                                    button("Convert…", self.convert, tip="Send selected songs to the Audio Converter"),
                                    button("Convert Albums to MP3…", self.convert_albums,
                                           tip="Queue every song of the selected songs' albums as MP3, keeping the "
                                               "artist/album folder structure"),
                                    button("Edit Selected…", self.edit, tip="Batch-edit metadata of selected songs")))

    def _fill(self, cb, values):
        cur = cb.currentText()
        cb.blockSignals(True)
        cb.clear()
        cb.addItem(cb.placeholder)
        cb.addItems([str(v) for v in values])
        cb.setCurrentText(cur if cur else cb.placeholder)
        cb.blockSignals(False)

    def refresh(self):
        c = self.app.conn
        for key in ("artist", "album", "genre", "format"):
            self._fill(self.filters[key], analysis.distinct(c, key))
        years = sorted({y[:4] for y in analysis.distinct(c, "year") if y[:4].isdigit()}, reverse=True)
        self._fill(self.filters["year"], years)
        self.rate.placeholder = "Any sample rate"
        self._fill(self.rate, [str(r) for r in analysis.distinct(c, "sample_rate")])
        self.reload()

    def current_filter(self):
        f = {"search": self.search.text().strip()}
        for k, cb in self.filters.items():
            if cb.currentText() and cb.currentText() != cb.placeholder:
                f[k] = cb.currentText()
        f["problem"] = analysis.PROBLEMS.get(self.problem.currentText())
        f["min_bitrate"] = self.min_br.value()
        if self.rate.currentText().isdigit():
            f["sample_rate"] = self.rate.currentText()
        return f

    def reload(self):
        rows = analysis.query_tracks(self.app.conn, self.current_filter())
        self.table.source_model.set_rows(rows)
        size = sum(r["size"] or 0 for r in rows)
        dur = sum(r["duration"] or 0 for r in rows)
        self.count.setText(f"{len(rows):,} songs · {W.fmt_size(size)} · {W.fmt_dur(dur)}")

    def clear_filters(self):
        for cb in self.filters.values():
            cb.setCurrentText(cb.placeholder)
        self.problem.setCurrentIndex(0)
        self.min_br.setValue(0)
        self.rate.setCurrentIndex(0)
        self.search.clear()
        self.reload()

    def open_with(self, problem=None, **kw):
        for w in (self.search, self.problem, self.min_br, self.rate, *self.filters.values()):
            w.blockSignals(True)
        self.clear_filters()
        for w in (self.search, self.problem, self.min_br, self.rate, *self.filters.values()):
            w.blockSignals(False)
        if problem:
            name = next(k for k, v in analysis.PROBLEMS.items() if v == problem)
            self.problem.setCurrentText(name)
        for k, v in kw.items():
            if k in self.filters:
                self.filters[k].setCurrentText(str(v))
        self.reload()

    def _sel(self):
        rows = self.table.selected_rows()
        if not rows:
            W.info(self, "No selection", "Select one or more songs first.")
        return rows

    def inspect(self):
        rows = self._sel()
        if rows:
            self.app.go("inspector", track_id=rows[0]["id"])

    def reveal(self):
        for r in self._sel()[:5]:
            W.reveal(r["path"])

    def play(self):
        rows = self._sel()
        if rows:
            self.app.go("player", rows=rows)

    def convert(self):
        rows = self._sel()
        if rows:
            self.app.go("converter", track_ids=[r["id"] for r in rows])

    def convert_albums(self):
        rows = self._sel()
        if rows:
            self.app.go("converter", track_ids=[r["id"] for r in rows], albums=True, fmt="MP3")

    def edit(self):
        rows = self._sel()
        if rows:
            edit_tracks(self, self.app, rows)


# ------------------------------------------------------------------ Batch edit
class EditDialog(QDialog):
    def __init__(self, parent, rows):
        super().__init__(parent)
        self.setWindowTitle(f"Edit metadata — {len(rows)} song(s)")
        self.setMinimumWidth(560)
        lay = QVBoxLayout(self)
        lay.addWidget(W.label("Tick the fields to change. Changes are queued in <b>Metadata Repair</b> as "
                              "BEFORE → AFTER proposals; nothing is written until you apply them.", wrap=True))
        grid = QGridLayout()
        self.edits = {}
        names = dict(title="Title", artist="Artist", albumartist="Album artist", album="Album", genre="Genre",
                     year="Year", track="Track #", disc="Disc #")
        for i, f in enumerate(tags.FIELDS):
            vals = set()
            for r in rows:
                v = r[f]
                if f in ("track", "disc"):
                    v = (f"{v}/{r[f + '_total']}" if r[f + "_total"] else str(v)) if v else ""
                vals.add(str(v or ""))
            chk = QCheckBox(names[f])
            ed = QLineEdit()
            if len(vals) == 1:
                ed.setText(vals.pop())
            else:
                ed.setPlaceholderText(f"<{len(vals)} different values — leave unticked to keep>")
            if f in ("title", "track") and len(rows) > 1:
                chk.setToolTip("Usually unique per song — be careful when batch editing")
            ed.textEdited.connect(lambda _t, c=chk: c.setChecked(True))
            grid.addWidget(chk, i, 0)
            grid.addWidget(ed, i, 1)
            self.edits[f] = (chk, ed)
        lay.addLayout(grid)
        lay.addWidget(W.label("Year: YYYY or YYYY-MM-DD. Track/Disc: number or number/total (e.g. 3/12). "
                              "Leave a ticked field empty to clear it.", "hint", wrap=True))
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("Queue changes")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def values(self):
        return {f: ed.text() for f, (chk, ed) in self.edits.items() if chk.isChecked()}


def edit_tracks(parent, app, rows):
    dlg = EditDialog(parent, rows)
    while dlg.exec():
        vals = dlg.values()
        if not vals:
            return
        try:
            n = repair.queue_manual(app.conn, [r["id"] for r in rows], vals)
        except ValueError as e:
            W.error(parent, "Invalid value", str(e))
            continue
        app.library_changed.emit()
        W.info(parent, "Changes queued", f"{n} change(s) queued for review in Metadata Repair.")
        app.go("repair")
        return
