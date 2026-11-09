"""Audio conversion with FFmpeg. Always writes new files to a separate destination; originals are only read.

Safety model:
* ffmpeg writes to a hidden temp file next to the destination; the finished file is published with os.link,
  which fails if the destination exists, so nothing is ever overwritten (even in a race).
* Outputs inside the source file's own folder are refused, so a converted copy can't land beside its original.
* The output is verified (ffprobe: audio stream, codec, duration) before it is published.
* Cancelling or a crash leaves at most a hidden '.mlmpart' temp file, which is removed on the next run.
"""
import hashlib
import json
import os
import subprocess
import time
import uuid
from dataclasses import asdict, dataclass

import mutagen

from . import db, scanner, tags

LOSSLESS = {"FLAC", "ALAC", "WAV", "AIFF", "APE"}
PART = ".mlmpart"

# fmt -> (extension, codec label, lossless)
OUTPUTS = {
    "MP3": (".mp3", "MP3 (LAME)", False),
    "AAC": (".m4a", "AAC", False),
    "FLAC": (".flac", "FLAC", True),
    "WAV": (".wav", "PCM WAV", True),
    "ALAC": (".m4a", "Apple Lossless", True),
    "OGG": (".ogg", "Ogg Vorbis", False),
    "OPUS": (".opus", "Opus", False),
}
MP3_PRESETS = [320, 256, 192, 128]
VBR_LABELS = {"MP3": "LAME V0 (best) … V9", "AAC": "quality 0 (best) … 14", "OGG": "quality 10 (best) … 0",
              "OPUS": "always VBR"}


class ConvertError(Exception):
    pass


@dataclass
class Settings:
    fmt: str = "MP3"
    bitrate: int = 320          # kbps, lossy CBR/ABR formats
    vbr: bool = False
    vbr_quality: int = 0        # MP3: V0-V9, AAC: 0-14 (AudioToolbox), OGG: 10-0
    sample_rate: int = 0        # 0 = keep
    channels: int = 0           # 0 = keep, 1 = mono, 2 = stereo
    bit_depth: int = 0          # WAV only: 0 = keep (16 or 24)
    preserve_tags: bool = True
    preserve_art: bool = True
    out_dir: str = ""
    structure: str = "mirror"   # "mirror" = keep folders relative to the library root, "flat"

    def key(self):
        d = asdict(self)
        d.pop("out_dir")
        return hashlib.sha1(json.dumps(d, sort_keys=True).encode()).hexdigest()[:12]

    def describe(self):
        if self.fmt in ("FLAC", "WAV", "ALAC"):
            q = "lossless"
        elif self.fmt == "OPUS":
            q = f"{self.bitrate} kbps VBR"
        elif self.vbr:
            q = {"MP3": f"VBR V{self.vbr_quality}", "AAC": f"VBR q{self.vbr_quality}",
                 "OGG": f"VBR q{self.vbr_quality}"}[self.fmt]
        else:
            q = f"{self.bitrate} kbps {'CBR' if self.fmt == 'MP3' else 'ABR'}"
        extra = [f"{self.sample_rate / 1000:g} kHz" if self.sample_rate else "",
                 {1: "mono", 2: "stereo"}.get(self.channels, "")]
        return " · ".join(x for x in [self.fmt, q, *extra] if x)


def ffmpeg():
    exe = scanner.tool("ffmpeg")
    if not exe:
        raise ConvertError("FFmpeg is not installed. Install it with:  brew install ffmpeg")
    return exe


def _has_encoder(name):
    try:
        out = subprocess.run([ffmpeg(), "-hide_banner", "-encoders"], capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError, ConvertError):
        return False
    return any(line.split()[1:2] == [name] for line in out.splitlines() if len(line.split()) > 1)


_ENC = {}


def encoder(name):
    if name not in _ENC:
        _ENC[name] = _has_encoder(name)
    return _ENC[name]


