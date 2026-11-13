"""The only module that modifies files. Every write requires edit_mode=True, is backed up,
logged to the history table + changes.log, and can be undone."""
import json
import os
import re
import shutil
import time
import uuid

import mutagen

from . import analysis, db, scanner, tags


class ReadOnlyError(Exception):
    pass


RULES = {"track": re.compile(r"^\d{1,4}(/\d{1,4})?$"), "disc": re.compile(r"^\d{1,3}(/\d{1,3})?$"),
         "year": re.compile(r"^\d{4}(-\d{2}(-\d{2})?)?$")}


def _guard(edit_mode):
    if not edit_mode:
        raise ReadOnlyError("Read-only mode is on. Switch to Edit Mode to modify files.")


def validate(field, value):
    if field not in tags.FIELDS:
        raise ValueError(f"Unknown field: {field}")
    v = (value or "").strip()
    if v and field in RULES and not RULES[field].match(v):
        raise ValueError(f"Invalid {field} value: “{value}”")
    if len(v) > 500:
        raise ValueError(f"{field} is too long")
    return v


def new_batch():
    return time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]


def log(line):
    with open(db.data_dir() / "changes.log", "a", encoding="utf-8") as fh:
        fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {line}\n")


def _hist(conn, batch, tid, path, field, old, new, backup, note):
    conn.execute("INSERT INTO history(batch, ts, track_id, path, field, old, new, backup, note) "
                 "VALUES(?,?,?,?,?,?,?,?,?)", (batch, time.time(), tid, path, field, old, new, backup, note))
    log(f"[{batch}] {path} :: {field}: {old!r} -> {new!r}")


def apply_changes(conn, edits, edit_mode, note="", progress=None, cancel=None):
    """edits: {track_id: {field: value, 'artwork': (bytes, mime) | None}}.
    Returns {'batch', 'applied': [track ids], 'errors': [...]}."""
    _guard(edit_mode)
    batch = new_batch()
    bdir = db.data_dir() / "backups" / batch
    applied, errors = [], []
    for i, (tid, changes) in enumerate(edits.items()):
        if cancel and cancel():
            break
        row = conn.execute("SELECT path FROM tracks WHERE id=?", (tid,)).fetchone()
        path = row["path"] if row else f"track {tid}"
        if progress:
            progress(i, len(edits), path)
        try:
            if not row or not os.path.isfile(path):
                raise FileNotFoundError("File not available (moved, deleted or drive disconnected)")
            clean = {f: validate(f, v) for f, v in changes.items() if f != "artwork"}
            f = mutagen.File(path)
            if f is None:
                raise ValueError("Unreadable audio file")
            before = tags.read_tags(f)
            bdir.mkdir(parents=True, exist_ok=True)
            backup_json = bdir / f"{tid}.json"
            art_backup = None
            if "artwork" in changes:
                old_art = tags.get_art(f)
                art_backup = ""
                if old_art:
                    art_backup = str(bdir / f"{tid}.art")
                    with open(art_backup, "wb") as fh:
                        fh.write(old_art[0])
            backup_json.write_text(json.dumps({"path": path, "tags": before}, indent=1))
            tags.write_tags(f, clean)
            if "artwork" in changes:
                art = changes["artwork"]
                tags.set_art(f, *(art if art else (None, None)))
            tags.save(f)
            for fld, new in clean.items():
                if (before.get(fld) or "") != new:
                    _hist(conn, batch, tid, path, fld, before.get(fld) or "", new, str(backup_json), note)
            if "artwork" in changes:
                art = changes["artwork"]
                desc = f"{art[1]} ({len(art[0]):,} bytes)" if art else "(removed)"
                _hist(conn, batch, tid, path, "artwork", "embedded" if art_backup else "(none)", desc,
                      art_backup, note)
            scanner.refresh_track(conn, tid, path)
            applied.append(tid)
        except Exception as e:
            errors.append(f"{os.path.basename(path)}: {e}")
        conn.commit()
    analysis.rebuild_issues(conn)
    return dict(batch=batch, applied=applied, errors=errors)


def apply_suggestions(conn, suggestion_ids, edit_mode, progress=None, cancel=None):
    _guard(edit_mode)
    rows = conn.execute(f"SELECT * FROM suggestions WHERE status='approved' AND id IN "
                        f"({','.join('?' * len(suggestion_ids))}) ORDER BY id", list(suggestion_ids)).fetchall()
    edits = {}
    for r in rows:
        edits.setdefault(r["track_id"], {})[r["field"]] = r["proposed"]
    res = apply_changes(conn, edits, edit_mode, note="approved suggestions", progress=progress, cancel=cancel)
    ok = set(res["applied"])
    conn.executemany("UPDATE suggestions SET status='applied' WHERE id=?",
                     [(r["id"],) for r in rows if r["track_id"] in ok])
    # other pending suggestions for the same field are now stale
    conn.executemany("UPDATE suggestions SET status='superseded' WHERE track_id=? AND field=? AND status='pending'",
                     [(r["track_id"], r["field"]) for r in rows if r["track_id"] in ok])
    conn.commit()
    return res


