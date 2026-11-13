"""Artwork gallery widgets: candidate cards, gallery, preview/compare dialogs, manual import panel, apply dialog."""
import os
import re

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, Qt, Signal
from PySide6.QtGui import QGuiApplication, QImage
from PySide6.QtWidgets import (QLayout, QButtonGroup, QCheckBox, QDialog, QDialogButtonBox, QFileDialog, QFrame, QGridLayout,
                               QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QRadioButton,
                               QScrollArea, QVBoxLayout, QWidget)

from .. import artfetch, artmatch as am, artsearch, db
from . import widgets as W
from .pages_main import button

CONF_COLOR = {am.EXACT: W.GREEN, am.STRONG: "#7bd88f", am.POSSIBLE: W.AMBER, am.WEAK: "#ff8a3d", am.UNVERIFIED: W.MUTED}
SOURCE = {"musicbrainz": "MusicBrainz / Cover Art Archive", "itunes": "iTunes", "deezer": "Deezer", "discogs": "Discogs",
          "fanart": "fanart.tv", "library": "Your own files", "manual": "Imported by you"}
KIND = {"original": "", "remaster": "Remaster / reissue", "deluxe": "Deluxe / expanded", "compilation": "Compilation",
        "regional": "Regional release", "alternate": "Alternate version", "promo": "Promotional image",
        "fan": "Fan artwork"}
EXTS = ("jpg", "jpeg", "png", "webp")
CARD_HEIGHT = 452  # thumbnail + badge + title/artist/source/edition lines + buttons


def res_text(c):
    w, h = c.get("width"), c.get("height")
    if not w:
        return "size unknown"
    tag = "  ✓ HD" if min(w, h) >= 1000 else "  ⚠ small" if min(w, h) < 500 else ""
    return f"{w}×{h}{tag}" + ("  not square" if max(w, h) / max(1, min(w, h)) > 1.03 else "")


def describe(c):
    """Rich-text summary used by the preview and compare dialogs."""
    color = CONF_COLOR.get(c["confidence"], W.TEXT)
    kind = KIND.get(c.get("kind", "original"), "")
    parts = [f"<b>{c['title']}</b>", f"{c['artist'] or '—'}" + (f" · {c['year']}" if c.get("year") else ""),
             f"<span style='color:{W.MUTED}'>{SOURCE.get(c['provider'], c['provider'])} · {res_text(c)}</span>"]
    if c.get("edition") or kind:
        parts.append(f"<span style='color:{W.AMBER if kind else W.MUTED}'>{kind or c['edition']}"
                     f"{' — ' + c['edition'] if kind and c.get('edition') else ''}</span>")
    parts.append(f"<span style='color:{color}; font-weight:600'>{c['confidence']}</span>")
    parts += [f"<span style='color:{W.MUTED}'>ⓘ {r}</span>" for r in c.get("reasons", [])[:4]]
    return "<br>".join(parts)


