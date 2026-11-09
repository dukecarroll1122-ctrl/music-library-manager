import io
import os
import shutil
import subprocess

import pytest

from mlm import db

FFMPEG = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"
needs_ffmpeg = pytest.mark.skipif(not os.path.exists(FFMPEG), reason="ffmpeg required to generate audio fixtures")


def make_audio(path, seconds=2, freq=440, codec_args=(), **meta):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    cmd = [FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", f"sine=frequency={freq}:duration={seconds}"]
    for k, v in meta.items():
        cmd += ["-metadata", f"{k}={v}"]
    subprocess.run(cmd + list(codec_args) + [str(path)], check=True)
    return str(path)


def audio_md5(path):
    """MD5 of decoded audio samples — proves tag edits never touch audio data."""
    out = subprocess.run([FFMPEG, "-v", "error", "-i", str(path), "-map", "0:a", "-f", "md5", "-"],
                         capture_output=True, text=True, check=True)
    return out.stdout.strip()


def png_bytes(color=(200, 30, 30), size=(300, 300)):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("MLM_DATA_DIR", str(tmp_path / "data"))
    c = db.connect(str(tmp_path / "test.db"))
    yield c
    c.close()


@pytest.fixture
def library(tmp_path):
    lib = tmp_path / "Music"
    make_audio(lib / "Artist A/Album X/01 One.mp3", title="One", artist="Artist A", album="Album X",
               album_artist="Artist A", date="2001", track="1/2", genre="Rock")
    make_audio(lib / "Artist A/Album X/02 Two.flac", freq=550, title="Two", artist="Artist A", album="Album X",
               album_artist="Artist A", date="2001", track="2/2", genre="Rock")
    make_audio(lib / "Artist B/Album Y/01.m4a", freq=660, codec_args=("-c:a", "aac", "-b:a", "128k"),
               title="Three", artist="Artist B", album="Album Y", date="1999", track="1")
    make_audio(lib / "Loose/untagged.wav", freq=330)
    make_audio(lib / "Loose/song.opus", freq=770, codec_args=("-c:a", "libopus"), title="Opus Song",
               artist="Artist C")
    (lib / "Loose/broken.mp3").write_bytes(os.urandom(4096))
    (lib / "Loose/._One.mp3").write_bytes(b"\x00\x05\x16\x07AppleDouble")  # macOS SD-card junk, must be skipped
    (lib / "Loose/notes.txt").write_text("not audio")
    return lib