def queue_manual(conn, track_ids, values):
    """Batch edit: queue user-entered values as approved suggestions (still need Apply)."""
    values = {k: validate(k, v) for k, v in values.items()}
    n = 0
    for tid in track_ids:
        t = conn.execute("SELECT * FROM tracks WHERE id=?", (tid,)).fetchone()
        for field, val in values.items():
            cur = t[field]
            if field in ("track", "disc"):
                tot = t[f"{field}_total"]
                cur = (f"{cur}/{tot}" if tot else str(cur)) if cur else ""
            if str(cur or "") == val:
                continue
            conn.execute("UPDATE suggestions SET status='superseded' WHERE track_id=? AND field=? "
                         "AND status IN ('pending','approved')", (tid, field))
            conn.execute("INSERT INTO suggestions(track_id, field, current, proposed, confidence, source, note, "
                         "status, created) VALUES(?,?,?,?,?,?,?,?,?)",
                         (tid, field, str(cur or ""), val, "Manual edit", "manual", "Entered by you",
                          "approved", time.time()))
            n += 1
    conn.commit()
    return n


def move_files(conn, moves, edit_mode, note="move", progress=None, cancel=None):
    """moves: [(track_id, new_path)]. Never overwrites; cross-volume moves copy then remove the source."""
    _guard(edit_mode)
    batch = new_batch()
    moved, errors = [], []
    for i, (tid, dst) in enumerate(moves):
        if cancel and cancel():
            break
        row = conn.execute("SELECT path FROM tracks WHERE id=?", (tid,)).fetchone()
        src = row["path"] if row else ""
        if progress:
            progress(i, len(moves), src)
        try:
            if not src or not os.path.isfile(src):
                raise FileNotFoundError("Source file not available")
            if src == dst:
                continue
            case_only = os.path.exists(dst) and os.path.samefile(src, dst)  # rename on case-insensitive APFS
            if os.path.exists(dst) and not case_only:
                raise FileExistsError(f"Destination already exists, not overwritten: {dst}")
            other = conn.execute("SELECT id, missing FROM tracks WHERE path=? AND id<>?", (dst, tid)).fetchone()
            if other and not other["missing"]:
                raise FileExistsError(f"Destination is another library track: {dst}")
            if other:
                conn.execute("DELETE FROM tracks WHERE id=?", (other["id"],))  # stale offline DB row only
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            if case_only:
                os.rename(src, dst)
            else:
                shutil.move(src, dst)
            conn.execute("UPDATE tracks SET path=? WHERE id=?", (dst, tid))
            _hist(conn, batch, tid, dst, "path", src, dst, "", note)
            moved.append(tid)
        except Exception as e:
            errors.append(f"{os.path.basename(src) or tid}: {e}")
        conn.commit()
    return dict(batch=batch, applied=moved, errors=errors)


def _sha1(path):
    import hashlib
    with open(path, "rb") as fh:
        return hashlib.sha1(fh.read()).hexdigest()


def batches(conn):
    return [dict(r) for r in conn.execute(
        "SELECT batch, min(ts) AS ts, count(*) AS changes, count(DISTINCT track_id) AS tracks, "
        "group_concat(DISTINCT field) AS fields, max(note) AS note, min(undone) AS undone "
        "FROM history GROUP BY batch ORDER BY ts DESC")]


def undo_batch(conn, batch, edit_mode, progress=None, cancel=None):
    _guard(edit_mode)
    rows = conn.execute("SELECT * FROM history WHERE batch=? AND undone=0 ORDER BY id DESC", (batch,)).fetchall()
    errors, done = [], []
    per_track = {}
    for r in rows:
        if r["field"] == "folder_art":  # folder.jpg / cover.jpg written by the artwork manager
            try:
                if r["backup"] and os.path.isfile(r["backup"]):
                    shutil.copy2(r["backup"], r["path"])  # put the previous image back
                elif os.path.isfile(r["path"]) and _sha1(r["path"]) == r["new"]:
                    os.remove(r["path"])  # only the file we created, and only if nobody changed it since
                else:
                    raise ValueError(f"{r['path']} was changed after it was created — left alone")
                done.append(r["id"])
            except Exception as e:
                errors.append(str(e))
        elif r["field"] == "path":
            try:
                if not os.path.isfile(r["new"]):
                    raise FileNotFoundError(f"{r['new']} no longer exists")
                if os.path.exists(r["old"]) and not os.path.samefile(r["old"], r["new"]):
                    raise FileExistsError(f"{r['old']} already exists, not overwritten")
                os.makedirs(os.path.dirname(r["old"]), exist_ok=True)
                shutil.move(r["new"], r["old"])
                conn.execute("UPDATE tracks SET path=? WHERE id=?", (r["old"], r["track_id"]))
                done.append(r["id"])
            except Exception as e:
                errors.append(str(e))
        else:
            per_track.setdefault(r["track_id"], []).append(r)
    for i, (tid, items) in enumerate(per_track.items()):
        if progress:
            progress(i, len(per_track), f"Restoring track {tid}")
        t = conn.execute("SELECT path FROM tracks WHERE id=?", (tid,)).fetchone()
        try:
            if not t or not os.path.isfile(t["path"]):
                raise FileNotFoundError("File not available")
            f = mutagen.File(t["path"])
            fields = {r["field"]: r["old"] for r in items if r["field"] != "artwork"}
            tags.write_tags(f, fields)
            for r in items:
                if r["field"] == "artwork":
                    if r["backup"] and os.path.isfile(r["backup"]):
                        data = open(r["backup"], "rb").read()
                        tags.set_art(f, data, tags.sniff_mime(data))
                    else:
                        tags.set_art(f, None, None)
            tags.save(f)
            scanner.refresh_track(conn, tid, t["path"])
            done += [r["id"] for r in items]
        except Exception as e:
            errors.append(f"track {tid}: {e}")
    conn.executemany("UPDATE history SET undone=1 WHERE id=?", [(i,) for i in done])
    conn.commit()
    log(f"[{batch}] UNDO: {len(done)} changes reverted, {len(errors)} errors")
    analysis.rebuild_issues(conn)
    return dict(reverted=len(done), errors=errors)
