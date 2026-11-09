import os
import shutil

import mutagen
import pytest

from conftest import audio_md5, make_audio, needs_ffmpeg, png_bytes
from mlm import analysis, duplicates, online, organize, repair, reports, scanner, tags

pytestmark = needs_ffmpeg


def by_name(conn):
    return {os.path.basename(r["path"]): dict(r) for r in conn.execute("SELECT * FROM tracks")}


# ---------- scanning & extraction ----------

def test_scan_extracts_metadata(conn, library):
    st = scanner.scan(conn, str(library))
    assert st["status"] == "completed"
    assert st["files"] == 6  # AppleDouble + txt skipped
    t = by_name(conn)
    assert "._One.mp3" not in t
    one = t["01 One.mp3"]
    assert (one["title"], one["artist"], one["album"], one["albumartist"]) == ("One", "Artist A", "Album X", "Artist A")
    assert (one["track"], one["track_total"], one["year"], one["genre"]) == (1, 2, "2001", "Rock")
    assert one["format"] == "MP3" and one["sample_rate"] == 44100 and abs(one["duration"] - 2) < 0.2
    assert t["02 Two.flac"]["format"] == "FLAC" and t["02 Two.flac"]["track"] == 2
    assert t["01.m4a"]["format"] == "AAC" and t["01.m4a"]["title"] == "Three"
    assert t["song.opus"]["format"] == "OPUS" and t["song.opus"]["artist"] == "Artist C"
    assert t["untagged.wav"]["format"] == "WAV" and t["untagged.wav"]["readable"] == 1
    assert t["broken.mp3"]["readable"] == 0 and t["broken.mp3"]["error"]


def test_scan_is_read_only_and_incremental(conn, library):
    before = {p: os.stat(p).st_mtime for p in scanner.collect(str(library))[0]}
    scanner.scan(conn, str(library))
    assert {p: os.stat(p).st_mtime for p in before} == before
    st = scanner.scan(conn, str(library))
    assert st["unchanged"] == 6 and st["new"] == 0 and st["updated"] == 0
    os.remove(library / "Loose/song.opus")
    st = scanner.scan(conn, str(library))
    assert st["missing"] == 1
    assert conn.execute("SELECT missing FROM tracks WHERE path LIKE '%song.opus'").fetchone()[0] == 1


def test_scan_unavailable_drive(conn, tmp_path):
    with pytest.raises(scanner.ScanError):
        scanner.scan(conn, str(tmp_path / "not-mounted"))


def test_scan_cancel(conn, library):
    st = scanner.scan(conn, str(library), cancel=lambda: True)
    assert st["status"] == "cancelled"


def test_issues_detected(conn, library):
    scanner.scan(conn, str(library))
    kinds = {}
    for r in conn.execute("SELECT t.path, i.kind FROM issues i JOIN tracks t ON t.id=i.track_id"):
        kinds.setdefault(os.path.basename(r[0]), set()).add(r[1])
    assert {"missing_title", "missing_artist", "missing_album", "missing_art"} <= kinds["untagged.wav"]
    assert kinds["broken.mp3"] == {"unreadable"}
    s = analysis.stats(conn)
    assert s["songs"] == 6 and s["corrupt"] == 1 and s["missing_art"] == 5


def test_track_rules():
    base = dict(readable=1, duration=100, title="Track 01", artist="Unknown Artist", album="A", track=5,
                track_total=3, year="2099", genre="(12)", has_art=1, error=None, path="/x.mp3")
    kinds = {k for k, _ in analysis.track_issues(base)}
    assert {"placeholder", "track_over_total", "bad_year", "suspicious_genre"} <= kinds


# ---------- duplicates ----------

def test_duplicates(conn, library):
    shutil.copy(library / "Artist A/Album X/01 One.mp3", library / "Loose/One copy.mp3")
    make_audio(library / "Other/One.flac", title="One", artist="Artist A", album="Best Of")
    make_audio(library / "Other/One live.mp3", seconds=5, title="One (Live)", artist="Artist A")
    scanner.scan(conn, str(library))
    assert duplicates.find_exact(conn) == 1
    assert duplicates.find_metadata(conn) == 1
    groups = {g["method"]: sorted(os.path.basename(m["path"]) for m in g["members"])
              for g in duplicates.load_groups(conn)}
    assert groups["exact"] == ["01 One.mp3", "One copy.mp3"]
    assert groups["metadata"] == ["01 One.mp3", "One copy.mp3", "One.flac"]  # live version excluded
    g = [g for g in duplicates.load_groups(conn) if g["method"] == "metadata"][0]
    best = [m for m in g["members"] if m["id"] == duplicates.best_copy(g["members"])][0]
    assert best["format"] == "FLAC"
    assert all(os.path.exists(m["path"]) for m in g["members"])  # nothing deleted


