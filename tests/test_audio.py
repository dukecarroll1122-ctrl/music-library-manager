"""Converter, player and audio verification. Only temporary files generated here are used."""
import json
import os
import subprocess
import threading

import mutagen
import pytest

from conftest import FFMPEG, audio_md5, make_audio, needs_ffmpeg, png_bytes
from mlm import convert, online, scanner, tags, verify

pytestmark = needs_ffmpeg


def tagged_source(path, seconds=4, freq=440, art=True, **meta):
    meta = dict(dict(title="One", artist="Artist A", album="Album X", album_artist="Album Artist", date="1999",
                     track="3/12", disc="1/2", genre="Rock"), **meta)
    p = make_audio(path, seconds=seconds, freq=freq, **meta)
    if art:
        f = mutagen.File(p)
        tags.set_art(f, png_bytes(), "image/png")
        tags.save(f)
    return p


def probe(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
                          "stream=codec_name,bit_rate,sample_rate,channels:format=duration,bit_rate", "-of", "json",
                          path], capture_output=True, text=True, check=True)
    j = json.loads(out.stdout)
    return dict(j["streams"][0], duration=float(j["format"]["duration"]), fbr=int(j["format"].get("bit_rate", 0)))


def run(conn, sources, s, root=None):
    return convert.run_jobs(conn, convert.plan_jobs(conn, sources, s, root=root), s)


# ================================================================ converter

def test_mp3_320_cbr_conversion(conn, tmp_path):
    src = tagged_source(tmp_path / "lib/A/Album X/03 One.flac", seconds=6)
    before = (audio_md5(src), os.path.getmtime(src))
    s = convert.Settings(fmt="MP3", bitrate=320, out_dir=str(tmp_path / "out"))
    job = run(conn, [(None, src)], s, root=str(tmp_path / "lib"))[0]
    assert job["status"] == "done", job["error"]
    p = probe(job["dst"])
    assert p["codec_name"] == "mp3" and int(p["bit_rate"]) == 320000 and abs(p["duration"] - 6) < 0.2
    assert job["dst"] == str(tmp_path / "out/A/Album X/03 One.mp3")  # folder structure kept
    assert (audio_md5(src), os.path.getmtime(src)) == before  # original untouched
    row = conn.execute("SELECT * FROM conversions").fetchone()
    assert row["status"] == "done" and row["dst"] == job["dst"] and json.loads(row["settings"])["bitrate"] == 320


@pytest.mark.parametrize("fmt", list(convert.OUTPUTS))
def test_metadata_and_artwork_preserved(conn, tmp_path, fmt):
    src = tagged_source(tmp_path / "lib/A/B/x.flac")
    art = tags.get_art(mutagen.File(src))[0]
    s = convert.Settings(fmt=fmt, out_dir=str(tmp_path / "out"))
    job = run(conn, [(None, src)], s)[0]
    assert job["status"] == "done", job["error"]
    f = mutagen.File(job["dst"])
    t = tags.read_tags(f)
    assert (t["title"], t["artist"], t["albumartist"], t["album"], t["genre"]) == \
        ("One", "Artist A", "Album Artist", "Album X", "Rock")
    assert t["year"].startswith("1999") and t["track"].split("/")[0] == "3" and t["disc"].split("/")[0] == "1"
    assert tags.get_art(f)[0] == art
    assert probe(job["dst"])["codec_name"].startswith(convert.EXPECTED_CODEC[fmt])


def test_vbr_mono_and_sample_rate(conn, tmp_path):
    src = tagged_source(tmp_path / "lib/a.flac", art=False)
    s = convert.Settings(fmt="MP3", vbr=True, vbr_quality=2, sample_rate=48000, channels=1,
                         out_dir=str(tmp_path / "out"))
    job = run(conn, [(None, src)], s)[0]
    p = probe(job["dst"])
    assert job["status"] == "done" and p["sample_rate"] == "48000" and p["channels"] == 1


def test_batch_album_conversion_keeps_structure(conn, tmp_path):
    lib = tmp_path / "Music"
    srcs = [tagged_source(lib / f"Artist/Album/{n:02d} Song.flac", freq=300 + 50 * n, title=f"Song {n}",
                          track=str(n), art=False) for n in range(1, 5)]
    scanner.scan(conn, str(lib))
    ids = [r[0] for r in conn.execute("SELECT id FROM tracks ORDER BY path")]
    s = convert.Settings(fmt="MP3", bitrate=192, out_dir=str(tmp_path / "out"))
    jobs = run(conn, [(i, p) for i, p in zip(ids, sorted(srcs))], s)
    assert [j["status"] for j in jobs] == ["done"] * 4
    assert sorted(os.listdir(tmp_path / "out/Artist/Album")) == [f"{n:02d} Song.mp3" for n in range(1, 5)]
    assert all(j["est"] and 0.5 < j["dst_size"] / j["est"] < 2 for j in jobs)  # estimate is in the right range


