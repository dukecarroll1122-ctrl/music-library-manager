"""Regression tests for audio inspection classes (the 'fragmented .m4a = corrupted' false positive)."""
import os
import shutil
import sqlite3
import stat

import mutagen
import pytest

from conftest import audio_md5, make_audio, needs_ffmpeg
from mlm import analysis, db, reports, repair, scanner, tags

pytestmark = needs_ffmpeg


def frag_m4a(path, **meta):
    """Fragmented MP4 (ftyp moov moof mdat …) like many downloaded/streamed .m4a files."""
    return make_audio(path, seconds=3, codec_args=("-c:a", "aac", "-b:a", "128k", "-movflags",
                                                   "+frag_keyframe+empty_moov+default_base_moof", "-frag_duration", "500000"),
                      **meta)


def classes(conn):
    return {os.path.basename(r["path"]): r["class"] for r in analysis.query_tracks(conn)}


def test_fragmented_mp4_is_healthy_not_corrupted(conn, tmp_path):
    p = frag_m4a(tmp_path / "lib/Frag.m4a", title="Frag", artist="A", album="B", track="1", date="2000",
                 genre="Jazz")
    assert scanner.is_fragmented_mp4(p)
    assert mutagen.File(p).info.length == 0  # the tag reader's limitation that caused the false positive
    scanner.scan(conn, str(tmp_path / "lib"))
    r = conn.execute("SELECT * FROM tracks").fetchone()
    assert r["status"] == "ok" and abs(r["duration"] - 3) < 0.3 and r["readable"] == 1
    assert "Fragmented MP4" in r["diag"]
    assert classes(conn)["Frag.m4a"] in ("Healthy", "Metadata issues")
    assert analysis.stats(conn)["corrupt"] == 0


def test_fragmented_mp4_edit_keeps_audio(conn, tmp_path):
    p = frag_m4a(tmp_path / "lib/Frag.m4a", title="Old")
    scanner.scan(conn, str(tmp_path / "lib"))
    before = audio_md5(p)
    tid = conn.execute("SELECT id FROM tracks").fetchone()[0]
    res = repair.apply_changes(conn, {tid: {"title": "New"}}, edit_mode=True)
    assert res["errors"] == [] and tags.read_tags(mutagen.File(p))["title"] == "New"
    assert audio_md5(p) == before
    assert conn.execute("SELECT status FROM tracks").fetchone()[0] == "ok"


def test_fragmented_with_partial_moov_gets_full_duration(conn, tmp_path):
    p = make_audio(tmp_path / "lib/f.m4a", seconds=4, codec_args=(
        "-c:a", "aac", "-movflags", "+frag_keyframe+default_base_moof", "-frag_duration", "500000"), title="F")
    assert 0 < mutagen.File(p).info.length < 1  # tag reader only counts the samples inside moov
    scanner.scan(conn, str(tmp_path / "lib"))
    assert abs(conn.execute("SELECT duration FROM tracks").fetchone()[0] - 4) < 0.3


def test_garbage_is_potentially_corrupted_until_decode_confirms(conn, tmp_path):
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib/junk.flac").write_bytes(os.urandom(6000))
    scanner.scan(conn, str(tmp_path / "lib"))
    assert classes(conn)["junk.flac"] == "Potentially corrupted"  # never "confirmed" from a scan alone
    tid = conn.execute("SELECT id FROM tracks").fetchone()[0]
    status, detail = scanner.verify_track(conn, tid)
    analysis.rebuild_issues(conn)
    assert status == "decode_failure" and classes(conn)["junk.flac"] == "Confirmed decoding failure"


def test_partial_decode_warning(conn, tmp_path):
    p = make_audio(tmp_path / "lib/dent.mp3", seconds=8, title="Dent", artist="A", album="B")
    data = bytearray(open(p, "rb").read())
    mid = len(data) // 2
    data[mid:mid + 1500] = os.urandom(1500)  # damage a few frames in the middle
    open(p, "wb").write(data)
    scanner.scan(conn, str(tmp_path / "lib"))
    assert conn.execute("SELECT status FROM tracks").fetchone()[0] == "ok"  # headers fine: scan can't tell
    status, detail = scanner.verify_track(conn, conn.execute("SELECT id FROM tracks").fetchone()[0])
    assert status == "partial" and "damaged packet" in detail


def test_no_audio_stream_is_unsupported(conn, tmp_path):
    p = tmp_path / "lib/video_only.m4a"
    p.parent.mkdir()
    import subprocess
    from conftest import FFMPEG
    subprocess.run([FFMPEG, "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=32x32:d=1", "-c:v", "mpeg4",
                    "-f", "mp4", str(p)], check=True)
    scanner.scan(conn, str(tmp_path / "lib"))
    r = conn.execute("SELECT * FROM tracks").fetchone()
    assert r["status"] == "unsupported" and "no audio stream" in r["status_detail"]
    assert classes(conn)["video_only.m4a"] == "Unsupported format or codec"