# ---------- safe metadata modification ----------

def test_read_only_mode_blocks_writes(conn, library):
    scanner.scan(conn, str(library))
    tid = conn.execute("SELECT id FROM tracks WHERE path LIKE '%One.mp3'").fetchone()[0]
    with pytest.raises(repair.ReadOnlyError):
        repair.apply_changes(conn, {tid: {"title": "X"}}, edit_mode=False)
    with pytest.raises(repair.ReadOnlyError):
        repair.move_files(conn, [(tid, "/tmp/x.mp3")], edit_mode=False)


@pytest.mark.parametrize("name", ["01 One.mp3", "02 Two.flac", "01.m4a", "song.opus", "untagged.wav"])
def test_edit_preserves_audio_and_art_then_undo(conn, library, name):
    path = next(p for p in scanner.collect(str(library))[0] if p.endswith("/" + name))
    art = png_bytes()
    f = mutagen.File(path)
    tags.set_art(f, art, "image/png")
    tags.save(f)
    scanner.scan(conn, str(library))
    tid = conn.execute("SELECT id FROM tracks WHERE path=?", (path,)).fetchone()[0]
    audio_before = audio_md5(path)
    orig = tags.read_tags(mutagen.File(path))

    res = repair.apply_changes(conn, {tid: {"title": "New Title", "year": "1987", "track": "7"}}, edit_mode=True)
    assert res["errors"] == [] and res["applied"] == [tid]
    f = mutagen.File(path)
    t = tags.read_tags(f)
    assert t["title"] == "New Title" and t["year"].startswith("1987") and t["track"].split("/")[0] == "7"
    assert tags.get_art(f)[0] == art, "artwork must be preserved when not explicitly changed"
    assert audio_md5(path) == audio_before, "audio data must be untouched"
    row = conn.execute("SELECT title FROM tracks WHERE id=?", (tid,)).fetchone()
    assert row["title"] == "New Title"
    assert conn.execute("SELECT count(*) FROM history WHERE batch=?", (res["batch"],)).fetchone()[0] >= 2

    repair.undo_batch(conn, res["batch"], edit_mode=True)
    restored = tags.read_tags(mutagen.File(path))
    assert restored["title"] == orig["title"] and restored["year"][:4] == orig["year"][:4]
    assert audio_md5(path) == audio_before


def test_artwork_replace_and_undo(conn, library):
    scanner.scan(conn, str(library))
    tid, path = conn.execute("SELECT id, path FROM tracks WHERE path LIKE '%Two.flac'").fetchone()
    assert tags.get_art(mutagen.File(path)) is None
    new = png_bytes((0, 0, 255), (600, 600))
    res = repair.apply_changes(conn, {tid: {"artwork": (new, "image/png")}}, edit_mode=True)
    assert res["errors"] == []
    assert tags.get_art(mutagen.File(path))[0] == new
    assert conn.execute("SELECT has_art, art_w FROM tracks WHERE id=?", (tid,)).fetchone()[:] == (1, 600)
    repair.undo_batch(conn, res["batch"], edit_mode=True)
    assert tags.get_art(mutagen.File(path)) is None


def test_invalid_values_rejected(conn, library):
    scanner.scan(conn, str(library))
    tid, path = conn.execute("SELECT id, path FROM tracks WHERE path LIKE '%One.mp3'").fetchone()
    res = repair.apply_changes(conn, {tid: {"track": "seven"}}, edit_mode=True)
    assert res["applied"] == [] and "Invalid track" in res["errors"][0]
    assert tags.read_tags(mutagen.File(path))["track"] == "1/2"


def test_suggestion_workflow(conn, library):
    scanner.scan(conn, str(library))
    tid, path = conn.execute("SELECT id, path FROM tracks WHERE path LIKE '%One.mp3'").fetchone()
    assert repair.queue_manual(conn, [tid], {"genre": "Alternative"}) == 1
    sid = conn.execute("SELECT id FROM suggestions WHERE track_id=?", (tid,)).fetchone()[0]
    res = repair.apply_suggestions(conn, [sid], edit_mode=True)
    assert res["errors"] == []
    assert tags.read_tags(mutagen.File(path))["genre"] == "Alternative"
    assert conn.execute("SELECT status FROM suggestions WHERE id=?", (sid,)).fetchone()[0] == "applied"