class CandidateCard(QFrame):
    preview = Signal(dict)
    chosen = Signal(dict)
    rejected = Signal(dict)
    compare_toggled = Signal(dict, bool)

    def __init__(self, cand, selected=False):
        super().__init__()
        self.cand = cand
        self.setObjectName("card")
        self.setFixedWidth(206)
        color = CONF_COLOR.get(cand["confidence"], W.TEXT)
        border = W.GREEN if selected else W.BORDER
        self.setStyleSheet(f"QFrame#card {{ border: 2px solid {border}; }}")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(4)
        self.img = QLabel()
        self.img.setFixedSize(184, 184)
        self.img.setAlignment(Qt.AlignCenter)
        pm = W.pixmap(cand.get("thumb"), 184)
        if pm.isNull():
            self.img.setText("no preview")
        else:
            self.img.setPixmap(pm)
        if cand.get("rejected"):
            self.setEnabled(True)
            self.img.setStyleSheet("opacity: 0.3;")
        lay.addWidget(self.img, 0, Qt.AlignHCenter)
        badge = QLabel(f"● {cand['confidence']}")
        badge.setStyleSheet(f"color: {color}; font-weight: 600; font-size: 11.5px;")
        lay.addWidget(badge)
        t = QLabel(f"<b>{cand['title']}</b>")
        t.setWordWrap(True)
        t.setMaximumHeight(38)
        t.setToolTip(cand["title"])
        lay.addWidget(t)
        sub = QLabel(f"{cand['artist'] or '—'}" + (f" · {cand['year']}" if cand.get("year") else ""))
        sub.setStyleSheet(f"color: {W.MUTED}; font-size: 11.5px;")
        sub.setWordWrap(True)
        sub.setMaximumHeight(32)
        lay.addWidget(sub)
        src = QLabel(f"{SOURCE.get(cand['provider'], cand['provider'])}<br>{res_text(cand)}")
        src.setStyleSheet(f"color: {W.MUTED}; font-size: 11px;")
        lay.addWidget(src)
        kind = KIND.get(cand.get("kind", "original"), "")
        ed = QLabel((kind or cand.get("edition") or " ") + (f" ({cand['edition']})" if kind and cand.get("edition") else ""))
        ed.setStyleSheet(f"color: {W.AMBER if kind else W.MUTED}; font-size: 11px;")
        ed.setWordWrap(True)
        ed.setMaximumHeight(30)
        lay.addWidget(ed)
        if cand.get("reasons"):
            self.setToolTip("\n".join("• " + r for r in cand["reasons"]))
        row = QHBoxLayout()
        row.setSpacing(4)
        for text, sig in (("Preview", self.preview), ("Select", self.chosen),
                          ("Restore" if cand.get("rejected") else "Reject", self.rejected)):
            b = button(text, lambda _=False, s=sig: s.emit(self.cand), "primary" if text == "Select" else None)
            b.setStyleSheet("padding: 4px 8px; font-size: 11.5px;")
            row.addWidget(b)
        lay.addLayout(row)
        self.cmp = QCheckBox("Compare")
        self.cmp.toggled.connect(lambda on: self.compare_toggled.emit(self.cand, on))
        lay.addWidget(self.cmp)


