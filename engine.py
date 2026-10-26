"""Smart-cut + merge engine on top of ffmpeg.

Only the few seconds around each cut point are re-encoded; everything else is
stream-copied. Pieces are written as MPEG-TS and joined with the concat demuxer.
"""
import bisect
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from fractions import Fraction

NO_WINDOW = 0x08000000 if os.name == "nt" else 0
EPS = 0.002
TS_BASE = 2.0  # keeps piece dts positive so the muxer never shifts the first piece
LOGLEVEL = os.environ.get("SMARTCUT_LOGLEVEL", "error")
SUPPORTED_VCODECS = ("h264", "hevc")


class CutError(Exception):
    pass


class Cancelled(Exception):
    pass


def _base_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def find_tool(name):
    base = _base_dir()
    for d in (getattr(sys, "_MEIPASS", base), base, os.path.join(base, "bin"), os.path.join(base, "ffmpeg")):
        p = os.path.join(d, name + ".exe")
        if os.path.isfile(p):
            return p
    return shutil.which(name)


def tools():
    ff, fp = find_tool("ffmpeg"), find_tool("ffprobe")
    if not ff or not fp:
        raise CutError(
            "ffmpeg.exe / ffprobe.exe 를 찾을 수 없습니다.\n"
            "프로그램과 같은 폴더에 넣거나, 명령 프롬프트에서\n"
            "winget install Gyan.FFmpeg\n"
            "를 실행한 뒤 다시 켜세요."
        )
    return ff, fp


# ---------- time helpers ----------

def parse_time(text):
    text = text.strip()
    if not text:
        return None
    parts = text.split(":")
    if len(parts) > 3:
        raise ValueError(text)
    sec = 0.0
    for p in parts:
        sec = sec * 60 + float(p)
    return sec


