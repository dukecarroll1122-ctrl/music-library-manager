"""Shared GUI pieces: theme, cards, bar charts, table model, background worker, helpers."""
import os
import subprocess
import time

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QSortFilterProxyModel, Qt, QThread, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QFrame, QHBoxLayout, QHeaderView, QLabel, QMessageBox,
                               QSizePolicy, QTableView, QVBoxLayout, QWidget)

from .. import db

BG, PANEL, CARD, BORDER = "#111316", "#0c0d10", "#1a1d22", "#272b33"
TEXT, MUTED, ACCENT = "#e7e9ec", "#8a919c", "#5b8cff"
GREEN, AMBER, RED, BLUE, VIOLET = "#3ecf8e", "#f5a524", "#f2555a", "#5b8cff", "#a879ff"
HEALTH_COLOR = {"ok": GREEN, "review": AMBER, "warning": "#ff8a3d", "error": RED}
CONF_COLOR = {"Verified": GREEN, "High-confidence match": "#7bd88f", "Possible match": AMBER,
              "Needs manual review": "#ff8a3d", "No match found": MUTED, "Manual edit": BLUE}

STYLE = f"""
* {{ color: {TEXT}; font-size: 13px; }}
QMainWindow, QWidget#page, QStackedWidget, QScrollArea, QScrollArea > QWidget > QWidget {{ background: {BG}; }}
QWidget#sidebar {{ background: {PANEL}; border-right: 1px solid {BORDER}; }}
QPushButton#nav {{ text-align: left; padding: 9px 14px; border: none; border-radius: 8px; color: {MUTED};
                   font-size: 13.5px; background: transparent; }}
QPushButton#nav:hover {{ background: #171a1f; color: {TEXT}; }}
QPushButton#nav:checked {{ background: #1d2330; color: {TEXT}; font-weight: 600; }}
QLabel#h1 {{ font-size: 24px; font-weight: 700; }}
QLabel#h2 {{ font-size: 15px; font-weight: 600; }}
QLabel#muted, QLabel#hint {{ color: {MUTED}; }}
QLabel#appname {{ font-size: 16px; font-weight: 700; }}
QFrame#card {{ background: {CARD}; border: 1px solid {BORDER}; border-radius: 12px; }}
QPushButton {{ background: #232731; border: 1px solid {BORDER}; border-radius: 7px; padding: 6px 13px; }}
QPushButton:hover {{ background: #2b303b; }}
QPushButton:disabled {{ color: #555b66; background: #1a1d22; }}
QPushButton#primary {{ background: {ACCENT}; border: none; color: white; font-weight: 600; }}
QPushButton#primary:hover {{ background: #6f9bff; }}
QPushButton#danger {{ background: #3a1f24; border: 1px solid #5c2a31; color: #ffb3b6; }}
QLineEdit, QComboBox, QSpinBox, QPlainTextEdit, QTextEdit {{ background: #16191e; border: 1px solid {BORDER};
    border-radius: 7px; padding: 5px 8px; selection-background-color: {ACCENT}; }}
QComboBox QAbstractItemView {{ background: #1a1d22; border: 1px solid {BORDER}; }}
QTableView, QTreeWidget, QListWidget {{ background: #14161a; alternate-background-color: #171a1f;
    border: 1px solid {BORDER}; border-radius: 8px; gridline-color: transparent;
    selection-background-color: #26375c; selection-color: {TEXT}; }}
QHeaderView::section {{ background: #181b20; color: {MUTED}; border: none; border-bottom: 1px solid {BORDER};
    padding: 6px 8px; font-weight: 600; }}
QTabWidget::pane {{ border: none; }}
QTabBar::tab {{ background: transparent; color: {MUTED}; padding: 8px 16px; border-bottom: 2px solid transparent; }}
QTabBar::tab:selected {{ color: {TEXT}; border-bottom: 2px solid {ACCENT}; }}
QProgressBar {{ background: #1a1d22; border: 1px solid {BORDER}; border-radius: 6px; height: 12px;
    text-align: center; font-size: 11px; }}
QProgressBar::chunk {{ background: {ACCENT}; border-radius: 5px; }}
QStatusBar {{ background: {PANEL}; border-top: 1px solid {BORDER}; }}
QScrollBar:vertical {{ background: transparent; width: 10px; }}
QScrollBar::handle:vertical {{ background: #2c313b; border-radius: 5px; min-height: 30px; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; }}
QScrollBar::handle:horizontal {{ background: #2c313b; border-radius: 5px; min-width: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QCheckBox::indicator, QRadioButton::indicator {{ width: 15px; height: 15px; }}
QGroupBox {{ border: 1px solid {BORDER}; border-radius: 10px; margin-top: 12px; padding: 12px; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 12px; padding: 0 4px; color: {MUTED}; }}
QSplitter::handle {{ background: {BG}; }}
QToolTip {{ background: #222630; color: {TEXT}; border: 1px solid {BORDER}; }}
"""


