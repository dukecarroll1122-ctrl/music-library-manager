"""Metadata Inspector and Metadata Repair."""
import json
import os
import time

import mutagen
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QComboBox, QFrame, QGridLayout, QLabel, QLineEdit, QScrollArea, QSplitter,
                               QTabWidget, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from .. import analysis, online, repair, scanner, tags
from . import widgets as W
from .pages_main import Page, button, edit_tracks

FIELD_NAMES = dict(title="Title", artist="Artist", albumartist="Album artist", album="Album", genre="Genre",
                   year="Year", track="Track", disc="Disc", artwork="Artwork")
CONF_HELP = {
    online.VERIFIED: "Your tags agree with a MusicBrainz release (title, artist, album and duration). "
                     "This means consistent, not guaranteed correct.",
    online.HIGH: "Strong match on title, artist and duration or album. Differences may still be legitimate.",
    online.POSSIBLE: "Partial match. Review proposed changes carefully.",
    online.REVIEW: "MusicBrainz returned results, but none matched well. Check manually.",
    online.NONE: "MusicBrainz returned nothing for this song.",
}


def track_value(t, f):
    v = t[f]
    if f in ("track", "disc"):
        return (f"{v}/{t[f + '_total']}" if t[f + "_total"] else str(v)) if v else ""
    return str(v or "")


def grid_form(pairs, highlight=()):
    w = QWidget()
    g = QGridLayout(w)
    g.setContentsMargins(0, 0, 0, 0)
    g.setHorizontalSpacing(14)
    g.setVerticalSpacing(5)
    for i, (k, v) in enumerate(pairs):
        kl = W.label(k, "muted")
        vl = QLabel(v if v not in (None, "") else "—")
        vl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        vl.setWordWrap(True)
        if v in (None, ""):
            vl.setStyleSheet(f"color: {W.MUTED};")
        if k in highlight:
            vl.setStyleSheet(f"color: {W.GREEN}; font-weight: 600;")
        g.addWidget(kl, i, 0, Qt.AlignTop)
        g.addWidget(vl, i, 1)
    g.setColumnStretch(1, 1)
    return w


def section(title):
    fr = QFrame()
    fr.setObjectName("card")
    lay = QVBoxLayout(fr)
    lay.setContentsMargins(16, 12, 16, 14)
    lay.addWidget(W.label(title, "h2"))
    return fr, lay


def clear_layout(lay):
    while lay.count():
        it = lay.takeAt(0)
        if it.widget():
            it.widget().hide()  # hide now; deleteLater only runs on the next event-loop pass
            it.widget().deleteLater()