def codec_args(s: Settings, src_bits=None):
    f = s.fmt
    if f == "MP3":
        a = ["-c:a", "libmp3lame"]
        a += ["-q:a", str(max(0, min(9, s.vbr_quality)))] if s.vbr else ["-b:a", f"{s.bitrate}k"]
        return a + ["-id3v2_version", "3"]
    if f == "AAC":
        if encoder("aac_at"):  # Apple's encoder is noticeably better than ffmpeg's built-in one
            a = ["-c:a", "aac_at"]
            return a + (["-aac_at_mode", "vbr", "-q:a", str(max(0, min(14, s.vbr_quality)))] if s.vbr
                        else ["-b:a", f"{s.bitrate}k"])
        return ["-c:a", "aac", "-b:a", f"{s.bitrate}k"]
    if f == "FLAC":
        return ["-c:a", "flac", "-compression_level", "8"]
    if f == "ALAC":
        return ["-c:a", "alac"]
    if f == "WAV":
        bits = s.bit_depth or (24 if (src_bits or 16) > 16 else 16)
        return ["-c:a", "pcm_s24le" if bits == 24 else "pcm_s16le"]
    if f == "OGG":
        a = ["-c:a", "vorbis", "-strict", "experimental"]
        return a + (["-q:a", str(max(0, min(10, s.vbr_quality)))] if s.vbr else ["-b:a", f"{s.bitrate}k"])
    if f == "OPUS":
        return ["-c:a", "libopus", "-b:a", f"{min(s.bitrate, 256)}k", "-vbr", "on"]  # libopus max for mono
    raise ConvertError(f"Unknown output format {f}")


def build_command(src, tmp, s: Settings, src_bits=None):
    """Argument list for subprocess (never a shell string). '-n' makes ffmpeg refuse to overwrite."""
    cmd = [ffmpeg(), "-hide_banner", "-nostdin", "-v", "error", "-n", "-i", src, "-map", "0:a:0", "-vn",
           "-map_metadata", "0" if s.preserve_tags else "-1"]
    sr = s.sample_rate
    if s.fmt == "OPUS" and sr not in (0, 8000, 12000, 16000, 24000, 48000):
        sr = 48000  # Opus only supports these rates
    if sr:
        cmd += ["-ar", str(sr)]
    if s.fmt == "OGG":
        cmd += ["-ac", "2"]  # FFmpeg's built-in Vorbis encoder only supports stereo
    elif s.channels:
        cmd += ["-ac", str(s.channels)]
    cmd += codec_args(s, src_bits)
    return cmd + ["-progress", "pipe:1", "-nostats", tmp]


def warnings_for(src_formats, s: Settings, src_kbps=()):
    """Honest quality warnings for a planned conversion."""
    out = []
    lossless_out = OUTPUTS[s.fmt][2]
    lossy_src = {f for f in src_formats if f not in LOSSLESS}
    if not lossless_out and set(src_formats) & LOSSLESS:
        out.append("Converting lossless files to a lossy format discards audio information permanently. "
                   "Keep the originals.")
    if not lossless_out and lossy_src:
        out.append("Re-encoding files that are already lossy (" + ", ".join(sorted(lossy_src)) + ") to another "
                   "lossy format always loses some quality. It cannot improve quality.")
    if lossless_out and lossy_src:
        out.append("Converting lossy files to a lossless format makes much larger files with no quality gain; "
                   "the result sounds exactly like the lossy source.")
    if not lossless_out and not s.vbr and src_kbps and s.fmt != "OPUS":
        higher = [k for k in src_kbps if k and k < s.bitrate and k < 900]
        if higher:
            out.append(f"{len(higher)} source file(s) have a lower bitrate than {s.bitrate} kbps. A higher output "
                       "bitrate only makes the file bigger; it does not restore lost quality.")
    if s.fmt == "OGG":
        out.append("This FFmpeg build only has the experimental Vorbis encoder (stereo only). Opus gives better "
                   "quality at the same size.")
    if s.fmt == "OPUS" and s.bitrate > 256:
        out.append("Opus is capped at 256 kbps (already transparent for most music).")
    if s.sample_rate and s.fmt == "OPUS" and s.sample_rate not in (8000, 12000, 16000, 24000, 48000):
        out.append("Opus always plays back at 48 kHz; the sample rate will be 48 kHz.")
    return out


