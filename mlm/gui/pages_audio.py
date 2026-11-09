"""Audio Converter, Music Player and Audio Verification pages."""
import json
import os
import threading
import time

from PySide6.QtCore import QThread, Qt, QTimer, QUrl, Signal
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
                               QGridLayout, QHeaderView, QLabel, QLineEdit, QListWidget, QListWidgetItem,
                               QProgressBar, QSlider, QSpinBox, QSplitter, QTableWidget, QTableWidgetItem,
                               QTabWidget, QVBoxLayout, QWidget)

from .. import analysis, convert, db, scanner
from . import widgets as W
from .pages_main import Page, button
from .pages_meta import section

STATUS_COLOR = {"queued": W.MUTED, "converting": W.BLUE, "done": W.GREEN, "failed": W.RED, "cancelled": W.AMBER,
                "skipped": W.AMBER}


def track_rows(conn, ids):
    if not ids:
        return []
    return [dict(r) for r in conn.execute(f"SELECT * FROM tracks WHERE id IN ({','.join('?' * len(ids))}) "
                                          f"ORDER BY path", list(ids))]


def album_mates(conn, rows):
    """Expand rows to every song in the same album folder."""
    out, seen = [], set()
    for r in rows:
        folder = os.path.dirname(r["path"])
        for m in conn.execute("SELECT * FROM tracks WHERE missing=0 AND substr(path,1,?)=? AND lower(album)=? "
                              "ORDER BY disc, track, path", (len(folder) + 1, folder + "/", (r["album"] or "").lower())):
            if os.path.dirname(m["path"]) == folder and m["id"] not in seen:
                seen.add(m["id"])
                out.append(dict(m))
    return out


# ================================================================== Converter
class ConvertWorker(QThread):
    updated = Signal(dict)
    finished_all = Signal(list)

    def __init__(self, db_path, jobs, settings):
        super().__init__()
        self.db_path, self.jobs, self.settings = db_path, jobs, settings
        self.cancelled = False

    def run(self):
        conn = db.connect(self.db_path)
        try:
            done = convert.run_jobs(conn, self.jobs, self.settings, on_update=lambda j: self.updated.emit(dict(j)),
                                    cancel=lambda: self.cancelled)
        finally:
            conn.close()
        self.finished_all.emit(done)


class AlbumPicker(QDialog):
    def __init__(self, parent, conn):
        super().__init__(parent)
        self.setWindowTitle("Add albums")
        self.resize(560, 520)
        lay = QVBoxLayout(self)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search albums…")
        self.list = QListWidget()
        self.list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        lay.addWidget(self.search)
        lay.addWidget(self.list, 1)
        self.albums = conn.execute(
            "SELECT coalesce(nullif(albumartist,''), artist) aa, album, count(*) n, min(id) any_id FROM tracks "
            "WHERE missing=0 AND album<>'' GROUP BY lower(aa), lower(album) ORDER BY lower(aa), lower(album)").fetchall()
        self.search.textChanged.connect(self.fill)
        self.fill()
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def fill(self):
        q = self.search.text().casefold()
        self.list.clear()
        for a in self.albums:
            if q in f"{a['aa']} {a['album']}".casefold():
                it = QListWidgetItem(f"{a['album']}  —  {a['aa'] or 'Unknown'}   ({a['n']} songs)")
                it.setData(Qt.UserRole, a["any_id"])
                self.list.addItem(it)

    def ids(self):
        return [it.data(Qt.UserRole) for it in self.list.selectedItems()]