# ------------------------------------------------------------------ Inspector
class InspectorPage(Page):
    COLS = [("health", "", None), ("name", "Title", None), ("artist", "Artist", None), ("album", "Album", None),
            ("issues_text", "Issues", None), ("mb", "MusicBrainz", None)]

    def __init__(self, app):
        super().__init__(app)
        self.outer.addWidget(W.page_header(
            "Metadata Inspector", "Find missing, inconsistent or suspicious metadata and compare it with "
            "MusicBrainz. A difference is not automatically an error: remasters, deluxe editions, compilations "
            "and regional releases legitimately differ."))
        self.filter = QComboBox()
        self.filter.addItems(["Songs with issues", "All songs", "Checked with MusicBrainz"] +
                             [f"Issue: {v[2]}" for v in analysis.KINDS.values()])
        self.filter.currentTextChanged.connect(lambda _: self.reload())
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search…")
        self.search.setClearButtonEnabled(True)
        self.timer = QTimer(self, singleShot=True, interval=250, timeout=self.reload)
        self.search.textChanged.connect(lambda: self.timer.start())

        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.addWidget(W.hbox(self.filter, self.search))
        self.table = W.make_table(self.COLS, [28, 160, 110, 120, 190])
        self.table.selectionModel().selectionChanged.connect(lambda *_: self.show_track())
        ll.addWidget(self.table, 1)
        self.count = W.label("", "muted")
        ll.addWidget(self.count)
        ll.addWidget(W.hbox(button("Check Selected with MusicBrainz", self.mb_selected, "primary"),
                            button("Check All Shown", self.mb_all), None))
        ll.addWidget(W.hbox(button("Verify Audio (ffmpeg)", self.verify,
                                   tip="Decode the whole file to detect corruption (read-only)"),
                            button("Edit…", self.edit), button("Reveal", self.reveal), None))

        self.detail = QWidget()
        self.dl = QVBoxLayout(self.detail)
        self.dl.setContentsMargins(4, 0, 8, 0)
        self.dl.setSpacing(12)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        scroll.setWidget(self.detail)
        split = QSplitter(Qt.Horizontal)
        split.addWidget(left)
        split.addWidget(scroll)
        split.setSizes([640, 520])
        self.outer.addWidget(split, 1)

    def refresh(self):
        self.reload()

    def reload(self, keep_id=None):
        sel = self.table.selected_rows()
        keep_id = keep_id or (sel[0]["id"] if sel else None)
        f = {"search": self.search.text().strip()}
        txt = self.filter.currentText()
        if txt == "Songs with issues":
            f["problem"] = "any"
        elif txt.startswith("Issue: "):
            f["issue_kind"] = next(k for k, v in analysis.KINDS.items() if v[2] == txt[7:])
        rows = analysis.query_tracks(self.app.conn, f)
        if txt == "Checked with MusicBrainz":
            rows = [r for r in rows if r["mb"]]
        self.table.source_model.set_rows(rows)
        self.count.setText(f"{len(rows):,} songs")
        if keep_id:
            self.select_id(keep_id)
        elif rows:
            self.table.selectRow(0)
        else:
            self.show_track()

    def select_id(self, tid):
        m = self.table.source_model
        for i, r in enumerate(m.rows):
            if r["id"] == tid:
                self.table.selectRow(self.table.proxy.mapFromSource(m.index(i, 0)).row())
                return True
        return False

    def open_with(self, track_id=None, **_):
        if track_id and not self.select_id(track_id):
            self.filter.setCurrentText("All songs")
            self.select_id(track_id)

    def show_track(self):
        clear_layout(self.dl)
        sel = self.table.selected_rows()
        if not sel:
            self.dl.addWidget(W.label("Select a song to inspect its metadata.", "muted"))
            self.dl.addStretch(1)
            return
        c = self.app.conn
        t = c.execute("SELECT * FROM tracks WHERE id=?", (sel[0]["id"],)).fetchone()
        if not t:
            return
        # header with artwork
        art = QLabel()
        art.setFixedSize(132, 132)
        art.setAlignment(Qt.AlignCenter)
        art.setStyleSheet(f"background: {W.CARD}; border: 1px solid {W.BORDER}; border-radius: 8px;")
        thumb = c.execute("SELECT data FROM thumbs WHERE art_hash=?", (t["art_hash"],)).fetchone() \
            if t["art_hash"] else None
        if thumb:
            art.setPixmap(W.pixmap(thumb[0], 128))
        else:
            art.setText("No artwork")
            art.setStyleSheet(art.styleSheet() + f"color: {W.MUTED};")
        head = QWidget()
        hl = QVBoxLayout(head)
        hl.setContentsMargins(0, 0, 0, 0)
        title = W.label(t["title"] or os.path.basename(t["path"]), "h1")
        title.setWordWrap(True)
        hl.addWidget(title)
        hl.addWidget(W.label(" · ".join(x for x in (t["artist"], t["album"], (t["year"] or "")[:4]) if x), "muted",
                             wrap=True))
        hl.addStretch(1)
        self.dl.addWidget(W.hbox(art, head, spacing=14))

        fr, lay = section("Metadata")
        lay.addWidget(grid_form([(FIELD_NAMES[f], track_value(t, f)) for f in tags.FIELDS]))
        self.dl.addWidget(fr)

        fr, lay = section("Issues")
        issues = c.execute("SELECT * FROM issues WHERE track_id=? ORDER BY severity", (t["id"],)).fetchall()
        if not issues:
            lay.addWidget(W.label("✓  No problems detected", wrap=True))
        for i in sorted(issues, key=lambda i: -analysis.SEVERITY_ORDER[i["severity"]]):
            color = {"error": W.RED, "warning": "#ff8a3d", "review": W.AMBER}.get(i["severity"], W.MUTED)
            lab = QLabel(f"<span style='color:{color}'>●</span> &nbsp;<b>{analysis.KINDS[i['kind']][2]}</b>"
                         + (f" <span style='color:{W.MUTED}'>— {i['detail']}</span>" if i["detail"] else ""))
            lab.setWordWrap(True)
            lay.addWidget(lab)
        self.dl.addWidget(fr)

        fr, lay = section("MusicBrainz")
        m = c.execute("SELECT * FROM mb_matches WHERE track_id=?", (t["id"],)).fetchone()
        if not m:
            lay.addWidget(W.label("Not checked yet. Use “Check Selected with MusicBrainz”.", "muted", wrap=True))
        else:
            color = W.CONF_COLOR.get(m["confidence"], W.TEXT)
            lay.addWidget(QLabel(f"<span style='color:{color}; font-size:15px; font-weight:600'>"
                                 f"{m['confidence']}</span> &nbsp;<span style='color:{W.MUTED}'>score {m['score']:.0f}"
                                 f" · checked {time.strftime('%b %d %H:%M', time.localtime(m['checked']))}</span>"))
            lay.addWidget(W.label(CONF_HELP.get(m["confidence"], ""), "muted", wrap=True))
            cands = json.loads(m["candidates"] or "[]")
            album_mode = bool(cands) and cands[0].get("album")
            if album_mode:
                lay.addWidget(W.label(f"Matched as part of the whole album. Best release: <b>{cands[0]['release']}</b>"
                                      f" ({cands[0]['date'] or 'no date'} {cands[0]['country'] or ''}, "
                                      f"{cands[0]['title']}). Releases compared:", wrap=True))
            if cands:
                tree = QTreeWidget()
                tree.setHeaderLabels(["Release", "Artist", "Tracks", "Date", "Country", "Score"] if album_mode else
                                     ["Title", "Artist", "Release", "Date", "Length", "Score"])
                tree.setRootIsDecorated(False)
                for cd in cands:
                    cols = ([cd["release"] or "", cd["artist"] or "", cd["title"] or "", cd["date"] or "",
                             cd["country"] or "", str(cd["score"])] if album_mode else
                            [cd["title"] or "", cd["artist"] or "", cd["release"] or "", cd["date"] or "",
                             W.fmt_dur(cd["length"]), str(cd["score"])])
                    tree.addTopLevelItem(QTreeWidgetItem(cols))
                tree.setMinimumHeight(30 + 24 * min(6, len(cands)))
                tree.setColumnWidth(0, 150)
                tree.setColumnWidth(2, 140)
                lay.addWidget(tree)
            n = c.execute("SELECT count(*) FROM suggestions WHERE track_id=? AND status='pending'",
                          (t["id"],)).fetchone()[0]
            if n:
                lay.addWidget(W.hbox(W.label(f"{n} proposed change(s) waiting for your review.", wrap=True), None,
                                     button("Review in Metadata Repair",
                                            lambda: self.app.go("repair", track_id=t["id"]))))
        self.dl.addWidget(fr)

        fr, lay = section("Audio & file")
        tech = [("Format", f"{t['format']} ({t['codec']})" if t["codec"] else t["format"]),
                ("Duration", W.fmt_dur(t["duration"])), ("Bitrate", W.fmt_kbps(t["bitrate"])),
                ("Sample rate", W.fmt_hz(t["sample_rate"])),
                ("Bit depth", f"{t['bits']}-bit" if t["bits"] else ""),
                ("Channels", str(t["channels"] or "")), ("File size", W.fmt_size(t["size"])),
                ("Artwork", f"{t['art_w']}×{t['art_h']} {t['art_mime'] or ''}" if t["has_art"] else "None"),
                ("Modified", time.strftime("%Y-%m-%d %H:%M", time.localtime(t["mtime"] or 0))),
                ("Location", t["path"])]
        kinds = [r[0] for r in c.execute("SELECT kind FROM issues WHERE track_id=?", (t["id"],))]
        tech.insert(0, ("Inspection", analysis.classify(t["status"], kinds)))
        if t["status_detail"]:
            tech.insert(1, ("Audio problem", t["status_detail"]))
        if t["diag"]:
            tech.append(("Inspection notes", t["diag"]))
        if t["error"]:
            tech.append(("Tag note", t["error"]))
        lay.addWidget(grid_form(tech))
        self.dl.addWidget(fr)

        fr, lay = section("All embedded tags")
        tree = QTreeWidget()
        tree.setHeaderLabels(["Tag", "Value"])
        tree.setRootIsDecorated(False)
        try:
            f = mutagen.File(t["path"]) if os.path.exists(t["path"]) else None
            raw = tags.raw_tags(f) if f else [("—", "File not available or unreadable")]
        except Exception as e:
            raw = [("error", str(e))]
        for k, v in raw:
            tree.addTopLevelItem(QTreeWidgetItem([k, v]))
        tree.setColumnWidth(0, 170)
        tree.setMinimumHeight(min(360, 34 + 24 * max(1, len(raw))))
        lay.addWidget(tree)
        self.dl.addWidget(fr)
        self.dl.addStretch(1)

    def _ids(self, rows=None):
        rows = rows if rows is not None else self.table.selected_rows()
        if not rows:
            W.info(self, "No songs", "Select one or more songs first.")
        return [r["id"] for r in rows]

    def _mb(self, ids):
        if not ids:
            return
        keep = self.table.selected_rows()

        def done(res):
            cts = ", ".join(f"{k}: {v}" for k, v in res["counts"].items() if v)
            msg = f"Results — {cts or 'none'}."
            n = self.app.conn.execute("SELECT count(*) FROM suggestions WHERE status='pending'").fetchone()[0]
            msg += f"\n\n{n} proposed change(s) are waiting in Metadata Repair. Nothing has been changed."
            if res["errors"]:
                msg += "\n\nStopped early: " + res["errors"][0]
            W.info(self, "MusicBrainz check finished", msg)
            if keep:
                self.select_id(keep[0]["id"])
        eta = online.estimate_seconds(self.app.conn, ids)
        if eta > 60 and not W.confirm(self, "Check with MusicBrainz",
                                      f"Songs are matched album by album. MusicBrainz allows about one request per "
                                      f"second, so checking {len(ids):,} songs takes up to {W.fmt_dur(eta)} "
                                      "(less for anything already cached). Continue?", ok="Start"):
            return
        self.app.run_task("MusicBrainz lookup", online.lookup_many, ids, on_done=done)

    def mb_selected(self):
        self._mb(self._ids())

    def mb_all(self):
        self._mb(self._ids(self.table.source_model.rows))

    def verify(self):
        rows = self.table.selected_rows()
        if not rows:
            return W.info(self, "No songs", "Select songs to verify.")

        def run(conn, rows, progress=None, cancel=None):
            from collections import Counter
            results, notes = Counter(), []
            for i, r in enumerate(rows):
                if cancel and cancel():
                    break
                if progress:
                    progress(i, len(rows), os.path.basename(r["path"]))
                status, detail = scanner.verify_track(conn, r["id"])
                results[analysis.STATUS_CLASS.get(status, "Decodes cleanly" if status else "Not checked")] += 1
                if status != scanner.OK:
                    notes.append(f"{os.path.basename(r['path'])}: {detail[:160]}")
            analysis.rebuild_issues(conn)
            return results, notes

        def done(res):
            results, notes = res
            msg = "\n".join(f"{k}: {v}" for k, v in results.items())
            if notes:
                msg += "\n\n" + "\n".join(notes[:15])
            W.info(self, "Audio verification (full decode)", msg + "\n\nResults are saved with each song.")
        self.app.run_task("Verifying audio", run, rows, on_done=done)

    def edit(self):
        rows = self.table.selected_rows()
        if rows:
            edit_tracks(self, self.app, [dict(self.app.conn.execute("SELECT * FROM tracks WHERE id=?",
                                                                    (r["id"],)).fetchone()) for r in rows])

    def reveal(self):
        for r in self.table.selected_rows()[:5]:
            W.reveal(r["path"])