def estimate_size(duration, s: Settings, src_size=0, sr=44100, ch=2, bits=16):
    """Rough output size in bytes, or None."""
    if not duration:
        return None
    sr = s.sample_rate or sr or 44100
    ch = s.channels or ch or 2
    if s.fmt == "WAV":
        b = s.bit_depth or (24 if (bits or 16) > 16 else 16)
        return int(duration * sr * ch * b / 8)
    if s.fmt in ("FLAC", "ALAC"):
        return int(duration * sr * ch * (bits or 16) / 8 * 0.6)
    if s.vbr:
        kbps = {"MP3": [245, 225, 190, 175, 165, 130, 115, 100, 85, 65], "OGG": [64, 80, 96, 112, 128, 160, 192,
                                                                               224, 256, 320, 500]}.get(s.fmt)
        if s.fmt == "MP3":
            k = kbps[max(0, min(9, s.vbr_quality))]
        elif s.fmt == "OGG":
            k = kbps[max(0, min(10, s.vbr_quality))]
        else:
            k = 256 - 12 * max(0, min(14, s.vbr_quality))
    else:
        k = s.bitrate
    return int(duration * k * 1000 / 8)


def output_path(src, s: Settings, root=None):
    stem = os.path.splitext(os.path.basename(src))[0]
    ext = OUTPUTS[s.fmt][0]
    sub = ""
    if s.structure == "mirror" and root:
        rel = os.path.relpath(os.path.dirname(src), root)
        sub = "" if rel.startswith("..") or rel == "." else rel
    return os.path.normpath(os.path.join(s.out_dir, sub, stem + ext))


def check_destination(src, dst):
    if not dst or not os.path.isabs(dst):
        raise ConvertError("Choose an output folder")
    if os.path.normcase(os.path.abspath(dst)) == os.path.normcase(os.path.abspath(src)):
        raise ConvertError("Output would replace the original file")
    if os.path.normcase(os.path.dirname(os.path.abspath(dst))) == os.path.normcase(
            os.path.dirname(os.path.abspath(src))):
        raise ConvertError("Output folder is the original's own folder — choose a separate destination")
    if os.path.exists(dst):
        raise FileExistsError(f"Already exists, not overwritten: {dst}")


def cleanup_partials(folder):
    """Remove leftover temp files from interrupted conversions (only our own '.mlmpart' files)."""
    n = 0
    for dirpath, _, files in os.walk(folder):
        for f in files:
            if f.startswith(".") and PART in f:
                try:
                    os.remove(os.path.join(dirpath, f))
                    n += 1
                except OSError:
                    pass
    return n


def _probe_audio(path):
    p = scanner.ffprobe(path)
    if not p or not p["ok"]:
        return None
    audio = [s for s in p["json"].get("streams", []) if s.get("codec_type") == "audio"]
    if not audio:
        return None
    s = audio[0]
    return dict(codec=s.get("codec_name"), duration=float(s.get("duration") or p["json"]["format"].get("duration")
                                                          or 0),
                sample_rate=int(s.get("sample_rate") or 0), channels=int(s.get("channels") or 0),
                bits=int(s.get("bits_per_raw_sample") or s.get("bits_per_sample") or 0))


EXPECTED_CODEC = {"MP3": "mp3", "AAC": "aac", "FLAC": "flac", "WAV": "pcm_", "ALAC": "alac", "OGG": "vorbis",
                  "OPUS": "opus"}