def test_never_overwrites(conn, tmp_path):
    src = tagged_source(tmp_path / "lib/A/x.flac", art=False)
    out = tmp_path / "out"
    s = convert.Settings(fmt="MP3", out_dir=str(out), structure="flat")
    (out).mkdir()
    (out / "x.mp3").write_bytes(b"precious")
    job = convert.plan_jobs(conn, [(None, src)], s)[0]
    assert job["status"] == "skipped" and "not overwritten" in job["error"]
    with pytest.raises(FileExistsError):
        convert.convert_file(src, str(out / "x.mp3"), s)
    assert (out / "x.mp3").read_bytes() == b"precious"
    # destination created by someone else while ffmpeg was running: publish must still refuse
    tmp = out / ".x.part.mp3"
    tmp.write_bytes(b"new")
    with pytest.raises(FileExistsError):
        convert.publish(str(tmp), str(out / "x.mp3"))
    assert (out / "x.mp3").read_bytes() == b"precious"


def test_refuses_original_folder_and_same_file(conn, tmp_path):
    src = tagged_source(tmp_path / "lib/x.flac", art=False)
    with pytest.raises(convert.ConvertError):
        convert.check_destination(src, src)
    with pytest.raises(convert.ConvertError):
        convert.check_destination(src, str(tmp_path / "lib/x.mp3"))
    s = convert.Settings(fmt="FLAC", out_dir=str(tmp_path / "lib"), structure="flat")
    assert convert.plan_jobs(conn, [(None, src)], s)[0]["status"] == "skipped"


def test_duplicate_jobs_are_not_queued_twice(conn, tmp_path):
    src = tagged_source(tmp_path / "lib/x.flac", art=False)
    s = convert.Settings(fmt="MP3", out_dir=str(tmp_path / "out"))
    assert len(convert.plan_jobs(conn, [(None, src), (None, src)], s)) == 1


def test_cancel_leaves_no_partial_files(conn, tmp_path):
    src = tagged_source(tmp_path / "lib/long.flac", seconds=120, art=False)
    out = tmp_path / "out"
    s = convert.Settings(fmt="MP3", out_dir=str(out))
    flag = threading.Event()
    jobs = convert.plan_jobs(conn, [(None, src)], s)

    def on_update(j):
        if j["status"] == "converting" and j["progress"] > 0:
            flag.set()
    done = convert.run_jobs(conn, jobs, s, on_update=on_update, cancel=flag.is_set)
    assert done[0]["status"] == "cancelled"
    assert not [f for _, _, fs in os.walk(out) for f in fs]  # no output, no temp file
    assert conn.execute("SELECT status FROM conversions").fetchone()[0] == "cancelled"