# ------------------------------------------------------------------ Repair
class RepairPage(Page):
    COLS = [("status", "Status", None), ("song", "Song", None), ("field_name", "Field", None),
            ("before", "Before", None), ("proposed", "After", None), ("confidence", "Confidence", None),
            ("source", "Source", None), ("note", "Note", None)]
    STATUS = {"Pending + approved": ("pending", "approved"), "Pending": ("pending",), "Approved": ("approved",),
              "Rejected": ("rejected",), "Applied": ("applied",), "All": None}

    def __init__(self, app):
        super().__init__(app)
        self.outer.addWidget(W.page_header(
            "Metadata Repair", "Review proposed changes as BEFORE → AFTER, approve or reject each one, then apply. "
            "Originals are backed up and every change is logged and can be undone."))
        tabs = QTabWidget()
        self.outer.addWidget(tabs, 1)

        # proposals tab
        prop = QWidget()
        pl = QVBoxLayout(prop)
        pl.setContentsMargins(0, 10, 0, 0)
        self.status = QComboBox()
        self.status.addItems(list(self.STATUS))
        self.conf = QComboBox()
        self.conf.addItems(["Any confidence"] + list(online.CONFIDENCES) + ["Manual edit"])
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search songs…")
        self.search.setClearButtonEnabled(True)
        for w in (self.status, self.conf):
            w.currentTextChanged.connect(lambda _: self.reload())
        self.search.textChanged.connect(lambda _: self.reload())
        self.track_filter = None
        self.track_chip = button("", self.clear_track_filter)
        self.track_chip.hide()
        pl.addWidget(W.hbox(self.status, self.conf, self.search, self.track_chip, None))
        self.table = W.make_table(self.COLS, [80, 220, 90, 170, 170, 150, 90])
        self.table.selectionModel().selectionChanged.connect(lambda *_: self.show_compare())
        split = QSplitter(Qt.Vertical)
        split.addWidget(self.table)
        self.compare = QWidget()
        self.cl = QGridLayout(self.compare)
        self.cl.setContentsMargins(0, 6, 0, 0)
        split.addWidget(self.compare)
        split.setSizes([420, 240])
        pl.addWidget(split, 1)
        self.count = W.label("", "muted")
        pl.addWidget(W.hbox(self.count, None,
                            button("Approve", lambda: self.set_status("approved")),
                            button("Reject", lambda: self.set_status("rejected")),
                            button("Reset to Pending", lambda: self.set_status("pending")),
                            button("Approve All High-Confidence", self.approve_high,
                                   tip="Approve shown Verified / High-confidence proposals for empty fields only"),
                            button("Apply Approved Changes…", self.apply, "primary")))
        tabs.addTab(prop, "Proposed changes")

        # history tab
        hist = QWidget()
        hl = QVBoxLayout(hist)
        hl.setContentsMargins(0, 10, 0, 0)
        self.batches = W.make_table([("when", "When", None), ("tracks", "Songs", None), ("changes", "Changes", None),
                                     ("fields", "Fields", None), ("note", "Description", None),
                                     ("state", "State", None), ("batch", "Batch", None)],
                                    [140, 60, 70, 200, 200, 90])
        self.batches.selectionModel().selectionChanged.connect(lambda *_: self.show_batch())
        self.changes = W.make_table([("path", "File", None), ("field", "Field", None), ("old", "Before", None),
                                     ("new", "After", None), ("undone", "Undone", None)], [320, 80, 200, 200])
        hs = QSplitter(Qt.Vertical)
        hs.addWidget(self.batches)
        hs.addWidget(self.changes)
        hl.addWidget(hs, 1)
        hl.addWidget(W.hbox(W.label("Backups and the full change log (changes.log) are in the data folder — see "
                                    "Settings.", "muted"), None,
                            button("Undo Selected Batch…", self.undo, "danger")))
        tabs.addTab(hist, "Repair history")

    def refresh(self):
        self.reload()
        rows = []
        for b in repair.batches(self.app.conn):
            b["when"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(b["ts"]))
            b["state"] = "Undone" if b["undone"] else "Applied"
            rows.append(b)
        self.batches.source_model.set_rows(rows)
        self.changes.source_model.set_rows([])

    def reload(self):
        st = self.STATUS[self.status.currentText()]
        where, args = ["1=1"], []
        if st:
            where.append(f"s.status IN ({','.join('?' * len(st))})")
            args += list(st)
        if self.conf.currentIndex() > 0:
            where.append("s.confidence=?")
            args.append(self.conf.currentText())
        if self.search.text().strip():
            where.append("(t.title LIKE ? OR t.artist LIKE ? OR t.album LIKE ?)")
            args += [f"%{self.search.text().strip()}%"] * 3
        if self.track_filter:
            where.append("s.track_id=?")
            args.append(self.track_filter)
        rows = []
        for r in self.app.conn.execute(
                f"SELECT s.*, t.title, t.artist, t.path FROM suggestions s JOIN tracks t ON t.id=s.track_id "
                f"WHERE {' AND '.join(where)} AND s.status<>'superseded' ORDER BY t.artist, t.album, t.track, s.id",
                args):
            d = dict(r)
            d["song"] = f"{d['artist'] or '?'} – {d['title'] or os.path.basename(d['path'])}"
            d["field_name"] = FIELD_NAMES.get(d["field"], d["field"])
            d["before"] = d["current"] or "(empty)"
            d["_color"] = {"proposed": W.GREEN, "before": W.MUTED if not d["current"] else W.TEXT}
            rows.append(d)
        self.table.source_model.set_rows(rows)
        pend = sum(1 for r in rows if r["status"] == "pending")
        appr = sum(1 for r in rows if r["status"] == "approved")
        self.count.setText(f"{len(rows):,} shown · {pend:,} pending · {appr:,} approved")
        self.show_compare()

    def open_with(self, track_id=None, **_):
        if track_id:
            self.track_filter = track_id
            t = self.app.conn.execute("SELECT title FROM tracks WHERE id=?", (track_id,)).fetchone()
            self.track_chip.setText(f"Only: {(t['title'] or 'song')[:30]}  ✕")
            self.track_chip.show()
            self.status.setCurrentText("Pending + approved")
            self.reload()
            if self.table.source_model.rows:
                self.table.selectRow(0)

    def clear_track_filter(self):
        self.track_filter = None
        self.track_chip.hide()
        self.reload()

    def show_compare(self):
        clear_layout(self.cl)
        sel = self.table.selected_rows()
        if not sel:
            self.cl.addWidget(W.label("Select a proposed change to compare the song's full metadata BEFORE and "
                                      "AFTER.", "muted"), 0, 0)
            return
        tid = sel[0]["track_id"]
        t = self.app.conn.execute("SELECT * FROM tracks WHERE id=?", (tid,)).fetchone()
        before = {f: track_value(t, f) for f in tags.FIELDS}
        after = dict(before)
        changed = set()
        selected_ids = {s["id"] for s in sel}
        for s in self.app.conn.execute("SELECT * FROM suggestions WHERE track_id=? AND status IN "
                                       "('approved','pending') ORDER BY id", (tid,)):
            if s["status"] == "approved" or s["id"] in selected_ids:
                after[s["field"]] = s["proposed"]
                changed.add(FIELD_NAMES[s["field"]])
        b, la = section("BEFORE  (current file)")
        la.addWidget(grid_form([(FIELD_NAMES[f], before[f]) for f in tags.FIELDS]))
        a, lb = section("AFTER  (approved + selected changes)")
        lb.addWidget(grid_form([(FIELD_NAMES[f], after[f]) for f in tags.FIELDS], highlight=changed))
        self.cl.addWidget(b, 0, 0)
        self.cl.addWidget(a, 0, 1)
        note = sel[0]["note"] or ""
        if note:
            self.cl.addWidget(W.label(f"ⓘ  {note}", "muted", wrap=True), 1, 0, 1, 2)

    def set_status(self, status):
        sel = self.table.selected_rows()
        if not sel:
            return W.info(self, "No selection", "Select one or more proposed changes.")
        sel = [s for s in sel if s["status"] in ("pending", "approved", "rejected")]
        self.app.conn.executemany("UPDATE suggestions SET status=? WHERE id=?", [(status, s["id"]) for s in sel])
        self.app.conn.commit()
        self.reload()

    def approve_high(self):
        rows = [r for r in self.table.source_model.rows if r["status"] == "pending" and not r["current"]
                and r["confidence"] in (online.VERIFIED, online.HIGH)]
        if not rows:
            return W.info(self, "Nothing to approve", "No shown pending high-confidence proposals fill an empty "
                                                      "field. Changes to existing values must be approved "
                                                      "individually.")
        self.app.conn.executemany("UPDATE suggestions SET status='approved' WHERE id=?", [(r["id"],) for r in rows])
        self.app.conn.commit()
        self.reload()
        W.info(self, "Approved", f"Approved {len(rows)} proposal(s) that fill empty fields. Review them, then "
                                 "click Apply.")

    def apply(self):
        ids = [r[0] for r in self.app.conn.execute("SELECT id FROM suggestions WHERE status='approved'")]
        if not ids:
            return W.info(self, "Nothing approved", "Approve one or more proposed changes first.")
        n_tracks = self.app.conn.execute(
            f"SELECT count(DISTINCT track_id) FROM suggestions WHERE status='approved'").fetchone()[0]
        if not self.app.require_edit():
            return
        if not W.confirm(self, "Apply approved changes?",
                         f"{len(ids)} approved change(s) will be written to {n_tracks} file(s).\n\n"
                         "Original metadata is backed up first, audio data and existing artwork are preserved, "
                         "and the batch can be undone from Repair history."):
            return
        self.app.run_task("Applying metadata changes", repair.apply_suggestions, ids, self.app.edit_mode,
                          on_done=lambda res: W.info(self, "Changes applied", W.summarize(res)))

    def show_batch(self):
        sel = self.batches.selected_rows()
        rows = [dict(r) for r in self.app.conn.execute("SELECT * FROM history WHERE batch=? ORDER BY id",
                                                       (sel[0]["batch"],))] if sel else []
        for r in rows:
            r["undone"] = "yes" if r["undone"] else ""
        self.changes.source_model.set_rows(rows)

    def undo(self):
        sel = self.batches.selected_rows()
        if not sel:
            return W.info(self, "No batch", "Select a batch in Repair history.")
        b = sel[0]
        if b["undone"]:
            return W.info(self, "Already undone", "This batch has already been undone.")
        if not self.app.require_edit():
            return
        if not W.confirm(self, "Undo this batch?", f"Restore the original values for {b['changes']} change(s) in "
                                                   f"{b['tracks']} file(s) from batch {b['batch']}?", ok="Undo"):
            return
        self.app.run_task("Undoing changes", repair.undo_batch, b["batch"], self.app.edit_mode,
                          on_done=lambda res: W.info(self, "Undo finished", W.summarize(res, "Reverted")))