def verify_output(path, s: Settings, src_duration):
    info = _probe_audio(path)
    if not info:
        raise ConvertError("Output verification failed: no readable audio stream")
    if not (info["codec"] or "").startswith(EXPECTED_CODEC[s.fmt]):
        raise ConvertError(f"Output verification failed: codec is {info['codec']}, expected {s.fmt}")
    if src_duration and abs(info["duration"] - src_duration) > max(1.0, src_duration * 0.01):
        raise ConvertError(f"Output verification failed: length {info['duration']:.1f}s, source "
                           f"{src_duration:.1f}s")
    if s.channels and s.fmt != "OGG" and info["channels"] != s.channels:
        raise ConvertError(f"Output verification failed: {info['channels']} channel(s), expected {s.channels}")
    return info


def copy_tags_and_art(src, dst, s: Settings):
    """Re-apply core tags (incl. track/disc numbers, album artist, year) and cover art with mutagen,
    because ffmpeg can't carry artwork into every container."""
    if not (s.preserve_tags or s.preserve_art):
        return []
    notes = []
    try:
        fs = mutagen.File(src)
    except Exception as e:
        return [f"Source tags unreadable: {e}"]
    fd = mutagen.File(dst)
    if fs is None or fd is None:
        return ["Tags not copied (unreadable)"]
    if tags.tag_kind(fd) is None:
        return [f"{s.fmt} output cannot hold tags"]
    if s.preserve_tags:
        t = {k: v for k, v in tags.read_tags(fs).items() if v}
        if t:
            tags.write_tags(fd, t)
    if s.preserve_art:
        art = tags.get_art(fs)
        if art and tags.tag_kind(fd) != "asf":
            tags.set_art(fd, art[0], art[1])
    tags.save(fd)
    return notes


def publish(tmp, dst):
    """Move the finished temp file to dst without ever overwriting an existing file."""
    try:
        os.link(tmp, dst)  # atomic; raises FileExistsError if dst exists
    except FileExistsError:
        raise
    except OSError:  # filesystems without hard links (exFAT/FAT SD cards, some network shares)
        fd = os.open(dst, os.O_CREAT | os.O_EXCL | os.O_WRONLY)  # reserve the name; fails if it exists
        os.close(fd)
        os.replace(tmp, dst)  # replaces only the empty placeholder we just created