def test_interrupted_partial_files_are_cleaned_up(conn, tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    (out / f".x.abcd{convert.PART}.mp3").write_bytes(b"half")
    (out / "keep.mp3").write_bytes(b"real")
    assert convert.cleanup_partials(str(out)) == 1
    assert os.listdir(out) == ["keep.mp3"]


def test_output_integrity_check(tmp_path):
    src = tagged_source(tmp_path / "x.flac", seconds=5, art=False)
    s = convert.Settings(fmt="FLAC")
    convert.verify_output(src, s, 5.0)
    with pytest.raises(convert.ConvertError):
        convert.verify_output(src, s, 60.0)  # length mismatch = truncated output
    junk = tmp_path / "junk.flac"
    junk.write_bytes(os.urandom(2000))
    with pytest.raises(convert.ConvertError):
        convert.verify_output(str(junk), s, 5.0)


def test_quality_warnings():
    w = convert.warnings_for({"FLAC"}, convert.Settings(fmt="MP3"))
    assert any("lossless files to a lossy" in x for x in w)
    w = convert.warnings_for({"AAC"}, convert.Settings(fmt="MP3", bitrate=320), [256])
    assert any("cannot improve quality" in x for x in w) and any("does not restore" in x for x in w)
    w = convert.warnings_for({"MP3"}, convert.Settings(fmt="FLAC"))
    assert any("no quality gain" in x for x in w)
    assert convert.warnings_for({"FLAC"}, convert.Settings(fmt="ALAC")) == []


def test_ffmpeg_command_is_an_argument_list(tmp_path):
    cmd = convert.build_command('/a/b; rm -rf ~ "x".flac', "/o/out.mp3", convert.Settings())
    assert isinstance(cmd, list) and '/a/b; rm -rf ~ "x".flac' in cmd and "-n" in cmd


# ================================================================ player

def _play(path, app):
    from PySide6.QtCore import QEventLoop, QTimer, QUrl
    from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
    p, out = QMediaPlayer(), QAudioOutput()
    out.setVolume(0.0)
    p.setAudioOutput(out)
    p.setSource(QUrl.fromLocalFile(str(path)))
    p.play()
    loop = QEventLoop()
    QTimer.singleShot(1500, loop.quit)
    loop.exec()
    res = (p.mediaStatus(), p.duration(), p.position(), p.error())
    p.stop()
    return res


@pytest.mark.parametrize("ext,args", [("mp3", ()), ("flac", ()), ("wav", ()), ("m4a", ("-c:a", "aac")),
                                      ("m4a", ("-c:a", "alac")), ("opus", ("-c:a", "libopus")),
                                      ("aiff", ())])
def test_playback_supported_formats(tmp_path, ext, args):
    from PySide6.QtMultimedia import QMediaDevices, QMediaPlayer
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    if not QMediaDevices.audioOutputs():
        pytest.skip("no audio output device")
    p = make_audio(tmp_path / f"t.{ext}", seconds=3, codec_args=args)
    before = (audio_md5(p), os.path.getmtime(p))
    status, duration, position, err = _play(p, app)
    assert err == QMediaPlayer.Error.NoError
    assert 2500 < duration < 3500 and position > 200
    assert (audio_md5(p), os.path.getmtime(p)) == before  # playback never modifies the file


# ================================================================ verification

def acoustid_result(score, title, artist, rid="rec-1", duration=None, groups=()):
    return {"score": score, "id": "aid", "recordings": [
        {"id": rid, "title": title, "duration": duration, "artists": [{"name": artist}],
         "releasegroups": [{"title": g, "type": "Album", "secondarytypes": st} for g, st in groups]}]}


LOCAL = dict(title="Billie Jean", artist="Michael Jackson", album="Thriller", duration=294)


@pytest.mark.parametrize("results,expected", [
    ([acoustid_result(0.97, "Billie Jean", "Michael Jackson", duration=294, groups=[("Thriller", [])])],
     verify.VERIFIED),
    ([acoustid_result(0.95, "Billie Jean (2008 Remaster)", "Michael Jackson", duration=294,
                      groups=[("Thriller", [])])], verify.VERIFIED),
    ([acoustid_result(0.95, "Billie Jean (Live)", "Michael Jackson", duration=330, groups=[("Live", [])])],
     verify.LIKELY),
    ([acoustid_result(0.95, "Billie Jean (Instrumental)", "Michael Jackson", groups=[("Thriller", [])])],
     verify.POSSIBLE),
    ([acoustid_result(0.95, "Billie Jean", "The Bates", groups=[("Pleasure + Pain", [])])], verify.POSSIBLE),
    ([acoustid_result(0.96, "Beat It", "Michael Jackson", groups=[("Thriller", [])])], verify.STRONG),
    ([acoustid_result(0.96, "Smells Like Teen Spirit", "Nirvana", groups=[("Nevermind", [])])], verify.STRONG),
    ([acoustid_result(0.62, "Smells Like Teen Spirit", "Nirvana")], verify.POSSIBLE),  # weak fp: never 'strong'
    ([acoustid_result(0.97, "Billie Jean", "Michael Jackson", duration=294, groups=[("Number Ones", ["Compilation"])])],
     verify.VERIFIED),
    ([acoustid_result(0.97, "Billie Jean", "Michael Jackson", duration=294, groups=[("Bad", [])])], verify.LIKELY),
    ([], verify.NO_MATCH),
    ([{"score": 0.3, "recordings": []}], verify.NO_MATCH),
    ([{"score": 0.9, "recordings": [{"id": "x"}]}], verify.REVIEW),
])
def test_compare_statuses(results, expected):
    assert verify.compare(LOCAL, results)["status"] == expected


def test_compare_picks_the_matching_recording_among_many():
    results = [acoustid_result(0.95, "Wrong Song", "Someone", rid="a"),
               acoustid_result(0.94, "Billie Jean", "Michael Jackson", rid="b", duration=294,
                               groups=[("Thriller", [])])]
    r = verify.compare(LOCAL, results)
    assert r["status"] == verify.VERIFIED and r["recording"]["id"] == "b"


def test_compare_featured_artist_and_missing_title():
    local = dict(title="Empire State of Mind", artist="Jay-Z feat. Alicia Keys", album="", duration=0)
    r = verify.compare(local, [acoustid_result(0.95, "Empire State of Mind", "JAY‐Z & Alicia Keys")])
    assert r["status"] in (verify.VERIFIED, verify.LIKELY)
    r = verify.compare(dict(LOCAL, title=""), [acoustid_result(0.95, "Billie Jean", "Michael Jackson")])
    assert r["status"] == verify.REVIEW and "Billie Jean" in r["notes"][0]


def test_no_match_is_never_called_wrong():
    r = verify.compare(LOCAL, [])
    assert r["status"] == verify.NO_MATCH and "not evidence the file is wrong" in r["notes"][0]


@pytest.mark.skipif(not scanner.tool("fpcalc"), reason="Chromaprint (fpcalc) not installed")
def test_fingerprint_identification_end_to_end(conn, tmp_path, monkeypatch):
    """Real fingerprints; AcoustID is simulated: it 'knows' two recordings by their exact fingerprints."""
    lib = tmp_path / "lib"
    right = make_audio(lib / "right.mp3", seconds=20, freq=440, title="Billie Jean", artist="Michael Jackson",
                       album="Thriller")
    wrong = make_audio(lib / "wrong.mp3", seconds=20, freq=1000, title="Billie Jean", artist="Michael Jackson",
                       album="Thriller")  # labelled Billie Jean, but it's different audio
    unknown = make_audio(lib / "unknown.mp3", seconds=20, freq=700, title="Rare B-side", artist="Nobody")
    db_fp = {verify.fingerprint(right)[1]: [acoustid_result(0.97, "Billie Jean", "Michael Jackson", duration=20,
                                                            groups=[("Thriller", [])])],
             verify.fingerprint(wrong)[1]: [acoustid_result(0.95, "Beat It", "Michael Jackson", rid="beat-it",
                                                            groups=[("Thriller", [])])]}
    calls = []

    def fake_lookup(key, duration, fp, ua=None):
        calls.append(fp)
        return db_fp.get(fp, [])
    monkeypatch.setattr(verify, "acoustid_lookup", fake_lookup)
    monkeypatch.setattr(verify, "mb_recording", lambda conn, rid: {})
    scanner.scan(conn, str(lib))
    before = {p: audio_md5(p) for p in (right, wrong, unknown)}
    ids = verify.pending_ids(conn)
    res = verify.run_batch(conn, ids, key="test-key")
    st = {os.path.basename(r[0]): r[1] for r in conn.execute(
        "SELECT t.path, v.status FROM verifications v JOIN tracks t ON t.id=v.track_id")}
    assert st == {"right.mp3": verify.VERIFIED, "wrong.mp3": verify.STRONG, "unknown.mp3": verify.NO_MATCH}
    assert res["counts"][verify.STRONG] == 1
    assert {p: audio_md5(p) for p in before} == before  # nothing modified
    # fingerprints are cached: a second run re-uses them and nothing is pending
    assert all(r[0] for r in conn.execute("SELECT fingerprint FROM tracks"))
    assert verify.pending_ids(conn) == []
    n = len(calls)
    verify.run_batch(conn, ids, key="test-key")
    assert len(calls) == n + 3  # lookups repeat; fingerprints come from the cache


@pytest.mark.skipif(not scanner.tool("fpcalc"), reason="Chromaprint (fpcalc) not installed")
def test_pause_resume_and_no_key(conn, tmp_path, monkeypatch):
    lib = tmp_path / "lib"
    for i in range(3):
        make_audio(lib / f"{i}.mp3", seconds=12, freq=300 + 200 * i, title=f"S{i}", artist="A")
    scanner.scan(conn, str(lib))
    ids = verify.pending_ids(conn)
    # without an API key: fingerprints are generated and cached, nothing is judged
    res = verify.run_batch(conn, ids, key="")
    assert res["done"] == 3 and conn.execute("SELECT count(*) FROM verifications").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM tracks WHERE fingerprint IS NOT NULL").fetchone()[0] == 3
    # pause for a while, then resume
    monkeypatch.setattr(verify, "acoustid_lookup", lambda *a, **k: [])
    state = {"polls": 0}

    def paused():
        state["polls"] += 1
        return state["polls"] < 4
    res = verify.run_batch(conn, ids, key="k", paused=paused)
    assert state["polls"] >= 4 and res["done"] == 3 and res["counts"][verify.NO_MATCH] == 3


@pytest.mark.skipif(not scanner.tool("fpcalc"), reason="Chromaprint (fpcalc) not installed")
def test_changed_file_needs_reverification(conn, tmp_path, monkeypatch):
    p = make_audio(tmp_path / "lib/a.mp3", seconds=12, title="T", artist="A")
    scanner.scan(conn, str(tmp_path / "lib"))
    monkeypatch.setattr(verify, "acoustid_lookup", lambda *a, **k: [])
    verify.run_batch(conn, verify.pending_ids(conn), key="k")
    assert verify.pending_ids(conn) == []
    f = mutagen.File(p)
    tags.write_tags(f, {"title": "T2"})
    tags.save(f)
    scanner.scan(conn, str(tmp_path / "lib"))
    assert len(verify.pending_ids(conn)) == 1
    assert conn.execute("SELECT fingerprint FROM tracks").fetchone()[0] is None  # cache invalidated