def fmt_size(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024


def fmt_dur(s):
    s = int(s or 0)
    return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


def fmt_kbps(b):
    return f"{(b or 0) // 1000} kbps" if b else ""


def fmt_hz(h):
    return f"{h / 1000:g} kHz" if h else ""


def label(text, obj=None, wrap=False):
    lab = QLabel(text)
    if obj:
        lab.setObjectName(obj)
    lab.setWordWrap(wrap)
    return lab


def hbox(*widgets, stretch_at=None, margins=(0, 0, 0, 0), spacing=8):
    w = QWidget()
    lay = QHBoxLayout(w)
    lay.setContentsMargins(*margins)
    lay.setSpacing(spacing)
    for i, x in enumerate(widgets):
        if x is None:
            lay.addStretch(1)
        elif isinstance(x, int):
            lay.addSpacing(x)
        else:
            lay.addWidget(x)
    return w


def page_header(title, subtitle=""):
    w = QWidget()
    lay = QVBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 6)
    lay.setSpacing(2)
    lay.addWidget(label(title, "h1"))
    if subtitle:
        lay.addWidget(label(subtitle, "muted", wrap=True))
    return w


def confirm(parent, title, text, ok="Apply"):
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Warning)
    box.setWindowTitle(title)
    box.setText(title)
    box.setInformativeText(text)
    yes = box.addButton(ok, QMessageBox.AcceptRole)
    box.addButton(QMessageBox.Cancel)
    box.setDefaultButton(QMessageBox.Cancel)
    box.exec()
    return box.clickedButton() is yes


def info(parent, title, text):
    QMessageBox.information(parent, title, text)


def error(parent, title, text):
    QMessageBox.critical(parent, title, text)


def summarize(res, verb="Applied"):
    n = len(res.get("applied", [])) if "applied" in res else res.get("reverted", 0)
    msg = f"{verb} {n} item(s)."
    if res.get("errors"):
        msg += f"\n\n{len(res['errors'])} problem(s):\n" + "\n".join(res["errors"][:15])
    return msg


def reveal(path):
    if path and os.path.exists(path):
        subprocess.Popen(["open", "-R", path])


def pixmap(data, size=None):
    pm = QPixmap()
    if data:
        pm.loadFromData(bytes(data))
    if size and not pm.isNull():
        pm = pm.scaled(size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    return pm


class Card(QFrame):
    def __init__(self, title, color=ACCENT, hint=""):
        super().__init__()
        self.setObjectName("card")
        self.setMinimumHeight(98)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(4)
        top = QHBoxLayout()
        dot = QLabel("●")
        dot.setStyleSheet(f"color: {color}; font-size: 10px;")
        t = label(title, "muted")
        top.addWidget(dot)
        top.addWidget(t)
        top.addStretch(1)
        lay.addLayout(top)
        self.value = QLabel("—")
        self.value.setStyleSheet("font-size: 26px; font-weight: 700;")
        lay.addWidget(self.value)
        self.sub = label(hint, "hint")
        self.sub.setStyleSheet(f"color: {MUTED}; font-size: 11.5px;")
        lay.addWidget(self.sub)

    def set(self, value, sub=None):
        self.value.setText(str(value))
        if sub is not None:
            self.sub.setText(sub)


class BarChart(QWidget):
    """Horizontal bar chart: one row per (label, value)."""

    def __init__(self, color=ACCENT, colors=None):
        super().__init__()
        self.data, self.color, self.colors = [], QColor(color), colors
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)

    def set_data(self, data):
        self.data = list(data)
        self.setMinimumHeight(max(40, 26 * len(self.data)))
        self.updateGeometry()
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        if not self.data:
            p.setPen(QColor(MUTED))
            p.drawText(self.rect(), Qt.AlignCenter, "No data yet — scan a folder")
            return
        w = self.width()
        label_w = min(170, int(w * 0.38))
        maxv = max(v for _, v in self.data) or 1
        f = QFont(self.font())
        f.setPointSizeF(11.5)
        p.setFont(f)
        for i, (name, v) in enumerate(self.data):
            y = i * 26
            p.setPen(QColor(TEXT))
            text = p.fontMetrics().elidedText(str(name), Qt.ElideRight, label_w - 8)
            p.drawText(0, y, label_w - 8, 22, Qt.AlignVCenter | Qt.AlignLeft, text)
            bar_w = max(2, int((w - label_w - 60) * v / maxv))
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(self.colors[i % len(self.colors)]) if self.colors else self.color)
            p.drawRoundedRect(label_w, y + 5, bar_w, 12, 4, 4)
            p.setPen(QColor(MUTED))
            p.drawText(label_w + bar_w + 6, y, 60, 22, Qt.AlignVCenter, f"{v:,}")