def fmt_time(sec):
    ms = int(round(sec * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h}:{m:02d}:{s:02d}.{ms:03d}"


# ---------- probing ----------

def _capture(cmd):
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", creationflags=NO_WINDOW)
    if p.returncode:
        raise CutError(p.stderr.strip() or "ffprobe failed")
    return p.stdout


def probe(path):
    _, fp = tools()
    j = json.loads(_capture([fp, "-v", "error", "-print_format", "json",
                             "-show_streams", "-show_format", path]))
    streams = j.get("streams", [])
    v = next((s for s in streams if s["codec_type"] == "video"
              and not s.get("disposition", {}).get("attached_pic")), None)
    if v is None:
        raise CutError(f"영상 스트림이 없습니다: {path}")
    fmt = j["format"]
    # nominal rate; avg_frame_rate differs slightly between recordings of the same settings
    fps = v.get("r_frame_rate", "0/0")
    if fps in ("0/0", ""):
        fps = v.get("avg_frame_rate", "30/1")
    return {
        "path": path,
        "duration": float(fmt.get("duration") or v.get("duration") or 0),
        "start": float(fmt.get("start_time") or 0),
        "vcodec": v["codec_name"],
        "w": v["width"], "h": v["height"],
        "fps": fps,
        "pix_fmt": v.get("pix_fmt", "yuv420p"),
        "profile": v.get("profile", ""),
        "audio": [(a["codec_name"], int(a.get("sample_rate") or 0), a.get("channels", 0),
                   int(a.get("bit_rate") or 0), (a.get("tags", {}).get("title") or a.get("tags", {}).get("name") or ""))
                  for a in streams if a["codec_type"] == "audio"],
    }


def keyframes(info):
    _, fp = tools()
    out = _capture([fp, "-v", "error", "-select_streams", "v:0",
                    "-show_entries", "packet=pts_time,flags", "-of", "csv=p=0", info["path"]])
    times, idxs, n = [], [], 0
    for line in out.splitlines():
        t, _, f = line.partition(",")
        if f.startswith("K") and t not in ("", "N/A"):
            times.append(float(t) - info["start"])
            idxs.append(n)
        n += 1
    order = sorted(range(len(times)), key=times.__getitem__)
    return [times[i] for i in order], [idxs[i] for i in order], n


def compatible(a, b):
    sig = lambda i: [x[:3] for x in i["audio"]]
    fa, fb = float(Fraction(a["fps"])), float(Fraction(b["fps"]))
    return (all(a[k] == b[k] for k in ("vcodec", "w", "h", "pix_fmt"))
            and abs(fa - fb) <= 0.01 * max(fa, fb) and sig(a) == sig(b))


# ---------- planning ----------

def keep_ranges(dur, cuts):
    """cuts: [(start, end|None)] -> ranges to keep."""
    norm = []
    for a, b in cuts:
        b = dur if b is None else min(b, dur)
        if a < 0 or a >= dur or b <= a:
            raise CutError(f"잘못된 구간: {fmt_time(a)} - {fmt_time(b)} (영상 길이 {fmt_time(dur)})")
        norm.append((a, b))
    keep, pos = [], 0.0
    for a, b in sorted(norm):
        if a > pos + EPS:
            keep.append((pos, a))
        pos = max(pos, b)
    if dur - pos > EPS:
        keep.append((pos, dur))
    return keep


def plan_pieces(info, kfs, keep, normalize):
    kfs, kidx, npk = kfs or ([], [], 0)
    ranges = []
    dur = info["duration"]
    for s, e in keep:
        pieces = []
        ranges.append(pieces)
        if e - s < EPS:
            continue
        if normalize:
            pieces.append(("enc", s, e, None, None))
            continue
        i = bisect.bisect_left(kfs, s - EPS)
        ks = kfs[i] if i < len(kfs) else None
        if e >= dur - EPS:
            # file duration can include an audio tail; the last video frame defines the end
            ke, n_end = (ks + (npk - kidx[i]) / float(Fraction(info["fps"])) if ks is not None else dur), npk
        else:
            j = bisect.bisect_right(kfs, e + EPS) - 1
            ke, n_end = (kfs[j], kidx[j]) if j >= 0 else (None, None)
        if ks is None or ke is None or ke - ks < EPS or ks >= e - EPS:
            pieces.append(("enc", s, e, None, None))
            continue
        if ks - s > EPS:
            pieces.append(("enc", s, ks, None, None))
        nxt = kfs[i + 1] if i + 1 < len(kfs) else dur
        # Some demuxers (mkv) land one GOP early when asked for the keyframe's exact time,
        # so seek to the middle of the GOP instead; timestamps are re-aligned with -itsoffset.
        pieces.append(("copy", ks, ke, n_end - kidx[i], ks + (nxt - ks) / 2))
        if e - ke > EPS and e < dur - EPS:
            pieces.append(("enc", ke, e, None, None))
    return ranges


# ---------- running ffmpeg ----------

def _drain_stderr(p, on_time, tail):
    for raw in p.stderr:
        line = raw.decode("utf-8", "replace").strip()
        m = re.match(r"out_time_us=(\d+)", line)
        if m:
            if on_time:
                on_time(int(m.group(1)) / 1e6)
        elif line and ("=" not in line or line.startswith(("Error", "["))):
            tail.append(line)


def _spawn(cmd, stdout=None, stdin=None, cwd=None):
    return subprocess.Popen(cmd, stdout=stdout, stdin=stdin, stderr=subprocess.PIPE,
                            creationflags=NO_WINDOW, cwd=cwd)


def _run(cmd, on_time, cancel, cwd=None):
    """Run ffmpeg to completion (output goes to a file)."""
    p = _spawn(cmd, cwd=cwd)
    tail = deque(maxlen=15)
    t = threading.Thread(target=_drain_stderr, args=(p, on_time, tail), daemon=True)
    t.start()
    while p.poll() is None:
        if cancel is not None and cancel.is_set():
            p.kill()
            p.wait()
            raise Cancelled()
        time.sleep(0.05)
    t.join(2)
    if p.returncode:
        raise CutError("ffmpeg 오류:\n" + "\n".join(tail))


def _stream(cmd, sink, on_time, cancel):
    """Run ffmpeg writing MPEG-TS to stdout and forward it into `sink` (another process's stdin)."""
    p = _spawn(cmd, stdout=subprocess.PIPE)
    tail = deque(maxlen=15)
    t = threading.Thread(target=_drain_stderr, args=(p, on_time, tail), daemon=True)
    t.start()
    try:
        while True:
            chunk = p.stdout.read(1 << 22)
            if not chunk:
                break
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            sink.write(chunk)
    except (BrokenPipeError, OSError):
        raise CutError("합치기 프로세스가 중단되었습니다.")
    finally:
        if p.poll() is None:
            p.kill()
        p.wait()
        t.join(2)
    if p.returncode:
        raise CutError("ffmpeg 오류:\n" + "\n".join(tail))


def _venc(t, scale):
    if t["vcodec"] == "hevc":
        a = ["-c:v", "libx265", "-crf", "18", "-preset", "fast", "-x265-params", "log-level=error"]
    else:
        a = ["-c:v", "libx264", "-crf", "16", "-preset", "fast"]
        prof = {"high": "high", "main": "main", "baseline": "baseline",
                "constrained baseline": "baseline"}.get(t["profile"].lower())
        if prof and t["pix_fmt"] == "yuv420p":
            a += ["-profile:v", prof]
    a += ["-pix_fmt", t["pix_fmt"], "-r", t["fps"]]
    if scale:
        w, h = t["w"], t["h"]
        a += ["-vf", f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
                     f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2"]
    return a


def _aenc(info, target, normalize):
    """Audio is always re-encoded (cheap) so it trims sample-accurately and stays in sync.
    Each track keeps its own sample rate / channel count / bitrate."""
    if not info["audio"]:
        return ["-an"]
    args = []
    for i, own in enumerate((target if normalize else info)["audio"]):
        br = min(max(own[3] or 192000, 96000 if own[2] == 1 else 128000), 320000)
        args += [f"-c:a:{i}", "aac", f"-b:a:{i}", str(br),
                 f"-ar:a:{i}", str(own[1] or 48000), f"-ac:a:{i}", str(own[2] or 2)]
    return args


def _ts_out(offset, dst):
    return ["-muxdelay", "0", "-muxpreload", "0", "-output_ts_offset", f"{offset:.6f}",
            "-f", "mpegts", dst]


def _first_pts(path):
    _, fp = tools()
    try:
        out = _capture([fp, "-v", "error", "-select_streams", "v:0", "-read_intervals", "%+#1",
                        "-show_entries", "packet=pts_time", "-of", "csv=p=0", path])
        return float(out.split()[0].strip(","))
    except (CutError, ValueError, IndexError):
        return None


def merge(jobs, output, on_progress=None, on_log=None, cancel=None):
    """jobs: [{"path": str, "cuts": [(start, end|None), ...]}], in output order."""
    ff, _ = tools()
    log = on_log or (lambda m: None)
    _emit = on_progress or (lambda f: None)
    _top = [0.0]

    def prog(f):
        _top[0] = max(_top[0], min(f, 1.0))
        _emit(_top[0])

    if not jobs:
        raise CutError("입력 파일이 없습니다.")
    out_abs = os.path.abspath(output)
    if any(os.path.abspath(j["path"]) == out_abs for j in jobs):
        raise CutError("출력 파일이 입력 파일과 같습니다.")

    infos = [probe(j["path"]) for j in jobs]
    for i in infos:
        if i["vcodec"] not in SUPPORTED_VCODECS:
            raise CutError(f"지원하지 않는 코덱({i['vcodec']}): H.264 / H.265 만 지원합니다.")
    target = infos[0]
    normalize = not all(compatible(target, i) for i in infos[1:])
    if normalize:
        if len({len(i["audio"]) for i in infos}) > 1:
            raise CutError("파일마다 오디오 트랙 수가 달라 합칠 수 없습니다.")
        log("파일들의 인코딩 설정이 서로 달라 전체를 다시 인코딩합니다 (시간이 오래 걸립니다).")

    plans = []
    for job, info in zip(jobs, infos):
        log(f"분석 중: {os.path.basename(info['path'])}")
        kfs = None if normalize else keyframes(info)
        keep = keep_ranges(info["duration"], job["cuts"])
        plans.append((info, plan_pieces(info, kfs, keep, normalize)))

    groups = [(info, r) for info, rngs in plans for r in rngs if r]
    if not groups:
        raise CutError("남는 영상이 없습니다.")
    n_all = sum(len(r) for _, r in groups)
    n_enc = sum(1 for _, r in groups for p in r if p[0] == "enc")
    sumdur = sum(p[2] - p[1] for _, r in groups for p in r)
    total = sumdur * 1.2
    log(f"조각 {n_all}개 (재인코딩 {n_enc}개, 나머지는 무손실 복사)")

    base = ["-hide_banner", "-loglevel", LOGLEVEL, "-nostdin", "-y", "-progress", "pipe:2", "-nostats"]
    tmp = tempfile.mkdtemp(prefix=".smartcut_", dir=os.path.dirname(out_abs) or ".")
    ext = os.path.splitext(out_abs)[1]
    part = out_abs + ".part" + ext
    final = None
    try:
        # Audio first, as ONE continuous AAC encode of all kept ranges (no encoder restarts at
        # the joins, so no clicks or timestamp overlaps).
        afile, work = None, 0.0
        if target["audio"]:
            afile = os.path.join(tmp, "audio.ts")
            layouts = {1: "mono", 2: "stereo", 3: "2.1", 4: "quad", 6: "5.1", 8: "7.1"}
            cmd = [ff, *base]
            parts, labels = [], [[] for _ in target["audio"]]
            for g, (info, pcs) in enumerate(groups):
                span = sum(e - s for _, s, e, _, _ in pcs)
                cmd += ["-ss", f"{pcs[0][1]:.6f}", "-i", info["path"]]
                for k, (_, sr, ch, _, _) in enumerate(target["audio"]):
                    parts.append(f"[{g}:a:{k}]atrim=duration={span:.6f},asetpts=PTS-STARTPTS,"
                                 f"aformat=sample_rates={sr or 48000}:"
                                 f"channel_layouts={layouts.get(ch, 'stereo')}[g{g}t{k}]")
                    labels[k].append(f"[g{g}t{k}]")
            for k in range(len(target["audio"])):
                parts.append("".join(labels[k]) + f"concat=n={len(groups)}:v=0:a=1[a{k}]")
            cmd += ["-filter_complex", ";".join(parts)]
            for k in range(len(target["audio"])):
                cmd += ["-map", f"[a{k}]"]
            cmd += _aenc(target, target, True) + _ts_out(TS_BASE, afile)
            _run(cmd, lambda t: prog(0.2 * min(t, sumdur) / total), cancel)
            work = 0.2 * sumdur

        # The muxer reads the video pieces from stdin as they are produced, so no
        # full-size temporary copy of the video is ever written to disk.
        cmd = [ff, "-hide_banner", "-loglevel", "error", "-y", "-f", "mpegts", "-i", "pipe:0"]
        if afile:
            # the AAC encoder emits 1024 priming samples first; move the audio earlier by that much
            sr = target["audio"][0][1] or 48000
            cmd += ["-itsoffset", f"{-1024 / sr:.6f}", "-i", os.path.basename(afile)]
        cmd += ["-map", "0:v"] + (["-map", "1:a"] if afile else []) + [
            "-c", "copy", "-output_ts_offset", f"{-TS_BASE:.6f}", "-avoid_negative_ts", "make_zero"]
        for i, a in enumerate(target["audio"]):
            if a[4]:
                cmd += [f"-metadata:s:a:{i}", f"title={a[4]}"]
        if ext.lower() in (".mp4", ".m4v", ".mov"):
            cmd += ["-bsf:a", "aac_adtstoasc"]
        cmd.append(part)
        final = _spawn(cmd, stdin=subprocess.PIPE, cwd=tmp)
        ftail = deque(maxlen=15)
        threading.Thread(target=_drain_stderr, args=(final, None, ftail), daemon=True).start()

        done, n = 0.0, 0
        for info, pcs in groups:
            for kind, s, e, nframes, seek in pcs:
                log(f"[{n + 1}/{n_all}] {'복사' if kind == 'copy' else '인코딩'} "
                    f"{fmt_time(s)} - {fmt_time(e)}")
                progress = lambda t, d=work, w=e - s: prog((d + min(t, w)) / total)
                want = done + TS_BASE
                if kind == "copy":
                    def copy_cmd(frames, dst):
                        return [ff, *base, "-itsoffset", f"{seek - s:.6f}", "-ss", f"{seek:.6f}",
                                "-i", info["path"], "-t", f"{e - s:.6f}", "-map", "0:v:0", "-an",
                                "-c:v", "copy", "-frames:v", str(frames)] + _ts_out(want, dst)
                    probe_ts = os.path.join(tmp, "check.ts")
                    _run(copy_cmd(4, probe_ts), None, cancel)
                    got = _first_pts(probe_ts)
                    if got is None or abs(got - want) > 0.15:
                        log("  복사 위치가 어긋나 이 조각은 재인코딩합니다.")
                        kind = "enc"
                if kind == "copy":
                    cmd = copy_cmd(nframes, "pipe:1")
                else:
                    cmd = [ff, *base, "-ss", f"{s:.6f}", "-i", info["path"], "-t", f"{e - s:.6f}",
                           "-map", "0:v:0", "-an"]
                    cmd += _venc(target, normalize) + ["-bf", "0"] + _ts_out(want, "pipe:1")
                _stream(cmd, final.stdin, progress, cancel)
                done += e - s
                work += e - s
                prog(work / total)
                n += 1

        log("마무리 중...")
        final.stdin.close()
        while final.poll() is None:
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            time.sleep(0.05)
        if final.returncode:
            raise CutError("ffmpeg 오류:\n" + "\n".join(ftail))
        os.replace(part, out_abs)
        prog(1.0)
        log("완료: " + out_abs)
    finally:
        if final is not None and final.poll() is None:
            final.kill()
            final.wait()
        if not os.environ.get("SMARTCUT_KEEP"):
            shutil.rmtree(tmp, ignore_errors=True)
        if os.path.exists(part):
            try:
                os.remove(part)
            except OSError:
                pass


def grab_frame(path, t, width=640, accurate=True):
    """One frame at time t as PNG bytes (None on failure)."""
    ff, _ = tools()
    cmd = [ff, "-hide_banner", "-loglevel", "error", "-nostdin"]
    if not accurate:
        cmd.append("-noaccurate_seek")
    cmd += ["-ss", f"{max(t, 0):.3f}", "-i", path, "-frames:v", "1",
            "-vf", f"scale={width}:-2", "-f", "image2pipe", "-vcodec", "png", "-"]
    p = subprocess.run(cmd, capture_output=True, creationflags=NO_WINDOW)
    return p.stdout or None