def test_tag_parse_failure_is_metadata_issue_not_corruption(conn, tmp_path, monkeypatch):
    make_audio(tmp_path / "lib/ok.mp3", title="T", artist="A", album="B")

    def broken(path, *a, **k):
        raise mutagen.MutagenError("can't sync to MPEG frame")
    monkeypatch.setattr(scanner.mutagen, "File", broken)
    scanner.scan(conn, str(tmp_path / "lib"))
    r = conn.execute("SELECT * FROM tracks").fetchone()
    assert r["status"] == "ok" and r["title"] == "T" and r["duration"] > 0  # read by ffprobe instead
    assert classes(conn)["ok.mp3"] == "Metadata issues"


def test_zero_length_without_ffprobe_is_only_potential(conn, tmp_path, monkeypatch):
    frag_m4a(tmp_path / "lib/f.m4a", title="T")
    monkeypatch.setattr(scanner, "tool", lambda name: None)
    scanner.scan(conn, str(tmp_path / "lib"))
    assert classes(conn)["f.m4a"] == "Potentially corrupted"


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
def test_unreadable_file_is_unavailable(conn, tmp_path):
    p = make_audio(tmp_path / "lib/locked.mp3", title="L")
    os.chmod(p, 0)
    try:
        scanner.scan(conn, str(tmp_path / "lib"))
    finally:
        os.chmod(p, stat.S_IRUSR | stat.S_IWUSR)
    assert classes(conn)["locked.mp3"] == "File unavailable"


def test_old_database_is_migrated_and_reclassified(tmp_path, monkeypatch):
    monkeypatch.setenv("MLM_DATA_DIR", str(tmp_path / "data"))
    p = frag_m4a(tmp_path / "lib/Frag.m4a", title="Frag", artist="A")
    good = make_audio(tmp_path / "lib/good.mp3", title="Good", artist="A")
    path = str(tmp_path / "old.db")
    old = sqlite3.connect(path)  # schema and rows exactly as the previous version stored them
    old.executescript(db.SCHEMA.replace(", status TEXT, status_detail TEXT, diag TEXT", ""))
    old.execute("INSERT INTO tracks(id, path, root, title, artist, duration, readable, error, size, mtime, format) "
                "VALUES(1, ?, ?, 'Frag', 'A', 0, 1, 'Zero duration — file may be truncated or corrupted', 1, 1, 'AAC')",
                (p, str(tmp_path / "lib")))
    old.execute("INSERT INTO tracks(id, path, root, title, artist, duration, readable, size, mtime, format, "
                "sample_rate, error) VALUES(2, ?, ?, 'Good', 'A', 2, 1, 1, 1, 'MP3', 44100, "
                "'Embedded artwork is not a readable image')", (good, str(tmp_path / "lib")))
    old.commit()
    old.close()
    conn = db.connect(path)  # additive migration
    assert conn.execute("SELECT count(*) FROM tracks").fetchone()[0] == 2
    res = scanner.reclassify(conn)
    assert res == {"checked": 2, "reinspected": 1}
    rows = {r["id"]: r for r in conn.execute("SELECT * FROM tracks")}
    assert rows[1]["status"] == "ok" and rows[1]["duration"] > 2 and rows[1]["error"] is None
    assert rows[1]["title"] == "Frag" and rows[1]["size"] == 1  # non-technical columns untouched
    assert rows[2]["status"] == "ok"
    assert rows[2]["error"] == "Embedded artwork is not a readable image"  # unrelated notes are preserved


def test_diagnostics_report(conn, tmp_path):
    frag_m4a(tmp_path / "lib/Frag.m4a", title="F")
    (tmp_path / "lib/junk.mp3").write_bytes(os.urandom(3000))
    scanner.scan(conn, str(tmp_path / "lib"))
    rows = reports.REPORTS["Audio diagnostics report"](conn)
    by = {os.path.basename(r["path"]): r for r in rows}
    assert by["junk.mp3"]["inspection_result"] == "Potentially corrupted" and by["junk.mp3"]["error_message"]
    assert "Fragmented MP4" in by["Frag.m4a"]["inspection_notes"]
    assert set(rows[0]) >= {"path", "format", "error_message", "inspection_result"}


def test_large_artwork_on_fragmented_mp4_keeps_audio(conn, tmp_path):
    import io
    from PIL import Image
    p = frag_m4a(tmp_path / "lib/Frag.m4a", title="F")
    scanner.scan(conn, str(tmp_path / "lib"))
    before = audio_md5(p)
    buf = io.BytesIO()
    Image.effect_noise((1500, 1500), 80).convert("RGB").save(buf, "JPEG", quality=95)  # ~2 MB, grows moov a lot
    tid = conn.execute("SELECT id FROM tracks").fetchone()[0]
    assert repair.apply_changes(conn, {tid: {"artwork": (buf.getvalue(), "image/jpeg")}}, edit_mode=True)["errors"] == []
    assert audio_md5(p) == before and scanner.verify_audio(p)[0] == "ok"
    assert tags.get_art(mutagen.File(p))[0] == buf.getvalue()