def chart_panel(title, chart):
    fr = QFrame()
    fr.setObjectName("card")
    lay = QVBoxLayout(fr)
    lay.setContentsMargins(16, 14, 16, 14)
    lay.addWidget(label(title, "h2"))
    lay.addWidget(chart)
    lay.addStretch(1)
    return fr


class TableModel(QAbstractTableModel):
    """columns: [(key, header, formatter|None)]. Special key 'health' renders a coloured dot."""

    def __init__(self, columns):
        super().__init__()
        self.columns, self.rows = columns, []

    def set_rows(self, rows):
        self.beginResetModel()
        self.rows = list(rows)
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()):
        return len(self.columns)

    def headerData(self, section, orient, role=Qt.DisplayRole):
        if orient == Qt.Horizontal and role == Qt.DisplayRole:
            return self.columns[section][1]

    def data(self, idx, role=Qt.DisplayRole):
        r = self.rows[idx.row()]
        key, _, fmt = self.columns[idx.column()]
        v = r.get(key)
        if role == Qt.DisplayRole:
            if key == "health":
                return "●"
            if key == "has_art":
                return "✓" if v else "—"
            return fmt(v) if fmt else ("" if v is None else str(v))
        if role == Qt.UserRole:  # sort key
            return v if v is not None else ""
        if role == Qt.ForegroundRole:
            if key == "health":
                return QColor(HEALTH_COLOR.get(v, MUTED))
            if key in ("mb", "confidence"):
                return QColor(CONF_COLOR.get(v, TEXT))
            if key == "has_art":
                return QColor(GREEN if v else MUTED)
            if key == "status":
                return QColor({"approved": GREEN, "rejected": RED, "applied": BLUE, "pending": AMBER}.get(v, MUTED))
            c = r.get("_color", {}).get(key) if isinstance(r.get("_color"), dict) else None
            if c:
                return QColor(c)
        if role == Qt.ToolTipRole:
            if key == "health":
                return r.get("issues_text") or "No problems found"
            return None if v is None else str(v)
        if role == Qt.TextAlignmentRole and key in ("health", "has_art"):
            return Qt.AlignCenter


class Proxy(QSortFilterProxyModel):
    def __init__(self):
        super().__init__()
        self.setSortRole(Qt.UserRole)
        self.setSortCaseSensitivity(Qt.CaseInsensitive)
        self.setFilterCaseSensitivity(Qt.CaseInsensitive)
        self.setFilterKeyColumn(-1)

    def lessThan(self, a, b):
        x, y = a.data(Qt.UserRole), b.data(Qt.UserRole)
        try:
            return x < y
        except TypeError:
            return str(x) < str(y)


def make_table(columns, widths=None):
    model, proxy, view = TableModel(columns), Proxy(), QTableView()
    proxy.setSourceModel(model)
    view.setModel(proxy)
    view.setSortingEnabled(True)
    view.setAlternatingRowColors(True)
    view.setSelectionBehavior(QAbstractItemView.SelectRows)
    view.setSelectionMode(QAbstractItemView.ExtendedSelection)
    view.verticalHeader().hide()
    view.verticalHeader().setDefaultSectionSize(26)
    view.setShowGrid(False)
    view.setWordWrap(False)
    view.horizontalHeader().setStretchLastSection(True)
    view.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
    for i, w in enumerate(widths or []):
        if w:
            view.setColumnWidth(i, w)
    view.sortByColumn(-1, Qt.AscendingOrder)

    def selected():
        return [model.rows[proxy.mapToSource(i).row()] for i in view.selectionModel().selectedRows()]
    view.selected_rows = selected
    view.source_model = model
    view.proxy = proxy
    return view


class Worker(QThread):
    """Runs fn(conn, *args, progress=, cancel=, **kw) on its own SQLite connection."""
    progressed = Signal(int, int, str)
    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(self, db_path, fn, args, kw):
        super().__init__()
        self.db_path, self.fn, self.args, self.kw = db_path, fn, args, kw
        self.cancelled = False
        self._last = 0.0

    def _progress(self, i, total, msg):
        now = time.time()
        if now - self._last > 0.05 or i >= total:
            self._last = now
            self.progressed.emit(i, total, msg)

    def run(self):
        conn = db.connect(self.db_path)
        try:
            res = self.fn(conn, *self.args, progress=self._progress, cancel=lambda: self.cancelled, **self.kw)
            self.succeeded.emit(res)
        except Exception as e:  # surfaced to the user in a dialog
            self.failed.emit(f"{type(e).__name__}: {e}" if not str(e) else str(e))
        finally:
            conn.close()
