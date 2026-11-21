"""One consistent icon set (Lucide-style outline strokes, 24×24) drawn from inline SVG, so icons share stroke width
and corner style and recolour with the theme. Used instead of text glyphs/emoji."""
from PySide6.QtCore import QByteArray, Qt
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

PATHS = {
    "dashboard": '<rect x="3" y="3" width="7" height="9" rx="1"/><rect x="14" y="3" width="7" height="5" rx="1"/>'
                 '<rect x="14" y="12" width="7" height="9" rx="1"/><rect x="3" y="16" width="7" height="5" rx="1"/>',
    "library": '<path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/>',
    "playlists": '<path d="M21 15V6"/><circle cx="18.5" cy="18" r="2.5"/><path d="M12 12H3"/><path d="M16 6H3"/>'
                 '<path d="M12 18H3"/>',
    "inspector": '<circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/>',
    "repair": '<path d="M17 3a2.85 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5Z"/>',
    "artwork": '<rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="9" cy="9" r="2"/>'
               '<path d="m21 15-3.1-3.1a2 2 0 0 0-2.8 0L6 21"/>',
    "duplicates": '<rect x="8" y="8" width="14" height="14" rx="2"/>'
                  '<path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"/>',
    "verification": '<path d="M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 '
                    '4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z"/>'
                    '<path d="m9 12 2 2 4-4"/>',
    "player": '<polygon points="6 3 20 12 6 21 6 3"/>',
    "converter": '<path d="M8 3 4 7l4 4"/><path d="M4 7h16"/><path d="m16 21 4-4-4-4"/><path d="M20 17H4"/>',
    "organize": '<path d="M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 '
                '0 0-2 2v13a2 2 0 0 0 2 2Z"/>',
    "reports": '<path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z"/>'
               '<path d="M14 2v4a2 2 0 0 0 2 2h4"/><path d="M10 9H8"/><path d="M16 13H8"/><path d="M16 17H8"/>',
    "settings": '<line x1="21" x2="14" y1="4" y2="4"/><line x1="10" x2="3" y1="4" y2="4"/><line x1="21" x2="12" y1="12" '
                'y2="12"/><line x1="8" x2="3" y1="12" y2="12"/><line x1="21" x2="16" y1="20" y2="20"/>'
                '<line x1="12" x2="3" y1="20" y2="20"/><line x1="14" x2="14" y1="2" y2="6"/>'
                '<line x1="8" x2="8" y1="10" y2="14"/><line x1="16" x2="16" y1="18" y2="22"/>',
    "lock": '<rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/>',
    "edit": '<path d="M12 20h9"/><path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4Z"/>',
}


def pixmap(name, color, size=18):
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="{color}" '
           f'stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">{PATHS[name]}</svg>')
    scale = 2  # crisp on Retina
    pm = QPixmap(size * scale, size * scale)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    QSvgRenderer(QByteArray(svg.encode())).render(p)
    p.end()
    pm.setDevicePixelRatio(scale)
    return pm


def icon(name, normal, active=None, size=18):
    """QIcon that is `normal` at rest and `active` when hovered or checked."""
    ic = QIcon()
    ic.addPixmap(pixmap(name, normal, size), QIcon.Normal, QIcon.Off)
    ic.addPixmap(pixmap(name, active or normal, size), QIcon.Active, QIcon.Off)
    ic.addPixmap(pixmap(name, active or normal, size), QIcon.Normal, QIcon.On)
    ic.addPixmap(pixmap(name, active or normal, size), QIcon.Active, QIcon.On)
    return ic
