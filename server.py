#!/usr/bin/env python3
"""
Cutwise — the easy AI video editor.
Runs 100% on your own PC. AI (speech, captions, filler removal) runs on your CPU cores;
exports use the AMD Radeon GPU encoder when it's available.
"""
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from pathlib import Path

from flask import Flask, abort, jsonify, request, send_file

APP_DIR = Path(__file__).resolve().parent
HOME = Path(os.environ.get("CUTWISE_HOME", Path.home() / "Cutwise"))
MEDIA, EXPORTS, CACHE, JOBDIR = (HOME / d for d in ("media", "exports", "cache", "jobs"))
for _d in (MEDIA, EXPORTS, CACHE, JOBDIR):
    _d.mkdir(parents=True, exist_ok=True)
LIB_FILE = HOME / "library.json"
PROJECT_FILE = HOME / "project.json"
PORT = int(os.environ.get("CUTWISE_PORT", 8765))
CPU = os.cpu_count() or 4
WHISPER_THREADS = max(4, CPU // 2) if CPU > 8 else CPU  # physical cores work best
WIN = os.name == "nt"
NOWIN = subprocess.CREATE_NO_WINDOW if WIN else 0


# ---------------------------------------------------------------- tools
def find_tool(name):
    p = shutil.which(name)
    if p:
        return p
    cands = [APP_DIR / "ffmpeg" / (name + (".exe" if WIN else ""))]
    if WIN:
        la = Path(os.environ.get("LOCALAPPDATA", ""))
        cands += [la / "Microsoft/WinGet/Links" / f"{name}.exe", Path("C:/ffmpeg/bin") / f"{name}.exe"]
        pk = la / "Microsoft/WinGet/Packages"
        if pk.exists():
            cands += list(pk.glob(f"Gyan.FFmpeg*/*/bin/{name}.exe"))
    for c in cands:
        if Path(c).exists():
            return str(c)
    return name


FFMPEG, FFPROBE = find_tool("ffmpeg"), find_tool("ffprobe")


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", creationflags=NOWIN, **kw)


def cpu_name():
    try:
        if WIN:
            import winreg
            k = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0")
            return winreg.QueryValueEx(k, "ProcessorNameString")[0].strip()
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.lower().startswith("model name"):
                return line.split(":", 1)[1].strip()
    except Exception:
        pass
    return platform.processor() or "CPU"


ENC = {"amf": False, "checked": False, "fc_new": True}


def detect_hw():
    try:
        r = run([FFMPEG, "-hide_banner", "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=320x240:d=0.4",
                 "-c:v", "h264_amf", "-f", "null", "-"], timeout=40)
        ENC["amf"] = r.returncode == 0
    except Exception:
        ENC["amf"] = False
    # Does this ffmpeg understand "-/filter_complex file" (ffmpeg 7+)?
    try:
        t = JOBDIR / "_fc_test.txt"
        t.write_text("[0:v]null[o]")
        r = run([FFMPEG, "-hide_banner", "-v", "error", "-f", "lavfi", "-i", "color=s=64x64:d=0.1",
                 "-/filter_complex", str(t), "-map", "[o]", "-f", "null", "-"], timeout=30)
        ENC["fc_new"] = r.returncode == 0
    except Exception:
        ENC["fc_new"] = False
    ENC["checked"] = True


def fc_args(path):
    return ["-/filter_complex", path] if ENC["fc_new"] else ["-filter_complex_script", path]


def venc(kind, quality):
    q = {"high": 18, "standard": 21, "small": 26}.get(quality, 21)
    if kind == "amf":
        return ["-c:v", "h264_amf", "-quality", "quality", "-rc", "cqp",
                "-qp_i", str(q), "-qp_p", str(q + 2), "-qp_b", str(q + 4)]
    return ["-c:v", "libx264", "-preset", "medium", "-crf", str(q), "-threads", "0"]


# ---------------------------------------------------------------- library
lock = threading.Lock()
clips = {}
jobs = {}


def save_lib():
    with lock:
        LIB_FILE.write_text(json.dumps(clips, indent=1))


def probe(path):
    r = run([FFPROBE, "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(path)])
    if r.returncode:
        raise RuntimeError("Can't read this file — is it a video?")
    d = json.loads(r.stdout or "{}")
    streams = d.get("streams", [])
    v = next((s for s in streams if s.get("codec_type") == "video"
              and not s.get("disposition", {}).get("attached_pic")), None)
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if not v:
        raise RuntimeError("No video track found in this file.")
    dur = float(d.get("format", {}).get("duration") or v.get("duration") or 0)
    if dur <= 0:
        raise RuntimeError("Couldn't work out how long this video is.")
    fps = 30.0
    try:
        n, dn = (v.get("avg_frame_rate") or v.get("r_frame_rate") or "30/1").split("/")
        fps = float(n) / float(dn) if float(dn) else 30.0
    except Exception:
        pass
    w, h = int(v.get("width") or 1920), int(v.get("height") or 1080)
    rot = 0
    for sd in v.get("side_data_list") or []:
        if "rotation" in sd:
            rot = int(float(sd["rotation"]))
    if "rotate" in (v.get("tags") or {}):
        rot = int(v["tags"]["rotate"])
    if abs(rot) % 180 == 90:
        w, h = h, w
    return dict(duration=round(dur, 3), width=w, height=h, fps=round(fps, 3) if 1 < fps < 241 else 30.0,
                vcodec=v.get("codec_name", ""), pix=v.get("pix_fmt", ""), has_audio=bool(a))


def needs_proxy(c):
    ext = Path(c["path"]).suffix.lower()
    if c["vcodec"] not in ("h264", "vp8", "vp9", "av1"):
        return True
    if "10" in c["pix"] or "422" in c["pix"] or "444" in c["pix"]:
        return True
    return ext not in (".mp4", ".m4v", ".mov", ".webm")


def add_clip(path, name=None):
    info = probe(path)
    cid = uuid.uuid4().hex[:10]
    thumb = CACHE / f"{cid}.jpg"
    run([FFMPEG, "-y", "-v", "error", "-ss", f"{min(1.0, info['duration'] / 3):.2f}", "-i", str(path),
         "-frames:v", "1", "-vf", "scale=320:-2", str(thumb)])
    clip = dict(id=cid, name=name or Path(path).name, path=str(path), proxy=None, proxy_state="none",
                proxy_progress=0, strip=False, added=time.time(), **info)
    if needs_proxy(clip):
        clip["proxy_state"] = "queued"
    with lock:
        clips[cid] = clip
    save_lib()
    threading.Thread(target=make_strip, args=(cid,), daemon=True).start()
    if clip["proxy_state"] == "queued":
        threading.Thread(target=make_proxy, args=(cid,), daemon=True).start()
    return clip


def make_strip(cid):
    """Filmstrip image so timeline blocks show what's in them."""
    c = clips.get(cid)
    if not c:
        return
    out = CACHE / f"{cid}.strip.jpg"
    n = 24
    rate = n / max(c["duration"], 0.5)
    r = run([FFMPEG, "-y", "-v", "error", "-skip_frame", "nokey", "-i", c["path"], "-an",
             "-vf", f"fps={rate:.5f},scale=96:54:force_original_aspect_ratio=increase,crop=96:54,tile={n}x1",
             "-frames:v", "1", str(out)])
    if r.returncode or not out.exists():  # some files have too few keyframes; decode everything
        run([FFMPEG, "-y", "-v", "error", "-i", c["path"], "-an",
             "-vf", f"fps={rate:.5f},scale=96:54:force_original_aspect_ratio=increase,crop=96:54,tile={n}x1",
             "-frames:v", "1", str(out)])
    if out.exists():
        c["strip"] = True
        save_lib()


PROXY_SEM = threading.Semaphore(2)


def make_proxy(cid):
    c = clips.get(cid)
    if not c:
        return
    with PROXY_SEM:
        c["proxy_state"] = "working"
        out = CACHE / f"{cid}.proxy.mp4"
        while not ENC["checked"]:
            time.sleep(0.3)
        kind = "amf" if ENC["amf"] else "x264"
        for attempt in (kind, "x264"):
            enc = (["-c:v", "h264_amf", "-quality", "speed", "-rc", "cqp", "-qp_i", "24", "-qp_p", "26"]
                   if attempt == "amf" else ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23"])
            try:
                ffmpeg_progress(["-i", c["path"], "-vf", "scale=-2:min(720\\,ih),format=yuv420p", *enc,
                                 "-c:a", "aac", "-b:a", "128k", "-ac", "2", "-movflags", "+faststart", str(out)],
                                c["duration"], lambda p: c.__setitem__("proxy_progress", round(p, 3)))
                c["proxy"], c["proxy_state"] = str(out), "ready"
                break
            except Exception:
                c["proxy_state"] = "failed"
        save_lib()


def load_lib():
    if not LIB_FILE.exists():
        return
    try:
        data = json.loads(LIB_FILE.read_text())
    except Exception:
        return
    for k, v in data.items():
        if not Path(v["path"]).exists():
            continue
        if v.get("proxy_state") != "none" and not (v.get("proxy") and Path(v["proxy"]).exists()):
            v["proxy"], v["proxy_state"] = None, "queued"
        clips[k] = v
    for k, v in clips.items():
        if v["proxy_state"] == "queued":
            threading.Thread(target=make_proxy, args=(k,), daemon=True).start()
        if not v.get("strip"):
            threading.Thread(target=make_strip, args=(k,), daemon=True).start()


# ---------------------------------------------------------------- jobs
def new_job(kind, label, key=None):
    j = dict(id=uuid.uuid4().hex[:8], kind=kind, label=label, key=key, progress=0.0,
             state="running", error=None, result=None, started=time.time())
    jobs[j["id"]] = j
    return j


def ffmpeg_progress(args, total, on_prog, cwd=None):
    cmd = [FFMPEG, "-hide_banner", "-y", "-nostats", "-progress", "pipe:1"] + args
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
                         errors="replace", cwd=cwd, creationflags=NOWIN)
    err = []
    t = threading.Thread(target=lambda: err.extend(p.stderr), daemon=True)
    t.start()
    for line in p.stdout:
        if line.startswith("out_time_us="):
            try:
                on_prog(min(0.999, int(line.split("=")[1]) / 1e6 / total) if total > 0 else 0)
            except ValueError:
                pass
    p.wait()
    t.join(timeout=5)
    if p.returncode:
        raise RuntimeError("".join(err[-12:]).strip() or "ffmpeg failed")


# ---------------------------------------------------------------- AI: speech
_models = {}
_model_lock = threading.Lock()
TRANSCRIBE_LOCK = threading.Lock()
MODELS = {"fast": "small", "best": "large-v3-turbo"}
# This nudges Whisper to keep "um"s and "uh"s instead of silently cleaning them up.
FILLER_PROMPT = "Umm, let me think, uh... like, hmm. Okay, so, um, here's what I, uh, think."


def whisper_ok():
    try:
        import faster_whisper  # noqa: F401
        return True
    except Exception:
        return False


def get_model(name):
    from faster_whisper import WhisperModel
    with _model_lock:
        if name not in _models:
            _models[name] = WhisperModel(name, device="cpu", compute_type="int8",
                                         cpu_threads=WHISPER_THREADS, download_root=str(HOME / "models"))
        return _models[name]


def do_transcribe(j, c, q, cf):
    try:
        with TRANSCRIBE_LOCK:
            j["label"] = "Loading the AI speech model (first time: downloading it)…"
            m = get_model(MODELS[q])
            j["label"] = f"Listening to “{c['name']}”"
            segs, info = m.transcribe(c["path"], word_timestamps=True, vad_filter=True, beam_size=5,
                                      initial_prompt=FILLER_PROMPT, condition_on_previous_text=False)
            words = []
            for s in segs:
                for w in s.words or []:
                    txt = w.word.strip()
                    if txt:
                        words.append(dict(s=round(w.start, 3), e=round(w.end, 3), w=txt))
                j["progress"] = min(0.99, s.end / max(c["duration"], 0.1))
            cf.write_text(json.dumps(words))
            j["result"] = dict(words=words, language=info.language)
            j["progress"], j["state"] = 1.0, "done"
    except Exception as e:
        j["state"], j["error"] = "error", f"Speech AI failed: {e}"


# ---------------------------------------------------------------- export
def safe_name(s):
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", s or "").strip().strip(".")
    return s[:80] or "My video"


def srt_time(t):
    t = max(0, t)
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h):02}:{int(m):02}:{int(s):02},{int(round((s - int(s)) * 1000)) % 1000:03}"


def write_srt(cues, path):
    out = []
    for i, c in enumerate(cues, 1):
        out.append(f"{i}\n{srt_time(c['s'])} --> {srt_time(c['e'])}\n{c['t']}\n")
    Path(path).write_text("\n".join(out), encoding="utf-8")


def do_export(j, segs, o):
    work = JOBDIR / j["id"]
    work.mkdir(parents=True, exist_ok=True)
    try:
        first = clips[segs[0]["clip"]]
        res = str(o.get("resolution", "source"))
        W, H = first["width"], first["height"]
        if res != "source":
            short = int(res)
            if W >= H:
                W, H = round(W / H * short), short
            else:
                W, H = short, round(H / W * short)
        W, H = W - W % 2, H - H % 2
        fps = min(60.0, float(o.get("fps") or first["fps"] or 30))
        total = sum(s["end"] - s["start"] for s in segs)

        inputs, fl = [], []
        for i, s in enumerate(segs):
            c = clips[s["clip"]]
            d = s["end"] - s["start"]
            inputs += ["-ss", f"{s['start']:.3f}", "-t", f"{d:.3f}", "-i", c["path"]]
            fl.append(f"[{i}:v:0]setpts=PTS-STARTPTS,scale={W}:{H}:force_original_aspect_ratio=decrease,"
                      f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:black,setsar=1,fps={fps:g},format=yuv420p[v{i}]")
            if c["has_audio"]:
                fl.append(f"[{i}:a:0]asetpts=PTS-STARTPTS,aresample=48000,"
                          f"aformat=sample_fmts=fltp:channel_layouts=stereo,apad,atrim=0:{d:.3f}[a{i}]")
            else:
                fl.append(f"anullsrc=r=48000:cl=stereo,atrim=0:{d:.3f},"
                          f"aformat=sample_fmts=fltp:channel_layouts=stereo[a{i}]")
        n = len(segs)
        fl.append("".join(f"[v{i}][a{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=1[cv][ca]")

        vpost, apost = [], []
        if o.get("enhance"):
            vpost.append("eq=contrast=1.05:saturation=1.12:gamma=1.02")
            apost.append("highpass=f=70,afftdn=nr=10:nf=-40,loudnorm=I=-16:TP=-1.5:LRA=11,aresample=48000")
        cues = o.get("cues") or []
        if o.get("captions") and cues:
            write_srt(cues, work / "captions.srt")
            fs = 20 if W >= H else 13
            vpost.append("subtitles=captions.srt:force_style='FontName=Arial,FontSize=%d,Bold=1,"
                         "PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,BorderStyle=1,Outline=2,"
                         "Shadow=1,MarginV=22'" % fs)
        fl.append(f"[cv]{','.join(vpost) or 'null'}[outv]")
        fl.append(f"[ca]{','.join(apost) or 'anull'}[outa]")
        (work / "graph.txt").write_text(";\n".join(fl), encoding="utf-8")

        out = EXPORTS / f"{safe_name(o.get('name'))}.mp4"
        k = 2
        while out.exists():
            out = EXPORTS / f"{safe_name(o.get('name'))} ({k}).mp4"
            k += 1

        while not ENC["checked"]:
            time.sleep(0.3)
        kinds = (["amf", "x264"] if (o.get("gpu", True) and ENC["amf"]) else ["x264"])
        last = None
        for kind in kinds:
            j["label"] = ("Exporting with your AMD Radeon GPU" if kind == "amf"
                          else f"Exporting on {CPU} CPU threads")
            try:
                ffmpeg_progress([*inputs, *fc_args("graph.txt"), "-map", "[outv]", "-map", "[outa]",
                                 *venc(kind, o.get("quality", "standard")), "-pix_fmt", "yuv420p",
                                 "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(out)],
                                total, lambda p: j.__setitem__("progress", p), cwd=str(work))
                last = None
                break
            except Exception as e:
                last = e
                out.unlink(missing_ok=True)
        if last:
            raise last
        j["result"] = dict(path=str(out), name=out.name, size=out.stat().st_size,
                           seconds=round(time.time() - j["started"], 1), duration=round(total, 2))
        j["progress"], j["state"] = 1.0, "done"
    except Exception as e:
        j["state"], j["error"] = "error", str(e)[-900:]
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ---------------------------------------------------------------- web app
app = Flask(__name__, static_folder=None)


@app.get("/")
def index():
    return send_file(APP_DIR / "web" / "index.html")


@app.get("/api/system")
def api_system():
    return jsonify(cpu=cpu_name(), threads=CPU, whisper_threads=WHISPER_THREADS, whisper=whisper_ok(),
                   amf=ENC["amf"], hw_checked=ENC["checked"], ffmpeg=Path(FFMPEG).exists() or bool(shutil.which(FFMPEG)),
                   exports=str(EXPORTS))


@app.get("/api/clips")
def api_clips():
    return jsonify(clips=list(clips.values()))


@app.delete("/api/clips/<cid>")
def api_clip_delete(cid):
    c = clips.pop(cid, None)
    if c:
        for f in CACHE.glob(f"{cid}.*"):
            f.unlink(missing_ok=True)
        if Path(c["path"]).parent == MEDIA:
            Path(c["path"]).unlink(missing_ok=True)
        save_lib()
    return jsonify(ok=True)


@app.post("/api/upload")
def api_upload():
    f = request.files.get("file")
    if not f:
        return jsonify(error="No file received."), 400
    name = Path(f.filename or "video.mp4").name
    dest = MEDIA / f"{uuid.uuid4().hex[:6]}_{safe_name(name)}"
    f.save(dest)
    try:
        return jsonify(clip=add_clip(dest, name))
    except Exception as e:
        dest.unlink(missing_ok=True)
        return jsonify(error=f"{name}: {e}"), 400


@app.post("/api/pick")
def api_pick():
    """Native 'open file' window — uses your files in place, no copying."""
    res = {}

    def dlg():
        try:
            import tkinter as tk
            from tkinter import filedialog
            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            res["files"] = filedialog.askopenfilenames(
                parent=root, title="Add videos to Cutwise",
                filetypes=[("Videos", "*.mp4 *.mov *.mkv *.avi *.webm *.m4v *.mts *.m2ts *.wmv *.flv *.3gp *.ts"),
                           ("All files", "*.*")])
            root.destroy()
        except Exception as e:
            res["error"] = str(e)

    t = threading.Thread(target=dlg)
    t.start()
    t.join()
    if "error" in res:
        return jsonify(fallback=True, error=res["error"])
    added, errs = [], []
    for f in res.get("files") or []:
        try:
            added.append(add_clip(f))
        except Exception as e:
            errs.append(f"{Path(f).name}: {e}")
    return jsonify(clips=added, errors=errs)


def _media(path):
    if not path or not Path(path).exists():
        abort(404)
    return send_file(path, conditional=True, max_age=0)


@app.get("/media/<cid>")
def media(cid):
    c = clips.get(cid) or abort(404)
    return _media(c["proxy"] if c.get("proxy_state") == "ready" else c["path"])


@app.get("/thumb/<cid>")
def thumb(cid):
    return _media(str(CACHE / f"{cid}.jpg"))


@app.get("/strip/<cid>")
def strip(cid):
    return _media(str(CACHE / f"{cid}.strip.jpg"))


@app.route("/api/project", methods=["GET", "POST"])
def api_project():
    if request.method == "POST":
        PROJECT_FILE.write_text(json.dumps(request.json))
        return jsonify(ok=True)
    try:
        return jsonify(json.loads(PROJECT_FILE.read_text()))
    except Exception:
        return jsonify({})


@app.get("/api/jobs/<jid>")
def api_job(jid):
    return jsonify(jobs.get(jid) or abort(404))


@app.post("/api/ai/silence")
def ai_silence():
    b = request.json
    c = clips.get(b["clip"]) or abort(404)
    s, e = float(b["start"]), float(b["end"])
    noise, mind = {"gentle": (-42, 1.0), "normal": (-35, 0.6), "aggressive": (-30, 0.35)}.get(
        b.get("level", "normal"), (-35, 0.6))
    if not c["has_audio"]:
        return jsonify(keep=[[s, e]], removed=0)
    r = run([FFMPEG, "-hide_banner", "-nostats", "-ss", f"{s:.3f}", "-t", f"{e - s:.3f}", "-i", c["path"],
             "-vn", "-af", f"silencedetect=noise={noise}dB:d={mind}", "-f", "null", "-"])
    sil, cur = [], None
    for m in re.finditer(r"silence_(start|end): (-?[\d.]+)", r.stderr):
        t = float(m.group(2)) + s
        if m.group(1) == "start":
            cur = t
        elif cur is not None:
            sil.append((cur, t))
            cur = None
    if cur is not None:
        sil.append((cur, e))
    pad, keep, pos = 0.12, [], s
    for a, z in sil:
        a2, z2 = max(s, a + pad), min(e, z - pad)
        if z2 - a2 <= 0.05:
            continue
        if a2 - pos > 0.25:
            keep.append([round(pos, 3), round(a2, 3)])
        pos = z2
    if e - pos > 0.25:
        keep.append([round(pos, 3), round(e, 3)])
    if not keep:
        keep = [[s, e]]
    return jsonify(keep=keep, removed=round((e - s) - sum(z - a for a, z in keep), 2))


@app.post("/api/ai/scenes")
def ai_scenes():
    b = request.json
    c = clips.get(b["clip"]) or abort(404)
    s, e = float(b["start"]), float(b["end"])
    th = {"few": 0.45, "normal": 0.32, "many": 0.22}.get(b.get("level", "normal"), 0.32)
    r = run([FFMPEG, "-hide_banner", "-nostats", "-threads", "0", "-ss", f"{s:.3f}", "-t", f"{e - s:.3f}",
             "-i", c["path"], "-an", "-vf", f"scale=320:-2,select='gt(scene,{th})',showinfo", "-f", "null", "-"])
    cuts, last = [], s
    for m in re.finditer(r"pts_time:\s*([\d.]+)", r.stderr):
        t = float(m.group(1)) + s
        if t - last >= 1.0 and e - t >= 1.0:
            cuts.append(round(t, 3))
            last = t
    return jsonify(cuts=cuts)


@app.post("/api/ai/transcribe")
def ai_transcribe():
    b = request.json
    cid, q = b["clip"], b.get("quality", "best")
    q = q if q in MODELS else "best"
    c = clips.get(cid) or abort(404)
    cf = CACHE / f"{cid}.{q}.words.json"
    if cf.exists():
        return jsonify(words=json.loads(cf.read_text()))
    if not c["has_audio"]:
        return jsonify(words=[])
    if not whisper_ok():
        return jsonify(error="The AI speech engine isn't installed yet. Close Cutwise and run “Start Cutwise” again."), 500
    key = f"{cid}.{q}"
    for j in jobs.values():
        if j["key"] == key and j["state"] == "running":
            return jsonify(job=j["id"])
    j = new_job("transcribe", f"Waiting to listen to “{c['name']}”", key)
    threading.Thread(target=do_transcribe, args=(j, c, q, cf), daemon=True).start()
    return jsonify(job=j["id"])


@app.post("/api/export")
def api_export():
    b = request.json
    segs = [s for s in b.get("segments", []) if s.get("clip") in clips and s["end"] - s["start"] > 0.04]
    if not segs:
        return jsonify(error="Your timeline is empty — add a video first."), 400
    j = new_job("export", "Getting ready to export…")
    threading.Thread(target=do_export, args=(j, segs, b), daemon=True).start()
    return jsonify(job=j["id"])


@app.get("/api/exports/<jid>/file")
def api_export_file(jid):
    j = jobs.get(jid) or abort(404)
    if not j.get("result"):
        abort(404)
    return send_file(j["result"]["path"], as_attachment=request.args.get("dl") == "1", conditional=True)


@app.post("/api/open-folder")
def api_open_folder():
    target = request.json.get("path") if request.is_json else None
    try:
        if WIN:
            if target and Path(target).exists():
                subprocess.Popen(["explorer", "/select,", target])
            else:
                os.startfile(str(EXPORTS))
        else:
            subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(EXPORTS)])
        return jsonify(ok=True)
    except Exception as e:
        return jsonify(error=str(e))


def port_in_use(p):
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", p)) == 0


def main():
    url = f"http://127.0.0.1:{PORT}"
    if port_in_use(PORT):
        print("Cutwise is already running — opening it.")
        webbrowser.open(url)
        return
    load_lib()
    threading.Thread(target=detect_hw, daemon=True).start()
    import logging
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    print(f"\n  Cutwise is running at {url}\n  Your exports go to {EXPORTS}\n  Keep this window open while editing.\n")
    if os.environ.get("CUTWISE_NO_BROWSER") != "1":
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    app.run(host="127.0.0.1", port=PORT, threaded=True)


if __name__ == "__main__":
    main()
