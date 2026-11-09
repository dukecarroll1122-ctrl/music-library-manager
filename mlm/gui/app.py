"""Main window: sidebar navigation, mode switch, background task runner."""
import os
import sys

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QAction, QIcon, QKeySequence
from PySide6.QtWidgets import (QApplication, QButtonGroup, QFileDialog, QHBoxLayout, QLabel, QMainWindow,
                               QProgressBar, QPushButton, QStackedWidget, QVBoxLayout, QWidget)

from .. import db, scanner
from . import widgets as W


class MainWindow(QMainWindow):
    library_changed = Signal()
    mode_changed = Signal(bool)

    def __init__(self, db_path=None):
        super().__init__()
        from . import pages_audio, pages_main, pages_meta, pages_tools
        self.db_path = db_path or db.default_db_path()
        self.conn = db.connect(self.db_path)
        self.edit_mode = False  # read-only is the default on every launch
        self.worker = None
        self._on_done = None
        self.setWindowTitle("Music Library Manager")
        self.resize(1360, 860)
        self.setMinimumSize(1080, 680)

        root = QWidget()
        lay = QHBoxLayout(root)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.setCentralWidget(root)

        side = QWidget()
        side.setObjectName("sidebar")
        side.setFixedWidth(224)
        sl = QVBoxLayout(side)
        sl.setContentsMargins(14, 18, 14, 14)
        sl.setSpacing(3)
        sl.addWidget(W.label("♫  Music Library", "appname"))
        sl.addWidget(W.label("Manager", "muted"))
        sl.addSpacing(18)

        self.stack = QStackedWidget()
        self.pages = {}
        self.nav = QButtonGroup(self)
        specs = [("dashboard", "Dashboard", pages_main.DashboardPage),
                 ("library", "Music Library", pages_main.LibraryPage),
                 ("inspector", "Metadata Inspector", pages_meta.InspectorPage),
                 ("repair", "Metadata Repair", pages_meta.RepairPage),
                 ("artwork", "Artwork Manager", pages_tools.ArtworkPage),
                 ("duplicates", "Duplicate Finder", pages_tools.DuplicatesPage),
                 ("verification", "Audio Verification", pages_audio.VerificationPage),
                 ("player", "Music Player", pages_audio.PlayerPage),
                 ("converter", "Audio Converter", pages_audio.ConverterPage),
                 ("organize", "Organization", pages_tools.OrganizePage),
                 ("reports", "Reports", pages_tools.ReportsPage),
                 ("settings", "Settings", pages_tools.SettingsPage)]
        icons = ["◧", "♪", "⌕", "✎", "▣", "⧉", "◉", "▶", "⇄", "⇅", "⎙", "⚙"]
        for i, (key, title, cls) in enumerate(specs):
            b = QPushButton(f"{icons[i]}   {title}")
            b.setObjectName("nav")
            b.setCheckable(True)
            b.clicked.connect(lambda _=False, k=key: self.go(k))
            self.nav.addButton(b, i)
            sl.addWidget(b)
            page = cls(self)
            page.setObjectName("page")
            self.pages[key] = page
            self.stack.addWidget(page)
            if i < 9:
                act = QAction(self)
                act.setShortcut(QKeySequence(f"Ctrl+{i + 1}"))
                act.triggered.connect(lambda _=False, k=key: self.go(k))
                self.addAction(act)
        self.keys = [s[0] for s in specs]
        sl.addStretch(1)
        self.np = QPushButton("")
        self.np.setObjectName("nav")
        self.np.setToolTip("Now playing — click to open the player")
        self.np.clicked.connect(lambda: self.go("player"))
        self.np.hide()
        sl.addWidget(self.np)

        self.mode_btn = QPushButton()
        self.mode_btn.clicked.connect(self.toggle_mode)
        sl.addWidget(self.mode_btn)
        self.mode_hint = W.label("", "hint", wrap=True)
        self.mode_hint.setStyleSheet(f"color: {W.MUTED}; font-size: 11px;")
        sl.addWidget(self.mode_hint)

        lay.addWidget(side)
        lay.addWidget(self.stack, 1)

        # status bar task area
        self.task_label = QLabel("Ready")
        self.task_label.setStyleSheet(f"color: {W.MUTED}; padding-left: 8px;")
        self.task_label.setMaximumWidth(760)
        self.progress = QProgressBar()
        self.progress.setFixedWidth(260)
        self.progress.hide()
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.hide()
        self.cancel_btn.clicked.connect(self.cancel_task)
        self.statusBar().addWidget(self.task_label, 1)
        self.statusBar().addPermanentWidget(self.progress)
        self.statusBar().addPermanentWidget(self.cancel_btn)

        self._update_mode_ui()
        self.library_changed.connect(self._mark_dirty)
        self.go("dashboard")

    # ---------- navigation ----------
    def go(self, key, **kw):
        page = self.pages[key]
        self.nav.button(self.keys.index(key)).setChecked(True)
        self.stack.setCurrentWidget(page)
        if getattr(page, "dirty", True):
            page.refresh()
            page.dirty = False
        if kw:
            page.open_with(**kw)

    def now_playing(self, item):
        fm = self.np.fontMetrics()
        self.np.setText(fm.elidedText(f"▶  {item['title']} — {item['artist'] or '?'}", Qt.ElideRight, 190))
        self.np.show()

    def _mark_dirty(self):
        for p in self.pages.values():
            p.dirty = True
        cur = self.stack.currentWidget()
        cur.refresh()
        cur.dirty = False

    # ---------- mode ----------
    def toggle_mode(self):
        if not self.edit_mode:
            if not W.confirm(self, "Switch to Edit Mode?",
                             "Edit Mode allows approved changes to be written to your music files: metadata, "
                             "artwork, renames and moves.\n\nNothing is ever changed without your confirmation, "
                             "every change is backed up and logged, and most changes can be undone.",
                             ok="Enable Edit Mode"):
                return
        self.edit_mode = not self.edit_mode
        self._update_mode_ui()
        self.mode_changed.emit(self.edit_mode)

    def _update_mode_ui(self):
        if self.edit_mode:
            self.mode_btn.setText("✎  Edit Mode")
            self.mode_btn.setStyleSheet(f"background: #3a2a12; border: 1px solid {W.AMBER}; color: {W.AMBER}; "
                                        "font-weight: 600; padding: 9px;")
            self.mode_hint.setText("Approved changes can be written to files. Click to return to read-only.")
        else:
            self.mode_btn.setText("🔒  Read-Only Mode")
            self.mode_btn.setStyleSheet(f"background: #12291f; border: 1px solid #1f5a3f; color: {W.GREEN}; "
                                        "font-weight: 600; padding: 9px;")
            self.mode_hint.setText("Files are never modified. Click to enable editing.")

    def require_edit(self):
        if self.edit_mode:
            return True
        if W.confirm(self, "Read-Only Mode is on",
                     "Files cannot be modified in Read-Only Mode. Switch to Edit Mode now?", ok="Switch to Edit Mode"):
            self.edit_mode = True
            self._update_mode_ui()
            self.mode_changed.emit(True)
            return True
        return False

    # ---------- background tasks ----------
    def busy(self):
        return self.worker is not None and self.worker.isRunning()

    def run_task(self, title, fn, *args, on_done=None, refresh=True, **kw):
        if self.busy():
            W.info(self, "Please wait", "Another task is still running. Cancel it or wait for it to finish.")
            return False
        self.worker = W.Worker(self.db_path, fn, args, kw)
        self._on_done, self._refresh_after, self._title = on_done, refresh, title
        self.worker.progressed.connect(self._progress)
        self.worker.succeeded.connect(self._succeeded)
        self.worker.failed.connect(self._failed)
        self.task_label.setText(title + "…")
        self.progress.setRange(0, 0)
        self.progress.show()
        self.cancel_btn.show()
        self.cancel_btn.setEnabled(True)
        self.worker.start()
        return True

    def cancel_task(self):
        if self.worker:
            self.worker.cancelled = True
            self.cancel_btn.setEnabled(False)
            self.task_label.setText("Cancelling…")

    def _progress(self, i, total, msg):
        if total:
            self.progress.setRange(0, total)
            self.progress.setValue(i)
        fm = self.task_label.fontMetrics()
        self.task_label.setText(fm.elidedText(f"{self._title}: {msg}", Qt.ElideMiddle, 740))

    def _end(self, text):
        self.progress.hide()
        self.cancel_btn.hide()
        self.task_label.setText(text)

    def _succeeded(self, res):
        self._end(f"{self._title} — done")
        if self._refresh_after:
            self.library_changed.emit()
        if self._on_done:
            self._on_done(res)

    def _failed(self, msg):
        self._end(f"{self._title} — failed")
        if self._refresh_after:
            self.library_changed.emit()
        W.error(self, self._title, msg)

    # ---------- scanning (used by several pages) ----------
    def scan_folder(self, path=None):
        if not path:
            start = "/Volumes" if os.path.isdir("/Volumes") else os.path.expanduser("~/Music")
            path = QFileDialog.getExistingDirectory(self, "Choose a music folder, drive or SD card", start)
        if not path:
            return

        def done(st):
            msg = (f"Scanned {st['files']:,} audio files in {st['root']}\n\nNew: {st['new']:,}   Updated: "
                   f"{st['updated']:,}   Unchanged: {st['unchanged']:,}\nDamaged or unavailable: {st['errors']:,}   "
                   f"No longer found: {st['missing']:,}")
            if st["status"] != "completed":
                msg = f"Scan {st['status']}.\n\n" + msg
            if st.get("walk_errors"):
                msg += "\n\nSome folders could not be read:\n" + "\n".join(st["walk_errors"][:5])
            W.info(self, "Scan finished", msg)
        db.set_setting(self.conn, "last_root", path)
        self.run_task("Scanning", scanner.scan, path, on_done=done)

    def closeEvent(self, e):
        conv, ver = self.pages["converter"], self.pages["verification"]
        if conv.busy() and not W.confirm(self, "Conversion in progress", "Quit and cancel the running conversion? "
                                         "Unfinished output files are removed; originals are untouched.", ok="Quit"):
            e.ignore()
            return
        self.pages["player"].player.stop()
        for w in (self.worker, conv.worker, ver.worker):
            if w is not None and w.isRunning():
                w.cancelled = True
                if hasattr(w, "paused"):
                    w.paused = False
                w.wait(15000)
        e.accept()


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Music Library Manager")
    app.setOrganizationName("MusicLibraryManager")
    app.setStyle("Fusion")
    from PySide6.QtGui import QColor, QPalette
    pal = QPalette()
    for role, col in ((QPalette.Window, W.BG), (QPalette.Base, "#16191e"), (QPalette.AlternateBase, "#1a1d22"),
                      (QPalette.Text, W.TEXT), (QPalette.WindowText, W.TEXT), (QPalette.Button, "#232731"),
                      (QPalette.ButtonText, W.TEXT), (QPalette.Highlight, "#26375c"),
                      (QPalette.HighlightedText, W.TEXT), (QPalette.ToolTipBase, "#222630"),
                      (QPalette.ToolTipText, W.TEXT), (QPalette.PlaceholderText, "#5f6672")):
        pal.setColor(role, QColor(col))
    app.setPalette(pal)
    app.setStyleSheet(W.STYLE)
    icon = os.path.join(os.path.dirname(__file__), "icon.png")
    if os.path.exists(icon):
        app.setWindowIcon(QIcon(icon))
    win = MainWindow()
    win.show()
    sys.exit(app.exec())