class ConverterPage(Page):
    HEAD = ["Original file", "Format", "Size", "Output", "Est. size", "Progress", "Status", "Output location"]

    def __init__(self, app):
        super().__init__(app)
        self.outer.addWidget(W.page_header(
            "Audio Converter", "Create converted copies with FFmpeg. Originals are only read — never changed, "
            "replaced or deleted — and copies always go to a separate folder."))
        tabs = QTabWidget()
        self.outer.addWidget(tabs, 1)
        main = QWidget()
        ml = QVBoxLayout(main)
        ml.setContentsMargins(0, 10, 0, 0)
        tabs.addTab(main, "Convert")

        # settings
        self.fmt = QComboBox()
        self.fmt.addItems(list(convert.OUTPUTS))
        self.preset = QComboBox()
        self.preset.addItems([f"{k} kbps" for k in convert.MP3_PRESETS] + ["Custom"])
        self.custom = QSpinBox()
        self.custom.setRange(32, 320)
        self.custom.setSingleStep(16)
        self.custom.setValue(224)
        self.custom.setSuffix(" kbps")
        self.vbr = QCheckBox("Variable bitrate")
        self.vbr_q = QSpinBox()
        self.vbr_q.setRange(0, 14)
        self.rate = QComboBox()
        self.rate.addItems(["Keep sample rate", "44.1 kHz", "48 kHz", "88.2 kHz", "96 kHz", "22.05 kHz"])
        self.ch = QComboBox()
        self.ch.addItems(["Keep channels", "Stereo", "Mono"])
        self.keep_tags = QCheckBox("Preserve metadata (title, artist, album artist, year, track and disc numbers…)")
        self.keep_tags.setChecked(True)
        self.keep_art = QCheckBox("Preserve album artwork")
        self.keep_art.setChecked(True)
        self.out = QLineEdit()
        self.out.setPlaceholderText("Output folder (must be separate from your originals)")
        self.structure = QComboBox()
        self.structure.addItems(["Keep artist/album folder structure", "All files in one folder"])
        g = QGridLayout()
        g.setHorizontalSpacing(10)
        g.addWidget(W.label("Format", "muted"), 0, 0)
        g.addWidget(W.hbox(self.fmt, self.preset, self.custom, self.vbr, self.vbr_q, None), 0, 1)
        g.addWidget(W.label("Audio", "muted"), 1, 0)
        g.addWidget(W.hbox(self.rate, self.ch, self.keep_tags, self.keep_art, None), 1, 1)
        g.addWidget(W.label("Save to", "muted"), 2, 0)
        g.addWidget(W.hbox(self.out, button("Browse…", self.browse), self.structure), 2, 1)
        gw = QWidget()
        gw.setLayout(g)
        ml.addWidget(gw)
        self.preview = QLabel()
        self.preview.setWordWrap(True)
        self.preview.setTextFormat(Qt.RichText)
        ml.addWidget(self.preview)

        ml.addWidget(W.hbox(button("Add Files…", self.add_files), button("Add Folder…", self.add_folder),
                            button("Add Albums…", self.add_albums), None,
                            button("Remove Selected", self.remove), button("Clear Queue", self.clear)))
        self.table = QTableWidget(0, len(self.HEAD))
        self.table.setHorizontalHeaderLabels(self.HEAD)
        self.table.verticalHeader().hide()
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setShowGrid(False)
        self.table.horizontalHeader().setStretchLastSection(True)
        for i, w in enumerate([260, 60, 76, 60, 76, 120, 170]):
            self.table.setColumnWidth(i, w)
        ml.addWidget(self.table, 1)
        self.summary = W.label("", "muted")
        self.start_btn = button("Start Conversion…", self.start, "primary")
        self.cancel_btn = button("Cancel", self.cancel)
        self.cancel_btn.setEnabled(False)
        ml.addWidget(W.hbox(self.summary, None, self.cancel_btn, self.start_btn))

        hist = QWidget()
        hl = QVBoxLayout(hist)
        hl.setContentsMargins(0, 10, 0, 0)
        self.history = W.make_table([("when", "When", None), ("status", "Status", None), ("src", "Original", None),
                                     ("dst_format", "Output", None), ("dst", "Output file", None),
                                     ("error", "Notes / error", None)], [140, 80, 300, 60, 300])
        hl.addWidget(self.history, 1)
        hl.addWidget(W.hbox(W.label("Errors are also written to conversion_errors.log in the data folder.", "muted"),
                            None, button("Reveal Output", self.reveal_history)))
        tabs.addTab(hist, "Conversion history")

        self.sources = []   # [(track_id, path, root)]
        self.jobs = []
        self.worker = None
        for w in (self.fmt, self.preset, self.rate, self.ch, self.structure):
            w.currentIndexChanged.connect(self.replan)
        for w in (self.custom, self.vbr_q):
            w.valueChanged.connect(self.replan)
        for w in (self.vbr, self.keep_tags, self.keep_art):
            w.toggled.connect(self.replan)
        self.out.editingFinished.connect(self.replan)
        self.fmt.currentIndexChanged.connect(self._fmt_changed)
        self._fmt_changed()

    # ---------- settings ----------
    def _fmt_changed(self):
        f = self.fmt.currentText()
        lossless = convert.OUTPUTS[f][2]
        self.preset.setVisible(not lossless)
        self.vbr.setVisible(f in ("MP3", "AAC", "OGG"))
        self.vbr_q.setRange(*{"MP3": (0, 9), "AAC": (0, 14), "OGG": (0, 10)}.get(f, (0, 9)))
        self.vbr_q.setToolTip(convert.VBR_LABELS.get(f, ""))
        if f == "OGG" and self.vbr_q.value() == 0:
            self.vbr_q.setValue(6)
        self._sync_widgets()

    def _sync_widgets(self):
        f = self.fmt.currentText()
        lossless = convert.OUTPUTS[f][2]
        vbr = self.vbr.isChecked() and self.vbr.isVisible()
        self.preset.setEnabled(not lossless and not vbr)
        self.custom.setVisible(not lossless and not vbr and self.preset.currentText() == "Custom")
        self.vbr_q.setVisible(vbr)

    def settings(self):
        f = self.fmt.currentText()
        p = self.preset.currentText()
        rates = {"44.1 kHz": 44100, "48 kHz": 48000, "88.2 kHz": 88200, "96 kHz": 96000, "22.05 kHz": 22050}
        return convert.Settings(
            fmt=f, bitrate=self.custom.value() if p == "Custom" else int(p.split()[0]),
            vbr=self.vbr.isChecked() and f in ("MP3", "AAC", "OGG"), vbr_quality=self.vbr_q.value(),
            sample_rate=rates.get(self.rate.currentText(), 0), channels={"Stereo": 2, "Mono": 1}.get(
                self.ch.currentText(), 0), preserve_tags=self.keep_tags.isChecked(),
            preserve_art=self.keep_art.isChecked(), out_dir=self.out.text().strip(),
            structure="mirror" if self.structure.currentIndex() == 0 else "flat")

    def refresh(self):
        if not self.out.text():
            last = db.get_setting(self.app.conn, "convert_out")
            self.out.setText(last or os.path.expanduser("~/Music/Converted"))
            self.replan()
        self.load_history()

    def browse(self):
        d = QFileDialog.getExistingDirectory(self, "Choose output folder", self.out.text() or os.path.expanduser("~"))
        if d:
            self.out.setText(d)
            self.replan()

    # ---------- sources ----------
    def open_with(self, track_ids=None, albums=False, fmt=None, **_):
        rows = track_rows(self.app.conn, track_ids or [])
        if albums:
            rows = album_mates(self.app.conn, rows)
        if fmt:
            self.fmt.setCurrentText(fmt)
            if albums:
                self.structure.setCurrentIndex(0)
        self._add([(r["id"], r["path"], None) for r in rows])

    def _add(self, items):
        if self.worker and self.worker.isRunning():
            return W.info(self, "Busy", "Wait for the current conversion to finish before changing the queue.")
        have = {s[1] for s in self.sources}
        new = [i for i in items if i[1] not in have]
        self.sources += new
        self.replan()
        skipped = len(items) - len(new)
        if skipped:
            self.app.task_label.setText(f"{skipped} file(s) were already in the queue")

    def add_files(self):
        exts = " ".join(f"*{e}" for e in sorted(scanner.AUDIO_EXT))
        paths, _ = QFileDialog.getOpenFileNames(self, "Choose audio files", os.path.expanduser("~/Music"),
                                                f"Audio ({exts})")
        self._add([self._lookup(p, os.path.dirname(os.path.dirname(p))) for p in paths])

    def add_folder(self):
        d = QFileDialog.getExistingDirectory(self, "Choose a folder to convert", os.path.expanduser("~/Music"))
        if d:
            files, _ = scanner.collect(d)
            self._add([self._lookup(p, os.path.dirname(d.rstrip("/"))) for p in sorted(files)])

    def add_albums(self):
        dlg = AlbumPicker(self, self.app.conn)
        if dlg.exec():
            self._add([(r["id"], r["path"], None) for r in album_mates(self.app.conn,
                                                                          track_rows(self.app.conn, dlg.ids()))])

    def _lookup(self, path, root):
        r = self.app.conn.execute("SELECT id FROM tracks WHERE path=?", (path,)).fetchone()
        return (r[0] if r else None, path, root)

    def remove(self):
        rows = sorted({i.row() for i in self.table.selectedIndexes()}, reverse=True)
        for r in rows:
            src = self.jobs[r]["src"]
            self.sources = [s for s in self.sources if s[1] != src]
        self.replan()

    def clear(self):
        if self.worker and self.worker.isRunning():
            return
        self.sources = []
        self.replan()

    # ---------- plan / table ----------
    def replan(self, *_):
        self._sync_widgets()
        if self.worker and self.worker.isRunning():
            return
        s = self.settings()
        self.jobs = convert.plan_jobs(self.app.conn, self.sources, s) if s.out_dir else [
            dict(id=str(i), track_id=t, src=p, dst="", src_format=os.path.splitext(p)[1][1:].upper(), src_size=0,
                 dst_format=s.fmt, est=None, kbps=0, status="skipped", progress=0, error="Choose an output folder")
            for i, (t, p, _) in enumerate(self.sources)]
        self.table.setRowCount(len(self.jobs))
        for i, j in enumerate(self.jobs):
            self._row(i, j)
        fmts = {j["src_format"] for j in self.jobs}
        warns = convert.warnings_for(fmts, s, [j["kbps"] for j in self.jobs]) if self.jobs else []
        est = sum(j["est"] or 0 for j in self.jobs if j["status"] == "queued")
        ready = sum(1 for j in self.jobs if j["status"] == "queued")
        html = f"<b>{s.describe()}</b>"
        if not convert.OUTPUTS[s.fmt][2] and s.fmt in ("MP3", "AAC", "OGG") and not s.vbr:
            html += f" &nbsp;<span style='color:{W.MUTED}'>({'constant' if s.fmt == 'MP3' else 'average'} bitrate)</span>"
        html += "".join(f"<br><span style='color:{W.AMBER}'>⚠ {w}</span>" for w in warns)
        self.preview.setText(html)
        self.summary.setText(f"{ready:,} ready · {len(self.jobs) - ready:,} skipped · estimated output "
                             f"{W.fmt_size(est) if est else '—'}")

    def _row(self, i, j):
        vals = [os.path.basename(j["src"]), j["src_format"], W.fmt_size(j["src_size"]), j["dst_format"],
                W.fmt_size(j["est"]) if j.get("est") else "—", None, j["status"] + (f" — {j['error']}" if j.get("error")
                                                                                   else ""), j["dst"]]
        for c, v in enumerate(vals):
            if c == 5:
                bar = self.table.cellWidget(i, 5)
                if not isinstance(bar, QProgressBar):
                    bar = QProgressBar()
                    bar.setRange(0, 100)
                    bar.setTextVisible(False)
                    bar.setFixedHeight(10)
                    self.table.setCellWidget(i, 5, bar)
                bar.setValue(int((j.get("progress") or 0) * 100))
                continue
            it = QTableWidgetItem(v)
            it.setToolTip(j["src"] if c == 0 else v)
            if c == 6:
                it.setForeground(W.QColor(STATUS_COLOR.get(j["status"], W.TEXT)))
            self.table.setItem(i, c, it)

    # ---------- run ----------
    def start(self):
        s = self.settings()
        if not s.out_dir:
            return W.info(self, "Output folder", "Choose an output folder first.")
        self.replan()
        todo = [j for j in self.jobs if j["status"] == "queued"]
        if not todo:
            return W.info(self, "Nothing to convert", "Add songs, albums or folders. Files whose output already "
                                                      "exists are skipped, never overwritten.")
        warns = convert.warnings_for({j["src_format"] for j in todo}, s, [j["kbps"] for j in todo])
        msg = (f"Create {len(todo):,} {s.describe()} cop{'y' if len(todo) == 1 else 'ies'} in:\n{s.out_dir}\n\n"
               "Your original files are only read. They will not be changed, replaced or deleted.")
        if warns:
            msg += "\n\n" + "\n\n".join("⚠ " + w for w in warns)
        if not W.confirm(self, "Start conversion?", msg, ok="Convert"):
            return
        db.set_setting(self.app.conn, "convert_out", s.out_dir)
        os.makedirs(s.out_dir, exist_ok=True)
        self.worker = ConvertWorker(self.app.db_path, self.jobs, s)
        self.worker.updated.connect(self._updated)
        self.worker.finished_all.connect(self._finished)
        self.start_btn.setEnabled(False)
        self.cancel_btn.setEnabled(True)
        self.worker.start()

    def _updated(self, j):
        for i, x in enumerate(self.jobs):
            if x["id"] == j["id"]:
                x.update(j)
                self._row(i, x)
                break
        n = sum(1 for x in self.jobs if x["status"] in ("done", "failed", "cancelled"))
        self.summary.setText(f"Converting… {n:,} of {sum(1 for x in self.jobs if x['status'] != 'skipped'):,} "
                             "finished")

    def _finished(self, jobs):
        self.start_btn.setEnabled(True)
        self.cancel_btn.setEnabled(False)
        from collections import Counter
        c = Counter(j["status"] for j in jobs)
        self.summary.setText(" · ".join(f"{v:,} {k}" for k, v in c.items()))
        self.load_history()
        fails = [f"{os.path.basename(j['src'])}: {j['error'][:150]}" for j in jobs if j["status"] == "failed"]
        W.info(self, "Conversion finished", f"Done: {c['done']}   Failed: {c['failed']}   Cancelled: "
                                            f"{c['cancelled']}   Skipped: {c['skipped']}"
               + ("\n\n" + "\n".join(fails[:10]) if fails else ""))
        self.sources = [s for s in self.sources if s[1] not in {j["src"] for j in jobs if j["status"] == "done"}]

    def cancel(self):
        if self.worker:
            self.worker.cancelled = True
            self.cancel_btn.setEnabled(False)
            self.summary.setText("Cancelling — partial outputs are removed…")

    def busy(self):
        return bool(self.worker and self.worker.isRunning())

    def load_history(self):
        rows = []
        for r in self.app.conn.execute("SELECT * FROM conversions ORDER BY id DESC LIMIT 2000"):
            d = dict(r)
            d["when"] = time.strftime("%Y-%m-%d %H:%M", time.localtime(d["finished"] or d["started"] or 0))
            d["_color"] = {"status": STATUS_COLOR.get(d["status"], W.TEXT)}
            rows.append(d)
        self.history.source_model.set_rows(rows)

    def reveal_history(self):
        for r in self.history.selected_rows()[:5]:
            W.reveal(r["dst"])