# ---------- organization ----------

def test_organize_plan_apply_undo(conn, library, tmp_path):
    scanner.scan(conn, str(library))
    rows = analysis.query_tracks(conn, {"artist": "Artist A"})
    dest = str(tmp_path / "Organized")
    p = organize.plan(rows, dest, organize.PRESETS[0])
    assert {os.path.relpath(x["dst"], dest) for x in p} == {"Artist A/Album X/01 - One.mp3",
                                                           "Artist A/Album X/02 - Two.flac"}
    os.makedirs(os.path.dirname(p[0]["dst"]))
    open(p[0]["dst"], "w").write("existing")  # must never be overwritten
    p = organize.plan(rows, dest, organize.PRESETS[0])
    assert sorted(x["status"] for x in p) == ["conflict: file exists", "ok"]
    ok = [(x["id"], x["dst"]) for x in p if x["status"] == "ok"]
    res = repair.move_files(conn, ok, edit_mode=True)
    assert len(res["applied"]) == 1 and os.path.exists(ok[0][1])
    assert open(p[0]["dst"]).read() == "existing"
    repair.undo_batch(conn, res["batch"], edit_mode=True)
    assert not os.path.exists(ok[0][1])
    assert os.path.exists(conn.execute("SELECT path FROM tracks WHERE id=?", (ok[0][0],)).fetchone()[0])


def test_filename_sanitising():
    t = dict(path="/a/b.mp3", artist="AC/DC", albumartist="", album='What? "Live": 1', title=".hidden",
             track=3, disc=1, year="1980", genre="")
    assert organize.render(organize.PRESETS[0], t) == "AC_DC/What_ _Live__ 1/03 - hidden.mp3"


# ---------- MusicBrainz grading (offline) ----------

def _rec(title, artist, album, length_ms, date="1991-01-01"):
    return {"id": "rec", "title": title, "length": length_ms, "artist-credit": [{"name": artist}],
            "releases": [{"id": "rel", "title": album, "date": date, "status": "Official",
                          "artist-credit": [{"name": artist}],
                          "media": [{"position": 1, "track": [{"number": "4"}]}]}]}


def test_mb_confidence_and_proposals():
    tr = dict(id=1, title="Smells Like Teen Spirit", artist="Nirvana", album="Nevermind", albumartist="",
              duration=301.0, year="2011", track=4, disc=None, genre="Grunge", path="/x.mp3")
    s = online.score(tr, tr["title"], tr["artist"], _rec("Smells Like Teen Spirit", "Nirvana", "Nevermind", 301000))
    assert online.classify(s, True) == online.VERIFIED
    props = {p[0]: p for p in online.proposals(tr, s, online.VERIFIED)}
    assert props["year"][2] == "1991" and "remaster" in props["year"][4]  # flagged, not assumed wrong
    assert props["year"][3] == online.POSSIBLE
    assert props["albumartist"][2] == "Nirvana"
    # recording found only on a compilation: don't rewrite the user's album fields
    s2 = online.score(tr, tr["title"], tr["artist"], _rec("Smells Like Teen Spirit", "Nirvana", "Grunge Hits", 301000))
    assert online.classify(s2, True) == online.HIGH
    assert "album" not in {p[0] for p in online.proposals(tr, s2, online.HIGH)}
    s3 = online.score(tr, tr["title"], tr["artist"], _rec("Something Else", "Other Band", "Z", 100000))
    assert online.classify(s3, True) == online.REVIEW


# ---------- reports ----------

def test_reports(conn, library, tmp_path):
    scanner.scan(conn, str(library))
    for name in reports.REPORTS:
        for ext in ("csv", "json"):
            n = reports.export(conn, name, str(tmp_path / f"r.{ext}"))
            assert n >= 0 and os.path.getsize(tmp_path / f"r.{ext}") > 0
    assert reports.export(conn, "Complete music inventory", str(tmp_path / "inv.csv")) == 6


def test_mb_never_fills_release_fields_from_other_release():
    tr = dict(id=1, title="Song", artist="Band", album="Real Album", albumartist="", duration=200.0, year="",
              track=None, disc=None, genre="", path="/x.mp3")
    rec = _rec("Song", "Band", "Hits Compilation", 200000)
    rec["releases"][0]["artist-credit"] = [{"name": "Various Artists"}]
    s = online.score(tr, "Song", "Band", rec)
    assert not {"albumartist", "year", "track"} & {p[0] for p in online.proposals(tr, s, online.HIGH)}