class Gallery(QScrollArea):
    """Scrollable grid of candidate cards. Re-flows when the window is resized."""
    preview = Signal(dict)
    chosen = Signal(dict)
    rejected = Signal(dict)
    compare_changed = Signal(list)

    def __init__(self):
        super().__init__()
        self.setWidgetResizable(True)
        self.setFrameShape(QScrollArea.NoFrame)
        self.body = QWidget()
        self.grid = QGridLayout(self.body)
        self.grid.setSpacing(10)
        self.grid.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.grid.setSizeConstraint(QLayout.SetMinimumSize)  # body grows with its cards; the area scrolls
        self.setWidget(self.body)
        self.cands, self.sel, self.cmp = [], None, []
        self.empty = ""

    def set_cands(self, cands, selected=None, empty=""):
        self.cands, self.sel, self.cmp, self.empty = list(cands), selected, [], empty
        self.layout_cards()

    def layout_cards(self):
        while self.grid.count():
            it = self.grid.takeAt(0)
            if it.widget():
                it.widget().hide()
                it.widget().deleteLater()
        if not self.cands:
            self.grid.addWidget(W.label(self.empty or "No results yet. Use Search All Sources.", "muted", True), 0, 0)
            return
        cols = max(1, (self.viewport().width() - 10) // 216)
        for i, c in enumerate(self.cands):
            card = CandidateCard(c, selected=c.get("idx") == self.sel)
            card.setFixedHeight(CARD_HEIGHT)
            card.preview.connect(self.preview)
            card.chosen.connect(self.chosen)
            card.rejected.connect(self.rejected)
            card.compare_toggled.connect(self._cmp)
            self.grid.addWidget(card, i // cols, i % cols)

    def _cmp(self, cand, on):
        self.cmp = [c for c in self.cmp if c is not cand]
        if on:
            self.cmp.append(cand)
        self.compare_changed.emit(self.cmp[-2:])

    def resizeEvent(self, e):
        old = max(1, (e.oldSize().width() - 10) // 216) if e.oldSize().width() > 0 else 0
        super().resizeEvent(e)
        if self.cands and max(1, (self.viewport().width() - 10) // 216) != old:
            self.layout_cards()


# ---------------------------------------------------------------- preview / compare
def _load_image(conn, cand, progress=None, cancel=None):
    data = artfetch.candidate_image(conn, cand)
    return data, artsearch.inspect_image(data)


class PreviewDialog(QDialog):
    """Full-size look at one candidate; the image downloads in the background (cached)."""

    def __init__(self, app, cand, on_select=None):
        super().__init__(app)
        self.app, self.cand, self.on_select = app, cand, on_select
        self.setWindowTitle(f"Preview — {cand['title']}")
        self.resize(860, 900)
        lay = QVBoxLayout(self)
        self.info = QLabel(describe(cand))
        self.info.setTextFormat(Qt.RichText)
        self.info.setWordWrap(True)
        lay.addWidget(self.info)
        self.scroll = QScrollArea()
        self.scroll.setAlignment(Qt.AlignCenter)
        self.scroll.setWidgetResizable(False)
        self.lab = QLabel("Downloading full-size image…")
        self.lab.setAlignment(Qt.AlignCenter)
        self.scroll.setWidget(self.lab)
        lay.addWidget(self.scroll, 1)
        self.actual = QCheckBox("Actual size (scroll to inspect detail)")
        self.actual.toggled.connect(self.show_pixmap)
        self.warn = QLabel()
        self.warn.setWordWrap(True)
        self.warn.setStyleSheet(f"color: {W.AMBER};")
        lay.addWidget(self.warn)
        buttons = [self.actual, None]
        if on_select:
            buttons.append(button("Select This Cover", self.select, "primary"))
        buttons.append(button("Close", self.accept))
        lay.addWidget(W.hbox(*buttons))
        self.data = None
        self.w = W.Worker(app.db_path, _load_image, (cand,), {})
        self.w.succeeded.connect(self.loaded)
        self.w.failed.connect(lambda m: self.lab.setText(f"Could not load the image:\n{m}"))
        self.w.start()

    def loaded(self, res):
        self.data, info = res
        self.info.setText(describe(dict(self.cand, width=info["w"], height=info["h"])))
        self.warn.setText("\n".join("⚠ " + w for w in info["warnings"]))
        self.show_pixmap()

    def show_pixmap(self, *_):
        if not self.data:
            return
        pm = W.pixmap(self.data)
        if not self.actual.isChecked():
            pm = pm.scaled(self.scroll.viewport().size() - self.scroll.viewport().size() * 0.02, Qt.KeepAspectRatio,
                           Qt.SmoothTransformation)
        self.lab.setPixmap(pm)
        self.lab.resize(pm.size())

    def select(self):
        self.on_select(self.cand)
        self.accept()

    def done(self, r):
        if self.w.isRunning():
            self.w.wait(3000)
        super().done(r)


class CompareDialog(QDialog):
    """Two candidates side by side at the same display size."""

    def __init__(self, app, a, b, on_select):
        super().__init__(app)
        self.setWindowTitle("Compare covers")
        self.resize(1040, 760)
        self.on_select = on_select
        lay = QHBoxLayout(self)
        self.labs, self.workers = [], []
        for cand in (a, b):
            col = QVBoxLayout()
            img = QLabel("Downloading…")
            img.setAlignment(Qt.AlignCenter)
            img.setMinimumSize(480, 480)
            img.setStyleSheet(f"background: {W.CARD}; border: 1px solid {W.BORDER}; border-radius: 10px;")
            info = QLabel(describe(cand))
            info.setTextFormat(Qt.RichText)
            info.setWordWrap(True)
            col.addWidget(img, 1)
            col.addWidget(info)
            col.addWidget(button("Use This Cover", lambda _=False, c=cand: self.use(c), "primary"))
            lay.addLayout(col, 1)
            w = W.Worker(app.db_path, _load_image, (cand,), {})
            w.succeeded.connect(lambda res, im=img, inf=info, c=cand: self.loaded(im, inf, c, res))
            w.failed.connect(lambda m, im=img: im.setText(f"Could not load:\n{m}"))
            w.start()
            self.workers.append(w)

    def loaded(self, img, info, cand, res):
        data, meta = res
        img.setPixmap(W.pixmap(data, 470))
        info.setText(describe(dict(cand, width=meta["w"], height=meta["h"])))

    def use(self, cand):
        self.on_select(cand)
        self.accept()

    def done(self, r):
        for w in self.workers:
            if w.isRunning():
                w.wait(3000)
        super().done(r)


# ---------------------------------------------------------------- manual import
def qimage_png(img):
    """Lossless PNG bytes of a QImage (the QByteArray must outlive the QBuffer, or Qt reads freed memory)."""
    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QIODevice.WriteOnly)
    img.save(buf, "PNG")
    buf.close()
    return bytes(ba)


class ImportPanel(QFrame):
    """Browse, drag & drop, paste from clipboard, or download from a URL. Validates before it offers the image."""
    loaded = Signal(bytes, dict, str)

    def __init__(self, app):
        super().__init__()
        self.app = app
        self.setAcceptDrops(True)
        lay = QVBoxLayout(self)
        self.drop = QLabel("Drop an image here\n(JPG, PNG or WebP)")
        self.drop.setAlignment(Qt.AlignCenter)
        self.drop.setMinimumHeight(110)
        self.drop.setStyleSheet(f"border: 2px dashed {W.BORDER}; border-radius: 12px; color: {W.MUTED}; font-size: 14px;")
        lay.addWidget(self.drop)
        self.url = QLineEdit()
        self.url.setPlaceholderText("…or paste an image address (https://…)")
        self.url.returnPressed.connect(self.from_url)
        lay.addWidget(W.hbox(button("Browse…", self.browse), button("Paste from Clipboard", self.paste), self.url,
                             button("Download", self.from_url)))
        self.preview = QLabel()
        self.preview.setAlignment(Qt.AlignCenter)
        self.preview.setMinimumHeight(260)
        lay.addWidget(self.preview, 1)
        self.msg = QLabel()
        self.msg.setWordWrap(True)
        self.msg.setTextFormat(Qt.RichText)
        lay.addWidget(self.msg)
        self.data = None
        self.info = None

    # ----- sources
    def browse(self):
        path, _ = QFileDialog.getOpenFileName(self, "Choose an image", os.path.expanduser("~/Pictures"),
                                              "Images (*.jpg *.jpeg *.png *.webp)")
        if path:
            self.from_path(path)

    def from_path(self, path):
        try:
            data, info = artfetch.import_file(path)
        except artsearch.ImageError as e:
            return self.fail(str(e))
        self.accept_image(data, info, os.path.basename(path), path)

    def from_url(self):
        url = self.url.text().strip()
        if not url:
            return
        self.msg.setText(f"<span style='color:{W.MUTED}'>Downloading…</span>")
        self.msg.repaint()
        try:
            data, info = artfetch.import_url(self.app.conn, url)
        except artsearch.ImageError as e:
            return self.fail(str(e))
        self.accept_image(data, info, url, url)

    def paste(self):
        md = QGuiApplication.clipboard().mimeData()
        if md.hasUrls():
            u = md.urls()[0]
            return self.from_path(u.toLocalFile()) if u.isLocalFile() else self.url.setText(u.toString()) or self.from_url()
        if md.hasImage():
            png = qimage_png(QGuiApplication.clipboard().image())
            try:
                info = artsearch.inspect_image(png)
            except artsearch.ImageError as e:
                return self.fail(str(e))
            return self.accept_image(png, info, "clipboard image", "clipboard")
        if md.hasText() and re.match(r"https?://", md.text().strip()):
            self.url.setText(md.text().strip())
            return self.from_url()
        self.fail("The clipboard doesn't contain an image or an image address.")

    def dragEnterEvent(self, e):
        md = e.mimeData()
        if md.hasUrls() or md.hasImage():
            e.acceptProposedAction()

    def dropEvent(self, e):
        md = e.mimeData()
        if md.hasUrls():
            u = md.urls()[0]
            if u.isLocalFile():
                return self.from_path(u.toLocalFile())
            self.url.setText(u.toString())
            return self.from_url()
        if md.hasImage():
            png = qimage_png(QImage(md.imageData()))
            try:
                self.accept_image(png, artsearch.inspect_image(png), "dropped image", "drop")
            except artsearch.ImageError as e:
                self.fail(str(e))

    # ----- result
    def fail(self, text):
        self.data = self.info = None
        self.preview.clear()
        self.msg.setText(f"<span style='color:{W.RED}'>✕ {text}</span>")

    def accept_image(self, data, info, label, source):
        self.data, self.info = data, dict(info, source=source)
        self.preview.setPixmap(W.pixmap(data, 300))
        warn = "".join(f"<br><span style='color:{W.AMBER}'>⚠ {w}</span>" for w in info["warnings"])
        self.msg.setText(f"<b>{label}</b> — {info['w']}×{info['h']} {info['fmt']} · {W.fmt_size(info['bytes'])}{warn}"
                         + ("" if info["warnings"] else f"<br><span style='color:{W.GREEN}'>✓ Looks good</span>"))
        self.loaded.emit(data, self.info, label)


# ---------------------------------------------------------------- apply options
class ApplyDialog(QDialog):
    """Where the chosen cover goes. Nothing is written until the user confirms here."""

    def __init__(self, app, tracks, title, info, default_missing_only=True):
        super().__init__(app)
        self.setWindowTitle("Apply artwork")
        self.setMinimumWidth(620)
        self.tracks = tracks
        lay = QVBoxLayout(self)
        lay.addWidget(W.label(f"<b>{title}</b><br>{info}", wrap=True))
        self.embed = QCheckBox("Embed in the songs' tags")
        self.embed.setChecked(True)
        self.only_missing = QRadioButton("Only songs without artwork (existing covers stay)")
        self.replace = QRadioButton("Replace embedded artwork too (the old cover is backed up first)")
        self.only_missing.setChecked(default_missing_only)
        self.replace.setChecked(not default_missing_only)
        self.folder = QCheckBox("Save as folder.jpg next to the songs")
        self.cover = QCheckBox("Save as cover.jpg next to the songs")
        self.replace_files = QCheckBox("Replace an existing folder.jpg / cover.jpg (the old file is backed up)")
        self.copy_to = QCheckBox("Save a separate copy (does not touch your music folders)")
        self.copy_path = QLineEdit(os.path.expanduser("~/Pictures"))
        for w in (self.only_missing, self.replace):
            w.setContentsMargins(24, 0, 0, 0)
        self.embed.toggled.connect(lambda on: [w.setEnabled(on) for w in (self.only_missing, self.replace)])
        for w in (self.embed, self.only_missing, self.replace, self.folder, self.cover, self.replace_files):
            lay.addWidget(w)
        lay.addWidget(W.hbox(self.copy_to, self.copy_path, button("Browse…", self.pick_dir)))
        lay.addWidget(W.label("Songs to apply to", "muted"))
        self.list = QListWidget()
        self.list.setMaximumHeight(170)
        for t in tracks:
            it = QListWidgetItem(("✓ " if t["has_art"] else "   ") + os.path.basename(t["path"]))
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Checked)
            it.setData(Qt.UserRole, t["id"])
            it.setToolTip(t["path"])
            self.list.addItem(it)
        lay.addWidget(self.list)
        lay.addWidget(W.hbox(button("All", lambda: self.check_all(True)), button("None", lambda: self.check_all(False)),
                             button("Only Without Artwork", self.check_missing),
                             W.label("✓ = already has artwork", "muted"), None))
        self.note = W.label("Embedding and folder files need Edit Mode. Audio data and all other tags are preserved.",
                            "muted", True)
        lay.addWidget(self.note)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("Apply")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def pick_dir(self):
        d = QFileDialog.getExistingDirectory(self, "Save copy to", self.copy_path.text())
        if d:
            self.copy_path.setText(d)
            self.copy_to.setChecked(True)

    def check_all(self, on):
        for i in range(self.list.count()):
            self.list.item(i).setCheckState(Qt.Checked if on else Qt.Unchecked)

    def check_missing(self):
        for i in range(self.list.count()):
            it = self.list.item(i)
            it.setCheckState(Qt.Unchecked if it.text().startswith("✓") else Qt.Checked)

    def ids(self):
        return [self.list.item(i).data(Qt.UserRole) for i in range(self.list.count())
                if self.list.item(i).checkState() == Qt.Checked]

    def options(self, conn):
        return artfetch.ApplyOptions(
            embed=self.embed.isChecked(), only_missing=self.only_missing.isChecked(),
            folder_jpg=self.folder.isChecked(), cover_jpg=self.cover.isChecked(),
            replace_files=self.replace_files.isChecked(),
            save_to=self.copy_path.text().strip() if self.copy_to.isChecked() else "",
            max_side=artfetch.embed_max_side(conn))

    def accept(self):
        o = self.options(self.parent().conn)
        if not (o.embed or o.folder_jpg or o.cover_jpg or o.save_to):
            return W.info(self, "Nothing to do", "Choose at least one place to put the artwork.")
        if not self.ids():
            return W.info(self, "No songs", "Tick at least one song.")
        super().accept()


def apply_selection(conn, track_ids, opts, edit_mode, cand=None, data=None, source=None, label="", job_id=None,
                    progress=None, cancel=None):
    """Worker entry point: download the chosen candidate if needed, then apply it."""
    if data is None:
        data = artfetch.candidate_image(conn, cand)
        source = artfetch.source_of(cand)
    return artfetch.apply_image(conn, track_ids, data, opts, edit_mode, source, label, job_id)


def summarize_apply(res):
    parts = []
    if res["embedded"]:
        parts.append(f"Embedded in {res['embedded']} song(s).")
    if res["files"]:
        parts.append("Saved:\n" + "\n".join("  " + f for f in res["files"][:6]))
    if res["skipped"]:
        parts.append("Skipped:\n" + "\n".join("  " + s for s in res["skipped"][:6]))
    if res["errors"]:
        parts.append(f"{len(res['errors'])} problem(s):\n" + "\n".join("  " + e for e in res["errors"][:6]))
    return "\n\n".join(parts) or "Nothing was changed."


# ---------------------------------------------------------------- settings tab
def _check_provider(conn, name, progress=None, cancel=None):
    from .. import artproviders as ap
    return ap.PROVIDERS[name].check(ap.Ctx.from_conn(conn, fresh=True))


class ArtSettings(QWidget):
    """Provider priority/enable switches, credentials, connection status, and download preferences."""

    STATE = {"ok": ("●", W.GREEN), "needs-key": ("●", W.AMBER), "invalid-key": ("●", W.RED), "error": ("●", W.RED),
             "off": ("○", W.MUTED), "unknown": ("○", W.MUTED)}

    def __init__(self, app):
        super().__init__()
        from PySide6.QtWidgets import QComboBox
        from .pages_meta import section
        self.app = app
        self._workers = []
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 10, 0, 0)
        fr, fl = section("Sources and priority")
        fl.addWidget(W.label("The order is the priority: it breaks ties between equally good matches and decides the "
                             "order when searching “in priority order”. A source that is unavailable or rejects its "
                             "key never stops the others.", "muted", True))
        self.rows = QVBoxLayout()
        fl.addLayout(self.rows)
        lay.addWidget(fr)

        fr, cl = section("Credentials (only for sources that need them)")
        self.discogs = QLineEdit()
        self.discogs.setEchoMode(QLineEdit.Password)
        self.discogs.setPlaceholderText("Discogs personal access token")
        self.fanart = QLineEdit()
        self.fanart.setEchoMode(QLineEdit.Password)
        self.fanart.setPlaceholderText("fanart.tv personal API key")
        for e, k in ((self.discogs, "discogs_token"), (self.fanart, "fanart_key")):
            e.editingFinished.connect(lambda e=e, k=k: self.save_key(k, e))
        g = QGridLayout()
        g.addWidget(W.label("Discogs", "muted"), 0, 0)
        g.addWidget(self.discogs, 0, 1)
        g.addWidget(W.label("fanart.tv", "muted"), 1, 0)
        g.addWidget(self.fanart, 1, 1)
        cl.addLayout(g)
        note = W.label("Discogs: create a token at <a style='color:#5b8cff' href='https://www.discogs.com/settings/"
                       "developers'>discogs.com/settings/developers</a> (free). fanart.tv: get a personal key at "
                       "<a style='color:#5b8cff' href='https://fanart.tv/get-an-api-key/'>fanart.tv/get-an-api-key</a> "
                       "(free). MusicBrainz, Cover Art Archive, iTunes and Deezer need nothing. Keys are stored only "
                       "in this app's local database and sent only to their own service.", "muted", True)
        note.setOpenExternalLinks(True)
        cl.addWidget(note)
        lay.addWidget(fr)

        fr, pl = section("Search and download")
        self.mode = QComboBox()
        self.mode.addItems(["Search every source at once", "Search in priority order, stop at the first strong match"])
        self.res = QComboBox()
        self.res.addItems(["Best available (original)", "At least 500 px", "At least 1000 px", "At least 1500 px",
                           "At least 2000 px"])
        self.side = QComboBox()
        self.side.addItems(["Keep as downloaded", "Scale down above 3000 px", "Scale down above 2000 px",
                            "Scale down above 1500 px"])
        for w, k in ((self.mode, "mode"), (self.res, "res"), (self.side, "side")):
            w.currentIndexChanged.connect(lambda _, k=k: self.save_prefs())
        g = QGridLayout()
        for r, (lab, w) in enumerate((("Search mode", self.mode), ("Download size", self.res),
                                      ("Embedded size limit", self.side))):
            g.addWidget(W.label(lab, "muted"), r, 0)
            g.addWidget(w, r, 1)
        pl.addLayout(g)
        pl.addWidget(W.label("Images are never upscaled: if the original is smaller than the size you ask for, you get "
                             "the original. JPEG and PNG are embedded byte-for-byte; WebP becomes lossless PNG. "
                             "folder.jpg/cover.jpg always get the image as downloaded.", "muted", True))
        lay.addWidget(fr)
        lay.addStretch(1)
        self.status, self.checks = {}, {}

    RES = [0, 500, 1000, 1500, 2000]
    SIDE = [0, 3000, 2000, 1500]

    def refresh(self):
        from .. import artproviders as ap
        c = self.app.conn
        cfg = ap.load_config(c)
        self.order, self.disabled = list(cfg["order"]), set(cfg["disabled"])
        for k, e in (("discogs_token", self.discogs), ("fanart_key", self.fanart)):
            e.setText(db.get_setting(c, k))
        for w in (self.mode, self.res, self.side):
            w.blockSignals(True)
        self.mode.setCurrentIndex(0 if artfetch.search_mode(c) == "all" else 1)
        self.res.setCurrentIndex(self.RES.index(artfetch.target_resolution(c)) if artfetch.target_resolution(c) in self.RES else 0)
        side = artfetch.embed_max_side(c)
        self.side.setCurrentIndex(self.SIDE.index(side) if side in self.SIDE else 1)
        for w in (self.mode, self.res, self.side):
            w.blockSignals(False)
        self.build_rows()

    def build_rows(self):
        from .. import artproviders as ap
        while self.rows.count():
            it = self.rows.takeAt(0)
            if it.widget():
                it.widget().hide()
                it.widget().deleteLater()
        for i, n in enumerate(self.order):
            p = ap.PROVIDERS[n]
            cb = QCheckBox(p.label)
            cb.setChecked(n not in self.disabled)
            cb.toggled.connect(lambda on, n=n: self.toggle(n, on))
            dot = QLabel("○")
            lab = QLabel(self.status.get(n, ("Not checked", "unknown"))[0])
            lab.setStyleSheet(f"color: {W.MUTED};")
            st = self.status.get(n, ("", "unknown"))[1]
            sym, col = self.STATE.get(st, self.STATE["unknown"])
            dot.setText(sym)
            dot.setStyleSheet(f"color: {col};")
            lab.setText(self.status.get(n, ("Not checked yet — click Test", "unknown"))[0])
            up, dn = button("↑", lambda _=False, i=i: self.move(i, -1)), button("↓", lambda _=False, i=i: self.move(i, 1))
            up.setEnabled(i > 0)
            dn.setEnabled(i < len(self.order) - 1)
            for b in (up, dn):
                b.setFixedWidth(34)
            row = W.hbox(W.label(f"{i + 1}.", "muted"), cb, None, dot, lab, up, dn, button("Test", lambda _=False, n=n: self.test(n)))
            row.setMinimumHeight(34)
            self.rows.addWidget(row)

    def move(self, i, d):
        self.order[i], self.order[i + d] = self.order[i + d], self.order[i]
        self.save_order()
        self.build_rows()

    def toggle(self, n, on):
        (self.disabled.discard if on else self.disabled.add)(n)
        self.save_order()

    def save_order(self):
        from .. import artproviders as ap
        ap.save_config(self.app.conn, self.order, self.disabled)

    def save_key(self, key, edit):
        db.set_setting(self.app.conn, key, edit.text().strip())

    def save_prefs(self):
        c = self.app.conn
        db.set_setting(c, "art_search_mode", "all" if self.mode.currentIndex() == 0 else "priority")
        db.set_setting(c, "art_resolution", str(self.RES[self.res.currentIndex()]))
        db.set_setting(c, "art_max_side", str(self.SIDE[self.side.currentIndex()]))

    def test(self, name):
        for e, k in ((self.discogs, "discogs_token"), (self.fanart, "fanart_key")):
            self.save_key(k, e)
        self.status[name] = ("Checking…", "unknown")
        self.build_rows()
        w = W.Worker(self.app.db_path, _check_provider, (name,), {})
        w.succeeded.connect(lambda res, n=name: self.tested(n, res))
        w.failed.connect(lambda m, n=name: self.tested(n, ("error", m)))
        self._workers.append(w)
        w.start()

    def tested(self, name, res):
        state, msg = res
        self.status[name] = (msg, state)
        self.build_rows()