# ================================================================== Player
def fmt_ms(ms):
    return W.fmt_dur((ms or 0) / 1000)


class PlayerPage(Page):
    """Plays files in place with Qt Multimedia (FFmpeg backend). Files are opened read-only."""

    def __init__(self, app):
        super().__init__(app)
        from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
        self.QMediaPlayer = QMediaPlayer
        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.player.setAudioOutput(self.audio)
        self.audio.setVolume(0.8)
        self.queue = []      # dicts: id, path, title, artist, album, art_hash
        self.index = -1
        self._seeking = False
        self._resume_at = None

        self.outer.addWidget(W.page_header("Music Player", "Plays files directly from where they are. Playback "
                                                           "never modifies your files."))
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.setSpacing(10)
        self.art = QLabel()
        self.art.setFixedSize(300, 300)
        self.art.setAlignment(Qt.AlignCenter)
        self.art.setStyleSheet(f"background: {W.CARD}; border: 1px solid {W.BORDER}; border-radius: 12px; "
                               f"color: {W.MUTED}; font-size: 40px;")
        ll.addWidget(self.art, 0, Qt.AlignHCenter)
        self.title = W.label("Nothing playing", "h1")
        self.title.setAlignment(Qt.AlignCenter)
        self.title.setWordWrap(True)
        self.sub = W.label("Add songs from the Library, Inspector or Audio Verification pages.", "muted", True)
        self.sub.setAlignment(Qt.AlignCenter)
        ll.addWidget(self.title)
        ll.addWidget(self.sub)
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(0, 0)
        self.slider.sliderPressed.connect(lambda: setattr(self, "_seeking", True))
        self.slider.sliderReleased.connect(self._seek_release)
        self.pos_l, self.dur_l = W.label("0:00", "muted"), W.label("0:00", "muted")
        ll.addWidget(self.slider)
        ll.addWidget(W.hbox(self.pos_l, None, self.dur_l))
        self.play_btn = button("▶  Play", self.toggle, "primary")
        self.play_btn.setMinimumWidth(110)
        self.vol = QSlider(Qt.Horizontal)
        self.vol.setRange(0, 100)
        self.vol.setValue(80)
        self.vol.setFixedWidth(120)
        self.vol.valueChanged.connect(lambda v: self.audio.setVolume(v / 100))
        ll.addWidget(W.hbox(None, button("⏮", self.prev, tip="Previous"), self.play_btn,
                            button("⏹", self.stop, tip="Stop"), button("⏭", self.next, tip="Next"), 16,
                            W.label("🔈", "muted"), self.vol, None))
        self.keep_pos = QCheckBox("Compare mode: keep the playback position when switching songs")
        self.keep_pos.setToolTip("Switch between two versions at the same moment to compare them by ear")
        ll.addWidget(self.keep_pos, 0, Qt.AlignHCenter)
        self.state = W.label("", "muted", True)
        self.state.setAlignment(Qt.AlignCenter)
        ll.addWidget(self.state)
        ll.addStretch(1)

        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(12, 0, 0, 0)
        rl.addWidget(W.label("Queue", "h2"))
        self.list = QListWidget()
        self.list.itemDoubleClicked.connect(lambda it: self.play_index(self.list.row(it)))
        rl.addWidget(self.list, 1)
        rl.addWidget(W.hbox(button("Add Files…", self.add_files), button("Remove", self.remove),
                            button("Clear", self.clear), None, button("Reveal", self.reveal),
                            button("Inspect", self.inspect)))
        split = QSplitter(Qt.Horizontal)
        split.addWidget(left)
        split.addWidget(right)
        split.setSizes([620, 460])
        self.outer.addWidget(split, 1)

        p = self.player
        p.positionChanged.connect(self._pos)
        p.durationChanged.connect(self._dur)
        p.playbackStateChanged.connect(self._state)
        p.mediaStatusChanged.connect(self._status)
        p.errorOccurred.connect(lambda _e, msg: self.state.setText(f"⚠ Cannot play this file: {msg}"))

    # ---------- queue ----------
    def item_for(self, r):
        r = dict(r)
        return dict(id=r.get("id"), path=r["path"], title=r.get("title") or os.path.basename(r["path"]),
                    artist=r.get("artist") or "", album=r.get("album") or "", art_hash=r.get("art_hash"),
                    label=r.get("label", ""))

    def enqueue(self, rows, play=True, replace=False):
        items = [self.item_for(r) for r in rows]
        if replace:
            self.clear()
        start = len(self.queue)
        self.queue += items
        self._fill()
        if play and items:
            self.play_index(start)

    def _fill(self):
        self.list.clear()
        for i, q in enumerate(self.queue):
            it = QListWidgetItem(("▶  " if i == self.index else "     ") + f"{q['title']} — {q['artist'] or '?'}"
                                 + (f"   [{q['label']}]" if q["label"] else ""))
            it.setToolTip(q["path"])
            self.list.addItem(it)
        if 0 <= self.index < self.list.count():
            self.list.setCurrentRow(self.index)

    def open_with(self, rows=None, play=True, replace=False, **_):
        if rows:
            self.enqueue(rows, play, replace)

    def add_files(self):
        exts = " ".join(f"*{e}" for e in sorted(scanner.AUDIO_EXT))
        paths, _ = QFileDialog.getOpenFileNames(self, "Choose audio files", os.path.expanduser("~/Music"),
                                                f"Audio ({exts})")
        rows = []
        for p in paths:
            r = self.app.conn.execute("SELECT * FROM tracks WHERE path=?", (p,)).fetchone()
            rows.append(dict(r) if r else {"path": p})
        self.enqueue(rows, play=not self.is_playing())

    def remove(self):
        i = self.list.currentRow()
        if 0 <= i < len(self.queue):
            if i == self.index:
                self.stop()
                self.index = -1
            elif i < self.index:
                self.index -= 1
            self.queue.pop(i)
            self._fill()

    def clear(self):
        self.stop()
        self.queue, self.index = [], -1
        self._fill()
        self._show(None)

    def _cur(self):
        return self.queue[self.index] if 0 <= self.index < len(self.queue) else None

    def reveal(self):
        q = self.queue[self.list.currentRow()] if 0 <= self.list.currentRow() < len(self.queue) else self._cur()
        if q:
            W.reveal(q["path"])

    def inspect(self):
        q = self.queue[self.list.currentRow()] if 0 <= self.list.currentRow() < len(self.queue) else self._cur()
        if q and q.get("id"):
            self.app.go("inspector", track_id=q["id"])

    # ---------- transport ----------
    def is_playing(self):
        return self.player.playbackState() == self.QMediaPlayer.PlaybackState.PlayingState

    def play_index(self, i):
        if not 0 <= i < len(self.queue):
            return
        keep = self.player.position() if self.keep_pos.isChecked() and self._cur() else None
        self.index = i
        q = self.queue[i]
        self.state.setText("")
        if not os.path.isfile(q["path"]):
            self.state.setText("⚠ File not available (moved, deleted or drive disconnected)")
            self._show(q)
            self._fill()
            return
        self._resume_at = keep
        self.player.setSource(QUrl.fromLocalFile(q["path"]))
        self.player.play()
        self._show(q)
        self._fill()
        self.app.now_playing(q)

    def toggle(self):
        if self.is_playing():
            self.player.pause()
        elif self._cur():
            if self.player.source().isEmpty():
                self.play_index(self.index)
            else:
                self.player.play()
        elif self.queue:
            self.play_index(0)

    def stop(self):
        self.player.stop()

    def next(self):
        if self.index + 1 < len(self.queue):
            self.play_index(self.index + 1)

    def prev(self):
        if self.player.position() > 3000 or self.index == 0:
            self.player.setPosition(0)
        else:
            self.play_index(self.index - 1)

    def _seek_release(self):
        self._seeking = False
        self.player.setPosition(self.slider.value())

    def _pos(self, ms):
        if not self._seeking:
            self.slider.setValue(ms)
        self.pos_l.setText(fmt_ms(ms))

    def _dur(self, ms):
        self.slider.setRange(0, ms)
        self.dur_l.setText(fmt_ms(ms))

    def _state(self, st):
        playing = st == self.QMediaPlayer.PlaybackState.PlayingState
        self.play_btn.setText("⏸  Pause" if playing else "▶  Play")

    def _status(self, st):
        S = self.QMediaPlayer.MediaStatus
        if st in (S.LoadedMedia, S.BufferedMedia) and self._resume_at:
            self.player.setPosition(self._resume_at)
            self._resume_at = None
        if st == S.EndOfMedia:
            self.next()
        if st == S.InvalidMedia:
            self.state.setText("⚠ This file cannot be decoded for playback")

    def _show(self, q):
        if not q:
            self.title.setText("Nothing playing")
            self.sub.setText("")
            self.art.clear()
            self.art.setText("♪")
            return
        self.title.setText(q["title"])
        self.sub.setText(" · ".join(x for x in (q["artist"], q["album"]) if x))
        data = None
        try:
            import mutagen
            from .. import tags
            f = mutagen.File(q["path"])
            art = tags.get_art(f) if f else None
            data = art[0] if art else None
        except Exception:
            pass
        pm = W.pixmap(data, 296) if data else None
        if pm and not pm.isNull():
            self.art.setPixmap(pm)
        else:
            self.art.clear()
            self.art.setText("♪")