def convert_file(src, dst, s: Settings, progress=None, cancel=None):
    """Convert one file. progress(fraction 0..1). Returns dict(dst, size, notes). Raises on failure."""
    check_destination(src, dst)
    if not os.path.isfile(src):
        raise ConvertError("Source file not available")
    src_info = _probe_audio(src)
    if not src_info:
        raise ConvertError("Source has no readable audio stream")
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    base, ext = os.path.splitext(os.path.basename(dst))
    tmp = os.path.join(os.path.dirname(dst), f".{base}.{uuid.uuid4().hex[:8]}{PART}{ext}")
    proc = subprocess.Popen(build_command(src, tmp, s, src_info["bits"]), stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
    try:
        dur = src_info["duration"] or 1
        for line in proc.stdout:
            if cancel and cancel():
                proc.terminate()
                proc.wait(10)
                raise InterruptedError("Cancelled")
            if line.startswith("out_time_us=") and progress:
                try:
                    progress(min(1.0, int(line.split("=")[1]) / 1e6 / dur))
                except ValueError:
                    pass
        err = proc.stderr.read()
        if proc.wait() != 0:
            raise ConvertError(f"FFmpeg failed: {err.strip()[:500]}")
        info = verify_output(tmp, s, src_info["duration"])
        notes = copy_tags_and_art(src, tmp, s)
        verify_output(tmp, s, src_info["duration"])  # still intact after tagging
        publish(tmp, dst)
        return dict(dst=dst, size=os.path.getsize(dst), notes=notes, info=info)
    finally:
        if proc.poll() is None:
            proc.kill()
        if os.path.exists(tmp):
            os.remove(tmp)


# ---------- jobs & history ----------

def plan_jobs(conn, sources, s: Settings, root=None):
    """sources: [(track_id or None, path[, root])]. Returns job dicts with output path, estimate and status.
    `root` is the folder the mirrored structure is relative to (default: the song's library scan root).
    Duplicate sources and already-converted outputs are marked instead of being queued twice."""
    jobs, seen_src, seen_dst = [], set(), set()
    for item in sources:
        tid, src = item[0], item[1]
        src_root = item[2] if len(item) > 2 else None
        row = conn.execute("SELECT * FROM tracks WHERE id=?", (tid,)).fetchone() if tid else None
        if row is None:
            row = conn.execute("SELECT * FROM tracks WHERE path=?", (src,)).fetchone()
        r = dict(row) if row else {}
        base_root = src_root or root or r.get("root") or os.path.dirname(os.path.dirname(src))
        dst = output_path(src, s, base_root)
        fmt = r.get("format") or os.path.splitext(src)[1][1:].upper()
        size = r.get("size") or (os.path.getsize(src) if os.path.exists(src) else 0)
        job = dict(id=uuid.uuid4().hex[:10], track_id=r.get("id"), src=src, dst=dst, src_format=fmt, src_size=size,
                   dst_format=s.fmt, est=estimate_size(r.get("duration"), s, size, r.get("sample_rate"),
                                                       r.get("channels"), r.get("bits")),
                   kbps=(r.get("bitrate") or 0) // 1000, status="queued", progress=0.0, error="")
        key = (src, s.key())
        if key in seen_src:
            continue  # same file + same settings already in this batch
        seen_src.add(key)
        if dst.casefold() in seen_dst:
            job.update(status="skipped", error="Another file in this batch maps to the same output name")
        elif os.path.exists(dst):
            job.update(status="skipped", error="Output already exists — not overwritten")
        else:
            try:
                check_destination(src, dst)
            except (ConvertError, FileExistsError) as e:
                job.update(status="skipped", error=str(e))
        seen_dst.add(dst.casefold())
        jobs.append(job)
    return jobs


def record(conn, job, s: Settings, started, finished):
    conn.execute("INSERT INTO conversions(track_id, src, dst, src_format, dst_format, settings, status, error, "
                 "src_size, dst_size, started, finished) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                 (job.get("track_id"), job["src"], job["dst"], job["src_format"], job["dst_format"],
                  json.dumps(asdict(s)), job["status"], job.get("error", ""), job.get("src_size"),
                  job.get("dst_size"), started, finished))
    conn.commit()
    if job["status"] in ("failed",):
        with open(db.data_dir() / "conversion_errors.log", "a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {job['src']} -> {job['dst']}: {job['error']}\n")


def run_jobs(conn, jobs, s: Settings, on_update=None, cancel=None, workers=3):
    """Run queued jobs (in parallel FFmpeg processes). on_update(job) is called on every change."""
    from concurrent.futures import ThreadPoolExecutor
    roots = {os.path.dirname(j["dst"]) for j in jobs}
    for r in {s.out_dir} | roots:
        if r and os.path.isdir(r):
            cleanup_partials(r)

    def one(job):
        if job["status"] != "queued":
            return job
        if cancel and cancel():
            job.update(status="cancelled")
            on_update and on_update(job)
            return job
        job.update(status="converting", progress=0.0, started=time.time())
        on_update and on_update(job)

        def prog(f):
            job["progress"] = f
            on_update and on_update(job)
        try:
            res = convert_file(job["src"], job["dst"], s, prog, cancel)
            job.update(status="done", progress=1.0, dst_size=res["size"], error="; ".join(res["notes"]))
        except InterruptedError:
            job.update(status="cancelled", error="Cancelled — no output written")
        except FileExistsError as e:
            job.update(status="skipped", error=str(e))
        except Exception as e:
            job.update(status="failed", error=str(e)[:500])
        job["finished"] = time.time()
        on_update and on_update(job)
        return job

    with ThreadPoolExecutor(max(1, workers)) as ex:
        done = list(ex.map(one, jobs))
    for j in done:
        if j["status"] in ("done", "failed", "cancelled", "skipped") and j.get("_recorded") is None:
            record(conn, j, s, j.get("started"), j.get("finished", time.time()))
            j["_recorded"] = True
    return done