# ================================================================== Audio Verification
VSTATUS_COLOR = {"Verified match": W.GREEN, "Likely match": "#7bd88f", "Possible mismatch": W.AMBER,
                 "Strong mismatch": W.RED, "No fingerprint match": W.MUTED, "Needs manual review": "#ff8a3d",
                 "Not verified": "#555b66"}


class VerifyWorker(QThread):
    progressed = Signal(int, int, str)
    done = Signal(dict)

    def __init__(self, db_path, ids, key):
        super().__init__()
        self.db_path, self.ids, self.key = db_path, ids, key
        self.cancelled = False
        self.paused = False

    def run(self):
        from .. import verify
        conn = db.connect(self.db_path)
        try:
            res = verify.run_batch(conn, self.ids, self.key, progress=lambda i, n, m: self.progressed.emit(i, n, m),
                                   cancel=lambda: self.cancelled, paused=lambda: self.paused)
        except Exception as e:  # e.g. fpcalc missing
            res = dict(counts={}, errors=[str(e)], done=0, cancelled=False)
        finally:
            conn.close()
        self.done.emit(res)


class VerificationPage(Page):
    COLS = [("vstatus", "Verification", None), ("name", "Title", None), ("artist", "Artist", None),
            ("album", "Album", None), ("identified", "Audio identified as", None), ("fp", "Match", None),
            ("note", "Notes", None)]

    def __init__(self, app):
        super().__init__(app)
        from .. import verify
        self.V = verify
        self.outer.addWidget(W.page_header(
            "Audio Verification", "Checks whether the actual audio matches the tags, using Chromaprint "
            "fingerprints and AcoustID/MusicBrainz. Results are evidence for you to review — files are never "
            "changed or replaced, and “no fingerprint match” never means a file is wrong."))
        self.banner = QLabel()
        self.banner.setWordWrap(True)
        self.banner.setTextFormat(Qt.RichText)
        self.banner.setOpenExternalLinks(True)
        self.outer.addWidget(self.banner)
        self.filter = QComboBox()
        self.filter.addItems(["Problems to review", "All songs", "Not verified yet"] + list(verify.STATUSES))
        self.filter.currentTextChanged.connect(lambda _: self.reload())
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search…")
        self.search.setClearButtonEnabled(True)
        self.timer = QTimer(self, singleShot=True, interval=250, timeout=self.reload)
        self.search.textChanged.connect(lambda: self.timer.start())
        self.outer.addWidget(W.hbox(self.filter, self.search))
        self.table = W.make_table(self.COLS, [150, 200, 140, 150, 240, 56])
        self.table.selectionModel().selectionChanged.connect(lambda *_: self.show_detail())
        self.table.doubleClicked.connect(lambda _: self.play())
        self.detail = QLabel()
        self.detail.setWordWrap(True)
        self.detail.setTextFormat(Qt.RichText)
        self.detail.setTextInteractionFlags(Qt.TextBrowserInteraction)
        self.detail.setOpenExternalLinks(True)
        self.detail.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        dframe, dl = section("Details")
        dl.addWidget(self.detail, 1)
        split = QSplitter(Qt.Vertical)
        split.addWidget(self.table)
        split.addWidget(dframe)
        split.setSizes([460, 200])
        self.outer.addWidget(split, 1)
        self.progress = QProgressBar()
        self.progress.setFixedWidth(220)
        self.progress.hide()
        self.count = W.label("", "muted")
        self.run_sel = button("Verify Selected", lambda: self.start(selected=True))
        self.run_all = button("Verify All Unchecked", lambda: self.start(selected=False), "primary")
        self.pause_btn = button("Pause", self.pause)
        self.cancel_btn = button("Cancel", self.cancel)
        for b in (self.pause_btn, self.cancel_btn):
            b.setEnabled(False)
        self.outer.addWidget(W.hbox(self.count, self.progress, None, button("▶ Play", self.play),
                                    button("Compare…", self.compare,
                                           tip="Queue this file with other library files of the same song, or of "
                                               "the song the audio was identified as"),
                                    button("Inspect", self.inspect), button("Reveal", self.reveal), 12,
                                    self.run_sel, self.run_all, self.pause_btn, self.cancel_btn))
        self.worker = None

    def refresh(self):
        key = db.get_setting(self.app.conn, "acoustid_key").strip()
        fp = scanner.tool("fpcalc")
        msgs = []
        if not fp:
            msgs.append(f"<span style='color:{W.RED}'>Chromaprint is not installed — run <code>brew install "
                        "chromaprint</code>.</span>")
        if not key:
            msgs.append(f"<span style='color:{W.AMBER}'>No AcoustID API key yet.</span> Fingerprints can still be "
                        "generated and cached now; identification needs a free key from "
                        "<a style='color:#5b8cff' href='https://acoustid.org/new-application'>acoustid.org</a> "
                        "(paste it in Settings).")
        self.banner.setText(" ".join(msgs))
        self.banner.setVisible(bool(msgs))
        self.run_all.setText("Verify All Unchecked" if key else "Fingerprint All")
        self.reload()

    def reload(self):
        f = self.filter.currentText()
        q = self.search.text().strip()
        where, args = ["t.missing=0"], []
        if q:
            where.append("(t.title LIKE ? OR t.artist LIKE ? OR t.album LIKE ? OR v.matched_title LIKE ?)")
            args += [f"%{q}%"] * 4
        if f == "Problems to review":
            where.append("v.status IN (?,?,?)")
            args += [self.V.POSSIBLE, self.V.STRONG, self.V.REVIEW]
        elif f == "Not verified yet":
            where.append("v.track_id IS NULL")
        elif f in self.V.STATUSES:
            where.append("v.status=?")
            args.append(f)
        rows = []
        order = {s: i for i, s in enumerate([self.V.STRONG, self.V.POSSIBLE, self.V.REVIEW, self.V.NO_MATCH,
                                             self.V.LIKELY, self.V.VERIFIED])}
        for r in self.app.conn.execute(
                f"SELECT t.*, v.status AS vs, v.fp_score, v.matched_title, v.matched_artist, v.notes, v.checked, "
                f"v.size AS vsize, v.mtime AS vmtime FROM tracks t LEFT JOIN verifications v ON v.track_id=t.id "
                f"WHERE {' AND '.join(where)} ORDER BY t.path LIMIT 20000", args):
            d = dict(r)
            stale = d["vs"] and (d["vsize"] != d["size"] or d["vmtime"] != d["mtime"])
            d["vstatus"] = (d["vs"] + " (file changed)") if stale else (d["vs"] or "Not verified")
            d["name"] = d["title"] or os.path.basename(d["path"])
            d["identified"] = f"{d['matched_title']} — {d['matched_artist']}" if d["matched_title"] else ""
            d["fp"] = f"{d['fp_score']:.0%}" if d["fp_score"] else ""
            notes = json.loads(d["notes"] or "[]")
            d["note"] = notes[0] if notes else ""
            d["_color"] = {"vstatus": VSTATUS_COLOR.get(d["vs"] or "Not verified", W.TEXT)}
            d["_order"] = order.get(d["vs"], 9)
            rows.append(d)
        rows.sort(key=lambda d: d["_order"])
        self.table.source_model.set_rows(rows)
        c = dict(self.app.conn.execute("SELECT v.status, count(*) FROM verifications v JOIN tracks t ON "
                                       "t.id=v.track_id WHERE t.missing=0 GROUP BY v.status").fetchall())
        total = self.app.conn.execute("SELECT count(*) FROM tracks WHERE missing=0").fetchone()[0]
        fps = self.app.conn.execute("SELECT count(*) FROM tracks WHERE missing=0 AND fingerprint IS NOT NULL"
                                    ).fetchone()[0]
        self.count.setText(f"{len(rows):,} shown · {sum(c.values()):,} of {total:,} verified · {fps:,} fingerprinted"
                           + "".join(f" · {v:,} {k.lower()}" for k, v in c.items()
                                     if k in (self.V.STRONG, self.V.POSSIBLE)))
        self.show_detail()

    def show_detail(self):
        sel = self.table.selected_rows()
        if not sel:
            self.detail.setText(f"<span style='color:{W.MUTED}'>Select a song to see how its audio was identified."
                                "</span>")
            return
        t = sel[0]
        v = self.app.conn.execute("SELECT * FROM verifications WHERE track_id=?", (t["id"],)).fetchone()
        if not v:
            self.detail.setText(f"<b>{t['name']}</b> — {t['artist'] or '?'}<br><span style='color:{W.MUTED}'>Not "
                                "verified yet.</span>")
            return
        color = VSTATUS_COLOR.get(v["status"], W.TEXT)
        html = (f"<span style='color:{color}; font-size:15px; font-weight:600'>{v['status']}</span>"
                f"&nbsp; <span style='color:{W.MUTED}'>fingerprint match {v['fp_score'] or 0:.0%} · checked "
                f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(v['checked']))}</span><br>"
                f"<b>Your tags:</b> “{t['title'] or '—'}” by {t['artist'] or '—'} · {t['album'] or '—'}<br>")
        if v["matched_title"]:
            mb = f"https://musicbrainz.org/recording/{v['recording_id']}"
            albums = json.loads(v["matched_albums"] or "[]")
            html += (f"<b>Audio identified as:</b> “{v['matched_title']}” by {v['matched_artist']}"
                     + (f" · on {', '.join(albums[:4])}" if albums else "")
                     + f" &nbsp;<a style='color:#5b8cff' href='{mb}'>MusicBrainz ↗</a><br>")
        for n in json.loads(v["notes"] or "[]"):
            html += f"<span style='color:{W.MUTED}'>ⓘ {n}</span><br>"
        cands = json.loads(v["candidates"] or "[]")
        if len(cands) > 1:
            html += "<br><b>Other candidates:</b> " + "; ".join(
                f"{c['title'] or '(untitled)'} — {c['artist'] or '?'} ({c['score']:.0%})" for c in cands[1:6])
        self.detail.setText(html)

    # ---------- run ----------
    def start(self, selected):
        if self.worker and self.worker.isRunning():
            return
        if not scanner.tool("fpcalc"):
            return W.error(self, "Chromaprint missing", "Install Chromaprint first:  brew install chromaprint")
        key = db.get_setting(self.app.conn, "acoustid_key").strip()
        ids = [r["id"] for r in self.table.selected_rows()] if selected else None
        if selected and not ids:
            return W.info(self, "No songs", "Select songs first.")
        ids = self.V.pending_ids(self.app.conn) if ids is None else ids
        if not ids:
            return W.info(self, "All done", "Every song has been verified. Select songs and use Verify Selected to "
                                            "check them again.")
        if len(ids) > 50:
            mins = len(ids) * (0.4 if key else 0.15) / 60
            if not W.confirm(self, "Start verification?",
                             f"{len(ids):,} songs. Fingerprints are cached and AcoustID allows about 3 lookups a "
                             f"second, so this takes roughly {mins:.0f} minutes. You can pause, resume or cancel at "
                             "any time and keep using the app. Nothing is modified.", ok="Start"):
                return
        self.worker = VerifyWorker(self.app.db_path, ids, key)
        self.worker.progressed.connect(self._progress)
        self.worker.done.connect(self._done)
        self.progress.setRange(0, len(ids))
        self.progress.setValue(0)
        self.progress.show()
        self.pause_btn.setEnabled(True)
        self.cancel_btn.setEnabled(True)
        self.pause_btn.setText("Pause")
        self.run_sel.setEnabled(False)
        self.run_all.setEnabled(False)
        self.worker.start()

    def _progress(self, i, n, msg):
        self.progress.setValue(i)
        state = "Paused" if self.worker and self.worker.paused else "Verifying"
        self.app.task_label.setText(f"{state} {i:,}/{n:,}: {msg}")
        if i % 25 == 0:
            self.reload()

    def _done(self, res):
        self.progress.hide()
        for b in (self.pause_btn, self.cancel_btn):
            b.setEnabled(False)
        self.run_sel.setEnabled(True)
        self.run_all.setEnabled(True)
        self.app.task_label.setText("Audio verification — " + ("cancelled" if res.get("cancelled") else "done"))
        self.reload()
        from .. import verify
        counts = "\n".join(f"{k}: {v:,}" for k, v in res.get("counts", {}).items() if v)
        errors = res.get("errors", [])
        if errors and errors[0] == verify.KEY_HELP:
            return W.error(self, "AcoustID API key not accepted",
                           verify.KEY_HELP + "\n\nNothing was changed. Fix the key in Settings (use Test Key), "
                                             "then run verification again.")
        stopped = bool(errors) and res.get("stopped")
        if counts:
            msg = f"Verified {res.get('done', 0):,} song(s).\n\n{counts}"
        elif self.worker and not self.worker.key and res.get("done"):
            msg = f"Fingerprinted {res['done']:,} song(s); fingerprints are cached for when you add an API key."
        else:
            msg = f"Processed {res.get('done', 0):,} song(s)."
        if stopped:
            msg = "Verification stopped early.\n\n" + msg
        if errors:
            msg += f"\n\n{len(errors)} problem(s):\n" + "\n".join(errors[:8])
        W.info(self, "Audio verification", msg)

    def pause(self):
        if not self.worker:
            return
        self.worker.paused = not self.worker.paused
        self.pause_btn.setText("Resume" if self.worker.paused else "Pause")
        self.app.task_label.setText("Audio verification paused" if self.worker.paused else "Resuming…")

    def cancel(self):
        if self.worker:
            self.worker.cancelled = True
            self.worker.paused = False
            self.cancel_btn.setEnabled(False)

    def busy(self):
        return bool(self.worker and self.worker.isRunning())

    # ---------- actions ----------
    def play(self):
        rows = self.table.selected_rows()
        if rows:
            self.app.go("player", rows=rows, replace=False)

    def compare(self):
        sel = self.table.selected_rows()
        if not sel:
            return W.info(self, "No song", "Select a song to compare.")
        t = sel[0]
        v = self.app.conn.execute("SELECT * FROM verifications WHERE track_id=?", (t["id"],)).fetchone()
        titles = {t["title"]} | ({v["matched_title"]} if v and v["matched_title"] else set())
        others = []
        for title in filter(None, titles):
            for r in self.app.conn.execute("SELECT * FROM tracks WHERE missing=0 AND id<>? AND lower(title)=? "
                                           "LIMIT 10", (t["id"], title.lower())):
                if r["id"] not in {o["id"] for o in others}:
                    others.append(dict(r, label="same title" if title == t["title"] else "identified song"))
        if v and v["recording_id"]:
            for r in self.app.conn.execute("SELECT * FROM tracks WHERE missing=0 AND id<>? AND acoustid_rec=?",
                                           (t["id"], v["recording_id"])):
                if r["id"] not in {o["id"] for o in others}:
                    others.append(dict(r, label="same recording"))
        self.app.pages["player"].keep_pos.setChecked(True)
        self.app.go("player", rows=[dict(t, label="this file")] + others, replace=True)
        if not others:
            W.info(self, "Compare", "No other library files with the same or identified title were found. The file "
                                    "is queued in the player on its own.")

    def inspect(self):
        rows = self.table.selected_rows()
        if rows:
            self.app.go("inspector", track_id=rows[0]["id"])

    def reveal(self):
        for r in self.table.selected_rows()[:5]:
            W.reveal(r["path"])
