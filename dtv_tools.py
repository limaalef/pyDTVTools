#!/usr/bin/env python3
"""
dtv_tools.py - DTV+ (TV 3.0) tools, used by the command line and by the web interface.

Subcommands:
  ts-tv3        process an MPEG-TS capture of TV 3.0 (VVC + LCEVC + MPEG-H) into MP4 with MP4Box:
                  single-track  VVC with LCEVC embedded as SEI T.35 (+ MPEG-H mhm1 audio)
                  dual-track    vvc1 + lvc1 (LCEVC as its own track, via NHML) (+ MPEG-H mhm1 audio)
  mpegh-scan    analyse the MPEG-H audio (presets, groups, native layout, availability timeline)
  mpegh-audio   render the chosen presets/groups to audio files (parallel decoding)
  mp4-cut       lossless cut of an MP4 at IDR frames (MP4Box -splitx) with A/V sync check
  transcode     single-track MP4 (VVC + LCEVC + MPEG-H) -> HEVC or AV1 10-bit HDR10, with every MPEG-H preset as an audio track
  frame         save one frame as HDR-native image (16-bit PNG / AVIF) and/or tone-mapped SDR image (PNG / JPG)
  sdr           redo the SDR image from a saved *_hdr.png (no decoding)
  dash-capture  record a live or static DASH stream (VVC + LCEVC + MPEG-H) into an MP4
  doctor        check the external programs (MP4Box, ffmpeg, MPEG-H tools) and Python modules

Only use dash-capture on streams you are entitled to record. See README.md.
"""
import argparse
import atexit
import bisect
import concurrent.futures
import hashlib
import io
import itertools
import json
import os
import math
import re
import shlex
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import threading
import time
import zlib
from datetime import datetime
from pathlib import Path

__version__ = "1.4.0"
PROTOCOL_VERSION = 2
# What a controlling program (the GUI) may rely on; listed by `describe`, `doctor` and the `hello` event.
FEATURES = ("json", "control", "begin", "stage-progress", "describe", "log-events", "isolation", "update-check", "dash-live", "preview", "fetch-tool")
NOWIN = {"creationflags": 0x08000000} if os.name == "nt" else {}
PJ = False          # machine mode: --json (protocol v2) or the legacy --progress-json
JSONL = False       # --json: stdout carries ONLY one JSON object per line
VERBOSE = False
COLOR = False
ASCII = False
NOTES: list[str] = []
CMD = ""
CUR_STEP = None
SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ["DTV_TOOLS_CONFIG"]) if os.environ.get("DTV_TOOLS_CONFIG") else SCRIPT_DIR / "dtv_tools.json"

BAD_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
WEIGHTS: dict = {}
ORDER: list = []
STEP_DEFS: list = []
STEP_T0: dict = {}
_bar = {"active": False, "pct": 0.0, "last": 0.0}


class Fail(Exception):
    """Erro esperado (mensagem para o usuário)."""


# ──────────────────────────── output plumbing / child processes ────────────────────────────
_LOCK = threading.RLock()
_OUT_HOOK = None          # function(str) set by a live panel that owns the terminal (dash-capture)
_PROCS: set = set()
_PROC_LOCK = threading.Lock()


def _out(msg):
    hook = _OUT_HOOK
    if hook is not None:
        hook(msg)
    elif JSONL:     # nothing but events may reach stdout in --json mode
        _emit({"type": "log", "level": "info", "msg": str(msg)})
    else:
        print(msg, flush=True)


_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _emit(obj):
    """One protocol event = one line of JSON (v2) or '@@DTV {json}' (legacy --progress-json)."""
    if JSONL:   # text from child programs may carry terminal colours; a front end does not want them
        obj = {k: (_ANSI.sub("", v) if k in ("msg", "message", "detail") and isinstance(v, str) else v) for k, v in obj.items()}
    line = json.dumps(obj, ensure_ascii=False, default=str, separators=(",", ":"))
    print(line if JSONL else "@@DTV " + line, flush=True)


def sp_popen(cmd, **kw):
    """subprocess.Popen that remembers the child so it can be killed if this process is terminated."""
    if os.name == "nt":                 # never a console window, whatever the caller forgot to pass
        kw["creationflags"] = kw.get("creationflags", 0) | 0x08000000
    p = subprocess.Popen(cmd, **kw)
    with _PROC_LOCK:
        _PROCS.difference_update({x for x in _PROCS if x.poll() is not None})
        _PROCS.add(p)
    return p


def sp_run(cmd, capture_output=False, timeout=None, input=None, **kw):
    """subprocess.run replacement built on sp_popen (children never outlive us)."""
    if capture_output:
        kw["stdout"] = subprocess.PIPE
        kw["stderr"] = subprocess.PIPE
    kw["stdin"] = subprocess.PIPE if input is not None else kw.get("stdin", subprocess.DEVNULL)
    p = sp_popen(cmd, **kw)
    try:
        out, err = p.communicate(input, timeout=timeout)
    except subprocess.TimeoutExpired:
        p.kill()
        p.communicate()
        raise
    except BaseException:
        p.kill()
        raise
    finally:
        with _PROC_LOCK:
            _PROCS.discard(p)
    return subprocess.CompletedProcess(cmd, p.returncode, out, err)


class Drain:
    """
    Reads a child's pipe continuously in a thread and keeps only the last `keep` bytes.
    A child that writes more than the OS pipe buffer (64 KB) blocks forever if nobody reads while we wait for it.
    """

    def __init__(self, pipe, keep=8192):
        self.keep, self.tail = keep, b""
        self.th = threading.Thread(target=self._run, args=(pipe,), daemon=True)
        self.th.start()

    def _run(self, pipe):
        try:
            while True:
                chunk = pipe.read(4096)
                if not chunk:
                    break
                self.tail = (self.tail + chunk)[-self.keep:]
        except (OSError, ValueError):
            pass

    def text(self, timeout=5):
        self.th.join(timeout)
        return self.tail.decode("utf-8", errors="replace")


def kill_children():
    with _PROC_LOCK:
        procs = list(_PROCS)
    for p in procs:
        try:
            if p.poll() is None:
                p.terminate()
        except OSError:
            pass
    t0 = time.time()
    for p in procs:
        try:
            p.wait(timeout=max(0.1, 3 - (time.time() - t0)))
        except Exception:
            try:
                p.kill()
            except OSError:
                pass


def _win_kill_on_close():
    """Windows: put this process in a job object so MP4Box/ffmpeg die with us even if we are killed hard."""
    import ctypes
    from ctypes import wintypes

    class BASIC(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]

    class IOC(ctypes.Structure):
        _fields_ = [(n, ctypes.c_uint64) for n in ("ro", "wo", "oo", "rb", "wb", "ob")]

    class EXT(ctypes.Structure):
        _fields_ = [("Basic", BASIC), ("Io", IOC), ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]

    k = ctypes.windll.kernel32
    k.CreateJobObjectW.restype = wintypes.HANDLE
    job = k.CreateJobObjectW(None, None)
    info = EXT()
    info.Basic.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    k.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info))
    k.AssignProcessToJobObject(job, k.GetCurrentProcess())
    globals()["_JOB_HANDLE"] = job  # keep the handle alive for the whole run


def install_exit_hooks():
    atexit.register(kill_children)

    def on_term(signum, frame):
        kill_children()
        raise SystemExit(128 + signum)

    for name in ("SIGTERM", "SIGBREAK"):
        if hasattr(signal, name):
            try:
                signal.signal(getattr(signal, name), on_term)
            except (ValueError, OSError):
                pass
    if os.name == "nt":
        try:
            _win_kill_on_close()
        except Exception:
            pass


# ──────────────────────────── temporary folders ────────────────────────────
TMP_MARK = ".dtv_tmp"
_LEGACY_TMP = re.compile(r"(_dtvtmp|_mptmp|_capture_tmp)$")


def make_tmp_dir(p: Path) -> Path:
    p.mkdir(parents=True, exist_ok=True)
    (p / TMP_MARK).write_text("created by dtv_tools; safe to delete\n", encoding="utf-8")
    return p


def safe_rmtree(p) -> bool:
    """Deletes a temporary folder only if dtv_tools created it (marker file or its own folder naming)."""
    p = Path(p)
    if not p.is_dir():
        return True
    if not ((p / TMP_MARK).is_file() or _LEGACY_TMP.search(p.name)):
        note(f"Folder not deleted for safety (it was not created by dtv_tools): {p}")
        return False
    shutil.rmtree(p, ignore_errors=True)
    return True


def src_sig(path) -> dict:
    st = os.stat(path)
    return {"path": str(Path(path).resolve()), "size": st.st_size, "mtime_ns": st.st_mtime_ns}


def tmp_session(tmp: Path, sig: dict) -> bool:
    """
    Prepares the temporary folder. If a previous (interrupted) run left intermediates made from a different source
    file or with different parameters, they are discarded. Returns True when the old intermediates can be reused.
    """
    sig_json = json.dumps(sig, sort_keys=True, default=str)
    f = tmp / "run_sig.json"
    reused = False
    if tmp.is_dir():
        if f.is_file() and f.read_text(encoding="utf-8", errors="replace") == sig_json:
            reused = True
        elif f.is_file() or any(x.name != TMP_MARK for x in tmp.iterdir()):
            note("Source file or parameters changed since the previous run (or its folder is unversioned): "
                 "discarding the old intermediate files.")
            if not safe_rmtree(tmp):
                raise Fail(f"Cannot reuse or clean the temporary folder: {tmp}")
    make_tmp_dir(tmp)
    f.write_text(sig_json, encoding="utf-8")
    return reused


def check_free_space(folder: Path, needed: int, what: str, hard=True):
    free = shutil.disk_usage(folder).free
    if free < needed:
        msg = f"Not enough free space for {what}: about {human(needed)} needed in {folder}, {human(free)} free."
        if hard:
            raise Fail(msg + " Free some space or use --tmpdir/--outdir on another disk.")
        note(msg)


# ──────────────────────────── external programs ────────────────────────────
TOOLS_DIR = Path(os.environ["DTV_TOOLS_DIR"]) if os.environ.get("DTV_TOOLS_DIR") else SCRIPT_DIR / "tools"
TOOL_DEFS = {
    "mp4box": {"label": "MP4Box", "names": ("MP4Box", "mp4box"), "flag": "--mp4box"},
    "ffmpeg": {"label": "ffmpeg", "names": ("ffmpeg",), "flag": "--ffmpeg"},
    "mpegh_ui": {"label": "mpeghUiManager", "names": ("mpeghUiManager",), "flag": "--mpegh-ui"},
    "mpegh_decoder": {"label": "mpeghDecoder", "names": ("mpeghDecoder",), "flag": "--mpegh-decoder"},
    "ffplay": {"label": "ffplay", "names": ("ffplay",), "flag": "--ffplay", "optional": True},      # only for the preview player
}
_TOOL_CACHE: dict = {}
_CFG_CACHE: dict = {}


def load_config() -> dict:
    """Optional dtv_tools.json next to the script: {"mp4box": "...", "ffmpeg": "...", "mpegh_ui": "...", "mpegh_decoder": "...", "ffplay": "..."}."""
    if "cfg" not in _CFG_CACHE:
        cfg = {}
        files = [CONFIG_PATH]
        if os.environ.get("DTV_TOOLS_CONFIG"):      # a controlling program's file wins, the one next to the script still fills the gaps
            files.insert(0, SCRIPT_DIR / "dtv_tools.json")
        for f in files:
            if f.is_file():
                try:
                    data = json.loads(f.read_text(encoding="utf-8"))
                    cfg.update({k: str(v) for k, v in data.items() if k in TOOL_DEFS and v})
                except (OSError, ValueError) as e:
                    note(f"{f.name} ignored (invalid): {e}")
        _CFG_CACHE["cfg"] = cfg
    return _CFG_CACHE["cfg"]


def find_tool(key: str, given: str = ""):
    """
    Locates an external program. Order: command-line option, dtv_tools.json, PATH, tools/ folder next to the script
    (and its direct sub-folders). An explicit option that does not exist is an error (no silent fall-through).
    Returns (absolute_path, source).
    """
    d = TOOL_DEFS[key]
    ck = (key, given or "")
    if ck in _TOOL_CACHE:
        return _TOOL_CACHE[ck]
    res = None
    if given:
        if Path(given).is_file():
            res = (str(Path(given).resolve()), "option")
        else:
            w = shutil.which(given)
            if w:
                res = (os.path.abspath(w), "option")
            else:
                raise Fail(f"{d['label']} not found at {d['flag']}: {given}")
    else:
        cfg = load_config().get(key)
        if cfg:
            if Path(cfg).is_file():
                res = (str(Path(cfg).resolve()), CONFIG_PATH.name)
            else:
                note(f"{CONFIG_PATH.name}: the path for '{key}' does not exist: {cfg}")
        if res is None:
            for n in d["names"]:
                w = shutil.which(n)
                if w:
                    res = (os.path.abspath(w), "PATH")
                    break
        if res is None and TOOLS_DIR.is_dir():
            folders = [TOOLS_DIR] + sorted(x for x in TOOLS_DIR.iterdir() if x.is_dir())
            for folder in folders:
                for n in d["names"]:
                    w = shutil.which(n, path=str(folder))
                    if w:
                        res = (os.path.abspath(w), f"tools/{folder.name}" if folder != TOOLS_DIR else "tools")
                        break
                if res:
                    break
    if res is None and key == "ffplay":                 # a build ships ffmpeg and ffplay together: look next to the ffmpeg in use
        try:
            ffdir = Path(find_tool("ffmpeg", "")[0]).parent
            for n in d["names"]:
                w = shutil.which(n, path=str(ffdir))
                if w:
                    res = (os.path.abspath(w), "ao lado do ffmpeg")
                    break
        except Fail:
            pass
    if res is None:
        raise Fail(f"{d['label']} not found. Searched: {d['flag']} option, {CONFIG_PATH.name}, PATH, {TOOLS_DIR}. "
                   "Run 'dtv_tools.py doctor' for details.")
    log(f"{d['label']}: {res[0]} ({res[1]})")
    _TOOL_CACHE[ck] = res
    return res


def optional_tool(key: str, given: str = ""):
    """Like find_tool but returns None when the program is simply not installed (an explicit bad option still fails)."""
    try:
        return find_tool(key, given or "")[0]
    except Fail:
        if given:
            raise
        return None


def tool_version(key: str, path: str) -> str:
    args = {"mp4box": [path, "-version"], "ffmpeg": [path, "-hide_banner", "-version"], "ffplay": [path, "-hide_banner", "-version"]}.get(key)
    if not args:
        return ""
    try:
        r = sp_run(args, capture_output=True, text=True, errors="replace", timeout=20, **NOWIN)
        txt = (r.stdout or "") + "\n" + (r.stderr or "")
        for ln in txt.splitlines():
            if "version" in ln.lower():
                return ln.strip()[:120]
        return next((ln.strip()[:120] for ln in txt.splitlines() if ln.strip()), "")
    except Exception as e:
        return f"(version unavailable: {e})"


_FF_CAPS: dict = {}


def ffmpeg_has_lcevc(ff: str) -> bool:
    if ff not in _FF_CAPS:
        try:
            r = sp_run([ff, "-hide_banner", "-filters"], capture_output=True, text=True, errors="replace", timeout=30, **NOWIN)
            _FF_CAPS[ff] = bool(re.search(r"\blcevc\b", (r.stdout or "") + (r.stderr or "")))
        except Exception:
            _FF_CAPS[ff] = False
    return _FF_CAPS[ff]


_FF_FILTERS: dict = {}


def ffmpeg_filters(exe: str) -> set:
    """Names of the filters of an ffmpeg/ffplay build (cached); empty when it cannot be listed."""
    if exe not in _FF_FILTERS:
        names = set()
        try:
            r = sp_run([exe, "-hide_banner", "-filters"], capture_output=True, text=True, errors="replace", timeout=30, **NOWIN)
            for ln in ((r.stdout or "") + "\n" + (r.stderr or "")).splitlines():
                m = re.match(r"\s*[T.][S.][C.]\s+(\S+)\s+\S+->\S+", ln)
                if m:
                    names.add(m.group(1))
        except Exception:
            pass
        _FF_FILTERS[exe] = names
    return _FF_FILTERS[exe]


def mp4box_major(version_line: str):
    m = re.search(r"GPAC version\s+(\d+)\.(\d+)", version_line or "")
    return (int(m.group(1)), int(m.group(2))) if m else None


_BOX_DIRS: list = []


def mp4box_title(text) -> str:
    """Value for MP4Box 'name=' : its option parser treats ':' '#' as separators and '%' '$' as escapes/templates."""
    t = re.sub(r"[\x00-\x1f]", " ", str(text))
    t = re.sub(r"\s*:\s*", " - ", t)
    t = t.translate({ord("#"): "-", ord("%"): "pct", ord("$"): "S"})
    return re.sub(r"\s+", " ", t).strip() or "audio"


class MP4BoxJob:
    """
    One MP4Box run in a private folder with neutral names (in0.ts, out0.mp4, ...) and that folder as the working
    directory, so no user path ever reaches MP4Box's argument parser. MP4Box reads ':' '#' as option separators in
    '-add' sources, '%XX' as URL escapes and '$...$' as templates (-out); spaces, '+', '&', '()', non-ASCII names also
    break on some platforms/builds. Inputs are exposed by hard link / symlink (copy as the last resort); outputs are
    written inside the folder and moved to their real path when MP4Box succeeds.
    """

    def __init__(self, near=None):
        self.dir = None
        cands = []
        if near:
            cands.append(Path(near).resolve().parent)       # same volume: hard links are free
        cands.append(Path(tempfile.gettempdir()))
        for c in cands:
            try:
                self.dir = Path(tempfile.mkdtemp(prefix=".dtvbox_", dir=str(c)))
                break
            except OSError:
                continue
        if self.dir is None:
            self.dir = Path(tempfile.mkdtemp(prefix="dtvbox_"))
        _BOX_DIRS.append(self.dir)
        self._n = 0
        self._outs = []

    @staticmethod
    def _suffix(path):
        sfx = Path(str(path)).suffix
        return sfx if re.fullmatch(r"\.[A-Za-z0-9]{1,8}", sfx) else ""

    def inp(self, path) -> str:
        """Neutral name (relative to self.dir) under which MP4Box sees the input file."""
        src = os.path.abspath(str(path))
        name = f"in{self._n}{self._suffix(src)}"
        self._n += 1
        dst = self.dir / name
        try:
            os.link(src, dst)
        except OSError:
            try:
                os.symlink(src, dst)
            except OSError:
                note(f"Could not link '{Path(src).name}'; copying it to a temporary folder for MP4Box.")
                shutil.copy2(src, dst)
        return name

    def inp_nhml(self, nhml_path, media_path) -> str:
        """NHML + the file its baseMediaFile points to; the reference is rewritten to the neutral name."""
        mname = self.inp(media_path)
        name = f"in{self._n}.nhml"
        self._n += 1
        txt = Path(nhml_path).read_text(encoding="utf-8")
        txt = re.sub(r'(baseMediaFile\s*=\s*")[^"]*(")', lambda m: m.group(1) + mname + m.group(2), txt, count=1)
        (self.dir / name).write_text(txt, encoding="utf-8")
        return name

    def out(self, final_path) -> str:
        name = f"out{len(self._outs)}{self._suffix(final_path) or '.mp4'}"
        self._outs.append((self.dir / name, Path(final_path)))
        return name

    def out_file(self, final_path) -> Path:
        """Where the (still not committed) output really is, for progress by size."""
        for tmp, fin in self._outs:
            if fin == Path(final_path):
                return tmp
        raise KeyError(final_path)

    def commit(self):
        for tmp, fin in self._outs:
            if tmp.is_file():
                fin.parent.mkdir(parents=True, exist_ok=True)
                fin.unlink(missing_ok=True)
                shutil.move(str(tmp), str(fin))

    def run(self, cmd, label, on_pct):
        tail = run_mp4box(cmd, label, str(self.dir), on_pct)
        self.commit()
        return tail

    def close(self):
        shutil.rmtree(self.dir, ignore_errors=True)
        try:
            _BOX_DIRS.remove(self.dir)
        except ValueError:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def mp4box_splitx(mp4box, src, t0, t1, out) -> bool:
    """MP4Box -splitx t0:t1 src -> out (any paths). True when MP4Box exited 0."""
    with MP4BoxJob(src) as box:
        i, o = box.inp(src), box.out(out)
        r = sp_run([mp4box, "-splitx", f"{t0:.2f}:{t1:.2f}", i, "-out", o],
                   capture_output=True, text=True, errors="replace", cwd=str(box.dir), **NOWIN)
        if r.returncode == 0:
            box.commit()
        return r.returncode == 0


def _cleanup_aliases():
    for d in list(_BOX_DIRS):
        shutil.rmtree(d, ignore_errors=True)


atexit.register(_cleanup_aliases)


def cmd_doctor(a):
    set_steps([("check", "Check the external programs")])
    set_stages({"check": 100})
    step("check", "running")
    needed_by = {
        "mp4box": "ts-tv3, mp4-cut, dash-capture, mpegh-scan, mpegh-audio, transcode (audio rendering)",
        "ffmpeg": "mpegh-audio, transcode, frame (required); LCEVC verification and decoding (needs a build with the 'lcevc' filter)",
        "mpegh_ui": "mpegh-scan, mpegh-audio, transcode (audio rendering)",
        "mpegh_decoder": "mpegh-audio, transcode (audio rendering)",
        "ffplay": "play (video preview in Cut MP4; optional)",
    }
    tools, rows, problems = [], [], []
    n_items = len(TOOL_DEFS) + 4        # programs + Python modules
    for idx, (key, d) in enumerate(TOOL_DEFS.items()):
        progress("check", idx / n_items, f"{d['label']}…", force=True)
        given = getattr(a, key, "") or ""
        entry = {"key": key, "name": d["label"], "needed_by": needed_by[key], "ok": False, "path": "", "source": "",
                 "version": "", "caps": {}, "optional": bool(d.get("optional"))}
        try:
            path, src = find_tool(key, given)
            entry.update(ok=True, path=path, source=src, version=tool_version(key, path))
            if key == "ffmpeg":
                entry["caps"]["lcevc_filter"] = ffmpeg_has_lcevc(path)
                have = ffmpeg_encoders(path)
                entry["caps"]["encoders"] = {k: e["ffmpeg"] in have for k, e in ENCODERS.items()}
            if key == "mp4box":
                mv = mp4box_major(entry["version"])
                entry["caps"]["vvc"] = bool(mv and mv[0] >= 2)
                if mv is None:
                    entry["caps"]["vvc"] = None
            say(f"{d['label']}: {path}  [{src}]" + (f"  {entry['version']}" if entry["version"] else ""), "ok")
            if key == "ffmpeg":
                say("  Video encoders: " + ", ".join(f"{ENCODERS[k]['ffmpeg']} {'ok' if v else 'no'}" for k, v in entry["caps"]["encoders"].items()))
            if key == "ffmpeg" and not entry["caps"]["lcevc_filter"]:
                say("  ffmpeg has no 'lcevc' filter: the LCEVC enhancement check will be skipped.", "warn")
            if key == "ffplay":
                entry["caps"]["lcevc_filter"] = "lcevc" in ffmpeg_filters(path)
            if key == "mp4box" and entry["caps"].get("vvc") is False:
                say("  MP4Box older than GPAC 2.0: VVC support is probably missing.", "warn")
        except Fail as e:
            say(f"{d['label']}: NOT FOUND ({e})" + (" (optional)" if d.get("optional") else ""), "" if d.get("optional") else "warn")
            problems.append(key)
        tools.append(entry)
        rows.append([d["label"], entry["path"] or "not found"])
    mods = {}
    mod_use = {"requests": "dash-capture", "rich": "dash-capture live panel", "numpy": "frame, sdr", "PIL": "frame, sdr (SDR images)"}
    for idx, (mod, use) in enumerate(mod_use.items()):
        progress("check", (len(TOOL_DEFS) + idx) / n_items, f"python: {mod}", force=True)
        try:
            __import__(mod)
            mods[mod] = True
        except ImportError:
            mods[mod] = False
        say(f"Python module '{mod}' ({use}): " + ("ok" if mods[mod] else "not installed (pip install " + ("pillow" if mod == "PIL" else mod) + ")"),
            "ok" if mods[mod] else "warn")
        rows.append([f"python:{mod}", "ok" if mods[mod] else "missing"])
    if sys.version_info < (3, 9):
        say(f"Python {sys.version.split()[0]} is too old (3.9+ required).", "err")
        problems.append("python")
    required_missing = [k for k in problems if k in ("mp4box", "python")]
    progress("check", 1.0, force=True)
    step("check", "done" if not required_missing else "error",
         "all required programs found" if not required_missing else "missing: " + ", ".join(required_missing))
    ev(type="result", out="", outputs=[], notes=NOTES, rows=rows, tools=tools, modules=mods, version=__version__,
       protocol=PROTOCOL_VERSION, features=list(FEATURES))
    return 2 if required_missing else 0




# ──────────────────────────── console / eventos ────────────────────────────
def init_console(no_color=False, ascii_only=False):
    global COLOR, ASCII
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    COLOR = bool(sys.stdout.isatty()) and not no_color and not os.environ.get("NO_COLOR") and not PJ
    if COLOR and os.name == "nt":
        try:
            import ctypes
            k = ctypes.windll.kernel32
            h = k.GetStdHandle(-11)
            mode = ctypes.c_uint()
            k.GetConsoleMode(h, ctypes.byref(mode))
            COLOR = bool(k.SetConsoleMode(h, mode.value | 0x0004))
        except Exception:
            COLOR = False
    ASCII = ascii_only or bool(os.environ.get("DTV_ASCII")) or (os.name == "nt" and not os.environ.get("WT_SESSION") and not os.environ.get("TERM_PROGRAM"))


def paint(code, text):
    return f"\033[{code}m{text}\033[0m" if COLOR else str(text)


bold = lambda t: paint("1", t)          # noqa: E731
dim = lambda t: paint("2", t)           # noqa: E731
red = lambda t: paint("31", t)          # noqa: E731
green = lambda t: paint("32", t)        # noqa: E731
yellow = lambda t: paint("33", t)       # noqa: E731
cyan = lambda t: paint("36", t)         # noqa: E731
ICON = lambda k: {"ok": "✔", "err": "✖", "warn": "⚠", "go": "▸", "dot": "·"}[k] if not ASCII else {"ok": "OK", "err": "X", "warn": "!", "go": ">", "dot": "-"}[k]   # noqa: E731


def ev(**kw):
    if PJ:
        if JSONL and kw.get("type") == "result":
            kw.setdefault("command", CMD)
        _emit(kw)


def _clear_bar():
    if _bar["active"] and not PJ:
        sys.stdout.write("\r\033[K" if COLOR else "\r" + " " * (shutil.get_terminal_size((100, 20)).columns - 1) + "\r")
        sys.stdout.flush()
    _bar["active"] = False


def say(msg="", kind=""):
    if JSONL:
        _emit({"type": "log", "level": {"warn": "warn", "err": "error", "ok": "ok"}.get(kind, "info"), "msg": str(msg)})
        return
    if PJ:
        _out(msg)
        return
    _clear_bar()
    if kind == "warn":
        msg = yellow(f"  {ICON('warn')} ") + msg
    elif kind == "err":
        msg = red(f"  {ICON('err')} ") + msg
    elif kind == "ok":
        msg = green(f"  {ICON('ok')} ") + msg
    _out(msg)


def log(msg=""):
    if JSONL:
        _emit({"type": "log", "level": "debug", "msg": str(msg)})
    elif PJ:
        _out(msg)
    elif VERBOSE:
        _clear_bar()
        _out(dim("    " + str(msg)))


DASH_LOG = None     # set by dash-capture on a terminal: warnings use its own log format
ORIG_LOOK = False   # dash-capture on a terminal: no step banners / progress bar (it has its own status panel)


def note(msg):
    if msg not in NOTES:
        NOTES.append(msg)
    if DASH_LOG is not None and not PJ:
        DASH_LOG("WARN", msg)
        return
    if JSONL:
        _emit({"type": "log", "level": "warn", "msg": str(msg), "note": True})
    elif PJ:
        _out("AVISO: " + msg)
    else:
        say(msg, "warn")


def fmt_time(s):
    s = float(s)
    return f"{s:.1f} s" if s < 60 else f"{int(s // 60)} min {int(s % 60):02d} s"


def fmt_hms(s):
    s = max(0, int(round(float(s or 0))))
    h, r = divmod(s, 3600)
    m, sec = divmod(r, 60)
    return f"{h:02d}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"


def step(sid, status, detail=""):
    global CUR_STEP
    if status == "running":
        CUR_STEP = sid
    ev(type="step", id=sid, status=status, detail=detail)
    if JSONL and sid in WEIGHTS and status in ("running", "done", "skipped"):
        # a stage always reports its edges, so even commands without finer progress (frame, doctor...) move the bars
        _stage_edge(sid, 0.0 if status == "running" else 1.0)
    if PJ or ORIG_LOOK:
        return
    labels = dict(STEP_DEFS)
    num = next((i + 1 for i, (k, _) in enumerate(STEP_DEFS) if k == sid), 0)
    label = labels.get(sid, sid)
    _clear_bar()
    if status == "running":
        STEP_T0[sid] = time.time()
        _out("\n" + cyan(ICON("go")) + " " + bold(f"[{num}/{len(STEP_DEFS)}] {label}") + (dim("  " + detail) if detail else ""))
    elif status == "done":
        el = time.time() - STEP_T0.get(sid, time.time())
        _out(green(f"  {ICON('ok')} ") + (detail or "concluído") + dim(f"  ({fmt_time(el)})"))
    elif status == "error":
        _out(red(f"  {ICON('err')} ") + (detail or "falhou"))
    elif status == "skipped":
        _out(dim(f"  {ICON('dot')} {detail or 'ignorado'}"))


def _stage_edge(stage, frac):
    done = 0.0
    for k in ORDER:
        if k == stage:
            break
        done += WEIGHTS.get(k, 0)
    ev(type="progress", stage=stage, frac=frac, pct=round(done + WEIGHTS.get(stage, 0) * frac, 1), detail="")


_last_prog = [0.0]


def progress(stage, frac, detail="", force=False):
    now = time.time()
    if not force and now - _last_prog[0] < 0.4:
        return
    _last_prog[0] = now
    done = 0.0
    for k in ORDER:
        if k == stage:
            break
        done += WEIGHTS.get(k, 0)
    frac = max(0.0, min(frac, 1.0))
    pct = done + WEIGHTS.get(stage, 0) * frac
    ev(type="progress", stage=stage, frac=round(frac, 4), pct=round(pct, 1), detail=detail)
    if PJ or ORIG_LOOK or not sys.stdout.isatty():
        return
    cols = shutil.get_terminal_size((100, 20)).columns
    width = 24
    filled = int(round(width * frac))
    bar = ("█" * filled + "░" * (width - filled)) if not ASCII else ("#" * filled + "-" * (width - filled))
    tail = f"  total {pct:3.0f}%"
    line = f"    {bar} {frac * 100:3.0f}%  {detail}"
    line = line[:max(10, cols - len(tail) - 2)]
    sys.stdout.write("\r\033[K" + (cyan(line.split("%")[0] + "%") + line.split("%", 1)[1] if COLOR else line) + dim(tail) if COLOR else "\r" + line.ljust(max(10, cols - len(tail) - 2)) + tail)
    sys.stdout.flush()
    _bar["active"] = True


def _locked(fn):
    def wrapper(*a, **k):
        with _LOCK:
            return fn(*a, **k)
    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    return wrapper


for _n in ("ev", "say", "log", "note", "step", "progress"):
    globals()[_n] = _locked(globals()[_n])


def human(n):
    n = float(n)
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or u == "TB":
            return f"{n:.0f} {u}" if u == "B" else f"{n:.2f} {u}"
        n /= 1024


def set_steps(defs):
    STEP_DEFS[:] = list(defs)
    STEP_T0.clear()


def set_stages(weights):
    """Declares the stage weights (percent of the total) and tells a controlling program the whole plan."""
    global WEIGHTS, ORDER
    WEIGHTS = dict(weights)
    ORDER = list(weights)
    if JSONL:
        ev(type="begin", command=CMD,
           steps=[{"id": k, "label": v, "weight": WEIGHTS.get(k, 0)} for k, v in STEP_DEFS])


# ──────────────────────────── MPEG-TS ────────────────────────────
def detect_packet(f):
    head = f.read(204 * 8 + 8)
    f.seek(0)
    for size, off in ((188, 0), (192, 4), (204, 0)):
        for start in range(0, min(size, 400)):
            if all(start + off + k * size < len(head) and head[start + off + k * size] == 0x47 for k in range(5)):
                return size, off, start
    raise Fail("Não encontrei pacotes MPEG-TS válidos (0x47 a cada 188 bytes). O arquivo é mesmo um TS?")


class TsReader:
    def __init__(self, path, start=0, limit=None, on_chunk=None):
        self.path, self.limit, self.on_chunk = path, limit, on_chunk
        self.size = os.path.getsize(path)
        self.resyncs = 0
        self.pos = 0
        with open(path, "rb") as f:
            self.psize, self.so, self.first = detect_packet(f)
        self.start = start or self.first

    def __iter__(self):
        ps, so = self.psize, self.so
        chunk_n = ps * 4096
        carry = b""
        with open(self.path, "rb") as f:
            f.seek(self.start)
            consumed = self.start
            while True:
                if self.limit is not None and consumed - self.start >= self.limit:
                    return
                data = f.read(chunk_n)
                if not data:
                    return
                buf = carry + data
                n, i = len(buf), 0
                while i + ps <= n:
                    if buf[i + so] != 0x47:
                        j = i + 1
                        while j + so + 2 * ps < n and not (buf[j + so] == 0x47 and buf[j + so + ps] == 0x47 and buf[j + so + 2 * ps] == 0x47):
                            j += 1
                        if j + so + 2 * ps >= n and len(data) == chunk_n:
                            break
                        self.resyncs += 1
                        i = j
                        continue
                    yield buf[i + so:i + so + 188]
                    i += ps
                carry = buf[i:]
                consumed += len(data)
                self.pos = min(consumed - len(carry), self.size)
                if self.on_chunk:
                    self.on_chunk(self)


class Sections:
    def __init__(self):
        self.buf = {}

    def feed(self, pid, pusi, payload):
        out = []
        if pusi:
            if not payload:
                return out
            ptr = payload[0]
            if pid in self.buf and ptr:
                self.buf[pid] += payload[1:1 + ptr]
                self._pop(pid, out)
            self.buf[pid] = bytearray(payload[1 + ptr:])
        elif pid in self.buf:
            self.buf[pid] += payload
        else:
            return out
        self._pop(pid, out)
        return out

    def _pop(self, pid, out):
        b = self.buf.get(pid)
        while b is not None and len(b) >= 3 and b[0] != 0xFF:
            need = 3 + (((b[1] & 0x0F) << 8) | b[2])
            if len(b) < need:
                break
            out.append(bytes(b[:need]))
            del b[:need]
            if not b or b[0] == 0xFF:
                self.buf.pop(pid, None)
                break


def parse_pat(sec):
    if len(sec) < 12 or sec[0] != 0x00 or not (sec[5] & 1):
        return {}
    progs = {}
    data = sec[8:-4]
    for i in range(0, len(data) - 3, 4):
        num = (data[i] << 8) | data[i + 1]
        pid = ((data[i + 2] & 0x1F) << 8) | data[i + 3]
        if num != 0:
            progs[num] = pid
    return progs


def parse_pmt(sec):
    if len(sec) < 16 or sec[0] != 0x02 or not (sec[5] & 1):
        return None
    prog = (sec[3] << 8) | sec[4]
    pil = ((sec[10] & 0x0F) << 8) | sec[11]
    pos, end = 12 + pil, len(sec) - 4
    streams = {}
    while pos + 5 <= end:
        stype = sec[pos]
        epid = ((sec[pos + 1] & 0x1F) << 8) | sec[pos + 2]
        eil = ((sec[pos + 3] & 0x0F) << 8) | sec[pos + 4]
        streams[epid] = (stype, bytes(sec[pos + 5:pos + 5 + eil]))
        pos += 5 + eil
    return prog, streams


def scan_psi(path, max_bytes=256 << 20):
    rd = TsReader(path, limit=max_bytes)
    sec = Sections()
    pat, pmts = {}, {}
    for pkt in rd:
        pid = ((pkt[1] & 0x1F) << 8) | pkt[2]
        afc = (pkt[3] >> 4) & 3
        if afc in (0, 2):
            continue
        if pid != 0 and pid not in pat.values():
            continue
        off = 5 + pkt[4] if afc == 3 else 4
        if off >= 188:
            continue
        for s in sec.feed(pid, bool(pkt[1] & 0x40), pkt[off:]):
            if pid == 0:
                for num, ppid in parse_pat(s).items():
                    pat[num] = ppid
            else:
                r = parse_pmt(s)
                if r:
                    pmts[r[0]] = r[1]
        if pat and all(n in pmts for n in pat):
            break
    if not pat:
        raise Fail("PAT não encontrada nos primeiros 256 MB: o arquivo não parece ser um TS com programas.")
    return {n: {"pmt_pid": pat[n], "streams": pmts.get(n, {})} for n in pat}


# ──────────────────────────── PES ────────────────────────────
def parse_ts(data, off):
    b0, b1, b2, b3, b4 = data[off:off + 5]
    return (((b0 >> 1) & 0x07) << 30) | (b1 << 22) | (((b2 >> 1) & 0x7F) << 15) | (b3 << 7) | ((b4 >> 1) & 0x7F)


NO_HEADER_IDS = {0xBC, 0xBE, 0xBF, 0xF0, 0xF1, 0xFF, 0xF2, 0xF8}
SEQ = itertools.count()


def parse_pes(pes):
    if len(pes) < 9 or pes[0:3] != b"\x00\x00\x01":
        return None, None, bytes(pes)
    if pes[3] in NO_HEADER_IDS:
        return None, None, bytes(pes[6:])
    flags = (pes[7] >> 6) & 3
    start = 9 + pes[8]
    pts = dts = None
    if flags == 2 and len(pes) >= 14:
        pts = dts = parse_ts(pes, 9)
    elif flags == 3 and len(pes) >= 19:
        pts, dts = parse_ts(pes, 9), parse_ts(pes, 14)
    return pts, dts, bytes(pes[start:])


class Es:
    def __init__(self, pid, stype, desc, path):
        self.pid, self.stype, self.desc, self.path = pid, stype, desc, path
        self.fh = open(path, "wb", buffering=1 << 20)
        self.buf = bytearray()
        self.index = []
        self.off = 0
        self.cc_errors = 0
        self.last_cc = None
        self.seen = None
        self.order = None

    def flush(self):
        if not self.buf:
            return
        pts, dts, data = parse_pes(self.buf)
        self.buf = bytearray()
        if not data:
            return
        self.fh.write(data)
        self.index.append((pts, dts, self.off, len(data)))
        self.off += len(data)
        if self.order is None:
            self.order = next(SEQ)

    def close(self):
        self.fh.close()


def demux(path, wanted, total, on_progress):
    t0 = time.time()

    def cb(rd):
        el = max(time.time() - t0, 1e-3)
        on_progress(rd.pos / max(total, 1), f"{human(rd.pos)} de {human(total)} · {rd.pos / el / 1048576:.0f} MB/s")

    rd = TsReader(path, on_chunk=cb)
    get = wanted.get
    for pkt in rd:
        pid = ((pkt[1] & 0x1F) << 8) | pkt[2]
        es = get(pid)
        if es is None:
            continue
        pusi = (pkt[1] >> 6) & 0x01
        afc = (pkt[3] >> 4) & 0x03
        cc = pkt[3] & 0x0F
        if es.seen is None:
            es.seen = next(SEQ)
        if afc in (0x01, 0x03) and pid not in (0x0000, 0x1FFF):
            if es.last_cc is not None:
                if cc != ((es.last_cc + 1) & 0x0F) and cc != es.last_cc:
                    es.cc_errors += 1
            es.last_cc = cc
        offset = 4
        if afc == 0x02:
            continue
        elif afc == 0x03:
            offset += 1 + pkt[4]
        if offset >= 188:
            continue
        if pusi:
            es.flush()
        es.buf += pkt[offset:]
    for es in sorted((e for e in wanted.values() if e.seen is not None), key=lambda e: e.seen):
        es.flush()
    for es in wanted.values():
        es.close()
    return rd


def read_frame(es, i, size=None):
    pts, dts, off, ln = es.index[i]
    with open(es.path, "rb") as f:
        f.seek(off)
        return f.read(ln if size is None else min(ln, size))


def is_length_prefixed_lcevc(payload: bytes) -> bool:
    if len(payload) < 6:
        return False
    n = int.from_bytes(payload[:4], "big")
    if n != len(payload) - 4:
        return False
    b0, b1 = payload[4], payload[5]
    lcevc_nut = (b0 >> 1) & 0x1F
    return (b0 & 0x80) == 0 and (b0 & 0x40) != 0 and b1 == 0xFF and lcevc_nut in (28, 29)


def lcevc_split_nals(payload: bytes) -> list:
    """Splits an LCEVC access unit into NAL units. Accepts 4-byte length-prefixed NAL(s) (MP4/TS-private) or Annex-B."""
    pos, total, nals = 0, len(payload), []
    while pos + 4 <= total:
        nlen = int.from_bytes(payload[pos:pos + 4], "big")
        if nlen <= 0 or pos + 4 + nlen > total:
            break
        nals.append(payload[pos + 4:pos + 4 + nlen])
        pos += 4 + nlen
    if pos == total and nals:
        return nals
    if payload.startswith(b"\x00\x00\x00\x01"):
        return [payload[4:]]
    if payload.startswith(b"\x00\x00\x01"):
        return [payload[3:]]
    return [payload]


def lcevc_annexb_payload(payload: bytes) -> bytes:
    """LCEVC NAL unit(s) joined by 3-byte start codes (what goes inside the SEI T.35 payload)."""
    return b"\x00\x00\x01".join(lcevc_split_nals(payload))


def classify_private_pid(stype: int, desc: bytes, frames: list) -> str:
    if stype == 0x33:
        return "VVC"
    if stype == 0x36:
        return "LCEVC"
    if stype in (0x2D, 0x2E):
        return "MPEGH"
    if b"VVC" in desc or b"vvc1" in desc:
        return "VVC"
    if b"LCEV" in desc or b"lvc1" in desc:
        return "LCEVC"
    if b"mhm1" in desc or b"mha1" in desc or b"\x7f" in desc:
        for i in range(len(desc) - 2):
            if desc[i] == 0x7F and desc[i + 2] == 0x0E:
                return "MPEGH"
    if not frames:
        return "EMPTY"
    sample_count = min(10, len(frames))
    annex_b_starts = mhas_sync_hits = vvc_nal_hits = lcevc_nal_hits = lcevc_lenpref_hits = 0
    for i in range(sample_count):
        payload = frames[i][2]
        if len(payload) < 6:
            continue
        if is_length_prefixed_lcevc(payload):
            lcevc_lenpref_hits += 1
            continue
        if b"\xC0\x01\xA5" in payload[:32]:
            mhas_sync_hits += 1
        sc_len = 4 if payload.startswith(b"\x00\x00\x00\x01") else (3 if payload.startswith(b"\x00\x00\x01") else 0)
        if sc_len > 0:
            annex_b_starts += 1
            b0, b1 = payload[sc_len], payload[sc_len + 1]
            vvc_nut = (b1 >> 3) & 0x1F
            lcevc_nut = (b0 >> 1) & 0x1F
            if lcevc_nut in (28, 29, 30, 31):
                lcevc_nal_hits += 1
            elif b0 == 0x00 and vvc_nut in (0, 1, 7, 8, 9, 12, 13, 14, 15, 16, 19, 20, 23):
                vvc_nal_hits += 1
    if lcevc_lenpref_hits > 0 and mhas_sync_hits == 0:
        return "LCEVC"
    if mhas_sync_hits > 0 or annex_b_starts == 0:
        return "MPEGH"
    if lcevc_nal_hits >= vvc_nal_hits and annex_b_starts > 0:
        return "LCEVC"
    if vvc_nal_hits > 0:
        return "VVC"
    return "UNKNOWN"


def create_vvc_sei_t35_nalu(lcevc_payload: bytes) -> bytes:
    clean_payload = b"\x00\x00\x01" + lcevc_annexb_payload(lcevc_payload)
    t35_header = bytes([0xB4, 0x00, 0x50, 0x00])
    sei_payload = t35_header + clean_payload
    payload_size = len(sei_payload)
    sei_msg = bytearray([0x04])
    while payload_size >= 255:
        sei_msg.append(0xFF)
        payload_size -= 255
    sei_msg.append(payload_size)
    sei_msg.extend(sei_payload)
    sei_msg.append(0x80)
    rbsp = bytearray()
    zero_count = 0
    for b in sei_msg:
        if zero_count == 2 and b in (0x00, 0x01, 0x02, 0x03):
            rbsp.append(0x03)
            zero_count = 0
        rbsp.append(b)
        zero_count = zero_count + 1 if b == 0x00 else 0
    return b"\x00\x00\x00\x01\x00\xB9" + bytes(rbsp)


def insert_sei_before_first_slice(au: bytes, sei_nal: bytes) -> bytes:
    i, starts = 0, []
    while True:
        i = au.find(b"\x00\x00\x01", i)
        if i < 0:
            break
        starts.append(i + 3)
        i += 3
    for s in starts:
        if s + 1 >= len(au):
            break
        nut = (au[s + 1] >> 3) & 0x1F
        if nut in (19, 23) or nut <= 11:
            cut = s - 3
            if cut > 0 and au[cut - 1] == 0:
                cut -= 1
            return au[:cut] + sei_nal + au[cut:]
    return sei_nal + au


def ensure_mhas_syncword(mhas_packet: bytes) -> bytes:
    return mhas_packet if mhas_packet.startswith(b"\xC0\x01\xA5") else b"\xC0\x01\xA5" + mhas_packet


def _xml_el(tag: str, attrs: list[tuple[str, object]], self_close: bool = True) -> str:
    body = " ".join(f'{k}="{v}"' for k, v in attrs)
    end = " />" if self_close else ">"
    return "<" + tag + (" " + body if body else "") + end


def write_lcevc_nhml(es, raw_bin_path: str, nhml_path: str, width=3840, height=2160):
    with open(raw_bin_path, "wb", buffering=1 << 20) as fbin, open(nhml_path, "w", encoding="utf-8") as fnhml, open(es.path, "rb") as src:
        fnhml.write('<?xml version="1.0" encoding="UTF-8" ?>\n')
        stream_attrs = [
            ("version", "1.0"),
            ("timeScale", "90000"),
            ("streamType", "4"),
            ("objectTypeIndication", "255"),
            ("specificInfoFile", ""),
            ("width", width),
            ("height", height),
            ("vendor", "VNOV"),
            ("codecVersion", "1"),
            ("codecRevision", "0"),
            ("compressorName", "LCEVC"),
            ("temporalQuality", "0"),
            ("spatialQuality", "0"),
            ("horizontalResolution", "4718592"),
            ("verticalResolution", "4718592"),
            ("bitDepth", "24"),
            ("mediaType", "vide"),
            ("mediaSubType", "lvc1"),
            ("baseMediaFile", raw_bin_path),
            ("inRootOD", "yes"),
            ("trackID", "2"),
        ]
        fnhml.write(_xml_el("NHNTStream", stream_attrs, self_close=False) + "\n")
        offset = 0
        base_dts = None
        for idx, (pts, dts, foff, flen) in enumerate(es.index):
            src.seek(foff)
            payload = src.read(flen)
            nals = lcevc_split_nals(payload)
            clean_nal = nals[0] if nals else b""
            sample_bytes = b"".join(len(n_).to_bytes(4, byteorder="big") + n_ for n_ in nals)
            sample_len = len(sample_bytes)
            fbin.write(sample_bytes)
            raw_dts = dts if dts is not None else (pts if pts is not None else idx * 1501)
            raw_pts = pts if pts is not None else raw_dts
            if base_dts is None:
                base_dts = raw_dts
            norm_dts = max(0, raw_dts - base_dts)
            cts_offset = max(0, raw_pts - raw_dts)
            lcevc_nut = (clean_nal[0] >> 1) & 0x1F if len(clean_nal) > 0 else 28
            is_rap = "yes" if (lcevc_nut == 29 or idx == 0) else "no"
            sample_attrs = [
                ("DTS", norm_dts),
                ("dataLength", sample_len),
                ("CTSOffset", cts_offset),
                ("isRAP", is_rap),
                ("mediaOffset", offset),
            ]
            fnhml.write("  " + _xml_el("NHNTSample", sample_attrs, self_close=True) + "\n")
            offset += sample_len
        fnhml.write("</NHNTStream>\n")


def verify_enhancement(ff: str, mp4_path: str, frames: int = 600, start=None):
    if not ff:
        say("ffmpeg da build DTV+ não configurado: verificação do enhancement ignorada.")
        return None
    if not ffmpeg_has_lcevc(ff):
        note("The ffmpeg in use has no 'lcevc' filter (a DTV+/LCEVC-enabled build is needed): enhancement check skipped.")
        return None
    cmd = [ff, "-hide_banner", "-loglevel", "debug", "-export_side_data", "enhancements",
           *(["-noaccurate_seek", "-ss", f"{start:.6f}"] if start is not None else []),
           "-i", mp4_path, "-map", "0:v:0", "-vf", "lcevc",
           "-frames:v", str(frames), "-f", "null", "-"]
    log("$ " + subprocess.list2cmdline(cmd))
    try:
        err = sp_run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", **NOWIN).stderr
    except Exception as e:
        note(f"Falha ao executar a verificação do enhancement: {e}")
        return None
    total = len(re.findall(r"hasEnhancement \d", err))
    hits = len(re.findall(r"hasEnhancement 1", err))
    sizes = sorted(set(re.findall(r"out PTS -?\d+, (\d+x\d+)", err)))
    where = "a partir do ponto escolhido" if start is not None else "no início do arquivo"
    miss = total - hits
    say(f"Verificação do LCEVC por amostragem (só os primeiros {frames} quadros pedidos, {where}; o arquivo inteiro não é decodificado): "
        f"{total} quadros analisados, {hits} com enhancement" + (f", {miss} sem" if total and miss else "") +
        f"; tamanho de saída: {', '.join(sizes) or 'n/d'}", "ok" if hits else "")
    if total and 0 < hits < total:
        note(f"{miss} dos {total} quadros da amostra vieram sem enhancement LCEVC. Se forem os primeiros, é a partida do filtro; "
             "se estiverem espalhados, faltou a camada nesses quadros.")
    if total == 0:
        note("O filtro lcevc do ffmpeg não processou nenhum quadro. Final do log: " + err[-300:].strip().replace("\n", " | "))
    elif hits == 0:
        note("No frame carried LCEVC enhancement data (is there an LCEVC layer in the file?).")
    return total, hits, sizes


# ──────────────────────────── MP4Box ────────────────────────────
def find_mp4box(given):
    return find_tool("mp4box", given or "")[0]


def run_mp4box(cmd, label, tmpdir, on_pct):
    log("$ " + subprocess.list2cmdline([str(c) for c in cmd]))
    env = dict(os.environ)
    p = sp_popen([str(c) for c in cmd], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                         cwd=tmpdir, env=env, **NOWIN)
    buf, tail = b"", []
    while True:
        chunk = p.stdout.read(512)
        if not chunk:
            break
        buf += chunk
        parts = re.split(rb"[\r\n]", buf)
        buf = parts.pop()
        for raw in parts:
            ln = raw.decode("utf-8", "replace").strip()
            if not ln:
                continue
            m = re.search(r"\((\d{1,3})/100\)", ln)
            if m:
                on_pct(int(m.group(1)) / 100.0, f"{label}: {m.group(1)}%")
                continue
            tail.append(ln)
            log("  " + ln)
    if buf.strip():
        tail.append(buf.decode("utf-8", "replace").strip())
    p.wait()
    if p.returncode != 0:
        why = next((t for t in reversed(tail) if re.search(r"error|invalid|fail|cannot|unsupported", t, re.I)), tail[-1] if tail else "")
        raise Fail(f"O MP4Box terminou com erro ({p.returncode}) em “{label}”: {why}"[:400])
    return tail


def unique_path(p: Path, overwrite):
    if overwrite or not p.exists():
        return p
    for i in range(1, 1000):
        q = p.with_name(f"{p.stem} ({i}){p.suffix}")
        if not q.exists():
            return q
    raise Fail("Não consegui criar um nome de saída livre.")


# ──────────────────────────── ISOBMFF (MP4) helpers ────────────────────────────
def mp4_iter(d, start=0, end=None):
    """Yields (type, payload_start, box_end) for the boxes found in d[start:end]."""
    end = len(d) if end is None else end
    pos = start
    while pos + 8 <= end:
        size = struct.unpack(">I", d[pos:pos + 4])[0]
        bt = d[pos + 4:pos + 8]
        hl = 8
        if size == 1:
            if pos + 16 > end:
                break
            size = struct.unpack(">Q", d[pos + 8:pos + 16])[0]
            hl = 16
        elif size == 0:
            size = end - pos
        if size < hl or pos + size > end:
            break
        yield bt, pos + hl, pos + size
        pos += size


def mp4_child(d, s, e, typ):
    for bt, cs, ce in mp4_iter(d, s, e):
        if bt == typ:
            return cs, ce
    return None


def mp4_read_moov(path):
    """Returns (moov_payload_bytes, payload_offset_in_file, top_level_boxes[(type, start, size)])."""
    file_size = os.path.getsize(path)
    boxes, moov = [], None
    with open(path, "rb") as f:
        pos = 0
        while pos + 8 <= file_size:
            f.seek(pos)
            hdr = f.read(16)
            if len(hdr) < 8:
                break
            size = struct.unpack(">I", hdr[:4])[0]
            hl = 8
            if size == 1:
                if len(hdr) < 16:
                    break
                size = struct.unpack(">Q", hdr[8:16])[0]
                hl = 16
            elif size == 0:
                size = file_size - pos
            if size < hl:
                break
            boxes.append((hdr[4:8], pos, size))
            if hdr[4:8] == b"moov" and moov is None:
                moov = (pos, hl, size)
            pos += size
        if moov is None:
            raise Fail("'moov' box not found: the file is not a regular MP4 (fragmented MP4 is not supported here).")
        f.seek(moov[0] + moov[1])
        data = f.read(moov[2] - moov[1])
    return data, moov[0] + moov[1], boxes


def _ver_off(d, p, v0, v1):
    return p + (v1 if d[p] == 1 else v0)


def mp4_tables(path):
    """Per track: sample tables (sizes, dts, ctts, sync, chunk offsets), edit list and codec info."""
    moov, moff, _ = mp4_read_moov(path)
    mv = mp4_child(moov, 0, len(moov), b"mvhd")
    mvts = struct.unpack(">I", moov[_ver_off(moov, mv[0], 12, 20):][:4])[0]
    tracks = []
    for bt, s, e in mp4_iter(moov):
        if bt != b"trak":
            continue
        tkhd = mp4_child(moov, s, e, b"tkhd")
        mdia = mp4_child(moov, s, e, b"mdia")
        if not tkhd or not mdia:
            continue
        tid = struct.unpack(">I", moov[_ver_off(moov, tkhd[0], 12, 20):][:4])[0]
        hdlr = mp4_child(moov, *mdia, b"hdlr")
        mdhd = mp4_child(moov, *mdia, b"mdhd")
        minf = mp4_child(moov, *mdia, b"minf")
        stbl = mp4_child(moov, *minf, b"stbl") if minf else None
        if not (hdlr and mdhd and stbl):
            continue
        handler = moov[hdlr[0] + 8:hdlr[0] + 12].decode("latin1")
        ts = struct.unpack(">I", moov[_ver_off(moov, mdhd[0], 12, 20):][:4])[0]
        tr = {"id": tid, "handler": handler, "ts": ts, "mvts": mvts, "elst": [], "fourcc": "?", "codec": None,
              "nal_len": None, "sizes": [], "dts": [], "ctts": None, "sync": None, "stsc": [], "chunk_off": [], "offs": None}
        stsd = mp4_child(moov, *stbl, b"stsd")
        if stsd:
            tr["fourcc"] = moov[stsd[0] + 12:stsd[0] + 16].decode("latin1")
            sd = moov[stsd[0]:stsd[1]]
            for fc, kind in ((b"vvcC", "vvc"), (b"hvcC", "hevc"), (b"avcC", "avc")):
                i = sd.find(fc)
                if i >= 0 and i + 4 + 24 <= len(sd):
                    pl = sd[i + 4:]
                    tr["codec"] = kind
                    tr["nal_len"] = {"vvc": ((pl[4] >> 1) & 3) + 1, "hevc": (pl[21] & 3) + 1, "avc": (pl[4] & 3) + 1}[kind]
                    break
        edts = mp4_child(moov, s, e, b"edts")
        elst = mp4_child(moov, *edts, b"elst") if edts else None
        if elst:
            es = elst[0]
            ver = moov[es]
            n = struct.unpack(">I", moov[es + 4:es + 8])[0]
            p = es + 8
            for _ in range(n):
                if ver == 1:
                    seg, mt = struct.unpack(">Qq", moov[p:p + 16])
                    tr["elst"].append({"seg": seg, "mt": mt, "off": moff + p + 8, "sz": 8})
                    p += 20
                else:
                    seg, mt = struct.unpack(">Ii", moov[p:p + 8])
                    tr["elst"].append({"seg": seg, "mt": mt, "off": moff + p + 4, "sz": 4})
                    p += 12
        box = {b2: (cs, ce) for b2, cs, ce in mp4_iter(moov, *stbl)}
        if b"stts" in box:
            p = box[b"stts"][0]
            n = struct.unpack(">I", moov[p + 4:p + 8])[0]
            t = 0
            for i in range(n):
                c, dl = struct.unpack(">II", moov[p + 8 + 8 * i:p + 16 + 8 * i])
                tr["dts"].extend(range(t, t + c * dl, dl) if dl else [t] * c)
                t += c * dl
            tr["dts_end"] = t
        if b"ctts" in box:
            p = box[b"ctts"][0]
            sgn = ">Ii" if moov[p] == 1 else ">II"
            n = struct.unpack(">I", moov[p + 4:p + 8])[0]
            lst = []
            for i in range(n):
                c, o = struct.unpack(sgn, moov[p + 8 + 8 * i:p + 16 + 8 * i])
                lst.extend([o] * c)
            tr["ctts"] = lst
        if b"stsz" in box:
            p = box[b"stsz"][0]
            const, cnt = struct.unpack(">II", moov[p + 4:p + 12])
            tr["sizes"] = [const] * cnt if const else list(struct.unpack(f">{cnt}I", moov[p + 12:p + 12 + 4 * cnt]))
        if b"stss" in box:
            p = box[b"stss"][0]
            n = struct.unpack(">I", moov[p + 4:p + 8])[0]
            tr["sync"] = [x - 1 for x in struct.unpack(f">{n}I", moov[p + 8:p + 8 + 4 * n])]
        if b"stsc" in box:
            p = box[b"stsc"][0]
            n = struct.unpack(">I", moov[p + 4:p + 8])[0]
            tr["stsc"] = [struct.unpack(">III", moov[p + 8 + 12 * i:p + 20 + 12 * i]) for i in range(n)]
        if b"stco" in box:
            p = box[b"stco"][0]
            n = struct.unpack(">I", moov[p + 4:p + 8])[0]
            tr["chunk_off"] = list(struct.unpack(f">{n}I", moov[p + 8:p + 8 + 4 * n]))
        elif b"co64" in box:
            p = box[b"co64"][0]
            n = struct.unpack(">I", moov[p + 4:p + 8])[0]
            tr["chunk_off"] = list(struct.unpack(f">{n}Q", moov[p + 8:p + 8 + 8 * n]))
        tracks.append(tr)
    return tracks


def mp4_sample_offsets(tr):
    if tr["offs"] is not None:
        return tr["offs"]
    sizes, stsc, co = tr["sizes"], tr["stsc"], tr["chunk_off"]
    offs, k, si = [], 0, 0
    for ci in range(len(co)):
        while si + 1 < len(stsc) and stsc[si + 1][0] <= ci + 1:
            si += 1
        o = co[ci]
        for _ in range(stsc[si][1]):
            if k >= len(sizes):
                break
            offs.append(o)
            o += sizes[k]
            k += 1
    tr["offs"] = offs
    return offs


def mp4_edit_info(tr):
    """(initial_delay_s, media_time, first_non_empty_elst_entry)."""
    empty, mt, ent = 0.0, 0, None
    for e in tr["elst"]:
        if e["mt"] == -1:
            empty += e["seg"] / tr["mvts"]
        else:
            mt, ent = e["mt"], e
            break
    return empty, mt, ent


def mp4_present(tr, k):
    """Presentation instant (s) of sample k: composition offset and edit list included."""
    empty, mt, _ = mp4_edit_info(tr)
    cts = tr["dts"][k] + (tr["ctts"][k] if tr["ctts"] else 0)
    return empty + (cts - mt) / tr["ts"]


def mp4_sync_kind(tr, f, k):
    """'IDR', 'CRA', 'GDR', 'BLA' or '?' from the NAL type of the first slice of sample k (VVC/HEVC/AVC)."""
    kind, nl = tr.get("codec"), tr.get("nal_len")
    if not kind or not nl:
        return "?"
    try:
        off = mp4_sample_offsets(tr)[k]
        size = tr["sizes"][k]
        pos = 0
        for _ in range(48):
            if pos + nl + 2 > size:
                break
            f.seek(off + pos)
            h = f.read(nl + 2)
            if len(h) < nl + 2:
                break
            ln = int.from_bytes(h[:nl], "big")
            b0, b1 = h[nl], h[nl + 1]
            if kind == "vvc":
                nut = b1 >> 3
                vcl, typ = nut <= 11, {7: "IDR", 8: "IDR", 9: "CRA", 10: "GDR"}.get(nut, "?")
            elif kind == "hevc":
                nut = (b0 >> 1) & 0x3F
                vcl, typ = nut < 32, {19: "IDR", 20: "IDR", 21: "CRA", 16: "BLA", 17: "BLA", 18: "BLA"}.get(nut, "?")
            else:
                nut = b0 & 0x1F
                vcl, typ = 1 <= nut <= 5, ("IDR" if nut == 5 else "?")
            if vcl:
                return typ
            pos += nl + ln
    except (OSError, IndexError):
        pass
    return "?"


# ──────────────────────────── MP4 timing from real PTS/DTS ────────────────────────────
class _Box:
    __slots__ = ("t", "data", "kids")

    def __init__(self, t, data=b"", kids=None):
        self.t, self.data, self.kids = t, data, kids


_TREE = {b"moov", b"trak", b"mdia", b"minf", b"stbl", b"edts"}


def _parse_tree(buf):
    out = []
    for bt, ps, be in mp4_iter(buf):
        if bt in _TREE:
            out.append(_Box(bt, b"", _parse_tree(buf[ps:be])))
        else:
            out.append(_Box(bt, buf[ps:be]))
    return out


def _build_tree(boxes):
    out = bytearray()
    for b in boxes:
        payload = _build_tree(b.kids) if b.kids is not None else b.data
        size = 8 + len(payload)
        if size > 0xFFFFFFFF:
            out += struct.pack(">I4sQ", 1, b.t, size + 8) + payload
        else:
            out += struct.pack(">I4s", size, b.t) + payload
    return bytes(out)


def _kid(box, typ):
    return next((k for k in box.kids if k.t == typ), None)


def _full(version, flags=0):
    return struct.pack(">I", (version << 24) | flags)


def _rle(values):
    out = []
    for v in values:
        if out and out[-1][1] == v:
            out[-1][0] += 1
        else:
            out.append([1, v])
    return out


def retime_mp4_tracks(path, specs, origin_sec):
    """
    Rewrites stts / ctts / elst / durations of video tracks of an MP4 made by MP4Box from a raw stream with a constant
    '-add ...:fps=' so that they follow the REAL decode/presentation timestamps of the source.
      specs: {track_id: (dts_seconds[], pts_seconds[])} - one entry per sample, decode order.
      origin_sec: presentation instant (same clock) that becomes time 0 of the file.
    Returns {track_id: 'ok' | reason it was left untouched}. Works in place when 'moov' is the last box; otherwise the file
    is rewritten through a temporary copy (the chunk offsets are fixed accordingly).
    """
    moov_buf, moov_off, top = mp4_read_moov(path)
    mbox = next((b for b in top if b[0] == b"moov"), None)
    moov_start, moov_size = mbox[1], mbox[2]
    tree = _parse_tree(moov_buf)
    mvhd = next(b for b in tree if b.t == b"mvhd")
    v = mvhd.data[0]
    mvts = struct.unpack(">I", mvhd.data[12:16] if v == 0 else mvhd.data[20:24])[0]
    result = {}
    moov_box = _Box(b"moov", b"", tree)
    track_durs = []
    for trak in [b for b in tree if b.t == b"trak"]:
        tkhd = _kid(trak, b"tkhd")
        tv = tkhd.data[0]
        tid = struct.unpack(">I", tkhd.data[12:16] if tv == 0 else tkhd.data[20:24])[0]
        mdia = _kid(trak, b"mdia")
        mdhd = _kid(mdia, b"mdhd")
        mv_ = mdhd.data[0]
        T0 = struct.unpack(">I", mdhd.data[12:16] if mv_ == 0 else mdhd.data[20:24])[0]
        T = max(T0, 90000)       # MP4Box uses the frame rate as timescale: too coarse for real (sub-frame, gap) timestamps
        stbl = _kid(_kid(mdia, b"minf"), b"stbl")

        def tkhd_dur():
            return struct.unpack(">I", tkhd.data[20:24])[0] if tv == 0 else struct.unpack(">Q", tkhd.data[28:36])[0]

        if tid not in specs:
            track_durs.append(tkhd_dur())
            continue
        dts_s, pts_s = specs[tid]
        stsz = _kid(stbl, b"stsz")
        n = struct.unpack(">I", stsz.data[8:12])[0]
        if n != len(dts_s) or n < 2:
            result[tid] = f"sample count differs (file {n}, source {len(dts_s)}); kept constant frame rate"
            track_durs.append(tkhd_dur())
            continue
        t0 = dts_s[0]
        dts_t = [int(round((x - t0) * T)) for x in dts_s]
        cts_t = [int(round((x - t0) * T)) for x in pts_s]
        deltas = [dts_t[i + 1] - dts_t[i] for i in range(n - 1)]
        if min(deltas) < 0:
            result[tid] = "decode timestamps go backwards; kept constant frame rate"
            track_durs.append(tkhd_dur())
            continue
        last = sorted(deltas)[len(deltas) // 2]
        deltas.append(last)
        # stts
        stts = _kid(stbl, b"stts")
        runs = _rle(deltas)
        stts.data = _full(0) + struct.pack(">I", len(runs)) + b"".join(struct.pack(">II", c, d) for c, d in runs)
        # ctts
        offs = [cts_t[i] - dts_t[i] for i in range(n)]
        old = _kid(stbl, b"ctts")
        if any(offs):
            ver = 1 if min(offs) < 0 else 0
            cr = _rle(offs)
            fmt = ">Ii" if ver else ">II"
            data = _full(ver) + struct.pack(">I", len(cr)) + b"".join(struct.pack(fmt, c, o) for c, o in cr)
            if old is not None:
                old.data = data
            else:
                stbl.kids.insert(stbl.kids.index(stts) + 1, _Box(b"ctts", data))
        elif old is not None:
            stbl.kids.remove(old)
        # edit list: empty edit for the presentation offset, then the media from its first composed frame
        mt = min(cts_t)
        start_pres = min(pts_s) - origin_sec
        empty = max(0, int(round(start_pres * mvts)))
        end_media = max(cts_t[i] + (deltas[i] if i < n else last) for i in range(n))
        seg = int(round((end_media - mt) * mvts / T))
        ents = ([(empty, -1)] if empty else []) + [(seg, mt)]
        if all(e[0] < 2 ** 31 and e[1] < 2 ** 31 for e in ents):
            elst_data = _full(0) + struct.pack(">I", len(ents)) + b"".join(struct.pack(">Ii", s_, m_) + struct.pack(">hh", 1, 0) for s_, m_ in ents)
        else:
            elst_data = _full(1) + struct.pack(">I", len(ents)) + b"".join(struct.pack(">Qq", s_, m_) + struct.pack(">hh", 1, 0) for s_, m_ in ents)
        edts = _kid(trak, b"edts")
        new_elst = _Box(b"elst", elst_data)
        if edts is None:
            trak.kids.insert(trak.kids.index(tkhd) + 1, _Box(b"edts", b"", [new_elst]))
        else:
            edts.kids = [new_elst]
        # durations (and the finer media timescale)
        if T != T0:
            if mv_ == 0:
                mdhd.data = mdhd.data[:12] + struct.pack(">I", T) + mdhd.data[16:]
            else:
                mdhd.data = mdhd.data[:20] + struct.pack(">I", T) + mdhd.data[24:]
        media_dur = dts_t[-1] + last
        trak_dur = empty + seg
        if mv_ == 0:
            mdhd.data = mdhd.data[:16] + struct.pack(">I", media_dur) + mdhd.data[20:]
        else:
            mdhd.data = mdhd.data[:24] + struct.pack(">Q", media_dur) + mdhd.data[32:]
        if tv == 0:
            tkhd.data = tkhd.data[:20] + struct.pack(">I", trak_dur) + tkhd.data[24:]
        else:
            tkhd.data = tkhd.data[:28] + struct.pack(">Q", trak_dur) + tkhd.data[36:]
        track_durs.append(trak_dur)
        result[tid] = "ok"
    if not any(r == "ok" for r in result.values()):
        return result
    movie_dur = max(track_durs) if track_durs else 0
    if v == 0:
        mvhd.data = mvhd.data[:16] + struct.pack(">I", movie_dur) + mvhd.data[20:]
    else:
        mvhd.data = mvhd.data[:24] + struct.pack(">Q", movie_dur) + mvhd.data[32:]

    file_size = os.path.getsize(path)
    mdat_after_moov = any(b[0] == b"mdat" and b[1] > moov_start for b in top)

    def fix_offsets(delta):
        for trak in [b for b in tree if b.t == b"trak"]:
            stbl = _kid(_kid(_kid(trak, b"mdia"), b"minf"), b"stbl")
            for kid in stbl.kids:
                if kid.t == b"stco":
                    n_ = struct.unpack(">I", kid.data[4:8])[0]
                    vals = [x + delta for x in struct.unpack(f">{n_}I", kid.data[8:8 + 4 * n_])]
                    if vals and max(vals) > 0xFFFFFFFF:
                        raise Fail("The file is too large to move its index: re-mux it with 64-bit offsets.")
                    kid.data = kid.data[:8] + struct.pack(f">{n_}I", *vals)
                elif kid.t == b"co64":
                    n_ = struct.unpack(">I", kid.data[4:8])[0]
                    vals = [x + delta for x in struct.unpack(f">{n_}Q", kid.data[8:8 + 8 * n_])]
                    kid.data = kid.data[:8] + struct.pack(f">{n_}Q", *vals)

    new_moov = _build_tree([moov_box])
    new_total = len(new_moov)
    if mdat_after_moov:
        fix_offsets(new_total - moov_size)
        new_moov = _build_tree([moov_box])
    if moov_start + moov_size == file_size:                      # moov is last: edit in place
        with open(path, "r+b") as f:
            f.seek(moov_start)
            f.write(new_moov)
            f.truncate()
    else:                                                          # moov first: rewrite through a temporary copy
        check_free_space(Path(path).parent, file_size + (1 << 24), "re-timing the MP4")
        tmp = path + ".retime.tmp"
        with open(path, "rb") as fi, open(tmp, "wb") as fo:
            for bt, st, sz in top:
                if bt == b"moov" and st == moov_start:
                    fo.write(new_moov)
                    continue
                fi.seek(st)
                left = sz
                while left > 0:
                    chunk = fi.read(min(left, 8 << 20))
                    if not chunk:
                        break
                    fo.write(chunk)
                    left -= len(chunk)
        os.replace(tmp, path)
    return result


def unwrap_index(es) -> int:
    """MPEG-TS PTS/DTS are 33-bit (they wrap every ~26.5 h): make them monotonic. Returns the number of wraps found."""
    if not es.index:
        return 0
    FULL, HALF = 1 << 33, 1 << 32
    st = {"p": [0, None], "d": [0, None]}
    wraps = 0

    def fix(v, key):
        nonlocal wraps
        if v is None:
            return None
        s_ = st[key]
        v += s_[0]
        if s_[1] is not None:
            if v < s_[1] - HALF:
                s_[0] += FULL
                v += FULL
                wraps += 1
            elif v > s_[1] + HALF:
                v -= FULL
        s_[1] = v
        return v

    es.index = [(fix(p, "p"), fix(d, "d"), o, ln) for p, d, o, ln in es.index]
    return wraps


def timing_lists(index, rate=90000.0, fps_hint=None):
    """
    (dts_seconds[], pts_seconds[]) per sample in decode order from a PES index [(pts, dts, off, len)].
    Missing timestamps are filled from the neighbours. Returns None when too many are missing.
    """
    n = len(index)
    if n < 2:
        return None
    pts = [x[0] for x in index]
    if all(x[1] is None for x in index) and all(p is not None for p in pts):
        srt = sorted(pts)                       # no DTS in the PES: rebuild a decode clock from the PTS...
        lag = max(0, max(a - b for a, b in zip(srt, pts)))   # ...moved back by the reorder delay so DTS <= PTS always holds
        dts = [x - lag for x in srt]
    else:
        dts = [x[1] if x[1] is not None else x[0] for x in index]
    miss = sum(1 for p, d in zip(pts, dts) if p is None or d is None)
    if miss > n * 0.05:
        return None
    good = [dts[i + 1] - dts[i] for i in range(n - 1) if dts[i] is not None and dts[i + 1] is not None and dts[i + 1] > dts[i]]
    step_ = sorted(good)[len(good) // 2] if good else int(rate / (fps_hint or 59.94))
    for i in range(n):
        if dts[i] is None:
            dts[i] = (dts[i - 1] + step_) if i else 0
        if pts[i] is None:
            pts[i] = dts[i]
    return [x / rate for x in dts], [x / rate for x in pts]


def av_delay_ms(first_audio_sec, origin_sec):
    """MP4Box ':delay=' value (ms) that places the first audio sample at its true offset from the video origin."""
    if first_audio_sec is None or origin_sec is None:
        return 0
    d = int(round((first_audio_sec - origin_sec) * 1000))
    return d if abs(d) >= 10 else 0


# ──────────────────────────── ts-tv3 ────────────────────────────
TS_STEPS = [("scan", "Ler a tabela de programas (PAT/PMT)"), ("demux", "Extrair as trilhas do TS"), ("classify", "Identificar VVC / LCEVC / MPEG-H"),
            ("pair", "Parear VVC e LCEVC"), ("build", "Montar os arquivos intermediários"), ("single", "Gerar o MP4 single-track (MP4Box)"),
            ("verify", "Verificar o enhancement LCEVC (ffmpeg)"), ("dual", "Gerar o MP4 dual-track (MP4Box)"), ("report", "Resumo")]
TS_WEIGHTS = {"scan": 2, "demux": 40, "classify": 1, "pair": 1, "build": 20, "single": 15, "verify": 6, "dual": 15, "report": 0}


def pid_arg(v):
    return None if v in (None, "") else int(str(v), 0)


def cmd_ts_tv3(a):
    src = Path(a.input).resolve()
    if not src.is_file():
        raise Fail(f"Arquivo não encontrado: {src}")
    outputs = [x for x in re.split(r"[,\s]+", a.outputs.lower()) if x]
    if not outputs or any(x not in ("single", "dual") for x in outputs):
        raise Fail("--outputs deve ser single, dual ou single,dual.")
    outdir = Path(a.outdir).resolve() if a.outdir else src.parent
    base = a.name.strip() if a.name else src.stem
    if BAD_NAME.search(base):
        raise Fail("O nome contém caracteres inválidos.")
    total = src.stat().st_size
    set_steps(TS_STEPS)
    set_stages(TS_WEIGHTS)

    say(bold(f"Arquivo: {src.name}") + dim(f"  ({human(total)})"))
    step("scan", "running")
    progress("scan", 0, force=True)
    programs = scan_psi(str(src))
    for num, pr in sorted(programs.items()):
        log(f"Programa {num} (PMT PID 0x{pr['pmt_pid']:04X}): " +
            (", ".join(f"0x{pid:04X}=0x{st:02X}" for pid, (st, _) in pr["streams"].items()) or "PMT não encontrada"))
    if a.program is not None:
        if a.program not in programs:
            raise Fail(f"Programa {a.program} não existe. Disponíveis: {', '.join(map(str, sorted(programs)))}.")
        prog = a.program
    else:
        prog = next(iter(programs))
        if len(programs) > 1:
            note(f"O TS tem {len(programs)} programas; usando o primeiro da PAT ({prog}). Use --program para escolher outro.")
    streams = programs[prog]["streams"]
    if not streams:
        raise Fail(f"Não encontrei a PMT do programa {prog} nos primeiros 256 MB do arquivo.")
    step("scan", "done", f"programa {prog} · {len(streams)} PID(s)")
    progress("scan", 1, force=True)

    if a.dry_run:
        for pid, (st, desc) in streams.items():
            say(f"PID 0x{pid:04X}  stream_type 0x{st:02X}  descritores: {desc.hex() or '-'}")
        ev(type="result", dry=True, out=str(outdir / f"{base}_single_track_4k.mp4"), outputs=[], notes=NOTES,
           rows=[["Programa", prog], ["PIDs", ", ".join(f"0x{p:04X}" for p in streams)]])
        return

    mp4box = find_mp4box(a.mp4box)
    ffmpeg_bin = optional_tool("ffmpeg", a.ffmpeg)
    outdir.mkdir(parents=True, exist_ok=True)
    tmp = (Path(a.tmpdir).resolve() / f"{base}_dtvtmp") if a.tmpdir else outdir / f".{base}_dtvtmp"
    try:
        fps_arg = float(a.fps) if a.fps else None
    except (TypeError, ValueError):
        raise Fail(f"--fps must be a number (got {a.fps!r}).")
    if fps_arg is not None and not (1 <= fps_arg <= 240):
        raise Fail("--fps out of range (1-240).")
    run_sig = {"v": 1, "src": src_sig(src), "prog": prog, "fps": fps_arg, "lcevc_size": a.lcevc_size,
               "pids": [str(getattr(a, k, "") or "") for k in ("vvc_pid", "lcevc_pid", "mpegh_pid")]}
    if not a.dry_run:
        tmp_session(tmp, run_sig)

    # Checkpoint de nomes de saída para manter os mesmos caminhos ao retomar
    plan_ckpt = tmp / "out_plan.json"
    out_paths = {}
    if plan_ckpt.is_file():
        try:
            saved_plan = json.loads(plan_ckpt.read_text(encoding="utf-8"))
            for k in outputs:
                if k in saved_plan:
                    out_paths[k] = Path(saved_plan[k])
        except Exception:
            out_paths.clear()
    if "single" in outputs and "single" not in out_paths:
        out_paths["single"] = unique_path(outdir / f"{base}_single_track_4k.mp4", a.overwrite)
    if "dual" in outputs and "dual" not in out_paths:
        out_paths["dual"] = unique_path(outdir / f"{base}_dual_track.mp4", a.overwrite)
    plan_ckpt.write_text(json.dumps({k: str(v) for k, v in out_paths.items()}, ensure_ascii=False), encoding="utf-8")
    ev(type="plan", out=str(next(iter(out_paths.values()))), outs=[str(p) for p in out_paths.values()])

    wanted = {}
    made = []
    verify = None
    succeeded = False
    try:
        demux_ckpt = tmp / "demux_state.json"
        demux_reused = False
        if demux_ckpt.is_file():
            try:
                st_data = json.loads(demux_ckpt.read_text(encoding="utf-8"))
                if st_data.get("prog") == prog and all((tmp / f"pid_{int(k):04X}.es").is_file() for k in st_data.get("pids", {})):
                    step("demux", "running")
                    for pid, (st, desc) in streams.items():
                        es_path = str(tmp / f"pid_{pid:04X}.es")
                        es = Es.__new__(Es)
                        es.pid, es.stype, es.desc, es.path = pid, st, desc, es_path
                        es.fh = None
                        es.buf = bytearray()
                        p_info = st_data["pids"].get(str(pid))
                        if p_info:
                            es.index = [tuple(x) for x in p_info["index"]]
                            es.off = p_info["off"]
                            es.cc_errors = p_info["cc_errors"]
                            es.order = p_info["order"]
                            es.seen = p_info["seen"]
                        else:
                            es.index, es.off, es.cc_errors, es.order, es.seen = [], 0, 0, None, None
                        wanted[pid] = es
                    demux_reused = True
                    frames_by_pid = sorted((e for e in wanted.values() if e.index), key=lambda e: e.order)
                    progress("demux", 1, "Reaproveitado do checkpoint anterior", force=True)
                    step("demux", "done", f"reaproveitado ({len(frames_by_pid)} trilha(s) em cache)")
            except Exception:
                wanted.clear()
                demux_reused = False

        if not demux_reused:
            free_tmp = shutil.disk_usage(tmp).free
            if free_tmp < total * 2.1:
                note(f"Pouco espaço na pasta temporária ({human(free_tmp)} livres; o ideal é ~{human(total * 2.1)}). Use --tmpdir em outro disco se faltar.")
            step("demux", "running")
            for pid, (st, desc) in streams.items():
                wanted[pid] = Es(pid, st, desc, str(tmp / f"pid_{pid:04X}.es"))
            log("Extraindo " + str(len(wanted)) + " PID(s): " + ", ".join(f"0x{p:04X}" for p in wanted))
            rd = demux(str(src), wanted, total, lambda f, d: progress("demux", f, d))
            if rd.resyncs:
                note(f"{rd.resyncs} perda(s) de sincronismo no TS (bytes ignorados até o próximo pacote válido).")
            progress("demux", 1, force=True)
            frames_by_pid = sorted((e for e in wanted.values() if e.index), key=lambda e: e.order)
            demux_ckpt.write_text(json.dumps({
                "prog": prog,
                "pids": {
                    str(e.pid): {
                        "index": e.index,
                        "off": e.off,
                        "cc_errors": e.cc_errors,
                        "order": e.order,
                        "seen": e.seen,
                    }
                    for e in wanted.values()
                }
            }), encoding="utf-8")
            step("demux", "done", f"{human(total)} lidos · {len(frames_by_pid)} trilha(s) com dados")

        for es_ in wanted.values():
            w_ = unwrap_index(es_)
            if w_:
                note(f"PID 0x{es_.pid:04X}: the 33-bit PTS/DTS counter wrapped {w_} time(s) (capture longer than ~26.5 h); timestamps were unwrapped.")

        step("classify", "running")
        vvc_pid = pid_arg(getattr(a, "vvc_pid", None))
        lcevc_pid = pid_arg(getattr(a, "lcevc_pid", None))
        mpegh_pid = pid_arg(getattr(a, "mpegh_pid", None))
        kinds = {}
        for es in frames_by_pid:
            sample = [(None, None, read_frame(es, i)) for i in range(min(10, len(es.index)))]
            kind = classify_private_pid(es.stype, es.desc, sample)
            total_kb = es.off / 1024.0
            pts_list = [f[0] for f in es.index if f[0] is not None]
            kinds[es.pid] = kind
            if kind == "VVC" and not vvc_pid:
                vvc_pid = es.pid
            elif kind == "LCEVC" and not lcevc_pid:
                lcevc_pid = es.pid
            elif kind == "MPEGH" and not mpegh_pid:
                mpegh_pid = es.pid
            say(f"PID 0x{es.pid:04X} (stream_type 0x{es.stype:02X}) → {bold(kind)}  ·  {len(es.index)} quadros, {total_kb / 1024:.2f} MB"
                + (f"  ·  duração {(max(pts_list) - min(pts_list)) / 90000.0:.2f} s" if pts_list else "  ·  sem PTS")
                + (red(f"  ·  {es.cc_errors} erro(s) de continuidade") if es.cc_errors else ""))
            if es.cc_errors:
                note(f"PID 0x{es.pid:04X}: {es.cc_errors} erro(s) de continuidade (pacotes perdidos na captura).")
        if not vvc_pid or not lcevc_pid:
            raise Fail("Não foi possível isolar VVC e LCEVC. Verifique os PIDs acima.")
        vvc, lcevc = wanted[vvc_pid], wanted[lcevc_pid]
        mpegh = wanted[mpegh_pid] if mpegh_pid and wanted[mpegh_pid].index else None
        step("classify", "done", "VVC + LCEVC" + (" + MPEG-H" if mpegh else ""))
        progress("classify", 1, force=True)

        step("pair", "running")
        lcevc_by_pts = {}
        for i, (p, _, _, _) in enumerate(lcevc.index):
            if p is not None:
                lcevc_by_pts[p] = i
        matched_pts = sum(1 for p, _, _, _ in vvc.index if p in lcevc_by_pts)
        say(f"Quadros VVC casados com LCEVC pelo PTS de 90 kHz: {matched_pts}/{len(vvc.index)}")
        use_index_fallback = matched_pts == 0
        if use_index_fallback:
            note("Nenhum PTS casou; usando pareamento por índice (pode desalinhar com B-frames).")
        step("pair", "done", f"{matched_pts}/{len(vvc.index)} quadros")
        progress("pair", 1, force=True)

        step("build", "running")
        merged = tmp / "trilha_combinada_sei.vvc"
        merged_done = tmp / "trilha_combinada_sei.vvc.done"
        raw_vvc = Path(vvc.path)
        if "single" in out_paths:
            if merged.is_file() and merged_done.is_file() and merged.stat().st_size > 0:
                progress("build", 0.8, "Trilha VVC+SEI reaproveitada do checkpoint", force=True)
            else:
                merged_done.unlink(missing_ok=True)
                nv = len(vvc.index)
                with open(merged, "wb", buffering=1 << 22) as fmerged, open(vvc.path, "rb") as fv, open(lcevc.path, "rb") as fl:
                    for idx, (pts, dts, off, ln) in enumerate(vvc.index):
                        fv.seek(off)
                        v_payload = fv.read(ln)
                        li = lcevc_by_pts.get(pts) if not use_index_fallback else (idx if idx < len(lcevc.index) else None)
                        l_data = None
                        if li is not None:
                            fl.seek(lcevc.index[li][2])
                            l_data = fl.read(lcevc.index[li][3])
                        if l_data:
                            sei = create_vvc_sei_t35_nalu(l_data)
                            fmerged.write(insert_sei_before_first_slice(v_payload, sei))
                        else:
                            fmerged.write(v_payload)
                        if idx % 256 == 0:
                            progress("build", 0.8 * idx / max(nv, 1), f"Injetando o LCEVC (SEI T.35) no VVC: {idx}/{nv}")
                merged_done.write_text("ok", encoding="utf-8")
        raw_mpegh = None
        if mpegh:
            raw_mpegh = tmp / "trilha_audio.mhas"
            mpegh_done = tmp / "trilha_audio.mhas.done"
            if raw_mpegh.is_file() and mpegh_done.is_file() and raw_mpegh.stat().st_size > 0:
                say("Áudio MPEG-H reaproveitado do checkpoint", "ok")
            else:
                mpegh_done.unlink(missing_ok=True)
                with open(raw_mpegh, "wb", buffering=1 << 20) as fmhas, open(mpegh.path, "rb") as fa:
                    for _, _, off, ln in mpegh.index:
                        fa.seek(off)
                        fmhas.write(ensure_mhas_syncword(fa.read(ln)))
                mpegh_done.write_text("ok", encoding="utf-8")
                say(f"Áudio MPEG-H salvo com syncwords MHAS ({len(mpegh.index)} quadros)", "ok")
        else:
            note("Nenhuma trilha MPEG-H identificada; os MP4 sairão sem áudio.")
        has_mpegh = raw_mpegh is not None
        vvc_pts = sorted([pts for pts, _, _, _ in vvc.index if pts is not None])
        if fps_arg:
            fps = fps_arg
        else:
            try:
                fps = round(90000.0 / ((vvc_pts[-1] - vvc_pts[0]) / (len(vvc_pts) - 1)), 3) if len(vvc_pts) > 10 else 59.94
            except ZeroDivisionError:
                fps = 59.94
        say(f"Frame rate usado: {fps} fps")
        v_timing = timing_lists(vvc.index, 90000.0, fps)
        l_timing = timing_lists(lcevc.index, 90000.0, fps)
        origin = min(v_timing[1]) if v_timing else None
        a_first = None
        if mpegh:
            a_pts = [p for p, _, _, _ in mpegh.index if p is not None]
            a_first = (min(a_pts) / 90000.0) if a_pts else None
        a_delay = av_delay_ms(a_first, origin)
        if a_first is not None and origin is not None:
            say(f"Audio starts {(a_first - origin) * 1000:+.0f} ms from the first video frame" + (f" (delay={a_delay} ms applied)" if a_delay else ""))
        if v_timing is None:
            note("Too many missing video timestamps: the MP4 keeps a constant frame rate (--fps).")
        step("build", "done")
        progress("build", 1, force=True)

        if "single" in out_paths:
            step("single", "running")
            single_done = tmp / "single.done"
            if out_paths["single"].is_file() and single_done.is_file() and out_paths["single"].stat().st_size > 0:
                made.append({"kind": "single", "title": "single-track (VVC + SEI LCEVC" + (" + MPEG-H)" if has_mpegh else ")"),
                             "path": str(out_paths["single"]), "size": out_paths["single"].stat().st_size})
                step("single", "done", out_paths["single"].name + f" · {human(made[-1]['size'])} (reaproveitado)")
                progress("single", 1, force=True)
            else:
                single_done.unlink(missing_ok=True)
                tmp_out = tmp / "saida_single.mp4"
                tmp_out.unlink(missing_ok=True)
                with MP4BoxJob(merged) as box:
                    cmd_single = [mp4box, "-add", f"{box.inp(merged)}:fps={fps}#video:id=1"]
                    if has_mpegh:
                        cmd_single += ["-add", f"{box.inp(raw_mpegh)}{':delay=' + str(a_delay) if a_delay else ''}#audio:id=2"]
                    cmd_single += ["-new", box.out(tmp_out)]
                    box.run(cmd_single, "single-track", lambda f, d: progress("single", f, d))
                if not tmp_out.is_file() or tmp_out.stat().st_size == 0:
                    raise Fail("O MP4Box não gerou o arquivo single-track.")
                if v_timing is not None:
                    specs_ = {1: v_timing}
                    rt = retime_mp4_tracks(str(tmp_out), specs_, origin)
                    bad = {k_: v_ for k_, v_ in rt.items() if v_ != "ok"}
                    if bad:
                        note("Real PTS timing not applied to some tracks: " + "; ".join(f"track {k_}: {v_}" for k_, v_ in bad.items()))
                    else:
                        log("single: timing rebuilt from the real PTS/DTS")
                shutil.move(str(tmp_out), str(out_paths["single"]))
                single_done.write_text("ok", encoding="utf-8")
                made.append({"kind": "single", "title": "single-track (VVC + SEI LCEVC" + (" + MPEG-H)" if has_mpegh else ")"),
                             "path": str(out_paths["single"]), "size": out_paths["single"].stat().st_size})
                step("single", "done", out_paths["single"].name + f" · {human(made[-1]['size'])}")
                progress("single", 1, force=True)
            step("verify", "running")
            verify = verify_enhancement(ffmpeg_bin, str(out_paths["single"]))
            step("verify", "done" if verify else "skipped", f"{verify[1]}/{verify[0]} quadros com enhancement" if verify else "ignorada")
            progress("verify", 1, force=True)
        else:
            step("single", "skipped", "não solicitado")
            step("verify", "skipped", "só com o single-track")

        if "dual" in out_paths:
            step("dual", "running")
            dual_done = tmp / "dual.done"
            if out_paths["dual"].is_file() and dual_done.is_file() and out_paths["dual"].stat().st_size > 0:
                made.append({"kind": "dual", "title": "dual-track (vvc1 + lvc1" + (" + mhm1)" if has_mpegh else ")"),
                             "path": str(out_paths["dual"]), "size": out_paths["dual"].stat().st_size})
                step("dual", "done", out_paths["dual"].name + f" · {human(made[-1]['size'])} (reaproveitado)")
                progress("dual", 1, force=True)
            else:
                dual_done.unlink(missing_ok=True)
                nhml_lcevc, bin_lcevc = tmp / "trilha_melhoria.nhml", tmp / "trilha_melhoria.bin"
                nhml_done = tmp / "trilha_melhoria.nhml.done"
                w, h = (int(x) for x in a.lcevc_size.lower().split("x"))
                if not (nhml_lcevc.is_file() and bin_lcevc.is_file() and nhml_done.is_file()):
                    nhml_done.unlink(missing_ok=True)
                    write_lcevc_nhml(lcevc, str(bin_lcevc), str(nhml_lcevc), w, h)
                    nhml_done.write_text("ok", encoding="utf-8")
                tmp_out = tmp / "saida_dual.mp4"
                tmp_out.unlink(missing_ok=True)
                with MP4BoxJob(raw_vvc) as box:
                    cmd_dual = [mp4box, "-add", f"{box.inp(raw_vvc)}:fps={fps}#video:id=1", "-add", f"{box.inp_nhml(nhml_lcevc, bin_lcevc)}#video:id=2"]
                    if has_mpegh:
                        cmd_dual += ["-add", f"{box.inp(raw_mpegh)}{':delay=' + str(a_delay) if a_delay else ''}#audio:id=3"]
                    cmd_dual += ["-new", box.out(tmp_out)]
                    box.run(cmd_dual, "dual-track", lambda f, d: progress("dual", f, d))
                if not tmp_out.is_file() or tmp_out.stat().st_size == 0:
                    raise Fail("O MP4Box não gerou o arquivo dual-track.")
                if v_timing is not None:
                    specs_ = {1: v_timing, 2: l_timing} if l_timing else {1: v_timing}
                    rt = retime_mp4_tracks(str(tmp_out), specs_, origin)
                    bad = {k_: v_ for k_, v_ in rt.items() if v_ != "ok"}
                    if bad:
                        note("Real PTS timing not applied to some tracks: " + "; ".join(f"track {k_}: {v_}" for k_, v_ in bad.items()))
                    else:
                        log("dual: timing rebuilt from the real PTS/DTS")
                shutil.move(str(tmp_out), str(out_paths["dual"]))
                dual_done.write_text("ok", encoding="utf-8")
                made.append({"kind": "dual", "title": "dual-track (vvc1 + lvc1" + (" + mhm1)" if has_mpegh else ")"),
                             "path": str(out_paths["dual"]), "size": out_paths["dual"].stat().st_size})
                step("dual", "done", out_paths["dual"].name + f" · {human(made[-1]['size'])}")
                progress("dual", 1, force=True)
        else:
            step("dual", "skipped", "não solicitado")

        step("report", "running")
        rows = [["Programa", str(prog)], ["Quadros VVC", str(len(vvc.index))], ["Frame rate", f"{fps} fps"],
                ["LCEVC pareado", f"{matched_pts}/{len(vvc.index)}" + (" (por índice)" if use_index_fallback else "")],
                ["Áudio MPEG-H", f"{len(mpegh.index)} quadros" if mpegh else "não encontrado"]]
        if verify:
            rows.append(["Enhancement LCEVC", f"{verify[1]}/{verify[0]} quadros"])
        ev(type="result", out=made[0]["path"], outputs=made, notes=NOTES, rows=rows,
           tracks=[{"pid": e.pid, "kind": kinds.get(e.pid, ""), "frames": len(e.index), "cc_errors": e.cc_errors} for e in frames_by_pid])
        step("report", "done")
        progress("report", 1, force=True)
        succeeded = True
    finally:
        for es in wanted.values():
            try:
                if getattr(es, "fh", None):
                    es.fh.close()
            except Exception:
                pass
        if not succeeded:
            say(f"Execução interrompida: arquivos intermediários preservados em {tmp} para permitir retomar.", "warn")
        elif a.keep_temp:
            say(f"Arquivos intermediários mantidos em: {tmp}")
        else:
            safe_rmtree(tmp)


# ──────────────────────────── mpegh-audio ────────────────────────────
CICP_CH = {2: 2, 6: 6, 16: 10, 19: 12}
CICP_NAME = {2: "2.0", 6: "5.1", 16: "5.1.4", 19: "7.1.4"}
FF_LAYOUT = {2: "stereo", 6: "5.1", 16: "5.1.4", 19: "12c"}      # CICP -> ffmpeg channel layout (same channel order)
LAYOUTS = {
    2: [("L", 30, 0), ("R", -30, 0)],
    6: [("L", 30, 0), ("R", -30, 0), ("C", 0, 0), ("LFE", 0, -1), ("Ls", 110, 0), ("Rs", -110, 0)],
    16: [("L", 30, 0), ("R", -30, 0), ("C", 0, 0), ("LFE", 0, -1), ("Ls", 110, 0), ("Rs", -110, 0),
         ("Ltf", 30, 1), ("Rtf", -30, 1), ("Ltr", 110, 1), ("Rtr", -110, 1)],
    19: [("L", 30, 0), ("R", -30, 0), ("C", 0, 0), ("LFE", 0, -1), ("Lrs", 135, 0), ("Rrs", -135, 0), ("Lss", 90, 0), ("Rss", -90, 0),
         ("Ltf", 45, 1), ("Rtf", -45, 1), ("Ltr", 135, 1), ("Rtr", -135, 1)],
}
FORMATS = {
    "aac": {"label": "AAC (.m4a)", "ext": ".m4a", "args": ["-c:a", "aac"], "br": "256k", "max": 8},
    "eac3": {"label": "E-AC-3 (.eac3)", "ext": ".eac3", "args": ["-c:a", "eac3"], "br": "640k", "max": 6},
    "flac": {"label": "FLAC (.flac)", "ext": ".flac", "args": ["-c:a", "flac"], "br": None, "max": 8},
    "wav": {"label": "WAV 24 bits (.wav)", "ext": ".wav", "args": ["-c:a", "pcm_s24le"], "br": None, "max": 64},
}
ELEM_RX = re.compile(r"^(non)?interactiveaudioelement(switch)?$", re.I)
MPEGH_FRAME_S = 1024 / 48000.0
MPEGH_STAGES = ("prepare", "decode", "check", "encode", "report")


def default_fmt(ch):
    return "aac" if ch <= 2 else ("eac3" if ch <= 6 else "wav")


def allowed_fmts(ch):
    return [k for k, v in FORMATS.items() if ch <= v["max"]]


def slug(s):
    import unicodedata
    s = unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode()
    return re.sub(r"[^A-Za-z0-9.]+", "_", s).strip("_") or "x"


def file_sig(path):
    st = os.stat(path)
    return hashlib.sha1(f"{os.path.abspath(path)}|{st.st_size}|{st.st_mtime_ns}".encode()).hexdigest()[:16]


def cache_root(given):
    return Path(given).resolve() if given else Path(tempfile.gettempdir()) / "dtv_audio_cache"


def purge_cache(root, days=14):
    try:
        for d in root.iterdir():
            if d.is_dir() and re.fullmatch(r"[0-9a-f]{16}", d.name) and time.time() - d.stat().st_mtime > days * 86400:
                shutil.rmtree(d, ignore_errors=True)
    except OSError:
        pass


def media_duration(ffmpeg, path):
    if not ffmpeg:
        return None
    try:
        r = sp_run([ffmpeg, "-hide_banner", "-i", str(path)], capture_output=True, text=True, errors="replace", timeout=60, **NOWIN)
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", r.stderr or "")
        return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)) if m else None
    except Exception:
        return None


_LABEL_KEY = {"MP4Box": "mp4box", "ffmpeg": "ffmpeg", "mpeghUiManager": "mpegh_ui", "mpeghDecoder": "mpegh_decoder"}


def need_tool(path, label):
    return find_tool(_LABEL_KEY[label], path or "")[0]


def prepare_audio(src, root, mp4box, stage="prepare", frac_span=(0.0, 1.0)):
    f0, f1 = frac_span
    progress(stage, f0, "Verificando cache do áudio MPEG-H", force=True)
    d = root / file_sig(src)
    d.mkdir(parents=True, exist_ok=True)
    out, ok = d / "audio.mp4", d / ".ok"
    if out.is_file() and ok.is_file():
        os.utime(d, None)
        progress(stage, f1, f"Áudio em cache ({human(out.stat().st_size)})", force=True)
        return d
    ok.unlink(missing_ok=True)
    part = d / "audio.partial.mp4"
    part.unlink(missing_ok=True)
    with open(src, "rb") as fh:
        head = fh.read(12)
    is_mp4 = head[4:8] in (b"ftyp", b"moov", b"mdat", b"styp", b"free", b"skip")
    with MP4BoxJob(src) as box:
        i_name = box.inp(src)
        box.run(
            [mp4box, "-add", f"{i_name}#audio" if is_mp4 else i_name, "-new", box.out(part)],
            "extração do áudio",
            lambda f, t: progress(stage, f0 + (f1 - f0) * f, t),
        )
    if not part.is_file() or part.stat().st_size == 0:
        raise Fail("O MP4Box não gerou o MP4 só com áudio. A entrada tem uma trilha de áudio MPEG-H (mhm1)?")
    part.replace(out)
    ok.write_text("ok")
    progress(stage, f1, f"Áudio extraído ({human(out.stat().st_size)})", force=True)
    return d


def _desc(node):
    ds = node.findall(".//customKind/description")
    if not ds:
        return ""
    pref = next((x for x in ds if x.get("isPreferred") == "true"), ds[0])
    return (pref.text or "").strip()


def parse_scenes(raw):
    """Lê todos os estados '[quadro] AudioSceneConfig' do -xmlSceneState."""
    import xml.etree.ElementTree as ET
    out = []
    parts = re.split(r"(?m)^[ \t]*\[(\d+)\]", raw)
    chunks = []
    if len(parts) > 1:
        for i in range(1, len(parts), 2):
            chunks.append((parts[i], parts[i + 1]))
    else:
        chunks.append(("0", raw))

    def first_root(text):
        text = re.sub(r"<\?xml[^>]*\?>", "", text).strip()
        marker = "<" + "AudioSceneConfig"
        if marker not in text:
            return None
        text = text[text.index(marker):]
        for _ in range(3):
            try:
                return ET.fromstring(text)
            except ET.ParseError as e:
                ln, col = e.position
                lines = text.split("\n")
                cut = sum(len(x) + 1 for x in lines[:ln - 1]) + col
                if "junk" not in str(e) or cut <= 0 or cut >= len(text):
                    raise Fail(f"XML da cena inválido: {e}")
                text = text[:cut]
        raise Fail("XML da cena inválido.")

    found = []
    for frame, body in chunks:
        root = first_root(body)
        if root is not None:
            found.append((frame, root))
    for frame, root in found:
        presets = []
        for p in root.iter("preset"):
            if p.get("isAvailable") == "false":
                continue
            pid = int(p.get("id"))
            els = []
            for ch in p:
                if ELEM_RX.match(ch.tag):
                    if ch.get("isAvailable") == "false":
                        continue
                    eid = int(ch.get("id"))
                    kn = ch.find("kind")
                    inter = not ch.tag.lower().startswith("noninteractive")
                    mute_ok = inter and any(x.tag.lower().startswith("muting") and x.get("isActionAllowed", "true").lower() != "false"
                                            for x in ch.iter())
                    els.append({
                        "id": eid,
                        "name": _desc(ch) or f"Grupo {eid}",
                        "kind": int(kn.get("code", "0")) if kn is not None else 0,
                        "interactive": inter,
                        "mute_ok": mute_ok,
                    })
            presets.append({
                "id": pid,
                "name": _desc(p) or f"Preset {pid}",
                "default": p.get("isDefault") == "true",
                "available": p.get("isAvailable") != "false",
                "elements": els
            })
        out.append({
            "frame": int(frame),
            "uuid": root.get("uuid", ""),
            "version": root.get("version", "11.1"),
            "config_changed": root.get("configChanged", "true").lower() != "false",
            "presets": presets
        })
    if not out:
        raise Fail("O XML da cena não tem nenhum AudioSceneConfig (o áudio é mesmo MPEG-H com cena de áudio?).")
    return out


def _key(scene):
    return tuple(
        (p["id"], p["name"], tuple((e["id"], e["name"]) for e in p["elements"]))
        for p in scene["presets"]
    )


def analyze_scenes(states, total_frames):
    """Unifica todos os presets e grupos encontrados em qualquer trecho do vídeo e monta a linha do tempo para o gráfico."""
    segs = []
    for i, s in enumerate(states):
        end = states[i + 1]["frame"] if i + 1 < len(states) else total_frames
        if total_frames and s["frame"] >= total_frames:
            continue
        if end is not None and total_frames:
            end = min(end, total_frames)
        segs.append({"start": s["frame"], "end": end, "key": _key(s), "i": i, "state": s})

    weight = {}
    for sg in segs:
        if sg["state"]["presets"]:
            weight[sg["key"]] = weight.get(sg["key"], 0) + (max(0, sg["end"] - sg["start"]) if sg["end"] is not None else 1)

    if weight:
        main_key = max(weight, key=lambda k: (len(k), weight[k]))
        main = next(s for s in states if _key(s) == main_key)
    else:
        main_key = _key(states[0])
        main = states[0]

    all_presets_map = {}
    all_groups_map = {}

    for sg in segs:
        s = sg["state"]
        dur_frames = max(1, (sg["end"] - sg["start"]) if sg["end"] is not None else 1)
        for p in s["presets"]:
            pid = p["id"]
            if pid not in all_presets_map:
                all_presets_map[pid] = {
                    "id": pid,
                    "names": {},
                    "default": False,
                    "available": True,
                    "elements_map": {},
                    "starts": [],
                }
            pm = all_presets_map[pid]
            pm["names"][p["name"]] = pm["names"].get(p["name"], 0) + dur_frames
            if p["default"]:
                pm["default"] = True
            pm["starts"].append(sg["start"])

            for e in p["elements"]:
                eid = e["id"]
                if eid not in pm["elements_map"]:
                    pm["elements_map"][eid] = {"id": eid, "names": {}, "kind": e["kind"], "interactive": e["interactive"], "mute_ok": False}
                if e.get("mute_ok"):
                    pm["elements_map"][eid]["mute_ok"] = True
                pm["elements_map"][eid]["names"][e["name"]] = pm["elements_map"][eid]["names"].get(e["name"], 0) + dur_frames

                if eid not in all_groups_map:
                    all_groups_map[eid] = {
                        "id": eid,
                        "names": {},
                        "kind": e["kind"],
                        "interactive": e["interactive"],
                        "presets": {},
                    }
                gm = all_groups_map[eid]
                gm["names"][e["name"]] = gm["names"].get(e["name"], 0) + dur_frames
                if e["kind"]:
                    gm["kind"] = e["kind"]
                if e["interactive"]:
                    gm["interactive"] = True
                gm["presets"][pid] = gm["presets"].get(pid, 0) + len(p["elements"])

    def _join_names(names_dict):
        ordered = [k for k, _ in sorted(names_dict.items(), key=lambda x: (-x[1], x[0]))]
        non_def = [k for k in ordered if k.lower() != "default"]
        if non_def and len(ordered) > 1:
            return " / ".join(non_def + [k for k in ordered if k.lower() == "default"])
        return " / ".join(ordered)

    unified_presets = []
    for pid in sorted(all_presets_map):
        pm = all_presets_map[pid]
        els = []
        for eid in sorted(pm["elements_map"]):
            em = pm["elements_map"][eid]
            els.append({
                "id": eid,
                "name": _join_names(em["names"]),
                "names": list(em["names"].keys()),
                "kind": em["kind"],
                "interactive": em["interactive"],
                "mute_ok": em.get("mute_ok", False),
            })
        unified_presets.append({
            "id": pid,
            "name": _join_names(pm["names"]),
            "names": list(pm["names"].keys()),
            "default": pm["default"],
            "available": True,
            "elements": els,
            "starts": sorted(set(pm["starts"])),
        })

    unified_groups = []
    for eid in sorted(all_groups_map):
        gm = all_groups_map[eid]
        best_pid = min(gm["presets"].keys(), key=lambda k: (len(all_presets_map[k]["elements_map"]), k))
        unified_groups.append({
            "id": eid,
            "name": _join_names(gm["names"]),
            "names": list(gm["names"].keys()),
            "kind": gm["kind"],
            "interactive": gm["interactive"],
            "preset_ids": sorted(gm["presets"].keys()),
            "carrier_preset": best_pid,
        })

    timeline = []
    for sg in segs:
        s = sg["state"]
        p_dict = {str(p["id"]): p["name"] for p in s["presets"]}
        g_dict = {}
        for p in s["presets"]:
            for e in p["elements"]:
                g_dict[str(e["id"])] = e["name"]
        timeline.append({
            "start": sg["start"],
            "end": sg["end"],
            "start_s": round(sg["start"] * MPEGH_FRAME_S, 2),
            "end_s": round(sg["end"] * MPEGH_FRAME_S, 2) if sg["end"] is not None else None,
            "uuid": s["uuid"],
            "has_scene": len(s["presets"]) > 0,
            "presets": p_dict,
            "groups": g_dict,
        })

    main_segs = [sg for sg in segs if sg["key"] == main_key]
    covered = sum(sg["end"] - sg["start"] for sg in main_segs if sg["end"] is not None)
    coverage = covered / total_frames if total_frames else None
    others = [{"start": sg["start"], "end": sg["end"], "presets": len(sg["state"]["presets"])} for sg in segs if sg["key"] != main_key]
    distinct_keys = {sg["key"] for sg in segs}

    return {
        "main": main,
        "unified_presets": unified_presets,
        "unified_groups": unified_groups,
        "timeline": timeline,
        "has_differences": len(distinct_keys) > 1 or len(segs) > 1,
        "starts": [sg["start"] for sg in segs if sg["state"]["presets"]],
        "coverage": coverage,
        "others": others,
        "changes": len(states),
        "total_frames": total_frames,
    }


ISO_TEXT = {"preset": "", "mute": " · isolado silenciando os demais elementos do preset",
            "edit": " · isolado com edição da cena"}


def option_layout(stream, native, forced, ids):
    """Layout in which a preset/group is rendered, from what the stream says about its audio elements.
    Channel bed(s) only -> the layout of that bed; objects only -> 2.0 (objects have no layout of their own); mixed or unknown -> native.
    Returns (cicp, text or '')."""
    els = (stream or {}).get("elements")
    if forced or not els or not ids or any(i not in els for i in ids):
        return native, ""
    ch = sum(els[i]["channels"] for i in ids)
    ob = sum(els[i]["objects"] for i in ids)
    if ch and not ob:
        pick = next((k for k, v in sorted(CICP_CH.items()) if v == ch), None)
        if pick is None:
            pick = native
        return pick, f"{ch} canais"
    if ob and not ch:
        return 2, f"{ob} objeto(s) de áudio, renderizado(s) em 2.0"
    return native, f"{ch} canais + {ob} objeto(s) mixados no layout do stream"


_SIDE_RX = re.compile(r"(?i)(?<![A-Za-zÀ-ÿ])(L|R|esq|dir|left|right|esquerd[oa]|direit[oa])(?![A-Za-zÀ-ÿ])")


def find_lr_pairs(groups):
    """Groups whose names differ only by a Left/Right marker (CAMPO L / CAMPO R) are the two halves of one stereo signal.
    -> [(left id, right id, base name)]"""
    def split(name):
        sides = {m.group(1).lower()[0] for m in _SIDE_RX.finditer(name)}
        if len(sides) != 1 or len(_SIDE_RX.findall(name)) != 1:
            return None
        base = re.sub(r"\s+", " ", _SIDE_RX.sub(" ", name)).strip(" -_.:/")
        return (base.lower(), "l" if sides == {"l"} or sides == {"e"} else "r" if sides == {"r"} or sides == {"d"} else None, base)
    seen, pairs = {}, []
    for g in groups:
        r = split(g["name"])
        if r and r[1]:
            seen.setdefault(r[0], {})[r[1]] = (g["id"], r[2])
    for v in seen.values():
        if "l" in v and "r" in v and v["l"][0] != v["r"][0]:
            pairs.append((v["l"][0], v["r"][0], v["l"][1] or "stereo"))
    return sorted(pairs)


def plan_isolation(presets, ids):
    """How to hear the audio elements `ids` alone. -> {"method", "preset", "others"}
    preset: a preset has nothing else; mute: the preset lets the others be switched off (action 40);
    edit: it does not (a preset always disables on/off for its members), so a temporary copy of the stream with an edited scene is decoded."""
    want = set(ids)
    best = None
    for p in presets:
        have = {e["id"] for e in p["elements"]}
        if not want <= have:
            continue
        others = [e["id"] for e in p["elements"] if e["id"] not in want]
        blocked = [e for e in p["elements"] if e["id"] not in want and not e.get("mute_ok")]
        method = "preset" if not others else "mute" if not blocked else "edit"
        key = ({"preset": 0, "mute": 1, "edit": 2}[method], len(p["elements"]), p["id"])
        if best is None or key < best[0]:
            best = (key, {"method": method, "preset": p["id"], "others": others})
    return best[1] if best else None


def isolation(presets, ids):
    """(can be isolated by the script alone?, text)."""
    plan = plan_isolation(presets, ids)
    if plan is None:
        return False, "não está em nenhum preset"
    if plan["method"] == "edit":
        return False, f"o preset {plan['preset']} não permite silenciar G" + ", G".join(map(str, plan["others"]))
    return True, ""


def build_catalog(info, native=2, stream=None, forced=False, compat=False):
    """Monta todas as opções de Presets e Grupos encontrados ao longo do vídeo (sem isolamento por diferença)."""
    if native not in CICP_CH:
        raise Fail(f"Layout nativo inválido: {native}. Use 2, 6, 16 ou 19.")
    cm = (lambda c: 6 if c == 16 else c) if compat else (lambda c: c)
    native = cm(native)
    pres = info.get("unified_presets") or sorted(info["main"]["presets"], key=lambda p: p["id"])
    groups = info.get("unified_groups") or []
    warnings = []
    if not pres:
        raise Fail("Nenhum preset encontrado em toda a cena de áudio.")
    opts = []

    def add(**o):
        ch = o["out_ch"]
        o.setdefault("fmt", default_fmt(ch))
        o["formats"] = allowed_fmts(ch)
        o["layout_orig"] = o["layout"]                              # the track's own layout: always shown as "original"
        o["layout_choices"] = [o["layout"]] if o["id"].startswith("pair:") else sorted({2, 6, o["layout"]})
        opts.append(o)

    for p in pres:
        names = " + ".join(f"G{e['id']} ({e['name']})" for e in p["elements"])
        lay, why = option_layout(stream, native, forced, [e["id"] for e in p["elements"]])
        lay = cm(lay)
        names = f"{names} · {why}" if why else names
        add(
            composition=why,
            id=f"preset:{p['id']}",
            family=f"preset:{p['id']}",
            group="presets",
            preset=p["id"],
            layout=lay,
            heuristic=False,
            title=p['name'],
            desc=names or "Preset da transmissão",
            inputs=[[f"p{p['id']}", lay]],
            expr=None,
            out="direct",
            out_ch=CICP_CH[lay],
            src_cicp=lay,
            highlight=[list(range(CICP_CH[lay]))],
            stem=f"Preset_{p['id']}_{slug(p['name'])}_{CICP_NAME[lay]}",
        )

    for g in groups:
        preset_list = ", ".join(f"Preset {pid}" for pid in g["preset_ids"])
        lay, why = option_layout(stream, native, forced, [g["id"]])
        lay = cm(lay)
        plan = plan_isolation(pres, [g["id"]]) or {"method": "preset", "preset": g["carrier_preset"], "others": []}
        add(
            isolation=plan["method"],
            iso_preset=plan["preset"],
            iso_keep=[g["id"]],
            composition=why,
            id=f"group:{g['id']}",
            family=f"group:{g['id']}",
            group="groups",
            element=g["id"],
            carrier_preset=g["carrier_preset"],
            preset_ids=g["preset_ids"],
            layout=lay,
            heuristic=False,
            title=g['name'],
            desc=f"Presente em: {preset_list}" + (f" · {why}" if why else "") + ISO_TEXT[plan["method"]],
            inputs=[[f"g{g['id']}", lay]],
            expr=None,
            out="direct",
            out_ch=CICP_CH[lay],
            src_cicp=lay,
            highlight=[list(range(CICP_CH[lay]))],
            stem=f"Grupo_{g['id']}_{slug(g['name'])}_{CICP_NAME[lay]}",
        )

    for lid, rid, base in find_lr_pairs(groups):
        plan = plan_isolation(pres, [lid, rid]) or {"method": "preset", "preset": 0, "others": []}
        add(
            id=f"pair:{lid}+{rid}",
            family=f"pair:{lid}+{rid}",
            group="groups",
            elements=[lid, rid],
            carrier_preset=plan["preset"],
            isolation=plan["method"],
            iso_preset=plan["preset"],
            iso_keep=[lid, rid],
            preset_ids=sorted({pid for g in groups if g["id"] in (lid, rid) for pid in g["preset_ids"]}),
            composition="par estéreo L+R",
            layout=2,
            heuristic=False,
            title=f"{base}",
            desc=f"Par estéreo: G{lid} (L) + G{rid} (R)" + ISO_TEXT[plan["method"]],
            inputs=[[f"m{lid}-{rid}", 2]],
            expr=None,
            out="direct",
            out_ch=2,
            src_cicp=2,
            highlight=[[0, 1]],
            stem=f"Par_{lid}-{rid}_{slug(base)}_2.0",
        )

    return {"options": opts, "warnings": warnings, "native": native}


def catalog_public():
    return {str(k): {"name": CICP_NAME[k], "channels": [{"n": n, "az": az, "el": el} for n, az, el in v]} for k, v in LAYOUTS.items()}


def load_scene(d, ui, audio, stage="scene", frac_span=(0.0, 1.0), quiet=False):
    f0, f1 = frac_span
    progress = (lambda *a, **k: None) if quiet else globals()["progress"]
    xmlp = d / "scene.xml"
    if not xmlp.is_file() or xmlp.stat().st_size == 0:
        tmp_out = d / "_ui.mp4"
        tmp_out.unlink(missing_ok=True)
        audio_sz = max(1, audio.stat().st_size if audio.is_file() else 1)
        cmd = [ui, "-if", str(audio), "-of", str(tmp_out), "-xmlSceneState", str(xmlp)]
        log("$ " + subprocess.list2cmdline(cmd))
        progress(stage, f0 + (f1 - f0) * 0.05, "Iniciando leitura da cena MPEG-H (mpeghUiManager)…", force=True)
        p = sp_popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, **NOWIN)
        drain = Drain(p.stdout)
        t0 = time.time()
        while p.poll() is None:
            time.sleep(0.35)
            el = time.time() - t0
            out_sz = tmp_out.stat().st_size if tmp_out.is_file() else 0
            xml_sz = xmlp.stat().st_size if xmlp.is_file() else 0
            if out_sz > 0:
                sub_f = min(0.92, out_sz / audio_sz)
            else:
                # Estimativa baseada no tempo e crescimento do XML quando _ui.mp4 é bufferizado
                sub_f = min(0.90, 1.0 - (1.0 / (1.0 + el * 0.25)))
            detail = f"Lendo estados da cena MPEG-H… {fmt_time(el)}" + (f" · XML {human(xml_sz)}" if xml_sz else "")
            progress(stage, f0 + (f1 - f0) * sub_f, detail)
        out_raw = drain.text()
        tmp_out.unlink(missing_ok=True)
        if p.returncode != 0 or not xmlp.is_file():
            raise Fail("O mpeghUiManager falhou: " + (out_raw.strip().splitlines() or ["sem mensagem"])[-1][:200])
    progress(stage, f0 + (f1 - f0) * 0.95, "Interpretando XML da cena de áudio…", force=True)
    res = parse_scenes(xmlp.read_text(encoding="utf-8", errors="replace"))
    progress(stage, f1, f"{len(res)} estado(s) de cena lido(s)", force=True)
    return res


MP_SCAN_STEPS = [("prepare", "Extrair só o áudio MPEG-H (MP4Box)"), ("scene", "Ler as cenas de áudio (mpeghUiManager)"), ("catalog", "Montar as opções de saída")]
MP_STEPS = [("prepare", "Preparar o áudio e a cena"), ("decode", "Decodificar os presets e grupos (em paralelo)"), ("check", "Verificar os arquivos decodificados"),
            ("encode", "Gerar os arquivos de áudio (ffmpeg)"), ("mux", "Juntar ao vídeo (MP4Box)"), ("report", "Resumo")]


class _Bits:
    """MSB-first bit reader (MPEG-H syntax)."""

    def __init__(self, data):
        self.b, self.p = data, 0

    def u(self, n):
        v = 0
        for _ in range(n):
            if self.p >> 3 >= len(self.b):
                raise EOFError
            v = (v << 1) | ((self.b[self.p >> 3] >> (7 - (self.p & 7))) & 1)
            self.p += 1
        return v

    def esc(self, a, b, c):
        v = self.u(a)
        if v == (1 << a) - 1:
            x = self.u(b)
            v += x
            if x == (1 << b) - 1:
                v += self.u(c)
        return v


def _mp4_sample_table(path):
    """[(file offset, size)] of every sample of the first track (stsz + stsc + stco/co64). Reads only the index, via mmap."""
    import mmap
    with open(path, "rb") as f:
        try:
            data = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
        except (ValueError, OSError):
            return []
        try:
            def boxes(s, e):
                p = s
                while p + 8 <= e:
                    sz, t = struct.unpack(">I4s", data[p:p + 8])
                    h = 8
                    if sz == 1:
                        sz, h = struct.unpack(">Q", data[p + 8:p + 16])[0], 16
                    elif sz == 0:
                        sz = e - p
                    if sz < h:
                        return
                    yield t, p + h, p + sz
                    p += sz

            def child(s, e, name):
                return next(((a, z) for t, a, z in boxes(s, e) if t == name), None)

            rng = (0, len(data))
            for name in (b"moov", b"trak", b"mdia", b"minf", b"stbl"):
                rng = child(*rng, name)
                if rng is None:
                    return []
            sizes, stsc, offs = [], [], []
            for t, x, y in boxes(*rng):
                if t == b"stsz":
                    ss, n = struct.unpack(">II", data[x + 4:x + 12])
                    sizes = [ss] * n if ss else list(struct.unpack(f">{n}I", data[x + 12:x + 12 + 4 * n]))
                elif t == b"stsc":
                    n = struct.unpack(">I", data[x + 4:x + 8])[0]
                    stsc = [struct.unpack(">III", data[x + 8 + 12 * i:x + 20 + 12 * i]) for i in range(n)]
                elif t in (b"stco", b"co64"):
                    n = struct.unpack(">I", data[x + 4:x + 8])[0]
                    w = 4 if t == b"stco" else 8
                    offs = list(struct.unpack(f">{n}{'I' if w == 4 else 'Q'}", data[x + 8:x + 8 + w * n]))
        finally:
            data.close()
    table, idx = [], 0
    for ci, off in enumerate(offs, 1):
        per = [spc for first, spc, _ in stsc if first <= ci][-1] if stsc else 1
        for _ in range(per):
            if idx >= len(sizes):
                return table
            table.append((off, sizes[idx]))
            off += sizes[idx]
            idx += 1
    return table


def _mp4_samples(path, limit):
    """Yields the bytes of the first `limit` samples of the first track."""
    table = _mp4_sample_table(path)[:limit]
    with open(path, "rb") as f:
        for off, size in table:
            f.seek(off)
            yield f.read(size)


def mhas_packets(smp):
    """(type, payload start, length) of the MHAS packets in a sample. Fast path for the usual 16-bit header."""
    pos, n = 0, len(smp)
    while n - pos > 2:
        v = (smp[pos] << 8) | smp[pos + 1]
        ptype, label, ln = v >> 13, (v >> 11) & 3, v & 0x7FF
        if ptype == 7 or label == 3 or ln == 0x7FF:             # escaped values: slow path
            r = _Bits(smp)
            r.p = pos * 8
            ptype = r.esc(3, 8, 8)
            r.esc(2, 8, 32)
            ln = r.esc(11, 24, 24)
            start = (r.p + 7) // 8
        else:
            start = pos + 2
        yield ptype, start, ln
        pos = start + ln


def mhas_stream_config(path, limit=600):
    """Reads the in-band MPEG-H data of an 'mhm1' track (it has no 'mhaC' box): the configuration (MHAS packet 1: reference layout
    and signal groups = channel beds / objects) and the audio scene info (packet 3: which signals each audio element uses).
    Returns None when the configuration cannot be read."""
    cfg = scene = None
    try:
        for i, smp in enumerate(_mp4_samples(path, limit)):
            for ptype, start, ln in mhas_packets(smp):
                if ptype == 1 and cfg is None:
                    cfg = _parse_3da_config(smp[start:start + ln], i)
                    if cfg is None:
                        return None
                elif ptype == 3 and cfg is not None and scene is None:
                    scene = _parse_mae_groups(smp[start:start + ln])
            if cfg is not None and (scene is not None or i > cfg["sample"] + 200):
                break
    except (EOFError, struct.error, OSError, IndexError):
        pass
    if cfg is not None:
        cfg["elements"] = _element_channels(cfg, scene)
    return cfg


def _asi_layout(payload):
    """Bit positions inside an mae_AudioSceneInfo payload: each group's default on/off bit and each preset condition's reference-ID field."""
    c = _Bits(payload)
    if not c.u(1):                                   # not the main stream
        return None
    if c.u(1):
        c.u(8)
    groups = []
    for _ in range(c.u(7)):
        gid = c.u(7)
        c.u(1)
        default_pos = c.p
        c.u(1)
        pos, gain = c.u(1), c.u(1)
        if pos:
            for n in (7, 7, 5, 5, 4, 4):
                c.u(n)
        if gain:
            c.u(6)
            c.u(5)
        n = c.u(7) + 1
        if c.u(1):
            c.u(7)
        else:
            for _ in range(n):
                c.u(7)
        groups.append({"id": gid, "default_pos": default_pos})
    for _ in range(c.u(5)):                          # switch groups
        c.u(5)
        if c.u(1):
            c.u(1)
        for _ in range(c.u(5) + 1):
            c.u(7)
        c.u(7)
    presets = []
    for _ in range(c.u(5)):
        pid = c.u(5)
        c.u(5)
        conds = []
        for _ in range(c.u(4) + 1):
            ref_pos = c.p
            ref = c.u(7)
            if c.u(1):
                c.u(1)
                if c.u(1):
                    c.u(8)
                c.u(1)
                if c.u(1):
                    c.u(8 + 6 + 4)
            conds.append({"ref": ref, "pos": ref_pos})
        presets.append({"id": pid, "conds": conds})
    return {"groups": groups, "presets": presets}


def _set_bits(buf, pos, n, val):
    for i in range(n):
        bit = (val >> (n - 1 - i)) & 1
        byte, off = (pos + i) >> 3, 7 - ((pos + i) & 7)
        buf[byte] = (buf[byte] & ~(1 << off)) | (bit << off)


def isolate_stream(src, dst, keep, preset_id):
    """Writes a copy of an MPEG-H audio MP4 in which preset `preset_id` plays only the audio elements in `keep`.

    The UI manager never lets an element that a preset lists be switched off (a preset always disables on/off for its members), so an element
    cannot be muted from a script. Instead, in every audio scene info packet: the other groups get default-off, and the preset's conditions that
    point to them are redirected to an unused group id (the decoder ignores a condition for a group that does not exist). Only fixed-size bit
    fields change, so the file keeps its size and structure. Returns the number of scene packets edited."""
    keep = set(keep)
    shutil.copyfile(src, dst)
    table = _mp4_sample_table(dst)
    patched = 0
    with open(dst, "r+b") as f:
        for off, size in table:
            f.seek(off)
            smp = f.read(size)
            for ptype, start, ln in mhas_packets(smp):
                if ptype != 3:
                    continue
                payload = bytearray(smp[start:start + ln])
                try:
                    lay = _asi_layout(payload)
                except EOFError:
                    lay = None
                if not lay:
                    continue
                ids = {g["id"] for g in lay["groups"]}
                unused = next(i for i in range(127, -1, -1) if i not in ids)
                for g in lay["groups"]:
                    if g["id"] not in keep:
                        _set_bits(payload, g["default_pos"], 1, 0)
                for p in lay["presets"]:
                    if p["id"] == preset_id:
                        for cd in p["conds"]:
                            if cd["ref"] in ids and cd["ref"] not in keep:
                                _set_bits(payload, cd["pos"], 7, unused)
                f.seek(off + start)
                f.write(bytes(payload))
                patched += 1
    return patched


def _parse_mae_groups(payload):
    """mae_AudioSceneInfo, group definitions only: {group id: {"signals": [indices], "position": bool}}."""
    try:
        c = _Bits(payload)
        c.u(1)
        if c.u(1):
            c.u(8)
        out = {}
        for _ in range(c.u(7)):
            gid = c.u(7)
            c.u(2)
            pos, gain = c.u(1), c.u(1)
            if pos:
                for n in (7, 7, 5, 5, 4, 4):
                    c.u(n)
            if gain:
                c.u(6)
                c.u(5)
            n = c.u(7) + 1
            if c.u(1):
                start = c.u(7)
                sig = list(range(start, start + n))
            else:
                sig = [c.u(7) for _ in range(n)]
            out[gid] = {"signals": sig, "position": bool(pos)}
        return out
    except EOFError:
        return None


def _element_channels(cfg, scene):
    """{audio element id: {"channels": n, "objects": n}} from the signal groups (signals are numbered group after group)."""
    if not scene or any(g.get("partial") for g in cfg["groups"]):
        return None
    kind, idx = {}, 0
    for g in cfg["groups"]:
        for _ in range(g["signals"]):
            kind[idx] = g["type"]
            idx += 1
    res = {}
    for gid, e in scene.items():
        ks = [kind.get(x) for x in e["signals"]]
        if None in ks:
            return None
        res[gid] = {"channels": ks.count("channels"), "objects": ks.count("object"), "position": e["position"]}
    return res


def _parse_3da_config(cfg, sample):
    c = _Bits(cfg)

    def speakers():
        t = c.u(2)
        if t == 0:
            return c.u(6), None
        n = c.esc(5, 8, 16) + 1
        if t == 1:
            for _ in range(n):
                c.u(7)
        return None, n

    try:
        profile = c.u(8)
        if c.u(5) == 31:
            c.u(24)
        c.u(3 + 1 + 1)
        ref, ref_n = speakers()
        groups = []
        for _ in range(c.u(5) + 1):
            gt, n = c.u(3), c.u(5)
            if n == 31:
                n += c.u(8)
            g = {"type": ("channels", "object", "saoc", "hoa")[gt] if gt < 4 else str(gt), "signals": n + 1}
            if gt == 0 and c.u(1):
                g["cicp"], _ = speakers()
            groups.append(g)
            if gt > 1:                                   # SAOC/HOA carry extra fields that are not parsed
                g["partial"] = True
                break
    except EOFError:
        return None
    return {"profile": profile, "cicp": ref, "ref_speakers": ref_n, "groups": groups, "sample": sample}


def describe_stream_config(m):
    if not m:
        return ""
    bed = [g["signals"] for g in m["groups"] if g["type"] == "channels"]
    obj = sum(g["signals"] for g in m["groups"] if g["type"] == "object")
    return (f"CICP {m['cicp']}; " if m.get("cicp") else "") + f"{len(m['groups'])} grupo(s) de sinais: " + \
        (f"{' + '.join(map(str, bed))} canais" if bed else "sem canais") + (f", {obj} objeto(s)" if obj else "")


def detect_native_info(audio_mp4, ffmpeg=None, mp4box=None):
    """-> (supported CICP used for rendering, {"source", "cicp", "config", "note"}). Order: mhaC box, in-band MHAS config,
    ffmpeg (only when it reports channels), MP4Box, then 2.0 as a last resort (with a note)."""
    info = {"source": None, "cicp": None, "config": None, "elements": None, "note": ""}

    def pick(cicp, channels=None):
        if cicp in CICP_CH:
            return cicp
        n = channels or 0
        best = max((k for k, v in CICP_CH.items() if v <= n), default=2) if n else 2
        info["note"] = (f"O layout do stream (CICP {cicp}{f', {n} canais' if n else ''}) não é um dos que a renderização suporta ({', '.join(CICP_NAME.values())}); "
                        f"usando {CICP_NAME[best]} (o decoder faz o downmix). Para outro, use --native-layout.")
        return best

    try:
        with open(audio_mp4, "rb") as f:
            buf = f.read(8 * 1024 * 1024)
        idx = buf.find(b"mhaC")
        if idx != -1 and idx + 10 <= len(buf):
            ref = buf[idx + 6] & 0x3F
            info.update(source="mhaC", cicp=ref)
            return pick(ref), info
    except OSError:
        pass

    m = mhas_stream_config(audio_mp4)
    if m and m.get("cicp"):
        bed = sum(g["signals"] for g in m["groups"] if g["type"] == "channels")
        info.update(source="MHAS", cicp=m["cicp"], config=m, elements=m.get("elements"))
        return pick(m["cicp"], bed or None), info

    if ffmpeg:
        try:
            r = sp_run([ffmpeg, "-hide_banner", "-i", str(audio_mp4)], capture_output=True, text=True, errors="replace", timeout=15, **NOWIN)
            err = r.stderr or ""
            for rx, val in ((r"Audio:.*?(7\.1\.4|12 channels|11\.1)", 19), (r"Audio:.*?(5\.1|6 channels)", 6), (r"Audio:.*?(stereo|2 channels|2\.0)", 2)):
                if re.search(rx, err, re.I):
                    info.update(source="ffmpeg", cicp=val)
                    return val, info
        except Exception:
            pass

    if mp4box:
        try:
            with MP4BoxJob(audio_mp4) as box:
                r = sp_run([mp4box, "-info", box.inp(audio_mp4)], capture_output=True, text=True, errors="replace",
                           timeout=15, cwd=str(box.dir), **NOWIN)
            mm = re.search(r"(?:ChannelLayout|referenceChannelLayout|Channels)\s*[:=]\s*([^\r\n]+)", (r.stdout or "") + "\n" + (r.stderr or ""), re.I)
            if mm:
                val = mm.group(1).strip().lower()
                v = 19 if ("19" in val or "7.1.4" in val or "12" in val) else 6 if ("6" in val or "5.1" in val) else 2 if ("2" in val or "stereo" in val) else None
                if v:
                    info.update(source="MP4Box", cicp=v)
                    return v, info
        except Exception:
            pass

    info["note"] = ("Não foi possível descobrir o layout nativo do stream; assumindo 2.0. Se o áudio for multicanal, informe --native-layout (6 = 5.1, 16 = 5.1.4, 19 = 7.1.4).")
    return 2, info


def detect_native_cicp(audio_mp4, ffmpeg=None, mp4box=None):
    cicp, info = detect_native_info(audio_mp4, ffmpeg, mp4box)
    if info["note"]:
        note(info["note"])
    return cicp


def do_scan(a):
    src = Path(a.input).resolve()
    if not src.is_file():
        raise Fail(f"Arquivo não encontrado: {src}")
    set_steps(MP_SCAN_STEPS)
    set_stages({"prepare": 65, "scene": 30, "catalog": 5})
    root = cache_root(a.cache_dir)
    root.mkdir(parents=True, exist_ok=True)
    purge_cache(root)
    step("prepare", "running")
    mp4box = find_mp4box(a.mp4box)
    d = prepare_audio(src, root, mp4box, stage="prepare", frac_span=(0.0, 1.0))
    step("prepare", "done", "áudio pronto (guardado em cache para as próximas vezes)")
    step("scene", "running")
    ui = need_tool(a.mpegh_ui, "mpeghUiManager")
    audio_mp4 = d / "audio.mp4"
    states = load_scene(d, ui, audio_mp4, stage="scene", frac_span=(0.0, 0.9))
    progress("scene", 0.95, "Analisando linha do tempo e presets…", force=True)
    ffmpeg_opt = optional_tool("ffmpeg", a.ffmpeg)
    dur = media_duration(ffmpeg_opt, audio_mp4)
    info = analyze_scenes(states, int(dur / MPEGH_FRAME_S) if dur else None)
    progress("scene", 1.0, f"{len(states)} estado(s) de cena", force=True)
    step("scene", "done", f"{len(states)} estado(s) de cena")
    step("catalog", "running")
    progress("catalog", 0.2, "Detectando layout nativo do stream…", force=True)
    detected_cicp, det = detect_native_info(audio_mp4, ffmpeg_opt, mp4box)
    use_layout = detected_cicp if (not a.native_layout or int(a.native_layout) == 0) else int(a.native_layout)
    compat = bool(getattr(a, "compat_51", False))
    cat = build_catalog(info, use_layout, det, bool(a.native_layout), compat)
    warns = list(cat["warnings"])
    if det["note"]:
        warns.append(det["note"])
    if compat:
        warns.append("Modo de compatibilidade: tudo o que seria CICP 16 (5.1.4) é renderizado como 5.1.")
    if info["has_differences"]:
        warns.append("A configuração de áudio (AudioSceneConfig) muda ao longo do vídeo. Confira no gráfico abaixo a disponibilidade de cada preset e grupo.")
    scan = {
        "sig": d.name,
        "cache": str(d),
        "duration": dur,
        "total_frames": info["total_frames"],
        "frame_s": MPEGH_FRAME_S,
        "scenes": info["changes"],
        "coverage": info["coverage"],
        "others": info["others"][:20],
        "timeline": info["timeline"],
        "has_differences": info["has_differences"],
        "presets": info["unified_presets"],
        "groups": info["unified_groups"],
        "options": cat["options"],
        "native": cat["native"],
        "detected_native": detected_cicp,
        "stream_layout": {"source": det["source"], "cicp": det["cicp"], "groups": (det["config"] or {}).get("groups"),
                          "text": describe_stream_config(det["config"])},
        "warnings": warns,
        "layouts": catalog_public(),
        "formats": {k: {"label": v["label"], "br": v["br"], "max": v["max"]} for k, v in FORMATS.items()},
    }
    (d / "catalog.json").write_text(json.dumps(scan, ensure_ascii=False), encoding="utf-8")
    progress("catalog", 1.0, f"{len(cat['options'])} opções prontas", force=True)
    step("catalog", "done", f"{len(cat['options'])} opções · layout {CICP_NAME.get(use_layout, '2.0')}")
    return scan


def print_catalog(scan):
    sl = scan.get("stream_layout") or {}
    say(bold(f"Layout detectado no stream: {CICP_NAME.get(scan.get('detected_native', 2), '2.0')} (usando {CICP_NAME.get(scan.get('native', 2), '2.0')})"))
    if sl.get("text"):
        say(f"Configuração lida do stream ({sl.get('source')}): {sl['text']}")
    opt = {o["id"]: o for o in scan.get("options", [])}

    def lay(oid):
        o = opt.get(oid)
        if not o:
            return ""
        comp = f"; origem: {o['composition']}" if o.get("composition") else ""
        others = [CICP_NAME[x] for x in o.get("layout_choices", []) if x != o["layout"]]
        return (f" · saída {CICP_NAME.get(o['layout'], '?')} original ({o['out_ch']} canais{comp})"
                + (f"; também {', '.join(others)}: --option {oid}@{others[0]}" if others else ""))

    say(bold("Presets encontrados no vídeo:"))
    for p in scan.get("presets", []):
        els = ", ".join(f"G{e['id']} ({e['name']})" for e in p.get("elements", []))
        say(f"  --option preset:{p['id']}   → Preset {p['id']}: {p['name']} [{els}]{lay('preset:' + str(p['id']))}")
    say(bold("Grupos (Audio Elements) encontrados no vídeo:"))
    for g in scan.get("groups", []):
        prs = ", ".join(f"P{pid}" for pid in g.get("preset_ids", []))
        o = opt.get("group:" + str(g["id"]))
        iso = "" if not o or o.get("isolation") != "edit" else "  [isolado editando a cena]"
        say(f"  --option group:{g['id']}    → Grupo {g['id']}: {g['name']} (em {prs}){lay('group:' + str(g['id']))}{iso}")
    pairs = [o for o in scan.get("options", []) if o["id"].startswith("pair:")]
    if pairs:
        say(bold("Pares estéreo (L/R) identificados pelo nome:"))
        for o in pairs:
            iso = "" if o.get("isolation") != "edit" else "  [isolado editando a cena]"
            say(f"  --option {o['id']}   → {o['title']}: {o['desc']}{iso}")


def cmd_mpegh_scan(a):
    scan = do_scan(a)
    ev(type="result", out=scan["cache"], outputs=[], scan=scan, notes=scan["warnings"])
    if not PJ:
        print_catalog(scan)


# ──────────────────────── extração e resiliência a falhas ────────────────────────
def action_script_for_target(path, states, target_kind, target_id, carrier_pid, uim_version, sleep_mode, start_offset_frame=0):
    """
    Gera o script de ActionEvent para um Preset ou Grupo ao longo das mudanças de cena.
    - Só emite ActionEvent nos estados de cena onde há presets válidos (evitando erro 440039 em cenas vazias).
    - Usa o UUID de cada cena em que o preset aparece.
    - Para grupos ('g') e conjuntos de grupos ('m'), seleciona o preset portador que contém o(s) grupo(s) e silencia (ação 40) os demais elementos.
      O decoder só obedece se o preset permitir silenciar o elemento (elemento interativo com muting permitido).
    """
    lines = []
    pos = 0
    for st in states:
        if not st["presets"]:
            continue
        if st["frame"] < start_offset_frame and any(s["frame"] >= start_offset_frame and s["presets"] for s in states):
            continue
        f = max(0, st["frame"] - start_offset_frame)
        uuid = st["uuid"] if st["uuid"] and st["uuid"] != "00000000-0000-0000-0000-000000000000" else ""
        if not uuid:
            continue
        ver = st.get("version") or uim_version or "11.1"
        avail_by_id = {p["id"]: p for p in st["presets"]}

        if target_kind == "p":
            use_pid = target_id if target_id in avail_by_id else st["presets"][0]["id"]
            other_eids = []
        else:
            want = set(target_id if isinstance(target_id, (list, tuple, set)) else [target_id])
            candidates = [p for p in st["presets"] if want <= {e["id"] for e in p["elements"]}]
            if candidates:
                # prefer a preset whose other elements can be muted (or that has none), then the smallest one
                def cost(p):
                    others = [e for e in p["elements"] if e["id"] not in want]
                    return (sum(1 for e in others if not e.get("mute_ok")), len(p["elements"]), p["id"])
                best_p = min(candidates, key=cost)
                use_pid = best_p["id"]
                other_eids = [e["id"] for e in best_p["elements"] if e["id"] not in want]
            else:
                use_pid = carrier_pid if carrier_pid in avail_by_id else st["presets"][0]["id"]
                other_eids = []

        if use_pid == 0 and not other_eids and not lines and f == 0:
            continue

        at = 0 if f == 0 else f + 2
        if at > pos:
            sleep_frames = (at - pos) if sleep_mode == "relative" else at
            lines.append(_xml_el("Sleep", [("Frame", sleep_frames)]))
            pos = at
        lines.append(_xml_el("ActionEvent", [
            ("uuid", uuid),
            ("version", ver),
            ("actionType", 30),
            ("paramInt", use_pid),
        ]))
        for oe in other_eids:                     # 40 = IIS_UIM_CMD_AUDIO_ELEMENT_MUTING_CHANGED (the only documented way to switch an element off)
            lines.append(_xml_el("ActionEvent", [
                ("uuid", uuid),
                ("version", ver),
                ("actionType", 40),
                ("paramInt", oe),
                ("paramBool", "true"),
            ]))

    if not lines:
        return False
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return True


def wav_info(path, fallback_ch=2, fallback_rate=48000, fallback_bps=3):
    """
    Lê e repara o cabeçalho de um WAV caso o mpeghDecoder tenha parado no meio (ex: erro 440039 em 02:36:26).
    Suporta arquivos WAV maiores que 4 GB (gravando 0xFFFFFFFF nos campos de 32 bits sem estourar struct.pack)
    e reconstrói o cabeçalho caso os primeiros 44 bytes tenham ficado zerados.
    """
    p = Path(path)
    if not p.is_file():
        return 0.0, 0
    size = p.stat().st_size
    if size < 100:
        return 0.0, 0

    u32_fmt = "<I"
    fmt_hdr = "<HI"
    align_hdr = "<H"
    full_hdr = "<4sI4s4sIHHIIHH4sI"

    with open(path, "r+b") as f:
        head = f.read(min(size, 4096))

        if head[:4] == b"\x00\x00\x00\x00":
            ch = fallback_ch
            rate = fallback_rate
            bps = fallback_bps
            align = ch * bps
            data_off = 44
            real = max(0, ((size - data_off) // align) * align)
            riff_sz = min(0xFFFFFFFF, 36 + real)
            data_sz = min(0xFFFFFFFF, real)
            hdr = struct.pack(
                full_hdr,
                b"RIFF", riff_sz, b"WAVE", b"fmt ", 16,
                1, ch, rate, rate * align, align, bps * 8,
                b"data", data_sz
            )
            f.seek(0)
            f.write(hdr)
            return real / align / rate, ch

        if head[:4] not in (b"RIFF", b"RF64") or head[8:12] != b"WAVE":
            return 0.0, 0

        pos, ch, rate, align, data_off = 12, 0, 0, 0, None
        while pos + 8 <= len(head):
            cid = head[pos:pos + 4]
            csz = struct.unpack(u32_fmt, head[pos + 4:pos + 8])[0]
            if cid == b"fmt ":
                ch, rate = struct.unpack(fmt_hdr, head[pos + 10:pos + 16])
                align = struct.unpack(align_hdr, head[pos + 20:pos + 22])[0]
            elif cid == b"data":
                data_off = pos + 8
                break
            if csz == 0xFFFFFFFF or csz == 0:
                pos += 8
            else:
                pos += 8 + csz + (csz & 1)

        if not (data_off and rate and align):
            return 0.0, 0

        real = max(0, ((size - data_off) // align) * align)
        data_sz32 = min(0xFFFFFFFF, real)
        riff_sz32 = min(0xFFFFFFFF, data_off - 8 + real)

        f.seek(data_off - 4)
        declared = struct.unpack(u32_fmt, f.read(4))[0]
        if declared != data_sz32:
            f.seek(data_off - 4)
            f.write(struct.pack(u32_fmt, data_sz32))
            f.seek(4)
            f.write(struct.pack(u32_fmt, riff_sz32))

        return real / align / rate, ch


def _create_silence_wav(ffmpeg, out_wav, duration_s, cicp):
    ch = CICP_CH[cicp]
    layout = FF_LAYOUT.get(cicp, f"{ch}c")
    cmd = [
        ffmpeg, "-hide_banner", "-y", "-v", "error",
        "-f", "lavfi", "-i", f"anullsrc=channel_layout={layout}:sample_rate=48000",
        "-t", f"{max(0.1, duration_s):.3f}",
        "-rf64", "auto", "-c:a", "pcm_s24le", str(out_wav)
    ]
    sp_run(cmd, capture_output=True, text=True, errors="replace", **NOWIN)


def decode_one(dec, ffmpeg, mp4box, audio, wav, cicp, script, states, target_kind, target_id, carrier_pid, dur, a):
    """
    Decodifica um preset/grupo com proteção completa contra falhas de stream (ex: Error [440039] Unable to process data):
    1. Executa mpeghDecoder.
    2. Se falhar no meio (ex: em 02:36:26 por ausência de preset ou pacote corrompido), recupera o WAV até o corte
       (mesmo se maior que 4 GB).
    3. Se ainda restar vídeo após o ponto de falha, avança além do trecho sem preset/corrompido, decodifica o restante
       e junta tudo mantendo a sincronia. Caso falhe no início, preenche o trecho inválido e prossegue.
    """
    cmd = [dec, "-if", str(audio), "-of", str(wav), "-tl", str(cicp), "-dse", str(a.drc_effect)]
    if a.target_loudness:
        cmd += ["-rl", str(a.target_loudness)]
    if script:
        cmd += ["-script", str(script)]
    log("$ " + subprocess.list2cmdline(cmd))
    r = sp_run(cmd, capture_output=True, text=True, errors="replace", **NOWIN)

    ok_file = Path(wav).is_file() and Path(wav).stat().st_size >= 100
    if r.returncode == 0 and ok_file:
        wav_info(wav, fallback_ch=CICP_CH[cicp])
        return None

    out_text = (r.stderr or "") + "\n" + (r.stdout or "")
    tail = (out_text.strip().splitlines() or ["sem mensagem"])[-1][:200]

    if not ok_file and script:
        cmd_ns = [dec, "-if", str(audio), "-of", str(wav), "-tl", str(cicp), "-dse", str(a.drc_effect)]
        if a.target_loudness:
            cmd_ns += ["-rl", str(a.target_loudness)]
        log("$ " + subprocess.list2cmdline(cmd_ns))
        r2 = sp_run(cmd_ns, capture_output=True, text=True, errors="replace", **NOWIN)
        ok_file = Path(wav).is_file() and Path(wav).stat().st_size >= 100
        if r2.returncode == 0 and ok_file:
            wav_info(wav, fallback_ch=CICP_CH[cicp])
            return {"cut": dur or 0.0, "recovered": True, "error": "Decodificado com cena nativa após falha no script de ação."}

    secs = 0.0
    if ok_file:
        secs, _ = wav_info(wav, fallback_ch=CICP_CH[cicp])

    m_frame = re.search(r"\[(\d+)\]\s*Error:", out_text)
    err_frame = int(m_frame.group(1)) if m_frame else None
    err_sec = (err_frame * MPEGH_FRAME_S) if err_frame is not None else secs

    if secs >= 1.0:
        if dur and (dur - secs) > 3.0 and mp4box and ffmpeg:
            next_valid_sec = None
            if err_frame is not None and states:
                for st in states:
                    if st["frame"] > err_frame and st["presets"]:
                        next_valid_sec = st["frame"] * MPEGH_FRAME_S
                        break
            resume_sec = next_valid_sec if (next_valid_sec and next_valid_sec > secs) else (max(secs, err_sec) + 2.0)
            if resume_sec < dur - 1.0:
                tail_mp4 = Path(wav).with_suffix(".tail.mp4")
                tail_wav = Path(wav).with_suffix(".tail.wav")
                tail_script = Path(wav).with_suffix(".tail.xml")
                merged_wav = Path(wav).with_suffix(".merged.wav")
                try:
                    split_ok = mp4box_splitx(mp4box, audio, resume_sec, dur + 10, tail_mp4)
                    if split_ok and tail_mp4.is_file() and tail_mp4.stat().st_size > 100:
                        cmd_t = [dec, "-if", str(tail_mp4), "-of", str(tail_wav), "-tl", str(cicp), "-dse", str(a.drc_effect)]
                        if a.target_loudness:
                            cmd_t += ["-rl", str(a.target_loudness)]
                        resume_frame = int(resume_sec / MPEGH_FRAME_S)
                        if script and action_script_for_target(tail_script, states, target_kind, target_id, carrier_pid, a.uim_version, a.sleep_mode, start_offset_frame=resume_frame):
                            cmd_t += ["-script", str(tail_script)]
                        sp_run(cmd_t, capture_output=True, text=True, errors="replace", **NOWIN)
                        if tail_wav.is_file() and tail_wav.stat().st_size >= 100:
                            t_secs, _ = wav_info(tail_wav, fallback_ch=CICP_CH[cicp])
                            if t_secs >= 0.5:
                                gap_s = max(0.0, resume_sec - secs)
                                ch = CICP_CH[cicp]
                                layout = FF_LAYOUT.get(cicp, f"{ch}c")
                                if gap_s > 0.02:
                                    fc = "[0:a][1:a][2:a]concat=n=3:v=0:a=1,apad[out]"
                                    cmd_m = [
                                        ffmpeg, "-hide_banner", "-y", "-v", "error",
                                        "-ignore_length", "1", "-i", str(wav),
                                        "-f", "lavfi", "-t", f"{gap_s:.3f}", "-i", f"anullsrc=channel_layout={layout}:sample_rate=48000",
                                        "-ignore_length", "1", "-i", str(tail_wav),
                                        "-filter_complex", fc, "-map", "[out]", "-t", f"{dur:.3f}",
                                        "-rf64", "auto", "-c:a", "pcm_s24le", str(merged_wav)
                                    ]
                                else:
                                    fc = "[0:a][1:a]concat=n=2:v=0:a=1,apad[out]"
                                    cmd_m = [
                                        ffmpeg, "-hide_banner", "-y", "-v", "error",
                                        "-ignore_length", "1", "-i", str(wav),
                                        "-ignore_length", "1", "-i", str(tail_wav),
                                        "-filter_complex", fc, "-map", "[out]", "-t", f"{dur:.3f}",
                                        "-rf64", "auto", "-c:a", "pcm_s24le", str(merged_wav)
                                    ]
                                mr = sp_run(cmd_m, capture_output=True, text=True, errors="replace", **NOWIN)
                                if mr.returncode == 0 and merged_wav.is_file() and merged_wav.stat().st_size > 100:
                                    merged_wav.replace(wav)
                                    wav_info(wav, fallback_ch=CICP_CH[cicp])
                                    return {"cut": secs, "resumed": resume_sec, "error": tail}
                except Exception:
                    pass
                finally:
                    for fp in (tail_mp4, tail_wav, tail_script, merged_wav):
                        fp.unlink(missing_ok=True)

        return {"cut": secs, "error": tail}

    if dur and dur > 1.0 and ffmpeg:
        first_valid_sec = None
        if states:
            for st in states:
                if st["presets"] and st["frame"] > 0:
                    first_valid_sec = st["frame"] * MPEGH_FRAME_S
                    break
        if first_valid_sec and first_valid_sec < dur - 1.0 and mp4box:
            tail_mp4 = Path(wav).with_suffix(".tail.mp4")
            tail_wav = Path(wav).with_suffix(".tail.wav")
            tail_script = Path(wav).with_suffix(".tail.xml")
            merged_wav = Path(wav).with_suffix(".merged.wav")
            try:
                split_ok = mp4box_splitx(mp4box, audio, first_valid_sec, dur + 10, tail_mp4)
                if split_ok and tail_mp4.is_file():
                    cmd_t = [dec, "-if", str(tail_mp4), "-of", str(tail_wav), "-tl", str(cicp), "-dse", str(a.drc_effect)]
                    if a.target_loudness:
                        cmd_t += ["-rl", str(a.target_loudness)]
                    resume_frame = int(first_valid_sec / MPEGH_FRAME_S)
                    if script and action_script_for_target(tail_script, states, target_kind, target_id, carrier_pid, a.uim_version, a.sleep_mode, start_offset_frame=resume_frame):
                        cmd_t += ["-script", str(tail_script)]
                    sp_run(cmd_t, capture_output=True, text=True, errors="replace", **NOWIN)
                    if tail_wav.is_file() and tail_wav.stat().st_size >= 100:
                        t_secs, _ = wav_info(tail_wav, fallback_ch=CICP_CH[cicp])
                        if t_secs >= 0.5:
                            ch = CICP_CH[cicp]
                            layout = FF_LAYOUT.get(cicp, f"{ch}c")
                            cmd_m = [
                                ffmpeg, "-hide_banner", "-y", "-v", "error",
                                "-f", "lavfi", "-t", f"{first_valid_sec:.3f}", "-i", f"anullsrc=channel_layout={layout}:sample_rate=48000",
                                "-ignore_length", "1", "-i", str(tail_wav),
                                "-filter_complex", "[0:a][1:a]concat=n=2:v=0:a=1,apad[out]",
                                "-map", "[out]", "-t", f"{dur:.3f}",
                                "-rf64", "auto", "-c:a", "pcm_s24le", str(merged_wav)
                            ]
                            mr = sp_run(cmd_m, capture_output=True, text=True, errors="replace", **NOWIN)
                            if mr.returncode == 0 and merged_wav.is_file():
                                merged_wav.replace(wav)
                                wav_info(wav, fallback_ch=CICP_CH[cicp])
                                return {"cut": first_valid_sec, "resumed": first_valid_sec, "error": tail}
            except Exception:
                pass
            finally:
                for fp in (tail_mp4, tail_wav, tail_script, merged_wav):
                    fp.unlink(missing_ok=True)

        _create_silence_wav(ffmpeg, wav, dur, cicp)
        if Path(wav).is_file() and Path(wav).stat().st_size >= 100:
            return {"cut": 0.0, "silent_fallback": True, "error": tail}

    raise Fail(f"O mpeghDecoder falhou ({Path(wav).name}): {tail}")


def parse_layout_name(text):
    """'5.1', '2.0' or a CICP number (2, 6, 16, 19) -> CICP, or None."""
    t = str(text).strip()
    for k, v in CICP_NAME.items():
        if t == v or t == str(k):
            return k
    return None


def parse_opt_spec(spec: str):
    """Aceita 'preset:0=eac3:640k', 'group:5=aac' e, no fim, '@LAYOUT' (group:5@2.0, preset:1=eac3:448k@5.1).
    -> (id, formato, bitrate, layout CICP ou None)."""
    m = re.fullmatch(r"([A-Za-z0-9:_.+-]+?)(?:=([a-z0-9]+)(?::(\d{2,4}k))?)?(?:@([0-9.]+))?", spec.strip())
    if not m:
        raise Fail(f"Opção inválida: {spec}")
    lay = None
    if m.group(4):
        lay = parse_layout_name(m.group(4))
        if lay is None:
            raise Fail(f"Layout inválido em '{spec}': use {', '.join(CICP_NAME.values())}.")
    return m.group(1), m.group(2), m.group(3), lay


def set_option_layout(o, lay):
    """Render this option in another layout (2.0 / 5.1 ...): decode target, channel count, formats, highlight and file name follow."""
    old = o["layout"]
    if lay == old:
        return o
    o["layout"] = lay
    o["inputs"] = [[tag, lay] for tag, _ in o["inputs"]]
    o["src_cicp"] = lay
    o["out_ch"] = CICP_CH[lay]
    o["formats"] = allowed_fmts(o["out_ch"])
    o["fmt"] = default_fmt(o["out_ch"])
    o["highlight"] = [list(range(CICP_CH[lay]))]
    suffix = "_" + CICP_NAME[old]
    o["stem"] = (o["stem"][:-len(suffix)] if o["stem"].endswith(suffix) else o["stem"]) + "_" + CICP_NAME[lay]
    return o


# Volume normalization of the rendered audio (--normalize). Measured on the decoded WAV with ffmpeg's loudnorm (EBU R128: integrated loudness and
# true peak, all channels, LFE excluded), then applied as ONE constant gain, so the dynamics of the mix are untouched. The gain never pushes
# the true peak above --true-peak: if it would, it is reduced and the file ends up quieter than the target (reported).
LOUDNESS_PRESETS = {"ebu": -23.0, "atsc": -24.0, "streaming": -16.0}


def loudness_target(a):
    """--normalize -> target in LUFS, or None (off). Validates --true-peak too."""
    v = str(getattr(a, "normalize", "off") or "off").strip().lower()
    tp = getattr(a, "true_peak", -1.0)
    if tp is None or not -9.0 <= tp <= 0.0:
        raise Fail("--true-peak deve estar entre -9 e 0 dBTP.")
    if v in ("off", "none", "no", "0"):
        return None
    if v in LOUDNESS_PRESETS:
        return LOUDNESS_PRESETS[v]
    try:
        x = float(v.replace(",", "."))
    except ValueError:
        raise Fail(f"--normalize: use off, {', '.join(LOUDNESS_PRESETS)} ou um valor em LUFS (ex.: -23).")
    if not -40.0 <= x <= -5.0:
        raise Fail("--normalize: o alvo deve estar entre -40 e -5 LUFS.")
    return x


def measure_loudness(ffmpeg, wav):
    """{'i': LUFS, 'tp': dBTP, 'lra': LU} of a WAV, or None when it cannot be measured (silence)."""
    cmd = [ffmpeg, "-hide_banner", "-nostats"] + (["-ignore_length", "1"] if Path(wav).suffix.lower() == ".wav" else []) + [
        "-i", str(wav), "-af", "loudnorm=I=-23:TP=-1:LRA=11:print_format=json", "-f", "null", "-"]
    r = sp_run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL, **NOWIN)
    m = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", r.stderr or "", re.S)
    if r.returncode != 0 or not m:
        raise Fail(f"O ffmpeg não conseguiu medir o volume de {Path(wav).name}: {(r.stderr or '').strip()[-200:]}")
    j = json.loads(m.group(0))
    try:
        i, tp = float(j["input_i"]), float(j["input_tp"])
    except ValueError:
        return None
    if math.isinf(i) or math.isnan(i) or math.isinf(tp):
        return None
    return {"i": i, "tp": tp, "lra": float(j.get("input_lra", 0) or 0)}


def loudness_gain(m, target, tp_limit):
    """-> (gain in dB, limited by the peak?). Gains under 0.05 dB are not applied."""
    g = target - m["i"]
    limited = False
    if m["tp"] + g > tp_limit:
        g, limited = tp_limit - m["tp"], True
    g = round(g, 2)
    return (0.0 if abs(g) < 0.05 else g), limited


def encode_audio(ffmpeg, wav_path, out_path, fmt_key, br, dur, cut_warn=False, on_pct=None, gain_db=0.0, trim_s=0.0):
    fdef = FORMATS[fmt_key]
    cmd = [
        ffmpeg, "-hide_banner", "-y", "-v", "error",
        "-progress", "pipe:1", "-nostats",
        "-ignore_length", "1"
    ]
    if trim_s > 0.0:
        cmd += ["-ss", f"{trim_s:.6f}"]
    cmd += ["-i", str(wav_path)]
    
    af = ([f"volume={gain_db:.2f}dB"] if gain_db else []) + (["apad"] if dur and cut_warn else [])
    if af:
        cmd += ["-af", ",".join(af)]
        
    out_dur = max(0.1, dur - trim_s) if dur else None
    if out_dur and cut_warn:
        cmd += ["-t", f"{out_dur:.3f}"]
    elif out_dur:
        cmd += ["-t", f"{out_dur:.3f}"]
        
    cmd += list(fdef["args"])
    use_br = br or fdef["br"]
    if use_br:
        cmd += ["-b:a", use_br]
    if fmt_key == "wav":
        cmd += ["-rf64", "auto"]
    cmd.append(str(out_path))
    log("$ " + subprocess.list2cmdline(cmd))

    p = sp_popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                         text=True, encoding="utf-8", errors="replace", **NOWIN)
    if p.stdout:
        for ln in p.stdout:
            ln = ln.strip()
            if on_pct and out_dur and out_dur > 0 and ln.startswith("out_time_us="):
                try:
                    us = int(ln.split("=", 1)[1])
                    if us >= 0:
                        on_pct(min(0.99, (us / 1_000_000.0) / out_dur))
                except ValueError:
                    pass
    err = (p.stderr.read() if p.stderr else "").strip()
    p.wait()
    if p.returncode != 0 or not Path(out_path).is_file() or Path(out_path).stat().st_size == 0:
        raise Fail(f"O ffmpeg falhou ao gerar {Path(out_path).name}: {err[:300]}")
    if on_pct:
        on_pct(1.0)


def pick_options(cat, raw_opts, fmt_override=None):
    """--option specs (preset:1, group:3=aac:192k ...) -> catalog options with the chosen format/bitrate."""
    by_id = {o["id"]: o for o in cat["options"]}
    if not raw_opts:                                   # default: every preset, no groups
        raw_opts = [o["id"] for o in cat["options"] if o["group"] == "presets"]
    chosen = []
    for spec in raw_opts:
        oid, fmt, br, lay = parse_opt_spec(spec)
        if oid not in by_id:
            avail = ", ".join(by_id.keys())
            raise Fail(f"A opção '{oid}' não existe neste arquivo. Disponíveis: {avail}")
        base_o = dict(by_id[oid])
        if lay is not None:
            ok_lay = base_o.get("layout_choices") or [base_o["layout"]]
            if lay not in ok_lay:
                raise Fail(f"{oid} não pode ser renderizado em {CICP_NAME[lay]} (original {CICP_NAME[base_o['layout']]}; "
                           f"opções: {', '.join(CICP_NAME[x] for x in ok_lay)}).")
            if lay != base_o["layout"]:
                note(f"{oid}: renderizado em {CICP_NAME[lay]} (o original é {CICP_NAME[base_o['layout']]}).")
            set_option_layout(base_o, lay)
        use_fmt = fmt or base_o["fmt"]
        if not fmt and fmt_override:
            if fmt_override in base_o["formats"]:
                use_fmt = fmt_override
            else:
                note(f"{oid}: o formato {fmt_override} não suporta {base_o['out_ch']} canais; usando {base_o['fmt']}.")
        if use_fmt not in base_o["formats"]:
            raise Fail(f"Formato '{use_fmt}' incompatível com {oid} ({base_o['out_ch']} canais).")
        base_o["fmt"] = use_fmt
        base_o["br"] = br
        chosen.append(base_o)
    return chosen


def mpegh_render(a, src, tools, outdir, tmp, raw_opts=None, fmt_override=None, pfx="", to_outdir=True):
    """
    Prepare -> decode -> check -> encode the chosen presets/groups (raw_opts; default = the presets).
    Step ids get the prefix `pfx` so another command can embed these four steps in its own list (transcode uses "a_").
    The audio files go to outdir (to_outdir) or tmp. Returns a dict (made, chosen, dur, use_layout, info, states, enc_plan, enc_map_path).
    """
    ffmpeg, mp4box, ui, dec = tools
    root = cache_root(a.cache_dir)
    root.mkdir(parents=True, exist_ok=True)
    purge_cache(root)
    step(pfx + "prepare", "running")
    d = prepare_audio(src, root, mp4box, stage=pfx + "prepare", frac_span=(0.0, 0.55))
    audio = d / "audio.mp4"
    states = load_scene(d, ui, audio, stage=pfx + "prepare", frac_span=(0.55, 0.90))
    progress(pfx + "prepare", 0.92, "Calculando duração e analisando cenas…", force=True)
    dur = media_duration(ffmpeg, audio)
    total_frames = int(dur / MPEGH_FRAME_S) if dur else None
    info = analyze_scenes(states, total_frames)
    forced = bool(a.native_layout) and int(a.native_layout) != 0
    use_layout, det = detect_native_info(audio, ffmpeg, mp4box)
    if forced:
        use_layout = int(a.native_layout)
    elif det["note"]:
        note(det["note"])
    compat = bool(getattr(a, "compat_51", False))
    cat = build_catalog(info, use_layout, det, forced, compat)
    if compat:
        use_layout = cat["native"]

    if info["has_differences"]:
        note("A configuração de áudio (AudioSceneConfig) muda ao longo do vídeo.")

    chosen = pick_options(cat, raw_opts, fmt_override)

    step(pfx + "prepare", "done", f"{len(chosen)} saída(s) · layout {CICP_NAME.get(use_layout, '2.0')}")
    progress(pfx + "prepare", 1.0, force=True)

    if not pfx:
        ev(type="plan", out=str(outdir), outs=[])

    needed_inputs = {}
    for o in chosen:
        for tag, cicp in o["inputs"]:
            needed_inputs[(tag, cicp)] = o

    step(pfx + "decode", "running")
    progress(pfx + "decode", 0.0, f"Verificando/decodificando {len(needed_inputs)} trilha(s)…", force=True)
    wav_paths = {}
    cut_warnings = {}
    jobs = max(1, min(int(a.jobs or max(1, (os.cpu_count() or 2) // 2)), len(needed_inputs)))

    expected_targets = []
    total_expected_bytes = 0
    for (tag, cicp) in needed_inputs:
        w_path = tmp / f"{tag}_c{cicp}.wav"
        exp_b = int((dur or 60.0) * 48000 * CICP_CH[cicp] * 3) + 44
        expected_targets.append((tag, w_path, exp_b))
        total_expected_bytes += max(1, exp_b)

    done_n = [0]
    reused_wavs = [0]
    stop_mon = threading.Event()
    dec_t0 = time.time()

    def _monitor_decode():
        while not stop_mon.wait(0.4):
            cur_bytes = 0
            for _, wp, exp_b in expected_targets:
                if wp.with_suffix(".wav.done").is_file() and wp.is_file():
                    cur_bytes += exp_b
                    continue
                sz = wp.stat().st_size if wp.is_file() else 0
                tail_wp = wp.with_suffix(".tail.wav")
                if tail_wp.is_file():
                    sz += tail_wp.stat().st_size
                cur_bytes += min(exp_b, sz)
            byte_frac = min(0.99, cur_bytes / max(1, total_expected_bytes))
            done_frac = done_n[0] / max(1, len(needed_inputs))
            frac = max(byte_frac, done_frac)
            el = max(0.1, time.time() - dec_t0)
            spd = cur_bytes / el / 1048576.0
            progress(
                pfx + "decode",
                frac,
                f"{done_n[0]}/{len(needed_inputs)} concluídos · {human(cur_bytes)} / {human(total_expected_bytes)} ({spd:.1f} MB/s)"
            )

    mon_thread = threading.Thread(target=_monitor_decode, daemon=True)
    mon_thread.start()

    # Assinatura dos parâmetros de decodificação para validar o checkpoint .done
    dec_sig = f"src={src_sig(src)['size']}:{src_sig(src)['mtime_ns']}|dse={a.drc_effect}|rl={a.target_loudness}|sm={a.sleep_mode}|ver={a.uim_version}|act=41"
    pending_bytes = sum(eb for _, wp, eb in expected_targets if not (wp.with_suffix(".wav.done").is_file() and wp.is_file()))
    check_free_space(tmp, int(pending_bytes * 1.05) + (64 << 20), "the decoded audio (WAV 24-bit)")

    def _job(item):
        (tag, cicp), opt = item
        wav = tmp / f"{tag}_c{cicp}.wav"
        done_marker = wav.with_suffix(".wav.done")

        # Se o WAV já foi decodificado com sucesso antes da falha, reaproveita sem rodar mpeghDecoder novamente
        if wav.is_file() and wav.stat().st_size > 100 and done_marker.is_file():
            try:
                meta = json.loads(done_marker.read_text(encoding="utf-8"))
                if meta.get("sig") == dec_sig:
                    secs, _ = wav_info(wav, fallback_ch=CICP_CH[cicp])
                    if secs >= 0.5:
                        return (tag, cicp), wav, meta.get("res"), True
            except Exception:
                pass
            done_marker.unlink(missing_ok=True)

        # Remove eventual WAV incompleto da tentativa anterior antes de recomeçar este item
        wav.unlink(missing_ok=True)
        target_kind = tag[0]                                    # p = preset, g = one group, m = several groups (e.g. an L/R pair)
        target_id = [int(x) for x in tag[1:].split("-")] if target_kind == "m" else int(tag[1:])
        carrier_pid = opt.get("carrier_preset", target_id if target_kind != "m" else target_id[0])
        job_audio, job_states, iso_dir = audio, states, None
        if opt.get("isolation") == "edit" and target_kind in "gm":
            # the preset does not let its elements be muted: decode a temporary copy whose scene plays only the wanted elements
            iso_dir = tmp / f"iso_{tag}"
            iso_dir.mkdir(parents=True, exist_ok=True)
            (iso_dir / TMP_MARK).write_text("dtv_tools temporary folder\n")
            job_audio = iso_dir / "audio.mp4"
            check_free_space(iso_dir, audio.stat().st_size + (64 << 20), "the edited copy of the audio")
            if isolate_stream(audio, job_audio, opt["iso_keep"], opt["iso_preset"]) == 0:
                raise Fail(f"Não consegui editar a cena de áudio para isolar {opt['title']}.")
            job_states = load_scene(iso_dir, ui, job_audio, quiet=True)
            target_kind, target_id, carrier_pid = "p", opt["iso_preset"], opt["iso_preset"]
        script_path = tmp / f"script_{tag}.xml"
        try:
            has_script = action_script_for_target(
                script_path, job_states, target_kind, target_id, carrier_pid, a.uim_version, a.sleep_mode
            )
            res = decode_one(
                dec, ffmpeg, mp4box, job_audio, wav, cicp,
                script_path if has_script else None,
                job_states, target_kind, target_id, carrier_pid, dur, a
            )
        finally:
            if iso_dir is not None and not a.keep_temp:
                safe_rmtree(iso_dir)
        done_marker.write_text(json.dumps({"sig": dec_sig, "res": res}, ensure_ascii=False), encoding="utf-8")
        return (tag, cicp), wav, res, False

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as ex:
            futs = [ex.submit(_job, item) for item in needed_inputs.items()]
            for fut in concurrent.futures.as_completed(futs):
                key, wav, res, was_reused = fut.result()
                wav_paths[key] = wav
                done_n[0] += 1
                if was_reused:
                    reused_wavs[0] += 1
                if res:
                    cut_warnings[key] = res
                    if res.get("resumed"):
                        note(f"{wav.name}: falha em {fmt_hms(res['cut'])} ({res['error']}); retomado em {fmt_hms(res['resumed'])} e sincronizado até o fim.")
                    elif res.get("silent_fallback"):
                        note(f"{wav.name}: sem preset decodificável ({res['error']}); preenchido com silêncio para não interromper o processo.")
                    elif res.get("cut"):
                        note(f"{wav.name}: o mpeghDecoder parou em {fmt_hms(res['cut'])} ({res['error']}); áudio preservado até esse ponto e preenchido até o final.")
                progress(pfx + "decode", done_n[0] / max(len(needed_inputs), 1), f"{done_n[0]}/{len(needed_inputs)} prontos", force=True)
    finally:
        stop_mon.set()
        mon_thread.join(timeout=1.0)

    dec_msg = f"{len(wav_paths)} trilha(s) base decodificada(s)"
    if reused_wavs[0]:
        dec_msg += f" ({reused_wavs[0]} reaproveitada(s))"
    step(pfx + "decode", "done", dec_msg)
    progress(pfx + "decode", 1.0, force=True)

    step(pfx + "check", "running")
    checked_info = []
    sorted_wavs = sorted(wav_paths.items())
    for idx, ((tag, cicp), wav) in enumerate(sorted_wavs):
        progress(pfx + "check", idx / max(len(sorted_wavs), 1), f"Verificando {wav.name}…", force=True)
        secs, ch = wav_info(wav, fallback_ch=CICP_CH[cicp])
        checked_info.append(f"{tag} ({fmt_hms(secs)}, {ch}ch)")
    step(pfx + "check", "done", ", ".join(checked_info))
    progress(pfx + "check", 1.0, force=True)

    step(pfx + "encode", "running")
    made = []
    keep_audio_files = to_outdir
    n_chosen = max(len(chosen), 1)
    reused_enc = 0

    # Mapa de arquivos de saída já alocados na execução anterior (para não criar "(1)" ao retomar)
    enc_map_path = tmp / "encode_plan.json"
    enc_plan = {}
    if enc_map_path.is_file():
        try:
            enc_plan = json.loads(enc_map_path.read_text(encoding="utf-8"))
        except Exception:
            enc_plan = {}

    norm_target = loudness_target(a)
    loud_info = {}
    for idx, o in enumerate(chosen):
        tag, cicp = o["inputs"][0]
        wav = wav_paths[(tag, cicp)]
        ext = FORMATS[o["fmt"]]["ext"]
        target_dir = outdir if keep_audio_files else tmp
        plan_key = f"{o['id']}|{o['fmt']}|{o.get('br') or ''}|{cicp}" + (f"|N{norm_target:g}/{a.true_peak:g}" if norm_target is not None else "")
        enc_done = tmp / f"enc_{slug(plan_key)}.done"

        if plan_key in enc_plan:
            out_file = Path(enc_plan[plan_key])
        else:
            out_file = unique_path(target_dir / f"{src.stem}_{o['stem']}{ext}", a.overwrite)
            enc_plan[plan_key] = str(out_file)
            enc_map_path.write_text(json.dumps(enc_plan, ensure_ascii=False), encoding="utf-8")

        trim_s = 0.0
        try:
            tables = mp4_tables(str(src))
            at = next((t for t in tables if t.get("handler") == "soun" or t.get("id") == 2), None)
            if at:
                _, mt_val, _ = mp4_edit_info(at)
                if mt_val is not None and mt_val > 0:
                    trim_s = mt_val / at["ts"]
        except Exception:
            pass

        if out_file.is_file() and out_file.stat().st_size > 0 and enc_done.is_file():
            reused_enc += 1
            progress(pfx + "encode", (idx + 1) / n_chosen, f"[{idx + 1}/{len(chosen)}] {out_file.name} (reaproveitado)", force=True)
        else:
            enc_done.unlink(missing_ok=True)
            out_file.unlink(missing_ok=True)
            has_cut = (tag, cicp) in cut_warnings
            progress(pfx + "encode", idx / n_chosen, f"[{idx + 1}/{len(chosen)}] Convertendo {out_file.name}…", force=True)
            gain = 0.0
            loud_info.pop(o["id"], None)
            if norm_target is not None:
                progress(pfx + "encode", idx / n_chosen, f"[{idx + 1}/{len(chosen)}] Medindo o volume de {out_file.name}…", force=True)
                m = measure_loudness(ffmpeg, wav)
                if m is None:
                    note(f"{out_file.name}: sem sinal para medir o volume; mantido como está.")
                else:
                    gain, limited = loudness_gain(m, norm_target, a.true_peak)
                    loud_info[o["id"]] = {"input_lufs": m["i"], "input_tp": m["tp"], "target_lufs": norm_target, "gain_db": gain,
                                          "limited_by_peak": limited, "output_lufs": round(m["i"] + gain, 2)}
                    say(f"  Volume de {out_file.name}: {m['i']:.1f} LUFS, pico {m['tp']:.1f} dBTP → {m['i'] + gain:.1f} LUFS ({gain:+.2f} dB)"
                        + (f" — limitado pelo pico máximo de {a.true_peak:g} dBTP; o alvo era {norm_target:g} LUFS" if limited else ""))
            encode_audio(
                ffmpeg, wav, out_file, o["fmt"], o.get("br"), dur, cut_warn=has_cut, gain_db=gain, trim_s=trim_s,
                on_pct=lambda sub_f, i=idx, fn=out_file.name: progress(
                    pfx + "encode", (i + sub_f) / n_chosen, f"[{i + 1}/{len(chosen)}] {fn} ({sub_f * 100:.0f}%)"
                )
            )
            enc_done.write_text("ok", encoding="utf-8")
            progress(pfx + "encode", (idx + 1) / n_chosen, f"[{idx + 1}/{len(chosen)}] {out_file.name} concluído", force=True)

        made.append({
            "kind": o["group"],
            "title": o["title"].replace(':', ''),
            "path": str(out_file),
            "size": out_file.stat().st_size,
            "duration": dur,
            "option_id": o["id"],
            **({"loudness": loud_info[o["id"]]} if o["id"] in loud_info else {}),
        })

    enc_msg = f"{len(made)} arquivo(s) de áudio gerado(s)"
    if reused_enc:
        enc_msg += f" ({reused_enc} reaproveitado(s))"
    step(pfx + "encode", "done", enc_msg)
    progress(pfx + "encode", 1.0, force=True)

    return {"made": made, "chosen": chosen, "dur": dur, "use_layout": use_layout, "info": info, "states": states,
            "enc_plan": enc_plan, "enc_map_path": enc_map_path}


def cmd_mpegh_audio(a):
    loudness_target(a)                                                 # validates --normalize / --true-peak before any long work
    src = Path(a.input).resolve()
    if not src.is_file():
        raise Fail(f"Arquivo não encontrado: {src}")
    mp4box = find_mp4box(a.mp4box)
    ffmpeg = need_tool(a.ffmpeg, "ffmpeg")
    ui = need_tool(a.mpegh_ui, "mpeghUiManager")
    dec = need_tool(a.mpegh_decoder, "mpeghDecoder")

    set_steps(MP_STEPS)
    set_stages({"prepare": 15, "decode": 50, "check": 5, "encode": 20, "mux": 10, "report": 0})

    outdir = Path(a.outdir).resolve() if a.outdir else src.parent / f"{src.stem}_audios"
    outdir.mkdir(parents=True, exist_ok=True)
    tmp = (Path(a.tmpdir).resolve() / f"{src.stem}_mptmp") if a.tmpdir else outdir / f".{src.stem}_mptmp"
    tmp_session(tmp, {"v": 1, "src": src_sig(src), "uim": a.uim_version, "native": str(a.native_layout)})

    succeeded = False
    try:
        mux_mode = (a.mux or "none").lower()
        ctx = mpegh_render(a, src, (ffmpeg, mp4box, ui, dec), outdir, tmp, a.option or None, to_outdir=mux_mode in ("none", "add"))
        made, dur, use_layout, info, states = ctx["made"], ctx["dur"], ctx["use_layout"], ctx["info"], ctx["states"]
        enc_plan, enc_map_path = ctx["enc_plan"], ctx["enc_map_path"]

        if mux_mode in ("add", "only"):
            step("mux", "running")
            progress("mux", 0.0, "Iniciando muxagem no MP4Box…", force=True)
            mux_out = Path(enc_plan["__mux__"]) if "__mux__" in enc_plan else unique_path(outdir / f"{src.stem}_multiaudio.mp4", a.overwrite)
            enc_plan["__mux__"] = str(mux_out)
            enc_map_path.write_text(json.dumps(enc_plan, ensure_ascii=False), encoding="utf-8")
            mux_out.unlink(missing_ok=True)

            with MP4BoxJob(src) as box:
                cmd_mux = [mp4box, "-add", box.inp(src)]
                delay_ms = int(a.audio_delay or 0)
                for item in made:
                    spec_add = f"{box.inp(item['path'])}#audio:name={mp4box_title(item['title'])}"
                    if delay_ms != 0:
                        spec_add += f":delay={delay_ms}"
                    cmd_mux += ["-add", spec_add]
                cmd_mux += ["-new", box.out(mux_out)]
                box.run(cmd_mux, "muxagem multiaudio", lambda f, d_txt: progress("mux", f, d_txt))
            mux_entry = {
                "kind": "mux",
                "title": "MP4 com faixas de áudio acrescentadas",
                "path": str(mux_out),
                "size": mux_out.stat().st_size,
                "duration": dur,
            }
            if mux_mode == "only":
                for item in made:
                    Path(item["path"]).unlink(missing_ok=True)
                made = [mux_entry]
            else:
                made.append(mux_entry)
            step("mux", "done", mux_out.name)
            progress("mux", 1.0, force=True)
        else:
            step("mux", "skipped", "não solicitado")

        step("report", "running")
        rows = [
            ["Duração", fmt_hms(dur) if dur else "n/d"],
            ["Layout utilizado", f"{CICP_NAME.get(use_layout, '2.0')} (CICP {use_layout})"],
            ["Estados de cena", str(len(states))],
            ["Presets encontrados", ", ".join(f"P{p['id']} ({p['name']})" for p in info["unified_presets"])],
            ["Grupos encontrados", ", ".join(f"G{g['id']} ({g['name']})" for g in info["unified_groups"])],
            ["Saídas geradas", str(len(made))],
        ]
        ev(type="result", out=made[0]["path"] if made else str(outdir), outputs=made, notes=NOTES, rows=rows)
        step("report", "done")
        progress("report", 1.0, force=True)
        succeeded = True
    finally:
        if not succeeded:
            say(f"Execução interrompida: arquivos intermediários preservados em {tmp} para permitir retomar.", "warn")
        elif a.keep_temp:
            say(f"Arquivos intermediários mantidos em: {tmp}")
        else:
            safe_rmtree(tmp)


# ──────────────────────────── mp4-cut ────────────────────────────
CUT_STEPS = [("read", "Read the keyframe table"), ("cut", "Cut without re-encoding (MP4Box)"),
             ("sync", "Check / fix the A/V sync"), ("verify", "Check the LCEVC enhancement")]
CUT_WEIGHTS = {"read": 8, "cut": 72, "sync": 10, "verify": 10}


def fmt_clock(sec):
    sec = max(0.0, float(sec))
    return f"{int(sec // 3600):02d}:{int(sec % 3600 // 60):02d}:{sec % 60:06.3f}"


def parse_time_input(t_str):
    """'HH:MM:SS.mmm', 'MM:SS' or 'SS.mmm' (comma accepted) -> seconds."""
    try:
        parts = [float(p) for p in str(t_str).strip().replace(",", ".").split(":")]
    except ValueError:
        raise Fail(f"Invalid time: {t_str!r} (use HH:MM:SS.mmm, MM:SS or seconds).")
    if not 1 <= len(parts) <= 3 or any(p < 0 for p in parts):
        raise Fail(f"Invalid time: {t_str!r}.")
    total = 0.0
    for p in parts:
        total = total * 60 + p
    return total


def cut_timeline(path):
    """
    Cut points of the video track: [(seconds, frame_number_1_based, kind)], plus (total_seconds, frames, timescale, notes).
    Only IDR frames (or frames whose type cannot be read) are offered: a CRA/BLA/GDR may start with leading pictures that
    cannot be decoded after a cut.
    """
    tracks = mp4_tables(path)
    v = next((t for t in tracks if t["handler"] == "vide"), None)
    if not v or not v["dts"]:
        raise Fail("No video track found in the file (fragmented MP4 is not supported).")
    ts, n = v["ts"], len(v["dts"])
    sync = v["sync"] if v["sync"] is not None else list(range(n))
    notes = []
    with open(path, "rb") as f:
        check = len(sync) <= 50000
        items = [(v["dts"][k] / ts, k + 1, mp4_sync_kind(v, f, k) if check else "?") for k in sync if 0 <= k < n]
    if not items:
        raise Fail("The video track has no sync samples (stss).")
    good = [i for i in items if i[2] in ("IDR", "?")]
    if len(good) != len(items):
        notes.append(f"{len(items) - len(good)} sync sample(s) of type CRA/BLA/GDR were left out of the cut points (leading pictures).")
    if not good:
        notes.append("No IDR frame found: cutting at CRA/GDR frames, the first pictures of a cut may not decode.")
        good = items
    shift = mp4_present(v, items[0][1] - 1) - items[0][0]
    if abs(shift) > 0.001:
        notes.append(f"The player timeline is {shift:+.3f}s from the decode clock (edit list / frame reordering); cut times use the decode clock.")
    total = v.get("dts_end", v["dts"][-1]) / ts
    return good, total, n, ts, notes


def idr_candidates(idr, target):
    times = [x[0] for x in idr]
    pos = bisect.bisect_left(times, target)
    prev_i = max(0, pos - 1) if (pos == len(times) or times[pos] > target) else pos
    next_i = min(len(times) - 1, pos)
    if prev_i == next_i and next_i + 1 < len(times):
        next_i = prev_i + 1
    p, nx = idr[prev_i], idr[next_i]
    return {"prev": p, "next": nx, "nearest": p if abs(p[0] - target) <= abs(nx[0] - target) else nx}


def _prompt_choice(label, target, cands, total, is_end):
    opts = [("IDEAL (nearest IDR)", cands["nearest"]), ("Previous IDR", cands["prev"]), ("Next IDR", cands["next"])]
    if is_end:
        opts.append(("Exact end of file", (total, -1, "")))
    print(f"\n SELECT THE {label.upper()} POINT (requested {fmt_clock(target)})")
    for i, (d, (t, n, _)) in enumerate(opts, 1):
        print(f" [{i}] {d:<22}: {fmt_clock(t)} | {'frame #' + str(n) if n > 0 else 'EOF':<12} | {(t - target) * 1000:+7.0f} ms")
    while True:
        ans = input(f"Choice for {label} [1]: ").strip()
        if not ans:
            return opts[0][1][0]
        if ans.isdigit() and 1 <= int(ans) <= len(opts):
            return opts[int(ans) - 1][1][0]
        print(f" -> invalid, type 1-{len(opts)}.")


def plan_cut(idr, total, raw_s, raw_e, snap, end_snap, interactive=False):
    """(start_idr_seconds, end_seconds, start_frame_number) or raises Fail."""
    req_s, req_e = parse_time_input(raw_s), parse_time_input(raw_e)
    if req_s < 0 or req_s >= total:
        raise Fail(f"Start {req_s:.3f}s must be between 0 and {total:.3f}s.")
    if req_e <= req_s:
        raise Fail(f"End ({req_e:.3f}s) must be greater than start ({req_s:.3f}s).")
    if req_e > total:
        note(f"End {req_e:.3f}s is past the end of the file; using {total:.3f}s.")
        req_e = total
    sc = idr_candidates(idr, req_s)
    a = _prompt_choice("START", req_s, sc, total, False) if interactive else sc[snap][0]
    a_item = next(i for i in idr if i[0] == a)
    later = [i for i in idr if i[0] > a + 0.001]
    if end_snap == "eof" or not later:
        b = total
    else:
        ec = idr_candidates(later, req_e)
        b = _prompt_choice("END", req_e, ec, total, True) if interactive else ec[end_snap][0]
    if b <= a:
        raise Fail("The chosen end point is not after the start point.")
    return a, b, a_item[1]


def estimate_cut_bytes(mp4_path, t_in, t_out, total_dur):
    """Rough size of a cut (proportional to its duration): the reference for the progress of MP4Box."""
    if total_dur <= 0:
        return 0
    return max(1, int(os.path.getsize(mp4_path) * max(0.0, t_out - t_in) / total_dur))


def run_cut_process(cmd, out_path, expected_bytes=0, on_info=None, poll=0.25, cwd=None):
    """
    Runs MP4Box and follows its progress through the bytes written to out_path (or the % MP4Box prints while the file
    does not exist yet). on_info(dict) receives: pct (0-100), elapsed, eta, total, speed (MB/s), size, expected.
    Returns (ok, last_output_lines).
    """
    t0 = time.time()
    state = {"pct": None, "tail": "", "last": 0.0}
    proc = sp_popen([str(c) for c in cmd], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, cwd=cwd, **NOWIN)

    def reader():
        buf = b""
        while True:
            ch = proc.stdout.read(1)
            if not ch:
                break
            if ch not in (b"\r", b"\n"):
                buf += ch
                continue
            line, buf = buf.decode("utf-8", "replace"), b""
            if not line.strip():
                continue
            state["tail"] = (state["tail"] + line.strip() + "\n")[-1500:]
            m = re.search(r"\((\d+)/(\d+)\)", line)
            if m and int(m.group(2)) > 0:
                state["pct"] = 100.0 * int(m.group(1)) / int(m.group(2))
            else:
                m = re.search(r"(\d+(?:\.\d+)?)\s*%", line)
                if m:
                    state["pct"] = float(m.group(1))

    th = threading.Thread(target=reader, daemon=True)
    th.start()

    def snapshot(final=False, ok=True):
        elapsed = time.time() - t0
        try:
            size = os.path.getsize(out_path)
        except OSError:
            size = 0
        pct = 100.0 * size / expected_bytes if (size > 0 and expected_bytes) else (state["pct"] or 0.0)
        pct = max(state["last"], min(pct, 99.9))          # monotonic; reaches 100 only when MP4Box ends
        if final and ok:
            pct = 100.0
        state["last"] = pct
        expected = size if (final and ok) else expected_bytes
        processed = size if (final or not expected_bytes) else max(size, expected_bytes * pct / 100.0)
        speed = processed / 1048576.0 / elapsed if elapsed > 0 else 0.0
        eta = elapsed * (100.0 - pct) / pct if pct >= 1.0 else None
        total = elapsed + eta if eta is not None else None
        return {"pct": pct, "elapsed": elapsed, "eta": eta, "total": total, "speed": speed, "size": size, "expected": expected}

    while proc.poll() is None:
        if on_info:
            on_info(snapshot())
        time.sleep(poll)
    th.join(timeout=2)
    ok = proc.returncode == 0 and os.path.exists(out_path)
    if on_info:
        on_info(snapshot(final=True, ok=ok))
    return ok, state["tail"]


def cut_segment(mp4box, src, out, t_in, t_out, total_dur, on_info=None):
    """
    Lossless cut of src between two times (the start must be an IDR from cut_timeline) with MP4Box -splitx.
    An existing `out` is replaced: the caller decides the name (see unique_path). Returns (ok, last_output_lines).
    """
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()
    # the start is 1 ms before the IDR so that MP4Box lands exactly on the chosen frame
    with MP4BoxJob(src) as box:
        cmd = [mp4box, "-splitx", f"{max(0.0, t_in - 0.001):.4f}:{t_out:.4f}", box.inp(src), "-out", box.out(out)]
        log("$ " + subprocess.list2cmdline(cmd))
        ok, tail = run_cut_process(cmd, str(box.out_file(out)), estimate_cut_bytes(str(src), t_in, t_out, total_dur), on_info,
                                   cwd=str(box.dir))
        if ok:
            box.commit()
        return ok and out.exists(), tail


# --- A/V sync after the cut: MP4Box -splitx may lose the initial delay (empty edit) of a non-video track ---
def _match_sample(src_tr, f_src, sig, expected, cand=None):
    """Finds in the source the sample that opens the same stretch as the output: size sequence + bytes must match.
    Returns (index, ambiguous)."""
    pts, seq = sig
    sizes, offs = src_tr["sizes"], mp4_sample_offsets(src_tr)
    n = len(seq)
    idxs = cand if cand is not None else range(len(sizes))
    c = [i for i in idxs if sizes[i] == seq[0] and i + n <= len(sizes)]
    if expected is not None:
        c.sort(key=lambda i: abs(src_tr["dts"][i] - expected))
    found = []
    for i in c[:2000]:
        if sizes[i:i + n] != seq:
            continue
        ok = True
        for k, b in pts:
            f_src.seek(offs[i + k])
            if f_src.read(len(b)) != b:
                ok = False
                break
        if ok:
            found.append(i)
            if len(found) == 2:
                break
    if not found:
        return None, False
    return found[0], len(found) > 1


def _heads(tr, f, spread=True):
    offs = mp4_sample_offsets(tr)
    total = min(len(tr["sizes"]), len(offs))
    ks = list(range(min(4 if spread else 2, total)))
    if spread and total > 4:
        step_ = max(1, total // 24)
        ks += list(range(step_, min(total, 900), step_))
    pts = []
    for k in sorted(set(ks)):
        f.seek(offs[k])
        pts.append((k, f.read(min(tr["sizes"][k], 256))))
    return pts, tr["sizes"][:min(total, 3000 if spread else 2)]


def av_measure(src_mp4, out_mp4, t_in=None, src_tables=None):
    """Offset of every non-video track of the output relative to the source, video as reference.
    drift < 0 => early, drift > 0 => late. Returns (per_track_list, info{video_sample, note})."""
    src_t = src_tables or mp4_tables(src_mp4)
    out_t = mp4_tables(out_mp4)
    sv = next((t for t in src_t if t["handler"] == "vide"), None)
    ov = next((t for t in out_t if t["handler"] == "vide"), None)
    if not sv or not ov or not ov["sizes"]:
        return [], {"note": "No comparable video track."}
    rel, info = [], {"video_sample": None, "note": ""}
    with open(src_mp4, "rb") as fs, open(out_mp4, "rb") as fo:
        exp_v = int(t_in * sv["ts"]) if t_in is not None else None
        j0, _ = _match_sample(sv, fs, _heads(ov, fo, spread=False), exp_v, sv["sync"])
        nota_v = ""
        if j0 is None and exp_v is not None and sv["sync"]:
            j0 = min(sv["sync"], key=lambda i: abs(sv["dts"][i] - exp_v))
            nota_v = "video did not match by bytes; reference = requested IDR"
        else:
            info["video_sample"] = j0
        if j0 is None:
            return [], {"note": "The first video frame could not be matched with the source."}
        t_ref = mp4_present(sv, j0) - mp4_present(ov, 0)
        for ot in out_t:
            if ot is ov or not ot["sizes"]:
                continue
            st = next((t for t in src_t if t["id"] == ot["id"] and t["handler"] == ot["handler"]), None) \
                or next((t for t in src_t if t["handler"] == ot["handler"] and t["fourcc"] == ot["fourcc"]), None)
            r = {"id": ot["id"], "handler": ot["handler"], "fourcc": ot["fourcc"], "drift_ms": None,
                 "ent": None, "ts": ot["ts"], "mt": None, "note": nota_v}
            if st is None:
                r["note"] = "track without a counterpart in the source"
                rel.append(r)
                continue
            empty_s, mt_s, _ = mp4_edit_info(st)
            i0, amb = _match_sample(st, fs, _heads(ot, fo), mt_s + (t_ref - empty_s) * st["ts"])
            r["amb"] = amb
            if i0 is None:
                r["note"] = "samples did not match the source (cannot be verified)"
                rel.append(r)
                continue
            drift = mp4_present(ot, 0) - (mp4_present(st, i0) - t_ref)
            _, mt_o, ent = mp4_edit_info(ot)
            r.update(drift_ms=drift * 1000.0, ent=ent, mt=mt_o)
            rel.append(r)
    return rel, info


def av_fix(out_mp4, rel, limit_ms=0.5):
    """Writes the correction into the output's 'elst' (4/8 bytes, in place)."""
    with open(out_mp4, "r+b") as f:
        for r in rel:
            d = r.get("drift_ms")
            if d is None or abs(d) < limit_ms:
                continue
            if r.get("amb"):
                r["note"] = "ambiguous measurement (identical audio, e.g. digital silence): not fixed"
                continue
            if r["ent"] is None:
                r["note"] = "no edit list in the output: cannot fix"
                continue
            new_mt = r["mt"] + int(round(d / 1000.0 * r["ts"]))
            if new_mt < 0:
                r["note"] = "no pre-roll margin before the first sample: cannot fix"
                continue
            f.seek(r["ent"]["off"])
            f.write(struct.pack(">q" if r["ent"]["sz"] == 8 else ">i", new_mt))
            r["fixed"] = True
    return rel


def av_check(src_mp4, out_mp4, t_in, expected_frame, fix=True, src_tables=None):
    """Measures (and fixes) the A/V sync of the output; also confirms which source frame opens the cut."""
    r = av_check_ex(src_mp4, out_mp4, t_in, expected_frame, fix, src_tables)
    return r["lines"], r["fixed"]


def av_check_ex(src_mp4, out_mp4, t_in, expected_frame, fix=True, src_tables=None, echo=True):
    """
    Same as av_check, with a structured result: {"lines": [...], "fixed": bool, "status": ...}.
    status: "ok" (every track in sync), "fixed" (a drift was found and corrected), "drift" (a drift remains),
    "unverified" (nothing could be measured). echo=False does not print the lines.
    """
    lines, fixed, rel = [], False, []
    try:
        rel, info = av_measure(src_mp4, out_mp4, t_in, src_tables)
        if info.get("note"):
            lines.append(info["note"])
        vs = info.get("video_sample")
        if vs is not None and expected_frame and vs + 1 != expected_frame:
            note(f"The cut opens at source frame #{vs + 1}, not at the requested IDR #{expected_frame}.")
            lines.append(f"cut starts at frame #{vs + 1} (requested #{expected_frame})")
        elif vs is not None:
            lines.append(f"cut starts exactly at the chosen IDR (frame #{vs + 1})")
        after = {}
        if fix and any(r.get("drift_ms") is not None and abs(r["drift_ms"]) >= 0.5 for r in rel):
            av_fix(out_mp4, rel)
            fixed = any(r.get("fixed") for r in rel)
            after = {r["id"]: r for r in av_measure(src_mp4, out_mp4, t_in, src_tables)[0]}
        for r in rel:
            name = f"track {r['id']} ({r['handler']}/{r['fourcc']})"
            d = r["drift_ms"]
            if d is None:
                lines.append(f"{name}: {r['note']}")
            elif abs(d) < 0.5:
                lines.append(f"{name}: in sync ({d:+.1f} ms)")
            else:
                sent = "EARLY" if d < 0 else "LATE"
                if r.get("fixed"):
                    res = after.get(r["id"], {}).get("drift_ms")
                    lines.append(f"{name}: was {sent} by {abs(d):.1f} ms -> FIXED" + (f" (residual {res:+.1f} ms)" if res is not None else ""))
                else:
                    lines.append(f"{name}: {sent} by {abs(d):.1f} ms ({r['note'] or 'not fixed'})")
    except Exception as e:  # the sync check never aborts a finished cut
        lines.append(f"A/V check unavailable: {e}")
    measured = [r for r in rel if r.get("drift_ms") is not None]
    if not measured:
        status = "unverified"
    elif any(r.get("fixed") for r in measured):
        status = "fixed"
    elif any(abs(r["drift_ms"]) >= 0.5 for r in measured):
        status = "drift"
    else:
        status = "ok"
    if echo:
        for ln in lines:
            say(f"[A/V] {ln}")
    return {"lines": lines, "fixed": fixed, "status": status}


def cmd_mp4_cut(a):
    src = Path(a.input).resolve()
    if not src.is_file():
        raise Fail(f"File not found: {src}")
    mp4box = find_mp4box(a.mp4box)
    ffmpeg_bin = optional_tool("ffmpeg", a.ffmpeg)
    set_steps(CUT_STEPS)
    set_stages(CUT_WEIGHTS)
    starts, ends = list(a.start or []), list(a.end or [])
    if len(starts) != len(ends):
        raise Fail("Use --start and --end in pairs (same number of each).")

    say(bold(f"File: {src.name}") + dim(f"  ({human(src.stat().st_size)})"))
    step("read", "running")
    progress("read", 0, force=True)
    idr, total, nframes, ts, tl_notes = cut_timeline(str(src))
    for n_ in tl_notes:
        note(n_)
    kinds = sorted({i[2] for i in idr})
    say(f"Duration {fmt_clock(total)} · {nframes} frames · {len(idr)} cut points (1 every ~{total / len(idr):.2f}s) · types: {', '.join(kinds)}")
    step("read", "done", f"{len(idr)} cut points")
    progress("read", 1, force=True)
    if a.list_idr:
        ev(type="result", out="", outputs=[], notes=NOTES,
           rows=[["Duration", fmt_clock(total)], ["Frames", str(nframes)], ["Cut points", str(len(idr))]],
           scan={"duration": total, "frames": nframes, "timescale": ts,
                 "idr": [{"time": round(t, 6), "frame": n, "kind": k} for t, n, k in idr]})
        return

    interactive = not starts and sys.stdin is not None and sys.stdin.isatty() and not PJ
    cuts = []
    if interactive:
        while True:
            rs = input("\nSTART time (e.g. 00:01:15.5 or 75.5): ").strip()
            re_ = input("END time   (e.g. 00:02:30 or 150): ").strip()
            try:
                cuts.append(plan_cut(idr, total, rs, re_, "nearest", "nearest", interactive=True))
            except Fail as e:
                say(str(e), "err")
            if input("\nAdd another cut from this file? [y/N]: ").strip().lower() not in ("y", "yes", "s", "sim"):
                break
    elif not starts:
        raise Fail("Give --start/--end (repeat the pair for several cuts), or use --list-idr to list the cut points.")
    else:
        for rs, re_ in zip(starts, ends):
            cuts.append(plan_cut(idr, total, rs, re_, a.snap, a.end_snap or a.snap))
    if not cuts:
        raise Fail("No valid cut to run.")

    base, ext = src.with_suffix(""), src.suffix or ".mp4"
    outs = []
    for i, (s_, e_, _) in enumerate(cuts, 1):
        if a.output:
            o = Path(a.output).resolve()
            if len(cuts) > 1:
                o = o.with_name(f"{o.stem}_{i:02d}{o.suffix}")
        elif len(cuts) == 1:
            o = Path(f"{base}_cut_{s_:.2f}s_{e_:.2f}s{ext}")
        else:
            o = Path(f"{base}_cut_{i:02d}_{s_:.2f}s_{e_:.2f}s{ext}")
        if o.resolve() == src:
            raise Fail("The output file would overwrite the input.")
        outs.append(unique_path(o, a.overwrite))
    ev(type="plan", out=str(outs[0]), outs=[str(o) for o in outs])
    for i, ((s_, e_, _), o) in enumerate(zip(cuts, outs), 1):
        say(f"Cut #{i}: {fmt_clock(s_)} -> {fmt_clock(e_)} | {fmt_hms(e_ - s_)} | {o}")

    step("cut", "running")
    made, fails, src_tables = [], 0, None
    sync_lines = []
    for i, ((s_, e_, frame_no), o) in enumerate(zip(cuts, outs), 1):
        n_ = len(cuts)
        ok, tail = cut_segment(
            mp4box, src, o, s_, e_, total,
            lambda inf, i=i, n_=n_: progress(
                "cut", (i - 1 + inf["pct"] / 100.0) / n_,
                f"cut {i}/{n_}: {human(inf['size'])} / ~{human(inf['expected'])} · {inf['speed']:.1f} MB/s"))
        if not ok:
            fails += 1
            say(f"Cut #{i} failed: {tail.strip()[-300:]}", "err")
            continue
        size = o.stat().st_size
        made.append({"kind": "cut", "title": f"cut {fmt_clock(s_)} - {fmt_clock(e_)}", "path": str(o), "size": size,
                     "start": round(s_, 6), "end": round(e_, 6), "duration": round(e_ - s_, 6)})
        say(f"{o.name} ({human(size)})", "ok")
    step("cut", "done" if made else "error", f"{len(made)}/{len(cuts)} cut(s)")
    if not made:
        raise Fail("MP4Box failed on every cut.")

    step("sync", "running")
    try:
        src_tables = mp4_tables(str(src))
    except Exception:
        src_tables = None
    for i, m in enumerate(made):
        idx = next(k for k, o in enumerate(outs) if str(o) == m["path"])
        lines, _ = av_check(str(src), m["path"], cuts[idx][0], cuts[idx][2], fix=not a.no_av_fix, src_tables=src_tables)
        sync_lines += [f"#{idx + 1}: {x}" for x in lines]
        m["size"] = os.path.getsize(m["path"])
        progress("sync", (i + 1) / len(made), force=True)
    step("sync", "done")

    step("verify", "running")
    verify = None
    for i, m in enumerate(made):
        verify = verify_enhancement(ffmpeg_bin, m["path"], 120)
        progress("verify", (i + 1) / len(made), force=True)
    step("verify", "done" if verify else "skipped", f"{verify[1]}/{verify[0]} frames with enhancement" if verify else "skipped")

    rows = [["Cuts", f"{len(made)}/{len(cuts)}"], ["Total size", human(sum(m["size"] for m in made))]]
    ev(type="result", out=made[0]["path"], outputs=made, notes=NOTES, rows=rows, sync=sync_lines)
    if fails:
        raise Fail(f"{fails} of {len(cuts)} cut(s) failed.")


# ──────────────────────────── dash-capture ────────────────────────────
DASH_STEPS = [("manifest", "Read the manifest"), ("capture", "Capture the segments"),
              ("package", "Package the MP4 (MP4Box)"), ("verify", "Check the LCEVC enhancement")]
DASH_WEIGHTS = {"manifest": 2, "capture": 83, "package": 10, "verify": 5}
DASH_KINDS = ("vvc_base", "lcevc_enh", "audio")
DASH_LOG_LEVELS = {"OFF": 0, "ERROR": 1, "WARN": 2, "INFO": 3, "DEBUG": 4}
SEG_RETRY_SECONDS = 10
RESPONSIBLE_USE = ("Record only streams you are entitled to record. Protected (DRM) streams are not supported "
                   "and nothing here bypasses any protection.")


def _strip_ns(tag):
    return tag.split("}", 1)[-1] if "}" in tag else tag


def _iso_duration(v):
    m = re.fullmatch(r"P(?:(\d+)Y)?(?:(\d+)M)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?)?", (v or "").strip())
    if not m:
        return None
    y, mo, d, h, mi, s_ = (float(x) if x else 0.0 for x in m.groups())
    return ((y * 365 + mo * 30 + d) * 24 + h) * 3600 + mi * 60 + s_


def _iso_datetime(v):
    from datetime import datetime, timezone
    v = (v or "").strip().replace("Z", "+00:00")
    m = re.fullmatch(r"(.*?\d\d:\d\d:\d\d)(?:\.(\d+))?(.*)", v)
    if not m:
        return None
    frac = (m.group(2) or "0")[:6].ljust(6, "0")
    try:
        dt = datetime.fromisoformat(f"{m.group(1)}.{frac}{m.group(3)}")
    except ValueError:
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp()


def _subst(tmpl, rep_id, bw, number=None, time_=None):
    vals = {"RepresentationID": rep_id, "Bandwidth": bw, "Number": number, "Time": time_}

    def f(m):
        name, fmt = m.group(1), m.group(2)
        if name == "":
            return "$"
        v = vals.get(name)
        if v is None:
            return m.group(0)
        if fmt and name != "RepresentationID":
            return fmt % int(v)
        return str(v)
    return re.sub(r"\$(\w*)(%0?\d+d)?\$", f, tmpl)


def _classify_rep(adap, rep):
    ct = adap.get("contentType", "").lower()
    mt = rep.get("mimeType", adap.get("mimeType", "")).lower()
    codecs = rep.get("codecs", adap.get("codecs", "")).lower()
    dep = rep.get("dependencyId", "")
    if "lvc1" in codecs or (dep and "video" in (ct + mt)):
        return "lcevc_enh"
    if "vvc1" in codecs or "vvi1" in codecs or ("video" in (ct + mt) and not dep):
        return "vvc_base"
    if "audio" in ct or "audio" in mt or "mhm1" in codecs or "mha1" in codecs:
        return "audio"
    return "ignore"


def _extra_kind(adap, rep):
    """What a representation that is NOT captured is: subtitles/captions get their own label so the user knows they exist."""
    ct = adap.get("contentType", "").lower()
    mt = rep.get("mimeType", adap.get("mimeType", "")).lower()
    codecs = rep.get("codecs", adap.get("codecs", "")).lower()
    if ct == "text" or mt.startswith("text/") or "ttml" in mt or "vtt" in mt or any(c in codecs for c in ("stpp", "wvtt", "tx3g", "cea", "dfxp")):
        return "subtitle"
    return "other"


def _rep_summary(adap, rep, kind):
    bw = rep.get("bandwidth", "0")
    return {"kind": kind, "id": rep.get("id", ""), "codecs": rep.get("codecs") or adap.get("codecs") or "",
            "mime": rep.get("mimeType") or adap.get("mimeType") or adap.get("contentType") or "",
            "lang": rep.get("lang") or adap.get("lang") or "", "bandwidth": int(bw) if bw.isdigit() else 0,
            "width": rep.get("width") or adap.get("width") or "", "height": rep.get("height") or adap.get("height") or "",
            "role": _child_value(adap, "Role") or _child_value(rep, "Role") or "",
            "label": next((c.text for c in adap if _strip_ns(c.tag) == "Label" and c.text), "") or ""}


def _child_value(el, name):
    for c in el:
        if _strip_ns(c.tag) == name:
            return c.get("value")
    return None


def _base(el, cur):
    for c in el:
        if _strip_ns(c.tag) == "BaseURL" and c.text and c.text.strip():
            import urllib.parse
            return urllib.parse.urljoin(cur, c.text.strip())
    return cur


def parse_mpd_tasks(mpd_bytes, mpd_url, out_dir, now=None, window=12, subs=False):
    """
    Reads a DASH manifest. Supported: SegmentTemplate with SegmentTimeline, or with @duration ($Number$), formatted
    identifiers ($Number%05d$), BaseURL at every level. Returns (tracks, is_dynamic, skipped, info) where info has
    'drm', 'periods' and 'unsupported'. With subs=True the first subtitle/caption representation is returned as
    tracks['subtitle'] (the other languages go to info['extra'] as 'alt_subtitle'); without it subtitles are only listed.
    """
    import urllib.parse
    import xml.etree.ElementTree as ET
    root = ET.fromstring(mpd_bytes)
    is_dynamic = root.get("type", "static").lower() == "dynamic"
    now = time.time() if now is None else now
    mpd_base = _base(root, mpd_url.rsplit("/", 1)[0] + "/")
    periods = [p for p in root if _strip_ns(p.tag) == "Period"]
    info = {"drm": False, "periods": len(periods), "unsupported": [], "extra": []}
    tracks = {k: {"init": None, "segs": [], "meta": None} for k in DASH_KINDS + ("subtitle",)}
    skipped = []
    if not periods:
        return tracks, is_dynamic, skipped, info
    period = periods[-1] if is_dynamic else periods[0]
    p_base = _base(period, mpd_base)
    p_start = _iso_duration(period.get("start", "")) or 0.0
    total_dur = _iso_duration(period.get("duration", "")) or _iso_duration(root.get("mediaPresentationDuration", ""))
    ast = _iso_datetime(root.get("availabilityStartTime", ""))
    for adap in period:
        if _strip_ns(adap.tag) != "AdaptationSet":
            continue
        a_base = _base(adap, p_base)
        adap_tmpl = next((c for c in adap if _strip_ns(c.tag) == "SegmentTemplate"), None)
        for rep in adap:
            if _strip_ns(rep.tag) != "Representation":
                continue
            kind = _classify_rep(adap, rep)
            if any(_strip_ns(c.tag) == "ContentProtection" for el in (adap, rep) for c in el):
                info["drm"] = True
            if kind == "ignore":
                if subs and _extra_kind(adap, rep) == "subtitle":
                    kind = "subtitle"
                else:
                    info["extra"].append(_rep_summary(adap, rep, _extra_kind(adap, rep)))      # listed, never downloaded
                    continue
            rep_id = rep.get("id", "0")
            chosen = tracks[kind]["meta"]
            if chosen is not None and chosen["rep_id"] != rep_id:
                skipped.append(f"{kind}:{rep_id}")
                info["extra"].append(_rep_summary(adap, rep, "alt_" + kind))
                continue
            bw = rep.get("bandwidth", "0")
            track_dir = os.path.join(out_dir, kind)
            os.makedirs(track_dir, exist_ok=True)
            tmpl = next((c for c in rep if _strip_ns(c.tag) == "SegmentTemplate"), adap_tmpl)
            if tmpl is None:
                info["unsupported"].append(f"{kind}:{rep_id} (only SegmentTemplate is supported)")
                continue
            r_base = _base(rep, a_base)
            timescale = int(tmpl.get("timescale", "1") or 1)
            if chosen is None:
                tracks[kind]["meta"] = {
                    "rep_id": rep_id, "timescale": timescale,
                    "width": rep.get("width") or adap.get("width"), "height": rep.get("height") or adap.get("height"),
                    "bandwidth": int(bw) if bw.isdigit() else 0, "lang": rep.get("lang") or adap.get("lang"),
                    "channels": _child_value(rep, "AudioChannelConfiguration") or _child_value(adap, "AudioChannelConfiguration"),
                    "role": _child_value(adap, "Role") or _child_value(rep, "Role"),
                    "codecs": rep.get("codecs") or adap.get("codecs") or "",
                    "label": next((c.text for c in adap if _strip_ns(c.tag) == "Label" and c.text), "") or "",
                }
            init_t, media_t = tmpl.get("initialization", ""), tmpl.get("media", "")
            start_num = int(tmpl.get("startNumber", "1"))
            if init_t:
                tracks[kind]["init"] = (urllib.parse.urljoin(r_base, _subst(init_t, rep_id, bw)), os.path.join(track_dir, "000000_init.mp4"))
            tl = next((c for c in tmpl if _strip_ns(c.tag) == "SegmentTimeline"), None)
            segs = tracks[kind]["segs"]
            if tl is not None:
                curr_t, curr_n = 0, start_num
                for s_ in tl:
                    if _strip_ns(s_.tag) != "S":
                        continue
                    if s_.get("t") is not None:
                        curr_t = int(s_.get("t"))
                    d, r = int(s_.get("d", "0")), int(s_.get("r", "0"))
                    if r < 0:   # open-ended repeat: up to the end of the period or 'now'
                        end_s = (total_dur if total_dur and not is_dynamic else (now - ast - p_start if ast else 0)) or 0
                        r = max(0, int((end_s * timescale - curr_t) // d) - 1) if d else 0
                    for _ in range(r + 1):
                        url = urllib.parse.urljoin(r_base, _subst(media_t, rep_id, bw, curr_n, curr_t))
                        segs.append((curr_t, url, os.path.join(track_dir, f"seg_{curr_t:016d}.m4s"), d / timescale))
                        curr_t += d
                        curr_n += 1
            elif tmpl.get("duration"):
                d = int(tmpl.get("duration"))
                seg_s = d / timescale
                if is_dynamic:
                    if ast is None:
                        info["unsupported"].append(f"{kind}:{rep_id} (dynamic manifest without availabilityStartTime)")
                        continue
                    last = int((now - ast - p_start) // seg_s) - 1
                    nums = range(max(0, last - window + 1), last + 1)
                else:
                    nums = range(int(-(-(total_dur or 0) // seg_s)))
                for k in nums:
                    n_ = k + start_num
                    url = urllib.parse.urljoin(r_base, _subst(media_t, rep_id, bw, n_, k * d))
                    segs.append((k * d, url, os.path.join(track_dir, f"seg_{k * d:016d}.m4s"), seg_s))
            else:
                info["unsupported"].append(f"{kind}:{rep_id} (SegmentTemplate without SegmentTimeline or @duration)")
    return tracks, is_dynamic, skipped, info


SUB_HANDLERS = (b"subt", b"text", b"sbtl", b"clcp")


def m4s_shift_tfdt(data, delta):
    """A media segment with every baseMediaDecodeTime moved earlier by `delta` ticks (floored at 0). Returns (bytes, first original time or None)."""
    buf = bytearray(data)
    first = None
    for bt, ps, be in mp4_iter(buf):
        if bt != b"moof":
            continue
        for tt, tps, tbe in mp4_iter(buf, ps, be):
            if tt != b"traf":
                continue
            for ct, cps, cbe in mp4_iter(buf, tps, tbe):
                if ct != b"tfdt" or cbe - cps < 8:
                    continue
                if buf[cps] == 1 and cbe - cps >= 12:
                    old = struct.unpack(">Q", buf[cps + 4:cps + 12])[0]
                    buf[cps + 4:cps + 12] = struct.pack(">Q", max(0, old - delta))
                else:
                    old = struct.unpack(">I", buf[cps + 4:cps + 8])[0]
                    buf[cps + 4:cps + 8] = struct.pack(">I", max(0, min(0xFFFFFFFF, old - delta)))
                if first is None:
                    first = old
    return bytes(buf), first


def build_subtitle_mp4(init_path, segments, timescale, origin_sec, end_sec, dest):
    """
    Joins the init segment and the subtitle segments [(start_ticks, path, dur_sec)] into one fragmented MP4 whose timeline starts at the
    video origin: segments that end before it (or start after `end_sec`) are dropped and the rest are moved by the origin. Returns a dict
    {kept, dropped, first_gap}: first_gap is how much (s) the first kept segment starts before the origin (it is clamped to 0).
    """
    shift = int(round(origin_sec * timescale))
    kept = dropped = 0
    first_gap = 0.0
    with open(dest, "wb") as out:
        with open(init_path, "rb") as f:
            out.write(f.read())
        for start, path, dur in sorted(segments, key=lambda x: x[0]):
            t0 = start / timescale
            if t0 + dur <= origin_sec or (end_sec is not None and t0 >= end_sec):
                dropped += 1
                continue
            with open(path, "rb") as f:
                data, first = m4s_shift_tfdt(f.read(), shift)
            if kept == 0 and first is not None:
                first_gap = max(0.0, origin_sec - first / timescale)
            out.write(data)
            kept += 1
    return {"kept": kept, "dropped": dropped, "first_gap": first_gap}


def lang3(code):
    """An ISO 639 code MP4Box accepts (2-3 letters), or ''."""
    c = (code or "").strip().lower()
    return c if re.fullmatch(r"[a-z]{2,3}", c) else ""


def mp4_subtitle_tracks(path):
    """[{id, fourcc, lang}] of the subtitle/text tracks of a regular MP4 ([] for any other file)."""
    try:
        moov, _, _ = mp4_read_moov(path)
    except (Fail, OSError):
        return []
    res = []
    for bt, s_, e_ in mp4_iter(moov):
        if bt != b"trak":
            continue
        tkhd, mdia = mp4_child(moov, s_, e_, b"tkhd"), mp4_child(moov, s_, e_, b"mdia")
        if not tkhd or not mdia:
            continue
        hdlr = mp4_child(moov, *mdia, b"hdlr")
        if not hdlr or bytes(moov[hdlr[0] + 8:hdlr[0] + 12]) not in SUB_HANDLERS:
            continue
        tid = struct.unpack(">I", moov[_ver_off(moov, tkhd[0], 12, 20):][:4])[0]
        mdhd = mp4_child(moov, *mdia, b"mdhd")
        lang = ""
        if mdhd:
            v = struct.unpack(">H", moov[_ver_off(moov, mdhd[0], 20, 32):][:2])[0]
            lang = "".join(chr(((v >> sh) & 31) + 0x60) for sh in (10, 5, 0))
            lang = lang if lang.isalpha() else ""
        minf = mp4_child(moov, *mdia, b"minf")
        stbl = mp4_child(moov, *minf, b"stbl") if minf else None
        stsd = mp4_child(moov, *stbl, b"stsd") if stbl else None
        fourcc = moov[stsd[0] + 12:stsd[0] + 16].decode("latin1") if stsd and stsd[1] - stsd[0] >= 16 else "?"
        res.append({"id": tid, "fourcc": fourcc, "lang": "" if lang == "und" else lang})
    return res


def reattach_subtitles(mp4box, src, dest, tracks):
    """Copies the subtitle tracks of `src` into the finished MP4 `dest` (ffmpeg cannot write stpp/TTML into MP4). Returns '' or the reason it failed."""
    tmp = dest.with_name(dest.name + ".sub.mp4")
    try:
        with MP4BoxJob(dest) as box:
            cmd = [mp4box]
            for t in tracks:
                cmd += ["-add", f"{box.inp(src)}#trackID={t['id']}"]
            cmd += [box.inp(dest), "-out", box.out(tmp)]
            box.run(cmd, "subtitles", lambda f, d: None)
        if not tmp.is_file() or not mp4_subtitle_tracks(tmp):
            return "o MP4Box não gravou a trilha de legenda"
        os.replace(tmp, dest)
        return ""
    except Fail as e:
        return str(e)
    finally:
        tmp.unlink(missing_ok=True)


def dash_session(a):
    """requests.Session with the User-Agent, extra headers and proxy of a dash-* command."""
    try:
        import requests
    except ImportError:
        raise Fail("Este comando precisa do módulo 'requests': pip install requests")
    sess = requests.Session()
    sess.headers["User-Agent"] = a.user_agent or f"dtv_tools/{__version__}"
    for h in a.header or []:
        if ":" not in h:
            raise Fail(f"--header deve ser 'Nome: valor' (recebi {h!r}).")
        k_, v_ = h.split(":", 1)
        sess.headers[k_.strip()] = v_.strip()
    if a.proxy:
        sess.proxies = {"http": a.proxy, "https": a.proxy}
    return sess


def cmd_dash_info(a):
    """Reads a DASH manifest and says what it carries (video, LCEVC, audio, subtitles) without downloading any media."""
    set_steps([("manifest", "Ler o manifesto")])
    set_stages({"manifest": 100})
    step("manifest", "running")
    sess = dash_session(a)
    try:
        resp = sess.get(a.mpd_url, timeout=a.timeout)
    except Exception as e:
        raise Fail(f"Não consegui baixar o manifesto: {type(e).__name__}: {e}")
    if resp.status_code != 200:
        raise Fail(f"O manifesto respondeu HTTP {resp.status_code}.")
    tmp = tempfile.mkdtemp(prefix="dtvinfo_")
    try:
        tracks, dynamic, skipped, info = parse_mpd_tasks(resp.content, a.mpd_url, tmp, subs=True)
    except Exception as e:
        raise Fail(f"O manifesto não pôde ser lido: {type(e).__name__}: {e}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    subs = []
    sm = tracks["subtitle"]["meta"]
    if sm:
        subs.append({"id": sm["rep_id"], "lang": sm.get("lang") or "", "codecs": sm.get("codecs") or "", "role": sm.get("role") or "",
                     "label": sm.get("label") or "", "recordable": bool(tracks["subtitle"]["segs"])})
    subs += [{"id": e["id"], "lang": e["lang"], "codecs": e["codecs"], "role": e["role"], "label": e["label"], "recordable": False}
             for e in info["extra"] if e["kind"] == "alt_subtitle"]
    streams = {"dynamic": bool(dynamic), "drm": bool(info["drm"]), "subtitles": subs,
               "video": [dict(kind=k, **{x: tracks[k]["meta"].get(x) for x in ("rep_id", "codecs", "width", "height", "bandwidth")}) for k in ("vvc_base", "lcevc_enh") if tracks[k]["meta"]],
               "audio": [{x: tracks["audio"]["meta"].get(x) for x in ("rep_id", "codecs", "lang", "channels", "bandwidth")}] if tracks["audio"]["meta"] else [],
               "unsupported": info["unsupported"]}
    if info["drm"]:
        note("O stream é protegido (ContentProtection/DRM): não pode ser capturado.")
    for u in info["unsupported"]:
        note(f"Endereçamento não suportado, trilha ignorada: {u}")
    rows = [["Manifesto", "dinâmico (ao vivo)" if dynamic else "estático"],
            ["Vídeo", ", ".join(f"{v['codecs']} {v['width']}x{v['height']}" for v in streams["video"]) or "não encontrado"],
            ["Áudio", ", ".join(f"{x['codecs']}" + (f" ({x['lang']})" if x.get("lang") else "") for x in streams["audio"]) or "não encontrado"],
            ["Legenda", ", ".join((x["lang"] or "?") + f" ({x['codecs'] or '?'})" + ("" if x["recordable"] else " [não gravável]") for x in subs) or "nenhuma"]]
    for k_, v_ in rows:
        say(f"  {k_}: {v_}")
    step("manifest", "done", "legenda encontrada" if subs else "sem legenda")
    ev(type="result", out="", outputs=[], notes=NOTES, rows=rows, stream=streams)
    return 0


def compute_live_start(tracks, take_count):
    """Start instant (s) so that BOTH video and LCEVC (and audio) already have a segment: the largest of the starts."""
    starts = []
    for k in DASH_KINDS:
        meta, segs = tracks[k]["meta"], tracks[k]["segs"]
        if meta and segs:
            starts.append(segs[max(0, len(segs) - take_count)][0] / meta["timescale"])
    return max(starts) if starts else None


def filter_from_start(tracks, start_time):
    dropped = {}
    for k in DASH_KINDS:
        meta = tracks[k]["meta"]
        if not meta:
            continue
        ts = meta["timescale"]
        before = len(tracks[k]["segs"])
        tracks[k]["segs"] = [s_ for s_ in tracks[k]["segs"] if (s_[0] / ts) >= (start_time - s_[3] * 0.25)]
        dropped[k] = before - len(tracks[k]["segs"])
    return dropped


def download_to_disk(session, url, filepath, timeout=(4, 10)):
    """Returns (bytes, error). A body shorter than Content-Length counts as a failure."""
    if os.path.exists(filepath) and os.path.getsize(filepath) > 0:
        return os.path.getsize(filepath), None
    try:
        resp = session.get(url, timeout=timeout)
        if resp.status_code != 200:
            return 0, f"HTTP {resp.status_code}"
        body = resp.content
        cl = resp.headers.get("Content-Length")
        if not body or (cl and cl.isdigit() and len(body) != int(cl)):
            return 0, "incomplete body"
        part = filepath + ".part"
        with open(part, "wb") as f:
            f.write(body)
        os.replace(part, filepath)
        return len(body), None
    except Exception as e:
        return 0, f"{type(e).__name__}: {e}"


# ---- ISOBMFF fragments (.m4s) ----
def _find_box(data, target):
    CONTAINERS = {b"moov", b"trak", b"mdia", b"minf", b"stbl", b"moof", b"traf", b"mvex", b"edts", b"dinf"}
    for bt, ps, be in mp4_iter(data):
        if bt == target:
            return data[ps:be]
        if bt in CONTAINERS:
            r = _find_box(data[ps:be], target)
            if r is not None:
                return r
        elif bt == b"stsd" and be - ps > 8:
            body = data[ps:be]
            for sbt, sps, sbe in mp4_iter(body, 8):
                if sbt == target:
                    return body[sps:sbe]
                if sbe - sps > 78:
                    r = _find_box(body[sps + 78:sbe], target)
                    if r is not None:
                        return r
    return None


def parse_init_file(init_path):
    data = open(init_path, "rb").read()
    timescale = 90000
    mdhd = _find_box(data, b"mdhd")
    if mdhd:
        timescale = struct.unpack(">I", mdhd[20:24] if mdhd[0] == 1 else mdhd[12:16])[0]
    default_dur = 0
    trex = _find_box(data, b"trex")
    if trex and len(trex) >= 20:
        default_dur = struct.unpack(">I", trex[12:16])[0]
    nal_len, params = 4, bytearray()
    vvcc = _find_box(data, b"vvcC")
    if vvcc and len(vvcc) >= 5:
        nal_len = ((vvcc[4] >> 1) & 0x03) + 1
        for idx in range(5, len(vvcc) - 4):
            nlen = struct.unpack(">H", vvcc[idx:idx + 2])[0]
            if 4 <= nlen <= len(vvcc) - (idx + 2):
                b0, b1 = vvcc[idx + 2], vvcc[idx + 3]
                if b0 == 0x00 and (b1 & 0x07) == 1 and ((b1 >> 3) & 0x1F) in (13, 14, 15, 16):
                    params.extend(b"\x00\x00\x00\x01" + vvcc[idx + 2:idx + 2 + nlen])
    return timescale, nal_len, bytes(params), default_dur


def parse_m4s_file(m4s_path, default_dur=0):
    """[(pts, dts, payload)] of every sample of a media segment (ticks of the track timescale)."""
    data = open(m4s_path, "rb").read()
    samples = []
    boxes = [(bt, ps, be) for bt, ps, be in mp4_iter(data)]
    for i, (bt, ps, be) in enumerate(boxes):
        if bt != b"moof":
            continue
        moof_offset = ps - 8
        payload = data[ps:be]
        traf = _find_box(payload, b"traf")
        if not traf:
            continue
        d_dur, d_size, base_off = default_dur, 0, None
        tfhd = _find_box(traf, b"tfhd")
        if tfhd and len(tfhd) >= 8:
            fl = struct.unpack(">I", tfhd[:4])[0] & 0xFFFFFF
            pos = 8
            if fl & 1:
                base_off = struct.unpack(">Q", tfhd[pos:pos + 8])[0]
                pos += 8
            if fl & 2:
                pos += 4
            if fl & 8:
                d_dur = struct.unpack(">I", tfhd[pos:pos + 4])[0]
                pos += 4
            if fl & 0x10:
                d_size = struct.unpack(">I", tfhd[pos:pos + 4])[0]
        base_dts = 0
        tfdt = _find_box(traf, b"tfdt")
        if tfdt:
            base_dts = struct.unpack(">Q", tfdt[4:12])[0] if tfdt[0] == 1 else struct.unpack(">I", tfdt[4:8])[0]
        mdat_start = next((b[1] for b in boxes[i + 1:] if b[0] == b"mdat"), None)
        cur = base_dts
        for sbt, sps, sbe in mp4_iter(traf):
            if sbt != b"trun" or sbe - sps < 8:
                continue
            trun = traf[sps:sbe]
            ver = trun[0]
            fl = struct.unpack(">I", trun[:4])[0] & 0xFFFFFF
            count = struct.unpack(">I", trun[4:8])[0]
            pos = 8
            data_off = None
            if fl & 1:
                data_off = struct.unpack(">i", trun[pos:pos + 4])[0]
                pos += 4
            if fl & 4:
                pos += 4
            read_pos = (base_off if base_off is not None else moof_offset) + data_off if data_off is not None else mdat_start
            for _ in range(count):
                s_dur, s_size, cts_off = d_dur, d_size, 0
                if fl & 0x100:
                    s_dur = struct.unpack(">I", trun[pos:pos + 4])[0]
                    pos += 4
                if fl & 0x200:
                    s_size = struct.unpack(">I", trun[pos:pos + 4])[0]
                    pos += 4
                if fl & 0x400:
                    pos += 4
                if fl & 0x800:
                    cts_off = struct.unpack(">i" if ver == 1 else ">I", trun[pos:pos + 4])[0]
                    pos += 4
                if read_pos is None or s_size <= 0 or read_pos + s_size > len(data):
                    raise ValueError(f"{os.path.basename(m4s_path)}: sample outside the segment (corrupt download)")
                samples.append((cur + cts_off, cur, data[read_pos:read_pos + s_size]))
                read_pos += s_size
                cur += s_dur
    return samples


def mp4_sample_to_annexb(sample, nal_len=4):
    if sample.startswith(b"\x00\x00\x00\x01") or sample.startswith(b"\x00\x00\x01"):
        return sample
    pos, total, out = 0, len(sample), bytearray()
    while pos + nal_len <= total:
        n = int.from_bytes(sample[pos:pos + nal_len], "big")
        pos += nal_len
        if n <= 0 or pos + n > total:
            break
        out += b"\x00\x00\x00\x01" + sample[pos:pos + n]
        pos += n
    return bytes(out) if out else sample


def is_vvc_rap_frame(au):
    """True if the access unit has an IRAP/GDR slice: IDR_W_RADL (7), IDR_N_LP (8), CRA (9) or GDR (10)."""
    i = 0
    while True:
        i = au.find(b"\x00\x00\x01", i)
        if i < 0 or i + 4 >= len(au):
            return False
        if ((au[i + 4] >> 3) & 0x1F) in (7, 8, 9, 10):
            return True
        i += 3


class LcevcMerger:
    """
    Pairs VVC frames with their LCEVC payload (by PTS) and writes them IN DECODE ORDER.
    - Before the first RAP that has an LCEVC pair nothing is written (the file starts on a clean 4K IRAP).
    - After the start NO frame is dropped: a frame whose LCEVC never arrives (waited > max_wait frames) is written
      without enhancement and counted, so the GOP is never broken in the middle of the video.
    - frames still unpaired when the capture ends (the tail) are discarded.
    """

    def __init__(self, write, on_start=None, max_wait=240):
        from collections import deque
        self.write, self.on_start, self.max_wait = write, on_start, max_wait
        self.waiting, self.lcevc = deque(), {}
        self.started = False
        self.written = self.paired = self.unpaired = self.skipped_pre = self.dropped_tail = 0

    def add_video(self, pts, dts, au):
        self.waiting.append((pts, dts, au))

    def add_lcevc(self, pts, payload):
        self.lcevc[pts] = payload

    def _emit(self, pts, dts, au, has_enh):
        self.write(pts, dts, au)
        self.written += 1
        self.paired += 1 if has_enh else 0

    def pump(self, final=False):
        q = self.waiting
        while q:
            pts, dts, au = q[0]
            pay = self.lcevc.pop(pts, None)
            if pay is not None:
                q.popleft()
                if not self.started:
                    if not is_vvc_rap_frame(au):
                        self.skipped_pre += 1
                        continue
                    self.started = True
                    if self.on_start:
                        self.on_start(pts, dts)
                self._emit(pts, dts, insert_sei_before_first_slice(au, create_vvc_sei_t35_nalu(pay)), True)
            elif final:
                q.popleft()
                self.dropped_tail += 1
            elif len(q) > self.max_wait:
                q.popleft()
                if self.started:
                    self.unpaired += 1
                    self._emit(pts, dts, au, False)
                else:
                    self.skipped_pre += 1
            else:
                break
        if len(self.lcevc) > 400:
            if q:
                floor = min(p for p, _, _ in q)
                for k in [k for k in self.lcevc if k < floor]:
                    del self.lcevc[k]
            elif len(self.lcevc) > 5000:
                for k in sorted(self.lcevc)[:-2400]:
                    del self.lcevc[k]


def dash_fmt_dur(sec):
    h, r = divmod(int(sec), 3600)
    m, s_ = divmod(r, 60)
    return f"{m:02d}m{s_:02d}s" if h == 0 else f"{h:02d}h{m:02d}m{s_:02d}s"


def dash_fmt_speed(bps):
    if bps <= 0:
        return "-"
    if bps >= 1024 * 1024:
        return f"{bps / 1024 / 1024:.1f}MBps"
    if bps >= 1024:
        return f"{bps / 1024:.0f}KBps"
    return f"{bps:.0f}Bps"


def dash_fmt_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


DASH_LOG_STYLES = {"ERROR": "bold red", "WARN": "yellow", "INFO": "green", "DEBUG": "bright_black"}


class TrackStats:
    SPEED_WINDOW = 3.0

    def __init__(self, tag, style):
        from collections import deque
        self.tag, self.style = tag, style
        self.parts, self.active, self.state = [], False, "Waiting"
        self.seen, self.done = {}, set()
        self.total_dur = self.done_dur = 0.0
        self.bytes_total = 0
        self._samples, self._lock = deque(), threading.Lock()

    def set_meta(self, kind, meta):
        parts = []
        bitrate = f"{round(meta['bandwidth'] / 1000)} Kbps" if meta["bandwidth"] else None
        role = meta["role"].capitalize() if meta["role"] else None
        if kind == "audio":
            if bitrate:
                parts.append(bitrate)
            if meta["lang"]:
                parts.append(meta["lang"])
            if meta["channels"]:
                parts.append(f"{meta['channels']}CH")
        else:
            if meta["width"] and meta["height"]:
                parts.append(f"{meta['width']}x{meta['height']}")
            if bitrate:
                parts.append(bitrate)
        if role:
            parts.append(role)
        self.parts, self.active = parts, True

    def register(self, key, dur):
        if key not in self.seen:
            self.seen[key] = dur
            self.total_dur += dur

    def mark_done(self, key, nbytes):
        if key in self.done:
            return
        self.done.add(key)
        self.done_dur += self.seen.get(key, 0.0)
        self.bytes_total += nbytes
        with self._lock:
            self._samples.append((time.monotonic(), nbytes))

    def speed(self):
        now = time.monotonic()
        with self._lock:
            while self._samples and now - self._samples[0][0] > self.SPEED_WINDOW:
                self._samples.popleft()
            return sum(b for _, b in self._samples) / self.SPEED_WINDOW

    @property
    def info(self):
        return " | ".join(self.parts)

    def line(self):
        """Plain-text version of the status line (no colours), used when rich is not available."""
        total, done = len(self.seen), len(self.done)
        pct = (done * 100 // total) if total else 0
        spd = dash_fmt_speed(self.speed()) if self.state == "Downloading" else "-"
        return (f"{self.tag} {self.info}    {dash_fmt_dur(self.done_dur)}/{dash_fmt_dur(self.total_dur)} "
                f"{done}/{total} {self.state.ljust(11)}{pct:>3}% {spd}")


def render_dash_status(trk, post=None):
    """Colour layout of the live status panel (same as the original capture script); `post` adds the packaging/verify line."""
    from rich.console import Group
    from rich.text import Text
    active = [t for t in trk.values() if t.active]
    if not active:
        return Group(Text(""), Text("Waiting for the manifest...", style="bright_black"))
    width = max(len(t.info) for t in active)
    lines = []
    for t in active:
        total, done = len(t.seen), len(t.done)
        pct = (done * 100 // total) if total else 0
        line = Text(no_wrap=True, overflow="ellipsis")
        line.append(t.tag, style=f"bold {t.style}")
        line.append(" ")
        for i, part in enumerate(t.parts):
            if i:
                line.append(" | ", style="bright_black")
            line.append(part, style="white")
        line.append(" " * (width - len(t.info) + 4))
        line.append(f"{dash_fmt_dur(t.done_dur)}/{dash_fmt_dur(t.total_dur)}", style="grey42")
        line.append(f" {done}/{total} ", style="bright_yellow" if done == total else "white")
        line.append(t.state.ljust(11), style="white" if t.state == "Downloading" else "bright_yellow")
        line.append(f"{pct:>3}%", style="green" if pct >= 100 else "white")
        line.append(f" {dash_fmt_speed(t.speed()) if t.state == 'Downloading' else '-'}",
                    style="green" if t.state == "Downloading" else "grey42")
        lines.append(line)
    if post and post.get("label"):
        line = Text(no_wrap=True, overflow="ellipsis")
        line.append("Pkg", style="bold magenta")
        line.append(" ")
        line.append(post["label"], style="white")
        line.append(" " * max(1, width - len(post["label"]) + 4))
        frac = post.get("frac")
        el = time.time() - post["t0"]
        if frac is not None:
            filled = int(20 * frac)
            line.append("[" + "#" * filled + "-" * (20 - filled) + "]", style="bright_black")
            line.append(f" {int(frac * 100):>3}%", style="green" if frac >= 1 else "white")
        else:
            line.append(("." * (int(el * 2) % 4 + 1)).ljust(4), style="bright_yellow")
        line.append(f" {dash_fmt_dur(el)}", style="grey42")
        if post.get("detail"):
            line.append(f" {post['detail']}", style="grey42")
        lines.append(line)
    return Group(Text(""), *lines)


def dash_fps(pts_list, timescale):
    """Median frame interval of the written frames -> fps (robust to gaps)."""
    s_ = sorted(set(pts_list))
    deltas = sorted(b - a for a, b in zip(s_, s_[1:]) if b > a)
    if len(deltas) < 10 or not timescale:
        return 59.94
    return round(timescale / deltas[len(deltas) // 2], 3)


def validate_dash_sync(mp4_path, expected_delay_s):
    """Valida se o atraso entre áudio e vídeo no MP4 final bate com a expectativa da rede."""
    try:
        tables = mp4_tables(str(mp4_path))
        vt = next((t for t in tables if t["handler"] == "vide"), None)
        at = next((t for t in tables if t.get("id") == 2 or t.get("handler") == "soun"), None)
        
        if not vt or not at:
            return "Validação ignorada: trilhas ausentes."
            
        v_pts = mp4_present(vt, 0)
        a_pts = mp4_present(at, 0)
        
        actual_delay = a_pts - v_pts
        diff = actual_delay - expected_delay_s
        
        # Tolerância de ~1 frame de áudio (21.3ms)
        if abs(diff) > 0.022:
            return f"AVISO CRÍTICO: Dessincronização de {diff * 1000:+.1f} ms no arquivo final! (Esperado: {expected_delay_s * 1000:.1f} ms, Real: {actual_delay * 1000:.1f} ms)"
        return f"Sincronismo validado: desvio de apenas {diff * 1000:+.1f} ms."
    except Exception as e:
        return f"Erro ao validar sincronismo: {e}"


def cmd_dash_capture(a):
    try:
        import requests
    except ImportError:
        raise Fail("dash-capture needs the 'requests' module: pip install requests")
    level = DASH_LOG_LEVELS[a.log_level]
    global VERBOSE

    console = None
    if not PJ:
        try:
            from rich.console import Console
            console = Console(highlight=False)
        except ImportError:
            if sys.stdout.isatty():
                print("Tip: install 'rich' (pip install rich) for the coloured status panel.")

    def clog(lv, msg):
        if DASH_LOG_LEVELS[lv] > level:
            return
        stamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        if console is not None:
            from rich.text import Text
            console.print(Text.assemble((stamp, "bright_black"), " ", (lv.ljust(5), DASH_LOG_STYLES[lv]), " : ", msg), soft_wrap=True)
        else:
            _out(f"{stamp} {lv.ljust(5)} : {msg}")

    global DASH_LOG, ORIG_LOOK
    DASH_LOG = clog
    ORIG_LOOK = not PJ

    if level >= 4:
        VERBOSE = True
    mp4box = find_mp4box(a.mp4box)
    ffmpeg_bin = optional_tool("ffmpeg", a.ffmpeg)
    want_subs = bool(getattr(a, "subtitles", False))
    if a.thread_count < 1:
        raise Fail("--thread-count must be >= 1.")
    set_steps(DASH_STEPS)
    set_stages(DASH_WEIGHTS)

    save_dir = Path(a.save_dir).resolve()
    requested = a.save_name or time.strftime("capture_%Y%m%d_%H%M%S")
    if requested.lower().endswith(".mp4"):
        requested = requested[:-4]
    if BAD_NAME.search(requested):
        raise Fail("The name contains invalid characters.")
    tmp_root = Path(a.tmpdir).resolve() if a.tmpdir else save_dir
    n = 0
    while True:
        name = requested if n == 0 or a.overwrite else f"{requested}_{n}"
        out_mp4 = save_dir / f"{name}.mp4"
        work_dir = tmp_root / f"{name}_tmp"
        if a.overwrite or (not out_mp4.exists() and not work_dir.exists()):
            break
        n += 1
    if n:
        note(f"'{requested}' already exists (MP4 or temporary folder); using '{name}'.")
    save_dir.mkdir(parents=True, exist_ok=True)
    if work_dir.exists() and a.overwrite:
        safe_rmtree(work_dir)
    make_tmp_dir(work_dir)
    temp_vvc, temp_aud = work_dir / "_realtime_merged.vvc", work_dir / "_realtime_audio.mhas"
    ev(type="plan", out=str(out_mp4), outs=[str(out_mp4)])

    clog("INFO", RESPONSIBLE_USE)
    clog("INFO", f"Manifest: {a.mpd_url}")
    clog("INFO", f"Output: {out_mp4}")
    clog("INFO", f"Temporary: {work_dir} (TTL {a.cleanup_after}s after processing)")
    if a.live_record_limit:
        clog("INFO", f"Recording limit: {dash_fmt_dur(a.live_record_limit)}")
    clog("INFO", "Press Ctrl+C (or send SIGTERM) to stop and package the MP4.")

    session = requests.Session()
    pool = max(16, a.thread_count * 2)
    adapter = requests.adapters.HTTPAdapter(pool_connections=pool, pool_maxsize=pool)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session.headers["User-Agent"] = a.user_agent or f"dtv_tools/{__version__}"
    for h in a.header or []:
        if ":" not in h:
            raise Fail(f"--header must be 'Name: value' (got {h!r}).")
        k_, v_ = h.split(":", 1)
        session.headers[k_.strip()] = v_.strip()
    if a.proxy:
        session.proxies = {"http": a.proxy, "https": a.proxy}

    trk = {"vvc_base": TrackStats("Vid", "cyan"), "lcevc_enh": TrackStats("Enh", "green"), "audio": TrackStats("Aud", "bright_blue")}
    processed = {k: set() for k in DASH_KINDS}
    ttl_files = {}
    init_info = {k: None for k in DASH_KINDS}
    failed_logged, first_fail = set(), {}
    abandoned = {k: set() for k in DASH_KINDS}
    aud_wait = []
    sst = {"init": None, "ts": 0, "meta": None, "files": {}, "dur": {}, "first_fail": {}, "lost": set(), "found": False}
    st = {"first_vvc_sec": None, "first_aud_sec": None, "has_audio": False, "cleaned": 0, "errors": 0, "announced": False,
          "live_start": None, "live_logged": False, "last_warn": ("", 0.0), "stop_reason": ""}
    from array import array
    timing_d, timing_p = array("d"), array("d")

    st_audio = {"expected_sec": None, "last_payload": None}
    AUDIO_FRAME_SEC = 1024 / 48000.0

    f_vvc = open(temp_vvc, "wb")
    f_aud = open(temp_aud, "wb")
    v_ts_holder = {"ts": 90000}

    def write_frame(pts, dts, au):
        f_vvc.write(au)
        ts_ = v_ts_holder["ts"]
        timing_d.append(dts / ts_)
        timing_p.append(pts / ts_)

    def on_start(pts, dts):
        st["first_vvc_sec"] = pts / v_ts_holder["ts"]
        clog("INFO", f"4K start (VVC IRAP + LCEVC) locked at t={st['first_vvc_sec']:.3f}s")

    merger = LcevcMerger(write_frame, on_start)

    def warn_throttled(msg):
        now = time.time()
        if msg != st["last_warn"][0] or now - st["last_warn"][1] > 5:
            clog("WARN", msg)
            st["last_warn"] = (msg, now)

    # live status panel (rich) only on a real terminal; elsewhere a status line every few seconds
    live = None
    post = {"label": "", "frac": None, "detail": "", "t0": time.time()}

    def post_set(label, frac=None, detail=""):
        """Progress line shown under the track lines after the capture ends (packaging, verification)."""
        if label != post["label"]:
            post["t0"] = time.time()
        post.update(label=label, frac=frac, detail=detail)
        ev(type="live", phase="post", label=label, frac=frac if frac is not None else -1, detail=detail)

    def stop_live():
        if live is not None:
            live.stop()
    if console is not None and sys.stdout.isatty():
        from rich.live import Live
        live = Live(get_renderable=lambda: render_dash_status(trk, post), console=console, refresh_per_second=8, transient=True)
        live.start()
        atexit.register(stop_live)
    last_status = [0.0]
    cap_t0 = [None]
    last_live = [0.0]

    def emit_live(act, done_dur):
        """Machine-readable snapshot for the GUI's live view (about once a second)."""
        now = time.time()
        if now - last_live[0] < 1.0:
            return
        last_live[0] = now
        ev(type="live", phase="capture", elapsed=round(now - (cap_t0[0] or now), 1), recorded=round(done_dur, 2),
           limit=a.live_record_limit or 0, frames=merger.written, paired=merger.paired, errors=st["errors"],
           tracks=[{"kind": k, "state": t.state, "done": len(t.done), "seen": len(t.seen), "bytes": t.bytes_total,
                    "speed": round(t.speed()), "dur": round(t.done_dur, 2)} for k, t in trk.items() if t.active])

    def heartbeat(force=False):
        act = [t for t in trk.values() if t.active]
        if not act:
            return
        done_dur = max(t.done_dur for t in act)
        emit_live(act, done_dur)
        if a.live_record_limit:
            frac = min(1.0, done_dur / a.live_record_limit)
        else:
            tot = sum(len(t.seen) for t in act)
            frac = (sum(len(t.done) for t in act) / tot) if (tot and not st.get("dynamic")) else 0.0
        detail = f"recorded {dash_fmt_dur(done_dur)} · {merger.written} frames"
        if live is None:
            progress("capture", frac, detail, force=force)
        if live is None and not PJ and time.time() - last_status[0] > 5:
            last_status[0] = time.time()
            for t in act:
                log(t.line())

    executor = concurrent.futures.ThreadPoolExecutor(max_workers=a.thread_count)
    consecutive = 0
    step("manifest", "running")
    try:
        while True:
            try:
                resp = session.get(a.mpd_url, timeout=3)
                if resp.status_code != 200:
                    raise RuntimeError(f"manifest returned HTTP {resp.status_code}")
                tracks, is_dynamic, skipped, minfo = parse_mpd_tasks(resp.content, a.mpd_url, str(work_dir), subs=want_subs)
                st["dynamic"] = is_dynamic
                if minfo["drm"]:
                    raise Fail("The stream is protected (ContentProtection/DRM): it cannot be captured.")
                if not st["announced"]:
                    found = [k for k in DASH_KINDS if tracks[k]["meta"]]
                    clog("INFO", f"{'Dynamic (live)' if is_dynamic else 'Static'} manifest: tracks found: {', '.join(found) or 'none'}")
                    if skipped:
                        clog("WARN", "Only the first representation of each track is used. Ignored: " + ", ".join(skipped))
                    if minfo["periods"] > 1:
                        note(f"The manifest has {minfo['periods']} Periods; only the {'last' if is_dynamic else 'first'} one is captured.")
                    for u in minfo["unsupported"]:
                        note(f"Unsupported addressing, track ignored: {u}")
                    if not tracks["vvc_base"]["meta"] or not tracks["lcevc_enh"]["meta"]:
                        note("Video or LCEVC track not found in the manifest: no 4K output is possible without both.")
                    st["announced"] = True
                    cap_t0[0] = time.time()
                    sub_meta = tracks["subtitle"]["meta"] if want_subs else None
                    extra_ev = list(minfo["extra"])
                    if want_subs:
                        if sub_meta and tracks["subtitle"]["segs"]:
                            extra_ev.append({"kind": "subtitle", "id": sub_meta["rep_id"], "codecs": sub_meta["codecs"], "lang": sub_meta["lang"] or "",
                                             "mime": "", "bandwidth": sub_meta["bandwidth"], "width": "", "height": "", "role": sub_meta["role"] or "",
                                             "label": sub_meta["label"], "recorded": True})
                            clog("INFO", f"Subtitles: recording '{sub_meta['lang'] or '?'}' ({sub_meta['codecs'] or '?'})")
                        else:
                            note("Legenda pedida, mas o manifesto não traz uma faixa de legenda que possa ser gravada.")
                    ev(type="tracks", dynamic=bool(is_dynamic), limit=a.live_record_limit or 0, drm=False,
                       captured=[dict(m, kind=k, id=m["rep_id"]) for k in DASH_KINDS if (m := tracks[k]["meta"])],
                       extra=extra_ev)
                    step("manifest", "done", f"{', '.join(found) or 'no tracks'}")
                    step("capture", "running")
                if is_dynamic and a.live_take_count > 0:
                    if st["live_start"] is None:
                        st["live_start"] = compute_live_start(tracks, a.live_take_count)
                    if st["live_start"] is not None:
                        dropped = filter_from_start(tracks, st["live_start"])
                        if not st["live_logged"]:
                            st["live_logged"] = True
                            clog("INFO", "Live: aligning the start on the latest common segment. Skipped: "
                                 + ", ".join(f"{trk[k].tag} {c}" for k, c in dropped.items()))
                for k in DASH_KINDS:
                    if tracks[k]["meta"]:
                        trk[k].set_meta(k, tracks[k]["meta"])
                    for seg_key, _, _, dur in tracks[k]["segs"]:
                        trk[k].register(seg_key, dur)
                # 1. init segments
                for k in DASH_KINDS:
                    if init_info[k] is None and tracks[k]["init"]:
                        i_url, i_path = tracks[k]["init"]
                        nb, err = download_to_disk(session, i_url, i_path)
                        if nb:
                            ts_, nlen, params, ddur = parse_init_file(i_path)
                            init_info[k] = {"ts": ts_, "nlen": nlen, "params": params, "ddur": ddur}
                            clog("INFO", f"Init {k} loaded (timescale={ts_}, nal_len={nlen})")
                            if k == "vvc_base":
                                v_ts_holder["ts"] = ts_
                                if params:
                                    f_vvc.write(params)
                # 2. parallel download of new segments
                dl, pending = [], {k: 0 for k in DASH_KINDS}
                for k in ("lcevc_enh", "vvc_base", "audio"):
                    for seg_key, seg_url, seg_path, _ in tracks[k]["segs"]:
                        if seg_key not in processed[k] and seg_key not in abandoned[k]:
                            dl.append((k, seg_key, seg_path, executor.submit(download_to_disk, session, seg_url, seg_path)))
                            pending[k] += 1
                    if pending[k]:
                        trk[k].state = "Downloading"
                # 3. process in order (LCEVC -> VVC -> audio)
                blocked = {k: False for k in DASH_KINDS}
                for k, seg_key, seg_path, fut in dl:
                    nbytes, err = fut.result()
                    pending[k] -= 1
                    if pending[k] == 0:
                        trk[k].state = "Waiting"
                    if not nbytes:
                        now = time.time()
                        if now - first_fail.setdefault((k, seg_key), now) > SEG_RETRY_SECONDS:
                            abandoned[k].add(seg_key)
                            clog("WARN", f"Segment lost ({trk[k].tag}): {os.path.basename(seg_path)} unavailable for more than {SEG_RETRY_SECONDS}s ({err})")
                        else:
                            blocked[k] = True
                            if (k, seg_key) not in failed_logged:
                                failed_logged.add((k, seg_key))
                                clog("DEBUG", f"Download failed {os.path.basename(seg_path)} ({k}): {err}; will retry")
                        continue
                    if blocked[k]:
                        continue
                    processed[k].add(seg_key)
                    trk[k].mark_done(seg_key, nbytes)
                    ttl_files[seg_path] = time.time()
                    if k == "lcevc_enh" and init_info["lcevc_enh"]:
                        l_ts = init_info["lcevc_enh"]["ts"]
                        v_ts = init_info["vvc_base"]["ts"] if init_info["vvc_base"] else l_ts
                        for pts, _, pay in parse_m4s_file(seg_path, init_info["lcevc_enh"]["ddur"]):
                            merger.add_lcevc(round(pts * v_ts / l_ts) if l_ts != v_ts else pts, pay)
                    elif k == "vvc_base" and init_info["vvc_base"]:
                        for pts, dts, raw in parse_m4s_file(seg_path, init_info["vvc_base"]["ddur"]):
                            merger.add_video(pts, dts, mp4_sample_to_annexb(raw, init_info["vvc_base"]["nlen"]))
                    elif k == "audio":
                        a_ts = init_info["audio"]["ts"] if init_info["audio"] else (tracks["audio"]["meta"]["timescale"] if tracks["audio"]["meta"] else 48000)
                        for pts, _, apay in parse_m4s_file(seg_path, init_info["audio"]["ddur"] if init_info["audio"] else 0):
                            aud_wait.append((pts / a_ts, apay))
                # 4. pair VVC + LCEVC (ordered)
                merger.pump()
                f_vvc.flush()
                if st["first_vvc_sec"] is not None and aud_wait:
                    for a_sec, apay in aud_wait:
                        if a_sec < st["first_vvc_sec"] - 0.010:
                            continue
                            
                        if st["first_aud_sec"] is None:
                            st["first_aud_sec"] = a_sec
                            st_audio["expected_sec"] = a_sec
                            clog("INFO", f"Audio synced to the first 4K frame (offset: {round((a_sec - st['first_vvc_sec']) * 1000):+d} ms)")
                            st["has_audio"] = True
                        
                        # ESTRATÉGIA: Padding Dinâmico para cobrir perdas de rede
                        if st_audio["expected_sec"] is not None:
                            gap = a_sec - st_audio["expected_sec"]
                            # Se o buraco for maior que 1.5 quadros, preenchemos repetindo o último quadro
                            if gap > AUDIO_FRAME_SEC * 1.5 and st_audio["last_payload"]:
                                missing = int(round(gap / AUDIO_FRAME_SEC))
                                # Limite de segurança (aprox 5 seg) para evitar loops infinitos em gaps bizarros
                                if 0 < missing < 250:
                                    clog("DEBUG", f"Padding: Injetando {missing} quadro(s) de áudio para mascarar gap temporal de {gap*1000:.1f}ms")
                                    pad_frame = st_audio["last_payload"]
                                    if not pad_frame.startswith(b"\xC0\x01\xA5"):
                                        pad_frame = b"\xC0\x01\xA5" + pad_frame
                                    for _ in range(missing):
                                        f_aud.write(pad_frame)
                        
                        if not apay.startswith(b"\xC0\x01\xA5"):
                            f_aud.write(b"\xC0\x01\xA5")
                        f_aud.write(apay)
                        
                        st_audio["last_payload"] = apay
                        st_audio["expected_sec"] = a_sec + AUDIO_FRAME_SEC
                        
                    aud_wait.clear()
                    f_aud.flush()
                if want_subs and tracks["subtitle"]["meta"]:
                    try:
                        sub = tracks["subtitle"]
                        sst["meta"], sst["found"] = sub["meta"], True
                        if sst["init"] is None and sub["init"]:
                            nb, err = download_to_disk(session, sub["init"][0], sub["init"][1])
                            if nb:
                                sst["init"], sst["ts"] = sub["init"][1], parse_init_file(sub["init"][1])[0]
                        if sst["init"]:
                            sub_ts = sst["ts"] or sub["meta"]["timescale"]
                            jobs_ = []
                            for seg_key, seg_url, seg_path, dur in sub["segs"]:
                                if seg_key in sst["files"] or seg_key in sst["lost"]:
                                    continue
                                if st["live_start"] is not None and seg_key / sub_ts < st["live_start"] - dur * 0.25:
                                    continue
                                jobs_.append((seg_key, seg_path, dur, executor.submit(download_to_disk, session, seg_url, seg_path)))
                            for seg_key, seg_path, dur, fut in jobs_:
                                nb, err = fut.result()
                                if nb:
                                    sst["files"][seg_key] = seg_path
                                    sst["dur"][seg_key] = dur
                                else:
                                    now_ = time.time()
                                    if now_ - sst["first_fail"].setdefault(seg_key, now_) > SEG_RETRY_SECONDS:
                                        sst["lost"].add(seg_key)
                                        clog("WARN", f"Subtitle segment lost: {os.path.basename(seg_path)} ({err})")
                    except Exception as e_:
                        warn_throttled(f"Subtitles: {type(e_).__name__}: {e_}")
                st["cleaned"] += cleanup_old_segments(ttl_files, a.cleanup_after)
                consecutive = 0
                heartbeat()
                act = [t for t in trk.values() if t.active]
                if a.live_record_limit and act and max(t.done_dur for t in act) >= a.live_record_limit:
                    clog("INFO", "Recording limit reached (--live-record-limit).")
                    break
                if not is_dynamic and act and not dl and all(len(t.done) == len(t.seen) for t in act):
                    clog("INFO", "Static manifest: every segment was downloaded.")
                    break
            except Fail:
                raise
            except KeyboardInterrupt:
                raise
            except Exception as e:
                consecutive += 1
                st["errors"] += 1
                warn_throttled(f"{type(e).__name__}: {e}")
                if a.max_errors and consecutive >= a.max_errors:
                    clog("ERROR", f"{consecutive} consecutive failures: stopping the capture (--max-errors).")
                    st["stop_reason"] = "errors"
                    break
            time.sleep(a.live_wait_time)
    except KeyboardInterrupt:
        clog("WARN", "Interrupted, finishing...")
    finally:
        post_set("Finishing the capture")
        executor.shutdown(wait=True, cancel_futures=True)
        merger.pump(final=True)
        f_vvc.close()
        f_aud.close()

    if st["stop_reason"] == "errors":
        note("The capture stopped because of repeated failures; what was captured so far is packaged.")
    # ---- summary
    for k in DASH_KINDS:
        t = trk[k]
        if t.active:
            missing = len(t.seen) - len(t.done)
            clog("INFO", f"Downloaded {t.tag}: {len(t.done)}/{len(t.seen)} segments | {dash_fmt_dur(t.done_dur)}/{dash_fmt_dur(t.total_dur)} | {dash_fmt_size(t.bytes_total)}"
                 + (f" | {missing} not downloaded" if missing else ""))
    if merger.unpaired:
        note(f"{merger.unpaired} frame(s) in the middle of the video had no LCEVC layer and were kept without enhancement (none was dropped).")
    if merger.dropped_tail:
        clog("INFO", f"Tail: {merger.dropped_tail} frame(s) without an LCEVC pair at the end were discarded.")
    clog("INFO", f"Captured: {merger.written} VVC frames (LCEVC in {merger.paired}/{merger.written}) | {st['cleaned']} .m4s removed by the TTL")
    keep_tmp = a.keep_temp or not a.del_after_done
    if merger.written == 0:
        step("capture", "error", "no video frame captured")
        stop_live()
        raise Fail(f"No video frame captured. Temporary files kept in: {work_dir}")
    step("capture", "done", f"{merger.written} frames")
    progress("capture", 1, force=True)

    # ---- package
    step("package", "running")
    fps = dash_fps(list(timing_p), 1)   # timing_p is already in seconds
    final_mp4 = unique_path(out_mp4, a.overwrite)
    if final_mp4 != out_mp4:
        note(f"{out_mp4.name} appeared during the capture; saving as {final_mp4.name}.")
    origin = min(timing_p)
    sub_info, sub_mp4 = None, None
    if want_subs and sst["init"] and sst["files"]:
        sub_mp4 = work_dir / "subtitles.mp4"
        end_sec = max(timing_p) + 1.0 / max(fps, 1.0)
        sub_info = build_subtitle_mp4(sst["init"], [(k, pth, sst["dur"][k]) for k, pth in sst["files"].items()], sst["ts"] or sst["meta"]["timescale"],
                                      origin, end_sec, sub_mp4)
        if not sub_info["kept"]:
            sub_mp4 = None
        elif sub_info["first_gap"] > 0.5:
            note(f"A primeira legenda começa {sub_info['first_gap']:.1f} s antes do vídeo e foi alinhada ao início dele.")
    elif want_subs and sst["found"]:
        note("Nenhum segmento de legenda foi baixado; o MP4 sai sem legenda.")
    box = MP4BoxJob(temp_vvc)
    cmd = [mp4box, "-add", f"{box.inp(temp_vvc)}:fps={fps}#video:id=1"]
    delay_ms = 0
    if st["has_audio"]:
        delay_ms = av_delay_ms(st["first_aud_sec"], origin)
        cmd += ["-add", f"{box.inp(temp_aud)}{':delay=' + str(delay_ms) if delay_ms else ''}#audio:id=2"]
    if sub_mp4 is not None:
        lg = lang3(sst["meta"].get("lang"))
        cmd += ["-add", f"{box.inp(sub_mp4)}{':lang=' + lg if lg else ''}"]
    tmp_out = work_dir / "capture_out.mp4"
    cmd += ["-new", box.out(tmp_out)]
    clog("INFO", f"Packaging the final MP4 ({merger.written} frames, {fps} fps)...")
    try:
        post_set("Packaging MP4 (MP4Box)", 0.0)
        box.run(cmd, "package", lambda f, d: (progress("package", f, d), post_set("Packaging MP4 (MP4Box)", f)))
    except Fail:
        box.close()
        stop_live()
        step("package", "error", "MP4Box failed")
        clog("WARN", f"Temporary files kept in: {work_dir}")
        raise
    box.close()
    post_set("Applying the real PTS timing")
    rt = retime_mp4_tracks(str(tmp_out), {1: (list(timing_d), list(timing_p))}, origin)
    if rt.get(1) != "ok":
        note(f"Real PTS timing not applied ({rt.get(1, 'no video track')}): the MP4 keeps a constant {fps} fps.")
    final_mp4.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(tmp_out), str(final_mp4))
    size = final_mp4.stat().st_size
    if st["has_audio"]:
        sync_msg = validate_dash_sync(final_mp4, (st["first_aud_sec"] - origin))
        if "AVISO CRÍTICO" in sync_msg:
            clog("ERROR", sync_msg)
            notes.append(sync_msg)
        else:
            clog("INFO", sync_msg)
    step("package", "done", f"{final_mp4.name} · {dash_fmt_size(size)}")
    progress("package", 1, force=True)

    step("verify", "running")
    post_set("Verifying the LCEVC enhancement (ffmpeg)")
    verify = verify_enhancement(ffmpeg_bin, str(final_mp4), 300)
    step("verify", "done" if verify else "skipped", f"{verify[1]}/{verify[0]} frames with enhancement" if verify else "skipped")

    rows = [["Frames", str(merger.written)], ["Frame rate", f"{fps} fps"], ["LCEVC", f"{merger.paired}/{merger.written}"],
            ["Audio", "yes" if st["has_audio"] else "no"], ["Audio delay", f"{delay_ms} ms"]]
    if want_subs:
        rows.append(["Subtitles", f"{sst['meta'].get('lang') or '?'} ({sub_info['kept']} segments)" if sub_mp4 is not None else "none"])
    if verify:
        rows.append(["Enhancement", f"{verify[1]}/{verify[0]} frames"])
    ev(type="result", out=str(final_mp4), outputs=[{"kind": "capture", "title": "capture (VVC + SEI LCEVC" + (" + MPEG-H)" if st["has_audio"] else ")"),
                                                    "path": str(final_mp4), "size": size}],
       notes=NOTES, rows=rows)
    if keep_tmp:
        clog("INFO", f"Temporary files kept in: {work_dir}")
    else:
        n_files, n_bytes = dir_stats(work_dir)
        post_set("Deleting the temporary files")
        if safe_rmtree(work_dir):
            clog("INFO", f"Deleted: {work_dir} ({n_files} files, {dash_fmt_size(n_bytes)})")
    stop_live()
    return 0


def set_output_hook(fn):
    """Sends every text line the tools would print to fn(str) instead (None restores the console). For GUIs."""
    global _OUT_HOOK
    _OUT_HOOK = fn


_set_out_hook = set_output_hook


def cleanup_old_segments(files, ttl):
    now, removed = time.time(), 0
    for fp, t in list(files.items()):
        if now - t >= ttl:
            try:
                if os.path.exists(fp):
                    os.remove(fp)
                    removed += 1
            except OSError:
                continue
            del files[fp]
    return removed


def dir_stats(path):
    files = size = 0
    for r, _, names in os.walk(path):
        for nm in names:
            try:
                size += os.path.getsize(os.path.join(r, nm))
                files += 1
            except OSError:
                pass
    return files, size


def parse_hms(v):
    try:
        parts = [float(p) for p in v.split(":")]
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid time: {v!r} (use HH:mm:ss)")
    if not 1 <= len(parts) <= 3:
        raise argparse.ArgumentTypeError(f"invalid time: {v!r} (use HH:mm:ss)")
    total = 0.0
    for p in parts:
        total = total * 60 + p
    return total


def str2bool(v):
    if isinstance(v, bool):
        return v
    s_ = v.strip().lower()
    if s_ in ("true", "1", "yes", "y", "on"):
        return True
    if s_ in ("false", "0", "no", "n", "off"):
        return False
    raise argparse.ArgumentTypeError(f"invalid boolean: {v!r} (use true/false)")


# ──────────────────────────── transcode / frame (HDR10) ────────────────────────────
DEFAULT_HDR10 = {"r": (0.7080, 0.2920), "g": (0.1700, 0.7970), "b": (0.1310, 0.0460), "wp": (0.3127, 0.3290),
                 "lmax": 1000.0, "lmin": 0.005, "maxcll": 1000, "maxfall": 300, "src": "default"}
HDR10_TAGS = "colorspace=bt2020nc:color_primaries=bt2020:color_trc=smpte2084:range=tv"
WXH_RE = re.compile(r"^(\d{2,5})x(\d{2,5})$")
HDR_MD_RX = re.compile(r"r\(([\d.]+)[,\s]+([\d.]+)\)\s*g\(([\d.]+)[,\s]+([\d.]+)\)\s*b\(([\d.]+)[,\s]+([\d.]+)\)\s*"
                       r"wp\(([\d.]+)[,\s]+([\d.]+)\)\s*min_luminance=([\d.]+),\s*max_luminance=([\d.]+)")
HDR_CLL_RX = re.compile(r"MaxCLL=(\d+),\s*MaxFALL=(\d+)")
AUDIO_EXT_FMT = {".m4a": "aac", ".eac3": "eac3", ".flac": "flac", ".wav": "wav"}
MUX_FORMATS = {".mp4": "mp4", ".m4v": "mp4", ".mov": "mov", ".mkv": "matroska"}      # output extension -> ffmpeg muxer


# Video encoders of `transcode`. One table drives the ffmpeg command, the --help text, `describe` (for front ends) and `doctor`.
#   ffmpeg: encoder name; codec: output codec (hevc/av1); crf: default quality; crf_range: valid range; presets: valid names
ENCODERS = {
    "x265":      {"label": "x265 (CPU)", "ffmpeg": "libx265", "codec": "hevc", "crf": 18, "crf_range": [0, 51], "step": 0.5,
                  "presets": ["ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow", "placebo"],
                  "preset_default": "", "params_hint": "key=value:key=value", "ref_mbps": 20},
    "nvenc":     {"label": "HEVC NVENC (GPU NVIDIA)", "ffmpeg": "hevc_nvenc", "codec": "hevc", "crf": 22, "crf_range": [0, 51], "step": 1,
                  "presets": [f"p{i}" for i in range(1, 8)], "preset_default": "p5", "params_hint": "-spatial-aq 1 -rc-lookahead 32", "ref_mbps": 20},
    "av1":       {"label": "AV1 SVT-AV1 (CPU)", "ffmpeg": "libsvtav1", "codec": "av1", "crf": 28, "crf_range": [0, 63], "step": 1,
                  "presets": [str(i) for i in range(0, 14)], "preset_default": "6", "params_hint": "tune=0:film-grain=8", "ref_mbps": 15},
    "av1-nvenc": {"label": "AV1 NVENC (GPU NVIDIA RTX 40+)", "ffmpeg": "av1_nvenc", "codec": "av1", "crf": 28, "crf_range": [0, 63], "step": 1,
                  "presets": [f"p{i}" for i in range(1, 8)], "preset_default": "p5", "params_hint": "-spatial-aq 1 -rc-lookahead 32", "ref_mbps": 15},
}
# Peak-bitrate cap (--max-bitrate, Mbit/s) with quality-based rate control, how each encoder takes it:
#   x265 vbv-maxrate + vbv-bufsize (x265-params) · SVT-AV1 mbr (svtav1-params, kbit/s; CRF mode only) · hevc_nvenc / av1_nvenc -maxrate + -bufsize
# ref_mbps is a reference value (HEVC 20, AV1 15 Mbit/s), only a suggestion shown by front ends; the cap is off unless asked for.
CODEC_NAMES = {"hevc": "HEVC", "av1": "AV1"}


def enc_crf(a):
    """--crf, or the default quality of the chosen encoder (the scales differ: x265 18, NVENC 22, SVT-AV1 28)."""
    return a.crf if getattr(a, "crf", None) is not None else ENCODERS[getattr(a, "encoder", "x265")]["crf"]


def enc_maxrate_kbps(a):
    """--max-bitrate in kbit/s (0 = no cap)."""
    mb = getattr(a, "max_bitrate", 0) or 0
    if mb < 0 or (0 < mb < 0.1):
        raise Fail("--max-bitrate deve ser 0 (sem limite) ou, no mínimo, 0.1 (Mbit/s).")
    return int(round(mb * 1000))


def enc_preset(a):
    """The preset to pass, or '' to let the encoder decide. A name that belongs to another encoder falls back to the default."""
    e = ENCODERS[getattr(a, "encoder", "x265")]
    p = getattr(a, "preset", "") or ""
    return p if p in e["presets"] else e["preset_default"]


def enc_extra(a):
    """Extra encoder parameters: --encoder-params, or the older per-encoder options."""
    if getattr(a, "encoder_params", ""):
        return a.encoder_params
    return (getattr(a, "x265_params", "") if a.encoder == "x265" else getattr(a, "nvenc_params", "")) or ""


def ffmpeg_encoders(ff):
    """Set of video encoder names the ffmpeg build has (cached)."""
    key = ("enc", ff)
    if key not in _FF_CAPS:
        try:
            r = sp_run([ff, "-hide_banner", "-encoders"], capture_output=True, text=True, errors="replace", timeout=30, **NOWIN)
            _FF_CAPS[key] = set(re.findall(r"^\s*V\S*\s+(\S+)", r.stdout or "", re.M))
        except Exception:
            _FF_CAPS[key] = set()
    return _FF_CAPS[key]


def svtav1_master(h):
    """Mastering display in the SVT-AV1 form: G(x,y)B(x,y)R(x,y)WP(x,y)L(max,min), decimal values."""
    return (f"G({h['g'][0]:.4f},{h['g'][1]:.4f})B({h['b'][0]:.4f},{h['b'][1]:.4f})R({h['r'][0]:.4f},{h['r'][1]:.4f})"
            f"WP({h['wp'][0]:.4f},{h['wp'][1]:.4f})L({h['lmax']:.4f},{h['lmin']:.4f})")


def same_file(a, b):
    try:
        return Path(a).resolve() == Path(b).resolve() or (Path(a).exists() and Path(b).exists() and os.path.samefile(a, b))
    except OSError:
        return False


def probe_streams(ffmpeg, path):
    """Video/audio streams and duration, parsed from 'ffmpeg -i' (no ffprobe needed)."""
    r = sp_run([ffmpeg, "-hide_banner", "-i", str(path)], capture_output=True, text=True, errors="replace", timeout=90, **NOWIN)
    txt = r.stderr or ""
    info = {"video": None, "audio": [], "duration": media_duration(ffmpeg, path)}
    for m in re.finditer(r"Stream #0:(\d+)\S*: (Video|Audio): ([^\n]+)", txt):
        idx, kind, rest = int(m.group(1)), m.group(2), m.group(3)
        codec = rest.split()[0].strip(",")
        if kind == "Audio":
            info["audio"].append({"index": idx, "codec": codec})
        elif info["video"] is None:
            sz = re.search(r",\s*(\d{2,5})x(\d{2,5})", rest)
            fps = re.search(r"([\d.]+) fps", rest)
            tg = re.search(r"\((?:(?:tv|pc|limited|full),\s*)?([\w-]+)/([\w-]+)/([\w-]+)", rest)
            info["video"] = {"index": idx, "codec": codec, "size": f"{sz.group(1)}x{sz.group(2)}" if sz else None,
                             "fps": float(fps.group(1)) if fps else None, "trc": tg.group(3) if tg else None}
    if not info["video"]:
        raise Fail(f"Nenhuma trilha de vídeo encontrada em {path}. É um MP4 gerado pelo dtv_tools?")
    return info


def probe_hdr10(ffmpeg, path):
    """Mastering display + MaxCLL/MaxFALL of the first decoded frame (base layer only, cheap), via the showinfo filter."""
    hdr = dict(DEFAULT_HDR10)
    try:
        r = sp_run([ffmpeg, "-hide_banner", "-nostdin", "-i", str(path), "-map", "0:v:0", "-vf", "showinfo", "-frames:v", "1",
                    "-f", "null", "-"], capture_output=True, text=True, errors="replace", timeout=120, **NOWIN)
        err = r.stderr or ""
    except Exception:
        err = ""
    m, c = HDR_MD_RX.search(err), HDR_CLL_RX.search(err)
    if m:
        v = [float(x) for x in m.groups()]
        hdr.update(r=(v[0], v[1]), g=(v[2], v[3]), b=(v[4], v[5]), wp=(v[6], v[7]), lmin=v[8], lmax=v[9], src="stream")
    if c:
        hdr.update(maxcll=int(c.group(1)), maxfall=int(c.group(2)), src="stream")
    if hdr["src"] == "default":
        note("Metadados HDR10 (mastering display / MaxCLL) não encontrados no stream: usando os valores padrão "
             "(primárias BT.2020, 1000/0,005 nits, MaxCLL/MaxFALL 1000,300).")
    return hdr


def x265_master(h):
    u = lambda p: (round(p[0] * 50000), round(p[1] * 50000))      # noqa: E731  (x265 units: 0.00002)
    g, b, r, w = u(h["g"]), u(h["b"]), u(h["r"]), u(h["wp"])
    return (f"G({g[0]},{g[1]})B({b[0]},{b[1]})R({r[0]},{r[1]})WP({w[0]},{w[1]})"
            f"L({round(h['lmax'] * 10000)},{round(h['lmin'] * 10000)})")


def hdr10_summary(h):
    return f"{h['lmax']:g}/{h['lmin']:g} nits, MaxCLL/MaxFALL {h['maxcll']}/{h['maxfall']} ({'do stream' if h['src'] == 'stream' else 'padrão'})"


def pick_size(a, measured):
    """--size wins; otherwise ONLY a single unambiguous size found by the verification; never a built-in constant."""
    if a.size:
        if not WXH_RE.match(a.size):
            raise Fail(f"--size deve ser LARGURAxALTURA, recebi {a.size!r}")
        return a.size
    if a.no_size_fix or measured is None:
        return None
    sizes = measured[2]
    if len(sizes) == 1:
        return sizes[0]
    note(("A verificação encontrou vários tamanhos de saída (" + ", ".join(sizes) + ")") if sizes else "A verificação não encontrou o tamanho de saída"
         + ": o tamanho não será forçado. Use --size LARGURAxALTURA se o encoder abrir no tamanho errado.")
    return None


def tc_prepare(a):
    ff = find_tool("ffmpeg", a.ffmpeg)[0]
    src = Path(a.input).resolve()
    if not src.is_file():
        raise Fail(f"Arquivo não encontrado: {src}")
    use_lcevc = not a.no_lcevc
    if use_lcevc and not ffmpeg_has_lcevc(ff):
        raise Fail("Este ffmpeg não tem o filtro 'lcevc' (use a build DTV+/LCEVC em --ffmpeg, ou --no-lcevc para um arquivo sem enhancement).")
    info = probe_streams(ff, src)
    if info["video"]["trc"] == "arib-std-b67":
        raise Fail("Fonte HLG detectada; esta ferramenta trata apenas PQ (SMPTE ST 2084).")
    return ff, src, info, use_lcevc


def run_ffmpeg_progress(cmd, total, stage):
    """Runs ffmpeg with -progress pipe:1 and feeds progress(); returns (returncode, stderr tail)."""
    log("$ " + subprocess.list2cmdline(cmd))
    p = sp_popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **NOWIN)
    err = Drain(p.stderr, keep=16384)
    cur = {}
    for raw in p.stdout:
        line = raw.decode("utf-8", "replace").strip()
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        cur[k] = v
        if k == "progress":
            try:
                done = int(cur.get("out_time_us") or 0) / 1e6
            except ValueError:
                done = 0.0
            frac = min(1.0, done / total) if total else 0.0
            progress(stage, frac, f"{fmt_hms(done)} / {fmt_hms(total)} · {cur.get('fps', '0')} fps · {cur.get('speed', '?').strip()}", force=(v == "end"))
            cur = {}
    rc = p.wait()
    return rc, err.text()


# ── transcode: audio ──
def split_options(a):
    return [x.strip() for spec in (a.option or []) for x in spec.split(",") if x.strip()]


def track_label(o):
    lay = CICP_NAME.get(o["src_cicp"], "?")
    orig = o.get("layout_orig", o["src_cicp"])
    if orig != o["src_cicp"]:
        lay += f" de {CICP_NAME.get(orig, '?')}"                        # e.g. "2.0 de 5.1": the track was rendered in a layout other than its own
    return f"{'P' if o['group'] == 'presets' else 'G'}{str(o['id']).split(':')[-1]} - {o['title']} ({lay})"


def audio_offset(ff, src, a):
    """Seconds to delay the rendered audio: 'auto' = first timestamp of the original audio track, else --audio-delay ms."""
    if str(a.audio_delay).strip().lower() != "auto":
        try:
            return float(a.audio_delay) / 1000.0
        except ValueError:
            raise Fail("--audio-delay deve ser 'auto' ou um número em milissegundos.")
    try:
        off = mp4_audio_offset(str(src))
        if off is not None:
            return off
    except Exception as e:
        note(f"Não consegui ler o atraso do áudio no arquivo de origem ({e}); usando 0 ms. Informe --audio-delay em ms se precisar.")
    return 0.0


def mp4_audio_offset(path):
    """
    Where the first audio sample sits on the player timeline, relative to the first displayed video frame (seconds, may be
    negative), from the edit lists of both tracks: empty edit (delay added by the cut / capture), media_time (audio trimmed at
    its start) and the video's own origin. The rendered audio starts at its own sample 0, so this is the offset it needs.
    None when the file has no audio track tables.
    """
    tables = mp4_tables(str(path))
    at = next((t for t in tables if t.get("handler") == "soun"), None)
    vt = next((t for t in tables if t.get("handler") == "vide"), None)
    if at is None or not at.get("dts"):
        return None
    a0 = mp4_present(at, 0)
    v0 = min((mp4_present(vt, k) for k in range(min(len(vt["dts"]), 64))), default=0.0) if vt and vt.get("dts") else 0.0
    return round(a0 - v0, 3)


def first_pts(ff, path, kind, index=0):
    """Timestamp (s) of the first decoded frame of the index-th video/audio stream (any container), None if it cannot be read."""
    flt = ["-vf", "showinfo", "-frames:v", "1"] if kind == "v" else ["-af", "ashowinfo", "-frames:a", "1"]
    try:
        r = sp_run([ff, "-hide_banner", "-nostdin", "-i", str(path), "-map", f"0:{kind}:{index}", *flt, "-f", "null", "-"],
                   capture_output=True, text=True, errors="replace", timeout=180, **NOWIN)
    except Exception:
        return None
    m = re.search(r"pts_time:\s*(-?[\d.]+)", r.stderr or "")
    return float(m.group(1)) if m else None


def check_audio_sync(ff, out, n_rendered, expected, tol=0.05):
    """After the encode: do the rendered audio tracks of the output sit `expected` s after the video? Returns (rows, problems)."""
    v = first_pts(ff, out, "v")
    rows, bad = [], []
    for j in range(n_rendered):
        a_ = first_pts(ff, out, "a", j)
        if v is None or a_ is None:
            rows.append((j, None))
            continue
        d_ = a_ - v
        rows.append((j, d_))
        if abs(d_ - expected) > tol:
            bad.append((j, d_))
    return rows, bad


def build_transcode_cmd(ff, src, out, a, hdr, size, use_lcevc, n_src_audio, items=(), offset=0.0, keep_original=True, target=None):
    enc = getattr(a, "encoder", "x265")
    extra = enc_extra(a)
    cmd = [ff, "-hide_banner", "-y", "-nostdin", "-nostats", "-progress", "pipe:1", "-i", str(src)]
    for it in items:                                                   # rendered presets: one input each
        if offset > 0:
            cmd += ["-itsoffset", f"{offset:.3f}"]                     # delay: the audio starts later than the video
        elif offset < 0:
            cmd += ["-ss", f"{-offset:.3f}"]                           # the audio starts before the first frame: drop that stretch
        cmd += ["-i", it["path"]]
    cmd += ["-map", "0:v:0"]
    titles = []
    for i, it in enumerate(items):
        cmd += ["-map", f"{i + 1}:a:0"]
        titles.append(it["title"])
    if a.audio != "none" and n_src_audio and (a.audio == "copy" or keep_original):
        cmd += ["-map", "0:a?"]                                        # original MPEG-H track(s), as-is
        if items:
            titles += [("MPEG-H original (mhm1)" if n_src_audio == 1 else f"Áudio original {k + 1}") for k in range(n_src_audio)]
    for j, t in enumerate(titles):
        cmd += [f"-metadata:s:a:{j}", f"title={t}", f"-metadata:s:a:{j}", f"handler_name={t}",
                f"-disposition:a:{j}", "default" if j == 0 else "0"]
    cmd += ["-vf", ("lcevc," if use_lcevc else "") + f"setparams={HDR10_TAGS}"]
    if size:
        cmd += ["-s", size]
    crf, preset = enc_crf(a), enc_preset(a)
    mrk = enc_maxrate_kbps(a)
    cap = ["-maxrate", f"{mrk}k", "-bufsize", f"{2 * mrk}k"] if mrk else []          # NVENC: the cap goes with vbr + -cq + -b:v 0
    if enc == "nvenc":                                                 # GPU: no x265 params; HDR10 SEI depends on frame side data
        cmd += ["-c:v", "hevc_nvenc", "-profile:v", "main10", "-pix_fmt", "p010le", "-preset", preset, "-tune", "hq",
                "-rc", "vbr", "-cq", str(int(round(crf))), "-b:v", "0", "-multipass", "fullres",
                "-bf", "3", "-b_ref_mode", "middle", "-tag:v", "hvc1"] + cap
        cmd += shlex.split(extra)
    elif enc == "av1-nvenc":
        cmd += ["-c:v", "av1_nvenc", "-pix_fmt", "p010le", "-preset", preset, "-tune", "hq",
                "-rc", "vbr", "-cq", str(int(round(crf))), "-b:v", "0", "-multipass", "fullres"] + cap
        cmd += shlex.split(extra)
    elif enc == "av1":                                                 # SVT-AV1 takes the HDR10 metadata as OBUs through its own parameters
        sp = ["color-primaries=9", "transfer-characteristics=16", "matrix-coefficients=9", "color-range=0",
              f"mastering-display={svtav1_master(hdr)}", f"content-light={hdr['maxcll']},{hdr['maxfall']}"]
        if mrk:
            sp.append(f"mbr={mrk}")                                      # maximum bitrate in kbit/s (CRF mode)
        if extra:
            sp.append(extra)
        cmd += ["-c:v", "libsvtav1", "-pix_fmt", "yuv420p10le", "-crf", str(int(round(crf))), "-preset", preset,
                "-svtav1-params", ":".join(sp)]
    else:
        xp = ["hdr10=1", "repeat-headers=1", "colorprim=bt2020", "transfer=smpte2084", "colormatrix=bt2020nc",
              f"master-display={x265_master(hdr)}", f"max-cll={hdr['maxcll']},{hdr['maxfall']}"]
        if mrk:
            xp += [f"vbv-maxrate={mrk}", f"vbv-bufsize={2 * mrk}"]       # capped CRF
        if extra:
            xp.append(extra)
        cmd += ["-c:v", "libx265"]
        if preset:
            cmd += ["-preset", preset]
        cmd += ["-crf", str(crf), "-pix_fmt", "yuv420p10le", "-x265-params", ":".join(xp), "-tag:v", "hvc1"]
    if MUX_FORMATS.get(out.suffix.lower()) in ("mp4", "mov"):
        cmd += ["-movflags", "+faststart"]
    cmd += ["-c:a", "copy"] if (a.audio != "none" and (items or n_src_audio)) else ["-an"]
    if a.limit:
        cmd += ["-t", str(a.limit)]
    if target:                                                         # written as <name>.part, renamed when finished
        cmd += ["-f", MUX_FORMATS[out.suffix.lower()]]
    return cmd + [str(target or out)]


TC_STEPS_VIDEO = [("probe", "Analisar o arquivo (HDR10 e LCEVC)")]
TC_STEPS_AUDIO = [("a_prepare", "Preparar o áudio e a cena"), ("a_decode", "Decodificar os presets (em paralelo)"),
                  ("a_check", "Verificar os arquivos decodificados"), ("a_encode", "Gerar os arquivos de áudio")]
TC_WEIGHTS_AUDIO = {"probe": 3, "a_prepare": 4, "a_decode": 8, "a_check": 1, "a_encode": 4, "encode": 79, "report": 1}
TC_WEIGHTS_VIDEO = {"probe": 4, "encode": 95, "report": 1}


def cmd_transcode(a):
    loudness_target(a)
    render = a.audio == "render"
    set_steps(TC_STEPS_VIDEO + (TC_STEPS_AUDIO if render else []) +
              [("encode", f"Codificar o vídeo {CODEC_NAMES[ENCODERS[a.encoder]['codec']]} HDR10" + (" e montar o arquivo" if render else "")), ("report", "Resumo")])
    set_stages(TC_WEIGHTS_AUDIO if render else TC_WEIGHTS_VIDEO)
    t0 = time.time()
    step("probe", "running")
    ff, src, info, use_lcevc = tc_prepare(a)
    tools = None
    if render:                                                         # fail early: audio tools are checked before the long work
        try:
            tools = (ff, find_mp4box(a.mp4box), need_tool(a.mpegh_ui, "mpeghUiManager"), need_tool(a.mpegh_decoder, "mpeghDecoder"))
        except Fail as e:
            raise Fail(f"{e}\nPara manter só a trilha MPEG-H original, sem renderizar os presets, use --audio copy (ou --audio none).")
    v = info["video"]
    say(f"Entrada: {v['codec']} {v['size'] or '?'} @ {v['fps'] or '?'} fps · {len(info['audio'])} trilha(s) de áudio "
        f"({', '.join(x['codec'] for x in info['audio']) or 'nenhuma'}) · {fmt_hms(info['duration'] or 0)}")
    E = ENCODERS[a.encoder]
    enc_maxrate_kbps(a)                                                # validates --max-bitrate before any long work
    if E["ffmpeg"] not in ffmpeg_encoders(ff):
        gpu = " (precisa de GPU NVIDIA compatível e driver)" if "nvenc" in a.encoder else ""
        raise Fail(f"Este ffmpeg não tem o codificador {E['ffmpeg']}{gpu}. Escolha outro com --encoder ({', '.join(ENCODERS)}) ou use um ffmpeg que o tenha.")
    if a.preset and a.preset not in E["presets"]:
        note(f"O preset '{a.preset}' não existe em {E['label']}; usando {E['preset_default'] or 'o padrão do codificador'}. Válidos: {', '.join(E['presets'])}.")
    if "nvenc" in a.encoder:
        note("NVENC: o encoder não aceita parâmetros de HDR10; os metadados (mastering display / MaxCLL) só saem no arquivo "
             "se vierem nos quadros de entrada. Confira a saída com ffprobe -show_frames.")
    if a.encoder == "av1":
        note("AV1 (SVT-AV1): mastering display e MaxCLL vão pelos parâmetros do encoder (precisa de SVT-AV1 1.2 ou mais novo). "
             "Confira a saída com ffprobe -show_frames.")
    hdr = probe_hdr10(ff, src)
    say(f"HDR10: {hdr10_summary(hdr)}")
    measured = verify_enhancement(ff, str(src), a.verify_frames) if use_lcevc else None
    if measured and measured[1] == 0:
        note("Sem enhancement LCEVC: a saída teria só a camada base (use --no-lcevc para um arquivo sem LCEVC).")
    size = pick_size(a, measured) or (v["size"] if not use_lcevc else None)
    say(f"Tamanho de saída: {size or 'não forçado (o filtro decide)'}")
    step("probe", "done", f"HDR10 {hdr['src'] == 'stream' and 'do stream' or 'padrão'} · saída {size or 'automática'}")
    progress("probe", 1.0, force=True)

    work = tmp = part = None
    items, offset = [], 0.0
    succeeded = False
    ext = {"mp4": ".mp4", "mkv": ".mkv"}.get(a.container)
    out = None
    try:
        if render and not a.dry_run:
            work = Path(a.tmpdir).resolve() / f"{src.stem}_tctmp" if a.tmpdir else (Path(a.outdir).resolve() if a.outdir else src.parent) / f".{src.stem}_tctmp"
            tmp_session(work, {"v": 1, "src": src_sig(src), "task": "transcode", "uim": a.uim_version, "native": str(a.native_layout)})
            adir, tmp = work / "audio", work / "mp"
            adir.mkdir(exist_ok=True)
            tmp.mkdir(exist_ok=True)
            ctx = mpegh_render(a, src, tools, adir, tmp, split_options(a) or None, fmt_override=None if a.audio_format == "auto" else a.audio_format,
                               pfx="a_", to_outdir=True)
            for m, o in zip(ctx["made"], ctx["chosen"]):
                items.append({"path": m["path"], "title": track_label(o), "id": o["id"],
                              "fmt": AUDIO_EXT_FMT.get(Path(m["path"]).suffix.lower(), "?")})
            offset = audio_offset(ff, src, a)
            say(f"Deslocamento do áudio renderizado: {offset * 1000:+.0f} ms "
                f"({'atraso da trilha de áudio no arquivo de origem (corte/captura)' if str(a.audio_delay).strip().lower() == 'auto' else '--audio-delay'})")
        elif render:
            note("--dry-run: a renderização do áudio foi pulada; o comando abaixo não tem as trilhas renderizadas.")
        if ext is None:
            ext = ".mp4" if all(i["fmt"] in ("aac", "eac3") for i in items) else ".mkv"
            if ext == ".mkv":
                note("Contêiner MKV: as trilhas renderizadas incluem PCM/FLAC, que não cabem em MP4. Use --container mp4 para forçar.")
        out = Path(a.output) if a.output else Path(a.outdir or src.parent) / f"{src.stem}_{E['codec']}_hdr10{ext}"
        if out.suffix.lower() not in MUX_FORMATS:
            raise Fail(f"A extensão do arquivo de saída deve ser {', '.join(MUX_FORMATS)} (recebi {out.suffix or 'nenhuma'}).")
        if E["codec"] == "av1" and out.suffix.lower() == ".mov":
            raise Fail("AV1 não é suportado em .mov pela maioria dos players; use .mp4 ou .mkv.")
        if same_file(out, src):
            raise Fail("O arquivo de saída não pode ser o próprio arquivo de entrada.")
        out.parent.mkdir(parents=True, exist_ok=True)
        out = unique_path(out, a.overwrite)
        part = out.with_name(out.name + ".part")
        n_src = len(info["audio"])
        total = min(info["duration"] or 0, float(a.limit)) if a.limit else (info["duration"] or 0)
        attempts = [True, False] if (render and a.original_audio == "keep" and n_src) else [a.original_audio == "keep" or a.audio == "copy"]
        step("encode", "running", out.name)
        rc, tail = 0, ""
        for k, keep in enumerate(attempts):
            cmd = build_transcode_cmd(ff, src, out, a, hdr, size, use_lcevc, n_src, items, offset, keep, target=None if a.dry_run else part)
            if a.dry_run:
                say(subprocess.list2cmdline(cmd))
                succeeded = True
                return 0
            rc, tail = run_ffmpeg_progress(cmd, total, "encode")
            if rc != 0 and k + 1 < len(attempts) and re.search(r"mhm1|mpegh|not currently supported|Could not write header|codec not", tail, re.I):
                note("Este ffmpeg/contêiner não consegue guardar a trilha MPEG-H original: tentando de novo sem ela.")
                continue
            break
        got = media_duration(ff, part) if part.is_file() else None
        if rc != 0:
            if "Error flushing BSFs" in tail and got and total and abs(got - total) <= max(2.0, total * 0.005):
                note("O ffmpeg reportou erro ao fechar a camada LCEVC, mas o arquivo tem a duração esperada.")
            else:
                raise Fail(f"O ffmpeg falhou (código {rc}):\n{tail[-1200:]}")
        final = probe_streams(ff, part)
        sync_rows = []
        if items and not a.limit:
            want = max(offset, 0.0)                                    # a negative offset trims the start of the audio: it then begins at 0
            sync_rows, sync_bad = check_audio_sync(ff, part, len(items), want)
            for j, d_ in sync_bad:
                note(f"Atraso do áudio no arquivo final: a trilha renderizada {j + 1} começa {d_ * 1000:+.0f} ms depois do vídeo, mas o esperado era "
                     f"{want * 1000:+.0f} ms. Se ouvir dessincronia, ajuste com --audio-delay (ms).")
        os.replace(part, out)                                          # only a finished file gets the final name
        sub_row = None
        sub_src = mp4_subtitle_tracks(src)
        if sub_src and getattr(a, "no_subtitles", False):
            sub_row = "removidas (--no-subtitles)"
        elif sub_src and out.suffix.lower() not in (".mp4", ".m4v"):
            note(f"A origem tem legenda, mas o contêiner {out.suffix.lstrip('.').upper()} não a recebe (TTML/stpp só cabe em MP4): a saída fica sem legenda.")
            sub_row = "não copiadas (contêiner)"
        elif sub_src:
            try:
                why = reattach_subtitles(find_mp4box(a.mp4box), src, out, sub_src)
            except Fail as e:
                why = str(e)
            if why:
                note(f"Não consegui copiar a legenda da origem para a saída ({why}). O vídeo está pronto, sem legenda.")
                sub_row = "falhou"
            else:
                sub_row = ", ".join((t["lang"] or "?") for t in sub_src) + " (copiadas da origem)"
        step("encode", "done", f"{out.name} · {human(out.stat().st_size)} · {fmt_hms(got or 0)}")
        progress("encode", 1.0, force=True)

        step("report", "running")
        rows = [["Entrada", src.name], ["Vídeo", f"{CODEC_NAMES[E['codec']]} 10-bit HDR10 ({E['label']}) · {size or v['size'] or '?'} · CRF {enc_crf(a):g}" + (f" · preset {enc_preset(a)}" if enc_preset(a) else "")
                + (f" · máx. {a.max_bitrate:g} Mbps" if enc_maxrate_kbps(a) else "")],
                ["HDR10", hdr10_summary(hdr)], ["Duração", fmt_hms(got or 0)], ["Contêiner", out.suffix.lstrip(".").upper()],
                *([["Atraso do áudio", (f"início aparado em {-offset * 1000:.0f} ms" if offset < 0 else f"{offset * 1000:+.0f} ms aplicado") + ("" if offset < 0 or not sync_rows or any(d_ is None for _, d_ in sync_rows) else
                    f" · medido no arquivo: {', '.join(f'{d_ * 1000:+.0f} ms' for _, d_ in sync_rows)}")]] if items else []),
                ["Trilhas de áudio", (f"{len(items)} renderizada(s): " + ", ".join(i['title'] for i in items) +
                                      (f" + {len(final['audio']) - len(items)} original(is)" if len(final['audio']) > len(items) else ""))
                 if items else f"{len(final['audio'])} (original)"],
                *([["Legendas", sub_row]] if sub_row else []),
                ["Tempo total", fmt_hms(time.time() - t0)]]
        made = [{"kind": "transcode", "title": f"Vídeo {CODEC_NAMES[E['codec']]} HDR10" + (" com todos os presets" if items else ""), "path": str(out.resolve()),
                 "size": out.stat().st_size, "duration": got}]
        say(bold("Resultado:"))
        for k_, v_ in rows:
            say(f"  {k_}: {v_}")
        say(f"  Arquivo: {out.resolve()}")
        ev(type="result", out=str(out.resolve()), outputs=made, notes=NOTES, rows=rows, audio_tracks=[i["title"] for i in items])
        step("report", "done")
        progress("report", 1.0, force=True)
        succeeded = True
    finally:
        if part and not succeeded:
            part.unlink(missing_ok=True)
        if work and not succeeded:
            say(f"Execução interrompida: arquivos intermediários preservados em {work} para permitir retomar.", "warn")
        elif work and a.keep_temp:
            say(f"Arquivos intermediários mantidos em: {work}")
        elif work:
            safe_rmtree(work)
    return 0


# ── frame / sdr ──
def locate_frame(path, a):
    """Display-order index of the wanted frame + the IDR to start decoding from (LCEVC may be temporal)."""
    tracks = mp4_tables(str(path))
    v = next((t for t in tracks if t["handler"] == "vide"), None)
    if not v or not v["dts"]:
        raise Fail("Nenhuma trilha de vídeo nas tabelas de amostras (MP4 fragmentado não é suportado; use --slow-seek --time).")
    n = len(v["dts"])
    pres = [mp4_present(v, k) for k in range(n)]
    order = sorted(range(n), key=pres.__getitem__)
    rank = [0] * n
    for r, k in enumerate(order):
        rank[k] = r
    ordered = [pres[k] for k in order]
    treq = None
    if a.frame is not None:
        if not 1 <= a.frame <= n:
            raise Fail(f"--frame deve estar entre 1 e {n}.")
        rt = a.frame - 1
    else:
        t = treq = parse_time_input(a.time)
        i = bisect.bisect_left(ordered, t)
        rt = min((j for j in (i - 1, i) if 0 <= j < n), key=lambda j: abs(ordered[j] - t))
    idr = cut_timeline(str(path))[0]
    ranks = [rank[x[1] - 1] for x in idr if 0 <= x[1] - 1 < n and rank[x[1] - 1] <= rt]
    ri = max(ranks) if ranks else 0
    fd = (ordered[-1] - ordered[0]) / max(1, n - 1)
    return {"rank": rt, "pts": ordered[rt], "idr_pts": ordered[ri], "skip": rt - ri, "fd": fd, "n": n, "treq": treq,
            "first": ordered[0], "last": ordered[-1]}


def decode_frame_rgb48(ff, src, loc, a, size, use_lcevc):
    """One frame -> uint16 RGB array (full range, still PQ-coded BT.2020)."""
    import numpy as np
    w, h = (int(x) for x in size.split("x"))
    pre = "lcevc," if use_lcevc else ""
    if a.slow_seek:
        sel, seek = f"select=gte(t\\,{max(0.0, loc['pts'] - loc['fd'] / 2):.6f})", []
    else:
        sel = f"select=eq(n\\,{loc['skip']})"
        seek = ["-noaccurate_seek", "-ss", f"{loc['idr_pts'] + loc['fd'] / 2:.6f}"]
    vf = (f"{pre}{sel},showinfo,setparams={HDR10_TAGS},"
          f"scale={w}:{h}:in_color_matrix=bt2020:in_range=tv:out_range=full:flags=accurate_rnd+full_chroma_int+bicubic,"
          "format=rgb48le")
    cmd = [ff, "-hide_banner", "-nostdin", *seek, "-i", str(src), "-map", "0:v:0", "-vf", vf, "-frames:v", "1", "-f", "rawvideo", "-"]
    log("$ " + subprocess.list2cmdline(cmd))
    p = sp_popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **NOWIN)
    err = Drain(p.stderr, keep=65536)
    data = p.stdout.read()
    rc = p.wait()
    txt = err.text()
    if rc != 0 or len(data) < w * h * 6:
        raise Fail(f"Não consegui decodificar o quadro (ffmpeg código {rc}, {len(data)} de {w * h * 6} bytes):\n{txt[-900:]}")
    m = re.search(r"pts_time:(-?[\d.]+)", txt)
    if m and not a.slow_seek:
        want, got = loc["pts"] - (loc["idr_pts"] + loc["fd"] / 2), float(m.group(1))
        if abs(got - want) > loc["fd"] * 0.6:
            raise Fail(f"O quadro decodificado está em {got:.3f}s, mas era esperado {want:.3f}s (o seek caiu em outro GOP). Tente --slow-seek.")
    return np.frombuffer(data[:w * h * 6], dtype="<u2").reshape(h, w, 3)


def png_chunk(tag, data):
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)


def write_png16_hdr(path, rgb, hdr):
    """16-bit RGB PNG with cICP (BT.2020 / PQ / RGB / full range) + mDCV + cLLI (PNG 3rd edition chunks)."""
    import numpy as np
    h, w, _ = rgb.shape
    be = rgb.astype(">u2").reshape(h, w * 3).view(np.uint8)          # (h, w*6) big-endian samples
    raw = np.empty((h, 1 + w * 6), np.uint8)
    raw[:, 0] = 1                                                      # filter 1 = Sub (bpp 6)
    raw[:, 1:7] = be[:, :6]
    raw[:, 7:] = be[:, 6:] - be[:, :-6]
    cp = lambda p: struct.pack(">HH", round(p[0] / 0.00002), round(p[1] / 0.00002))      # noqa: E731
    mdcv = (cp(hdr["r"]) + cp(hdr["g"]) + cp(hdr["b"]) + cp(hdr["wp"]) +
            struct.pack(">II", round(hdr["lmax"] * 10000), round(hdr["lmin"] * 10000)))
    clli = struct.pack(">II", hdr["maxcll"] * 10000, hdr["maxfall"] * 10000)
    png = (b"\x89PNG\r\n\x1a\n" + png_chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 16, 2, 0, 0, 0)) +
           png_chunk(b"cICP", bytes([9, 16, 0, 1])) + png_chunk(b"mDCV", mdcv) + png_chunk(b"cLLI", clli) +
           png_chunk(b"IDAT", zlib.compress(raw.tobytes(), 6)) + png_chunk(b"IEND", b""))
    Path(path).write_bytes(png)


def write_avif_hdr(ff, rgb, path, crf, chroma="444"):
    """10-bit AVIF (4:4:4 = AV1 High profile, 4:2:0 = Main profile, more widely supported) tagged BT.2020 / PQ (colr nclx 9/16/9).
    Needs libaom-av1 in the ffmpeg build. Carries no mastering-display/MaxCLL data (the 16-bit PNG does)."""
    h, w, _ = rgb.shape
    cmd = [ff, "-hide_banner", "-loglevel", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb48le", "-s", f"{w}x{h}",
           "-i", "-", "-vf", f"scale=out_color_matrix=bt2020:out_range=tv,format=yuv{chroma}p10le,setparams={HDR10_TAGS}",
           "-c:v", "libaom-av1", "-crf", str(crf), "-cpu-used", "6", "-still-picture", "1", "-frames:v", "1", str(path)]
    r = sp_run(cmd, capture_output=True, input=rgb.tobytes(), **NOWIN)
    if r.returncode != 0:
        raise Fail("Falha ao gerar o AVIF: " + (r.stderr or b"").decode("utf-8", "replace")[-500:])


def pq_eotf(e):
    import numpy as np
    m1, m2, c1, c2, c3 = 2610 / 16384, 2523 / 4096 * 128, 3424 / 4096, 2413 / 4096 * 32, 2392 / 4096 * 32
    ep = np.power(e, 1 / m2)
    return 10000.0 * np.power(np.maximum(ep - c1, 0) / (c2 - c3 * ep), 1 / m1)


def tone_curve(name, x, peak):
    """Maps scene-linear x (1.0 = SDR white) to [0,1]; peak = x value of the HDR peak."""
    import numpy as np
    if name == "clip" or peak <= 1.0:
        return np.minimum(x, 1.0)
    if name == "reinhard":
        return x * (1 + x / (peak * peak)) / (1 + x)
    if name == "hable":
        def hb(t):
            A, B, C, D, E, F = .15, .50, .10, .20, .02, .30
            return ((t * (A * t + C * B) + D * E) / (t * (A * t + B) + D * F)) - E / F
        k = 2.0
        return np.minimum(hb(x * k) / hb(peak * k), 1.0)
    j = 0.3                                                            # mobius (mpv): linear up to j, smooth roll-off
    a = -j * j * (peak - 1) / (j * j - 2 * j + peak)
    b = (j * j - 2 * j * peak + peak) / max(peak - 1, 1e-6)
    return np.where(x <= j, x, (b * b + 2 * b * j + j * j) / (b - a) * (x + a) / (x + b))


def tonemap_sdr(rgb16, hdr, algo, white, peak_nits, exposure=0.0, desat=0.0, saturation=1.0):
    """
    PQ BT.2020 (uint16, full range) -> 8-bit sRGB/BT.709. The curve acts on max(R,G,B) (hue-preserving).
    white: HDR nits that become SDR white (higher = darker image); exposure: stops; desat: highlight desaturation
    (ffmpeg 'desat' semantics, 0 = off); saturation: final multiplier (1 = unchanged).
    """
    import numpy as np
    m2020to709 = np.array([[1.6605, -0.5876, -0.0728], [-0.1246, 1.1329, -0.0083], [-0.0182, -0.1006, 1.1187]], np.float32)
    gain = 2.0 ** exposure
    peak = max(peak_nits / white * gain, 1.0)
    out = np.empty(rgb16.shape, np.uint8)
    wy = np.array([0.2126, 0.7152, 0.0722], np.float32)
    step_rows = 256
    for y in range(0, rgb16.shape[0], step_rows):
        e = rgb16[y:y + step_rows].astype(np.float32) / 65535.0
        lin = np.maximum((pq_eotf(e) / white * gain).astype(np.float32) @ m2020to709.T, 0)
        if desat > 0:                                      # pull very bright pixels towards grey (like ffmpeg tonemap)
            sig = (lin @ wy)[..., None]
            k = np.power(np.maximum(sig - 0.18, 1e-6) / np.maximum(sig, 1e-6), 10.0 / desat)
            lin = lin * (1 - k) + sig * k
        m = np.maximum(lin.max(axis=2, keepdims=True), 1e-6)
        lin = np.clip(lin * (tone_curve(algo, m, peak) / m), 0, 1)
        if saturation != 1.0:
            lum = (lin @ wy)[..., None]
            lin = np.clip(lum + saturation * (lin - lum), 0, 1)
        srgb = np.where(lin <= 0.0031308, 12.92 * lin, 1.055 * np.power(lin, 1 / 2.4) - 0.055)
        out[y:y + step_rows] = np.clip(srgb * 255 + 0.5, 0, 255).astype(np.uint8)
    return out


def save_sdr(img8, path, fmt, quality):
    try:
        from PIL import Image
    except ImportError:
        raise Fail("A imagem SDR precisa de Pillow e numpy: pip install pillow numpy")
    im = Image.fromarray(img8, "RGB")
    icc = None
    try:
        from PIL import ImageCms
        icc = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    except Exception:
        pass
    kw = {"icc_profile": icc} if icc else {}
    if fmt == "jpg":
        im.save(path, quality=quality, subsampling=0, optimize=True, **kw)
    else:
        im.save(path, compress_level=6, **kw)


def read_png16_hdr(path):
    """(uint16 RGB array, hdr dict) from a PNG written by write_png16_hdr."""
    import numpy as np
    b = Path(path).read_bytes()
    if b[:8] != b"\x89PNG\r\n\x1a\n":
        raise Fail("Não é um arquivo PNG.")
    hdr, i, idat = dict(DEFAULT_HDR10), 8, b""
    w = h = 0
    while i < len(b):
        n, t = struct.unpack(">I", b[i:i + 4])[0], b[i + 4:i + 8]
        d = b[i + 8:i + 8 + n]
        if t == b"IHDR":
            w, h, depth, ctype = struct.unpack(">IIBB", d[:10])
            if depth != 16 or ctype != 2:
                raise Fail("Só aceito o PNG RGB de 16 bits gerado pelo comando 'frame'.")
        elif t == b"cLLI":
            hdr["maxcll"], hdr["maxfall"] = (x // 10000 for x in struct.unpack(">II", d))
        elif t == b"mDCV":
            v = struct.unpack(">HHHHHHHHII", d)
            hdr.update(r=(v[0] * 2e-5, v[1] * 2e-5), g=(v[2] * 2e-5, v[3] * 2e-5), b=(v[4] * 2e-5, v[5] * 2e-5),
                       wp=(v[6] * 2e-5, v[7] * 2e-5), lmax=v[8] / 10000, lmin=v[9] / 10000)
        elif t == b"IDAT":
            idat += d
        i += 12 + n
    raw = np.frombuffer(zlib.decompress(idat), np.uint8).reshape(h, 1 + w * 6)
    if not (raw[:, 0] == 1).all():
        raise Fail("Filtro PNG não suportado (esperado o filtro Sub usado pelo comando 'frame').")
    px = np.cumsum(raw[:, 1:].reshape(h, w, 6).astype(np.uint32), axis=1, dtype=np.uint32) & 0xFF   # undo Sub, bpp 6
    px = px.astype(np.uint8).reshape(h, w * 6)
    return px.view(">u2").reshape(h, w, 3).astype("<u2"), hdr


FRAME_STEPS = [("probe", "Localizar o quadro e medir o tamanho"), ("decode", "Decodificar o quadro"), ("save", "Salvar as imagens"), ("report", "Resumo")]
FRAME_WEIGHTS = {"probe": 20, "decode": 55, "save": 24, "report": 1}


def tone_args_summary(a):
    return f"{a.tonemap} · branco {a.white_nits:g} nits" + (f" · exposição {a.exposure:+g}" if a.exposure else "") + \
        (f" · saturação {a.saturation:g}" if a.saturation != 1.0 else "")


def cmd_frame(a):
    try:
        import numpy  # noqa: F401  (availability check)
    except ImportError:
        raise Fail("O comando frame precisa de numpy: pip install numpy pillow")
    set_steps(FRAME_STEPS)
    set_stages(FRAME_WEIGHTS)
    hdr_fmts = [] if a.hdr == "none" else [x.strip() for x in a.hdr.split(",")]
    sdr_fmts = [] if a.sdr == "none" else [x.strip() for x in a.sdr.split(",")]
    if any(x not in ("png", "avif") for x in hdr_fmts) or any(x not in ("png", "jpg") for x in sdr_fmts):
        raise Fail("--hdr aceita png,avif,none; --sdr aceita png,jpg,none.")
    step("probe", "running")
    ff, src, info, use_lcevc = tc_prepare(a)
    hdr = probe_hdr10(ff, src)
    loc = locate_frame(src, a)
    if loc["treq"] is not None and not loc["first"] - loc["fd"] <= loc["treq"] <= loc["last"] + loc["fd"]:
        note(f"O tempo pedido ({fmt_clock(loc['treq'])}) está fora do vídeo ({fmt_clock(loc['first'])} a {fmt_clock(loc['last'])}): "
             f"usando o quadro mais próximo.")
    say(f"Quadro #{loc['rank'] + 1} (ordem de exibição) em {fmt_clock(loc['pts'])} · decodificando a partir do IDR em {fmt_clock(loc['idr_pts'])} (+{loc['skip']} quadros)")
    measured = verify_enhancement(ff, str(src), min(8, loc["skip"] + 2), start=None if a.slow_seek else loc["idr_pts"] + loc["fd"] / 2) if use_lcevc else None
    size = pick_size(a, measured) or (info["video"]["size"] if not use_lcevc else None)
    if not size:
        raise Fail("Não consegui determinar o tamanho do quadro pela verificação; informe --size LARGURAxALTURA.")
    step("probe", "done", f"tamanho {size}")
    step("decode", "running")
    rgb = decode_frame_rgb48(ff, src, loc, a, size, use_lcevc)
    step("decode", "done", f"{rgb.shape[1]}x{rgb.shape[0]}")
    step("save", "running")
    base = Path(a.output) if a.output else Path(a.outdir or src.parent) / f"{src.stem}_f{loc['rank'] + 1:06d}"
    base.parent.mkdir(parents=True, exist_ok=True)
    base = base.with_suffix("") if base.suffix.lower() in (".png", ".jpg", ".jpeg", ".avif") else base
    made = []
    for f in hdr_fmts:
        p = unique_path(base.with_name(base.name + f"_hdr.{f}"), a.overwrite)
        if f == "png":
            write_png16_hdr(p, rgb, hdr)
        elif "libaom-av1" not in (sp_run([ff, "-hide_banner", "-encoders"], capture_output=True, text=True, errors="replace", **NOWIN).stdout or ""):
            note("Este ffmpeg não tem libaom-av1: AVIF ignorado (o PNG de 16 bits é o arquivo HDR).")
            continue
        else:
            write_avif_hdr(ff, rgb, p, a.avif_crf, a.avif_chroma)
        made.append({"kind": "frame-hdr", "title": "Imagem HDR (" + ("PNG 16 bits, BT.2020/PQ" if f == "png" else "AVIF 10 bits, BT.2020/PQ") + ")", "path": str(p.resolve()), "size": p.stat().st_size})
    if sdr_fmts:
        peak = a.peak_nits or hdr["maxcll"] or hdr["lmax"]
        img8 = tonemap_sdr(rgb, hdr, a.tonemap, a.white_nits, float(peak), a.exposure, a.desat, a.saturation)
        for f in sdr_fmts:
            p = unique_path(base.with_name(base.name + f"_sdr.{f}"), a.overwrite)
            save_sdr(img8, p, f, a.jpg_quality)
            made.append({"kind": "frame-sdr", "title": f"Imagem SDR ({f.upper()}, BT.709/sRGB)", "path": str(p.resolve()), "size": p.stat().st_size})
    step("save", "done", f"{len(made)} arquivo(s)")
    step("report", "running")
    rows = [["Quadro", f"#{loc['rank'] + 1} · {fmt_clock(loc['pts'])}"], ["Tamanho", size], ["HDR10", hdr10_summary(hdr)]]
    if sdr_fmts:
        rows.append(["Tone mapping SDR", tone_args_summary(a)])
    say(bold("Resultado:"))
    for m in made:
        say(f"  {m['title']}: {m['path']} ({human(m['size'])})")
    ev(type="result", out=made[0]["path"] if made else "", outputs=made, notes=NOTES, rows=rows)
    step("report", "done")
    progress("report", 1.0, force=True)
    return 0


# ──────────────────────────── preview (Cortar MP4) ────────────────────────────
PREVIEW_TONEMAP = ("zscale=t=linear:npl=100", "format=gbrpf32le", "zscale=p=bt709", "tonemap=tonemap=hable:desat=0",
                   "zscale=t=bt709:m=bt709:r=tv", "format=yuv420p")


def preview_vf(exes, width, lcevc=True, tone=True):
    """
    Video filter chain of the preview: lcevc (enhancement) -> scale -> HDR-to-SDR (zscale + tonemap). Each stage is used only when
    the build has the filter (the first of `exes` that lists its filters decides). Returns (chain, [what was left out]).
    """
    have = next((f for f in (ffmpeg_filters(x) for x in exes if x) if f), None)
    parts, left = [], []
    if lcevc:
        if have is None or "lcevc" in have:
            parts.append("lcevc")
        else:
            left.append("este programa não tem o filtro lcevc: só a camada base é mostrada")
    parts.append(f"scale={int(width)}:-2:flags=bilinear")
    if tone:
        if have is not None and not {"zscale", "tonemap"} <= have:
            left.append("sem zscale/tonemap: as cores HDR aparecem sem conversão (imagem lavada)")
        else:
            parts += [f"setparams={HDR10_TAGS}", *PREVIEW_TONEMAP]
    return ",".join(parts), left


def preview_frame_cmd(ff, src, t, vf, out):
    return [ff, "-hide_banner", "-nostdin", "-loglevel", "error", "-ss", f"{max(0.0, t):.3f}", "-i", str(src), "-map", "0:v:0",
            "-vf", vf, "-frames:v", "1", "-update", "1", "-y", str(out)]


def play_cmd(ffplay, src, start, end, vf, title):
    cmd = [ffplay, "-hide_banner", "-loglevel", "warning", "-nostats", "-autoexit", "-an", "-window_title", title]
    if start > 0:
        cmd += ["-ss", f"{start:.3f}"]
    if end is not None:
        cmd += ["-t", f"{max(0.04, end - start):.3f}"]
    return cmd + ["-vf", vf, "-i", str(src)]


def cmd_preview(a):
    """Still frames of a few instants (cheap: one seek + one decoded frame each), SDR and scaled, for the Cut MP4 page."""
    set_steps([("decode", "Extrair quadros"), ("report", "Resumo")])
    set_stages({"decode": 95, "report": 5})
    step("decode", "running")
    ff = find_tool("ffmpeg", a.ffmpeg)[0]
    src = Path(a.input).resolve()
    if not src.is_file():
        raise Fail(f"Arquivo não encontrado: {src}")
    times = [parse_time_input(t) for t in a.time]
    out = Path(a.outdir).resolve() if a.outdir else Path(tempfile.mkdtemp(prefix="dtv_preview_"))
    out.mkdir(parents=True, exist_ok=True)
    vf, left = preview_vf([ff], a.width, not a.no_lcevc, not a.no_tonemap)
    for x in left:
        note(x)
    stamp = int(time.time() * 1000)

    def one(item):
        i, t = item
        p = out / f"pv_{stamp}_{i}.png"
        cmd = preview_frame_cmd(ff, src, t, vf, p)
        log("$ " + subprocess.list2cmdline(cmd))
        r = sp_run(cmd, capture_output=True, text=True, errors="replace", timeout=180, **NOWIN)
        if r.returncode != 0 or not p.is_file():
            return i, t, None, (r.stderr or "").strip()[-400:]
        return i, t, p, ""

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=min(2, len(times))) as ex:
        got = list(ex.map(one, list(enumerate(times))))
    outs = [{"kind": "preview", "index": i, "time": t, "path": str(p), "size": p.stat().st_size} for i, t, p, _ in got if p]
    fails = [(t, e) for _, t, p, e in got if not p]
    if not outs:
        raise Fail("Não consegui extrair nenhum quadro:\n" + (fails[0][1] if fails else ""))
    for t, e in fails:
        note(f"Quadro em {fmt_clock(t)} não extraído: {e.splitlines()[-1] if e else 'erro desconhecido'}")
    step("decode", "done", f"{len(outs)} quadro(s)")
    step("report", "running")
    ev(type="result", out=outs[0]["path"], outputs=outs, notes=NOTES, rows=[["Quadros", str(len(outs))], ["Filtros", vf]], previews=outs)
    step("report", "done")
    progress("report", 1.0, force=True)
    return 0


def cmd_play(a):
    """Plays a stretch of the video in an ffplay window (no audio: MPEG-H is not decodable by ffplay). Waits until it is closed."""
    set_steps([("play", "Reproduzir")])
    set_stages({"play": 100})
    step("play", "running")
    play = find_tool("ffplay", a.ffplay)[0]
    ff = optional_tool("ffmpeg", a.ffmpeg)
    src = Path(a.input).resolve()
    if not src.is_file():
        raise Fail(f"Arquivo não encontrado: {src}")
    start = parse_time_input(a.start) if a.start else 0.0
    end = parse_time_input(a.end) if a.end else None
    if end is not None and end <= start:
        raise Fail("O fim deve ser depois do início.")
    vf, left = preview_vf([play, ff], a.width, not a.no_lcevc, not a.no_tonemap)
    for x in left:
        note(x)
    title = f"{src.name}  [{fmt_clock(start)} – {fmt_clock(end) if end is not None else 'fim'}]"
    cmd = play_cmd(play, src, start, end, vf, title)
    log("$ " + subprocess.list2cmdline(cmd))
    p = sp_popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL)
    err = Drain(p.stderr, keep=8192)
    try:
        rc = p.wait()
    except BaseException:
        p.kill()
        raise
    if rc != 0:
        raise Fail(f"O ffplay terminou com código {rc}:\n{err.text()[-600:]}")
    step("play", "done")
    progress("play", 1.0, force=True)
    ev(type="result", out="", outputs=[], notes=NOTES, rows=[["Trecho", f"{fmt_clock(start)} – {fmt_clock(end) if end is not None else 'fim'}"], ["Filtros", vf]])
    return 0


# ──────────────────────────── assisted download of programs ────────────────────────────
_BTBN = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/"
_LIMA_REPO = "limaalef/ffmpeg-autobuild-lcevc-mpegh"
_LIMA_NOTICE = ("Pacote do projeto limaalef/ffmpeg-autobuild-lcevc-mpegh (GitHub): ffmpeg, ffprobe, ffplay, mpeghDecoder e mpeghUIManager já "
                "compilados com VVC, LCEVC (V-Nova) e MPEG-H (Fraunhofer), para Windows x64, Linux x64 (glibc 2.39 ou mais nova; sem ffplay) e "
                "macOS Intel/Apple Silicon (macOS 11 ou mais novo). A build inclui componentes GPL/não-livres (libx265, libfdk-aac...), então não "
                "pode ser redistribuída livremente, e o LCEVCdec e o mpeghdec têm licenças próprias: confira as licenças de cada componente antes "
                "de usar ou compartilhar. O arquivo é conferido pelo SHA-256 publicado na release (digest do GitHub ou SHA256SUMS.txt).")


def _lima(pattern):
    return {"github": _LIMA_REPO, "asset": pattern, "package": "ffmpeg-lcevc-mpegh"}


_LIMA_ASSETS = {"win64": _lima(r"win64-static-limaalef\.zip$"), "linux64": _lima(r"linux64-static-limaalef\.tar\.xz$"),
                "macos-intel": _lima(r"macos-intel-limaalef\.zip$"), "macos-arm64": _lima(r"macos-arm64-limaalef\.zip$")}
_PKG = ("ffmpeg", "ffplay", "mpegh_ui", "mpegh_decoder")
TOOL_SOURCES = {}
for _k in _PKG:
    TOOL_SOURCES[_k] = {
        "title": "Pacote FFmpeg + LCEVC + MPEG-H (limaalef)",
        "provides": _PKG,
        "license": "GPL v3 e licenças próprias de LCEVCdec (V-Nova) e mpeghdec (Fraunhofer)",
        "license_url": "https://github.com/" + _LIMA_REPO,
        "notice": _LIMA_NOTICE,
        "assets": {k: dict(v) for k, v in _LIMA_ASSETS.items()},
    }
_BTBN_SRC = {
    "title": "FFmpeg + ffplay (builds automáticas BtbN, GPL)",
    "provides": ("ffmpeg", "ffplay"),
    "license": "GPL v3 (a build inclui componentes GPL, como libx265)",
    "license_url": "https://www.ffmpeg.org/legal.html",
    "notice": ("O FFmpeg é software livre; esta build usa a licença GPL v3, que impõe condições à redistribuição (inclusive do que você "
               "empacotar com ela). Os arquivos vêm de github.com/BtbN/FFmpeg-Builds, não do projeto FFmpeg, e são conferidos pelo SHA-256 "
               "publicado na mesma página. Esta build NÃO tem o filtro lcevc nem MPEG-H: para isso use a sua build."),
    "assets": {
        "winarm64": {"url": _BTBN + "ffmpeg-master-latest-winarm64-gpl.zip", "sha256_url": _BTBN + "checksums.sha256", "package": "ffmpeg-btbn"},
        "linuxarm64": {"url": _BTBN + "ffmpeg-master-latest-linuxarm64-gpl.tar.xz", "sha256_url": _BTBN + "checksums.sha256", "package": "ffmpeg-btbn"},
    },
}
for _k in ("ffmpeg", "ffplay"):                     # sem pacote do limaalef (Windows/Linux ARM64): o ffmpeg/ffplay genérico
    for _plat, _asset in _BTBN_SRC["assets"].items():
        TOOL_SOURCES[_k]["assets"].setdefault(_plat, _asset)
TOOL_SOURCES["mp4box"] = {
    "title": "MP4Box (GPAC)",
    "provides": ("mp4box",),
    "license": "LGPL v2.1 ou posterior",
    "license_url": "https://github.com/gpac/gpac/blob/master/COPYING",
    "notice": ("O GPAC é distribuído como instalador (.exe/.dmg) ou pacote do sistema, sem arquivo avulso estável: não há download "
               "automático. Baixe em https://gpac.io/downloads/ ou, se você tiver o endereço de um .zip/.tar com o MP4Box, informe-o abaixo "
               "(com o SHA-256 se o publicador fornecer)."),
    "assets": {},
}
GENERIC_NOTICE = ("Você informou um endereço próprio: o programa baixado tem a licença do seu autor e a conferência dele é sua. O SHA-256 abaixo "
                  "só prova que o arquivo é o mesmo que o publicador divulgou, não que ele é seguro.")


def source_platform_key():
    import platform
    m = platform.machine().lower()
    arm = m in ("arm64", "aarch64")
    if sys.platform.startswith("win"):
        return "winarm64" if arm else "win64"
    if sys.platform.startswith("linux"):
        return "linuxarm64" if arm else "linux64"
    return "macos-arm64" if arm else "macos-intel"


def tool_source_info(key):
    src = TOOL_SOURCES.get(key) or {}
    asset = (src.get("assets") or {}).get(source_platform_key()) or {}
    return {"title": src.get("title", TOOL_DEFS[key]["label"]), "license": src.get("license", ""), "license_url": src.get("license_url", ""),
            "notice": src.get("notice", GENERIC_NOTICE), "provides": list(src.get("provides", (key,))),
            "url": asset.get("url", ""), "sha256_url": asset.get("sha256_url", ""), "auto": bool(asset),
            "github": asset.get("github", ""), "asset": asset.get("asset", ""), "package": asset.get("package", key),
            "sha256_auto": bool(asset.get("github"))}


def resolve_github_asset(repo, pattern, timeout):
    """Latest release of a GitHub repository -> (download url, sha256 or ''). The hash is the `digest` GitHub publishes for the asset."""
    base = os.environ.get("DTV_GITHUB_API", "https://api.github.com").rstrip("/")
    if not url_allowed(base):
        raise Fail("DTV_GITHUB_API precisa ser https://.")
    with http_open(f"{base}/repos/{repo}/releases/latest", timeout) as r:
        rel = json.loads(r.read(5_000_000).decode("utf-8", "replace"))
    for a in rel.get("assets") or []:
        if re.search(pattern, a.get("name", "")):
            dg = str(a.get("digest") or "")
            sha = dg.split(":", 1)[1].lower() if dg.lower().startswith("sha256:") else ""
            if not sha:                                          # a sibling '<asset>.sha256' file, if the project publishes one
                for b in rel.get("assets") or []:
                    if b.get("name") in (a["name"] + ".sha256", a["name"] + ".sha256sum", "SHA256SUMS.txt", "SHA256SUMS", "checksums.sha256"):
                        with http_open(b["browser_download_url"], timeout) as r2:
                            sha = parse_checksums(r2.read(200_000).decode("utf-8", "replace"), a["name"]) or ""
                        if sha:
                            break
            return a["browser_download_url"], sha, rel.get("tag_name", "")
    raise Fail(f"A última release de {repo} não tem arquivo '{pattern}'.")


def url_allowed(u):
    from urllib.parse import urlparse
    pu = urlparse(u)
    if pu.scheme == "https" and pu.hostname:
        return True
    return pu.scheme == "http" and pu.hostname in ("localhost", "127.0.0.1", "::1")          # http only for tests / local mirrors


def parse_checksums(text, filename):
    """SHA-256 of `filename` in a checksum file ('<hash>  <name>' lines, or a bare hash when the file has just one)."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.startswith("#")]
    for ln in lines:
        m = re.match(r"^([0-9a-fA-F]{64})\s+\*?(.+)$", ln)
        if m and os.path.basename(m.group(2).strip()) == filename:
            return m.group(1).lower()
    if len(lines) == 1 and re.fullmatch(r"[0-9a-fA-F]{64}", lines[0]):
        return lines[0].lower()
    return None


def http_open(url, timeout):
    import urllib.error
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": f"dtv_tools/{__version__}"})
    try:
        return urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        raise Fail(f"O servidor respondeu HTTP {e.code} para {url}")
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        raise Fail(f"Não foi possível baixar (sem internet?): {getattr(e, 'reason', e)}")


def safe_extract(archive, dest):
    """Extracts a zip / tar(.gz/.xz/.bz2) refusing paths that leave `dest` and links. Returns False when it is not an archive."""
    import tarfile
    import zipfile
    root = Path(dest).resolve()

    def inside(name):
        t = (root / name).resolve()
        return t == root or root in t.parents
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as z:
            for n in z.namelist():
                if not inside(n):
                    raise Fail(f"Arquivo compactado recusado: caminho suspeito '{n}'.")
            z.extractall(root)
            for i in z.infolist():                      # zip does not restore the executable bit
                mode = (i.external_attr >> 16) & 0o777
                if mode & 0o100 and not i.is_dir():
                    (root / i.filename).chmod(mode)
        return True
    if tarfile.is_tarfile(archive):
        with tarfile.open(archive) as t:
            for m in t.getmembers():
                if not inside(m.name) or m.issym() or m.islnk():
                    if m.issym() or m.islnk():
                        continue
                    raise Fail(f"Arquivo compactado recusado: caminho suspeito '{m.name}'.")
            members = [m for m in t.getmembers() if not (m.issym() or m.islnk())]
            t.extractall(root, members=members)
        return True
    return False


def find_programs(folder, keys):
    """{key: path} of the executables named like TOOL_DEFS[key]['names'] found under `folder`."""
    exts = ("", ".exe") if os.name == "nt" else ("",)
    want = {}
    for k in keys:
        for n in TOOL_DEFS[k]["names"]:
            for e in exts:
                want[(n + e).lower()] = k
    found = {}
    for dp, _dn, fns in os.walk(folder):
        for fn in sorted(fns):
            k = want.get(fn.lower())
            if k and k not in found:
                found[k] = os.path.join(dp, fn)
    for pth in found.values():
        try:
            os.chmod(pth, os.stat(pth).st_mode | 0o111)
        except OSError:
            pass
    return found


def cmd_fetch_tool(a):
    """Downloads a program, checks its SHA-256, unpacks it and reports where the executables are (never bundled in releases)."""
    import hashlib
    import shutil as _sh
    set_steps([("source", "Preparar"), ("download", "Baixar"), ("verify", "Conferir SHA-256"), ("install", "Extrair e localizar"), ("report", "Resumo")])
    set_stages({"source": 3, "download": 75, "verify": 7, "install": 13, "report": 2})
    key = a.tool
    info = tool_source_info(key)
    step("source", "running")
    url = a.url or info["url"]
    resolved_hash, tag = "", ""
    if not url and info["github"]:
        url, resolved_hash, tag = resolve_github_asset(info["github"], info["asset"], a.timeout)
        say(f"Última release de {info['github']}: {tag}", "ok")
    auto_src = not a.url
    if not url:
        raise Fail(f"Não há download automático do {TOOL_DEFS[key]['label']} para este sistema. Informe o endereço com --url.")
    if not url_allowed(url):
        raise Fail("Só endereços https:// são aceitos para baixar programas.")
    if not a.accept_license:
        raise Fail("Leia o aviso de licença e confirme com --accept-license:\n" + (info["notice"] if not a.url or a.url == info["url"] else GENERIC_NOTICE))
    name = os.path.basename(url.split("?")[0]) or f"{key}.bin"
    expected = (a.sha256 or "").strip().lower()
    if expected and not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise Fail("O SHA-256 informado não tem 64 dígitos hexadecimais.")
    if not expected and resolved_hash and auto_src:
        expected = resolved_hash
        say(f"SHA-256 esperado (publicado pelo GitHub): {expected}", "ok")
    sums_url = a.sha256_url or (info["sha256_url"] if url == info["url"] else "")
    if not expected and sums_url and not (auto_src and info["github"]):
        if not url_allowed(sums_url):
            raise Fail("O endereço do arquivo de checksums precisa ser https://.")
        with http_open(sums_url, a.timeout) as r:
            expected = parse_checksums(r.read(2_000_000).decode("utf-8", "replace"), name) or ""
        if not expected:
            raise Fail(f"O arquivo de checksums não lista '{name}'.")
        say(f"SHA-256 esperado (de {sums_url}): {expected}", "ok")
    if not expected and not a.allow_unverified:
        raise Fail("Sem SHA-256 para conferir o arquivo. Informe --sha256 (ou --sha256-url); só use --allow-unverified se aceitar o risco.")
    dest = Path(a.dest).resolve() if a.dest else TOOLS_DIR.resolve()
    dl = dest / ".download"
    dl.mkdir(parents=True, exist_ok=True)
    part = dl / (name + ".part")
    step("source", "done")
    step("download", "running")
    h = hashlib.sha256()
    got, t0, limit = 0, time.time(), int(a.max_size * 1024 * 1024)
    log(f"GET {url}")
    try:
        with http_open(url, a.timeout) as r, open(part, "wb") as f:
            total = int(r.headers.get("Content-Length") or 0)
            if total and total > limit:
                raise Fail(f"O arquivo tem {human(total)}, acima do limite de {human(limit)} (--max-size).")
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                got += len(chunk)
                if got > limit:
                    raise Fail(f"O download passou do limite de {human(limit)} (--max-size).")
                h.update(chunk)
                f.write(chunk)
                spd = got / max(time.time() - t0, 0.001)
                progress("download", got / total if total else 0.0, f"{human(got)}" + (f" de {human(total)}" if total else "") + f" · {human(spd)}/s")
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    progress("download", 1.0, f"{human(got)}", force=True)
    step("download", "done", human(got))
    step("verify", "running")
    digest = h.hexdigest()
    if expected and digest != expected:
        part.unlink(missing_ok=True)
        raise Fail(f"SHA-256 diferente do esperado: o arquivo foi descartado.\n  esperado: {expected}\n  obtido:   {digest}")
    verified = bool(expected)
    if verified:
        say(f"SHA-256 conferido: {digest}", "ok")
    else:
        note(f"Arquivo sem verificação (SHA-256 {digest}).")
    step("verify", "done", "conferido" if verified else "não verificado")
    step("install", "running")
    target = dest / (info["package"] if auto_src else key)
    marker = target / ".dtv_fetch.json"
    if target.exists():
        if not marker.is_file():
            part.unlink(missing_ok=True)
            raise Fail(f"A pasta {target} já existe e não foi criada por este programa; remova-a ou use --dest.")
        _sh.rmtree(target)
    target.mkdir(parents=True)
    archive = dl / name
    os.replace(part, archive)
    try:
        if safe_extract(archive, target):
            archive.unlink(missing_ok=True)
        elif name.lower().endswith((".exe", ".msi", ".dmg", ".pkg", ".deb", ".rpm")) and not find_programs_name(name, info["provides"]):
            raise Fail(f"Isto é um instalador, não um programa avulso. Ele ficou em {archive}: execute-o e informe depois o caminho do programa instalado.")
        else:
            _sh.move(str(archive), str(target / name))
    except Fail:
        raise
    found = find_programs(target, info["provides"] + [key])
    if not found:
        raise Fail(f"Não achei o {TOOL_DEFS[key]['label']} dentro do arquivo baixado ({target}).")
    if key not in found:
        note(f"Este pacote não traz o {TOOL_DEFS[key]['label']} (fica sem; os demais foram instalados).")
    marker.write_text(json.dumps({"tool": key, "url": url, "sha256": digest, "verified": verified, "license": info["license"],
                                  "date": time.strftime("%Y-%m-%d %H:%M:%S")}, ensure_ascii=False, indent=1), encoding="utf-8")
    step("install", "done")
    step("report", "running")
    for k, pth in found.items():
        say(f"{TOOL_DEFS[k]['label']}: {pth}", "ok")
    rows = [["Programa", TOOL_DEFS[key]["label"]], ["Origem", url], ["SHA-256", digest + ("  (conferido)" if verified else "  (NÃO verificado)")],
            ["Licença", info["license"] or "do autor"], ["Pasta", str(target)]]
    first = found.get(key) or next(iter(found.values()))
    ev(type="result", out=first, outputs=[], notes=NOTES, rows=rows,
       installed={"tool": key, "path": found.get(key, ""), "found": found, "sha256": digest, "verified": verified, "url": url, "size": got, "dir": str(target)})
    step("report", "done")
    progress("report", 1.0, force=True)
    return 0


def find_programs_name(filename, keys):
    low = filename.lower()
    return any(low.startswith(n.lower()) for k in keys for n in TOOL_DEFS[k]["names"])


# BUILD-REMOVE-START-ADB
# ──────────────────────────── Acesso via ADB (fora das compilações) ────────────────────────────
ADB_API_PORT = 44642
PLATFORM_TOOLS_URL = "https://developer.android.com/tools/releases/platform-tools"


def find_adb(given=""):
    """adb: the given path, PATH, the Android SDK (ANDROID_HOME / ANDROID_SDK_ROOT / default folder) or ./platform-tools. '' when none."""
    names = ("adb.exe", "adb") if os.name == "nt" else ("adb",)
    if given:
        return given if os.path.isfile(given) else ""
    w = shutil.which("adb")
    if w:
        return w
    roots = [os.environ.get("ANDROID_HOME", ""), os.environ.get("ANDROID_SDK_ROOT", "")]
    if os.name == "nt":
        roots.append(os.path.join(os.environ.get("LOCALAPPDATA", ""), "Android", "Sdk"))
    elif sys.platform == "darwin":
        roots.append(os.path.expanduser("~/Library/Android/sdk"))
    else:
        roots.append(os.path.expanduser("~/Android/Sdk"))
    here = Path(__file__).resolve().parent
    cands = [os.path.join(r, "platform-tools", n) for r in roots if r for n in names]
    cands += [str(here / "platform-tools" / n) for n in names] + [str(here.parent / "platform-tools" / n) for n in names]
    return next((c for c in cands if os.path.isfile(c)), "")


def adb_serial(device):
    """-> (serial, over_network). 'IP' becomes 'IP:5555'; 'IP:port' is kept; anything else is a USB serial."""
    d = device.strip()
    if re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", d):
        return d + ":5555", True
    if re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}:\d+", d):
        return d, True
    return d, False


def parse_adb_devices(text):
    """`adb devices -l` -> [{serial, state, desc}]."""
    out = []
    for ln in text.splitlines():
        ln = ln.strip()
        if not ln or ln.startswith(("List of", "*")):
            continue
        parts = ln.split(None, 2)
        if len(parts) >= 2:
            out.append({"serial": parts[0], "state": parts[1], "desc": parts[2] if len(parts) > 2 else ""})
    return out


def parse_forward_list(raw):
    """`adb forward --list` -> [{serial, local, remote}]."""
    out = []
    for ln in raw.splitlines():
        p = ln.split()
        if len(p) >= 3:
            out.append({"serial": p[0], "local": p[1], "remote": p[2]})
    return out


def media_local_url(current_media):
    """currentMedia (an address on 127.0.0.1 INSIDE the device) -> (port, same address on this computer)."""
    from urllib.parse import urlsplit
    u = urlsplit(current_media)
    if not u.port:
        raise Fail(f"Não consegui extrair a porta de '{current_media}'")
    return u.port, f"{u.scheme}://127.0.0.1:{u.port}{u.path}" + (f"?{u.query}" if u.query else "")


def parse_proc_net_tcp(raw):
    """/proc/net/tcp(6) -> [(ip, port)] in LISTEN (state 0A)."""
    res = []
    for ln in raw.strip().splitlines()[1:]:
        p = ln.split()
        if len(p) < 4 or p[3].upper() != "0A" or ":" not in p[1]:
            continue
        ip_hex, port_hex = p[1].split(":")
        try:
            port = int(port_hex, 16)
        except ValueError:
            continue
        ip = ".".join(str(x) for x in reversed(bytes.fromhex(ip_hex))) if len(ip_hex) == 8 else ip_hex
        res.append((ip, port))
    return res


def parse_netstat_listen(raw):
    """`netstat -tln` / `ss -tln` -> [(ip, port)] in LISTEN."""
    res = []
    for ln in raw.splitlines():
        if "LISTEN" not in ln.upper():
            continue
        m = re.search(r"([\d\.\*]+|\[?::1?\]?):(\d+)\s", ln.strip() + " ")
        if m:
            res.append((m.group(1), int(m.group(2))))
    return res


def adb_run(adb, args, timeout=15):
    """-> (returncode, stdout, stderr); never raises (a hung or missing adb comes back as rc -1/-2)."""
    try:
        r = sp_run([adb, *args], capture_output=True, text=True, errors="replace", timeout=timeout, **NOWIN)
        return r.returncode, (r.stdout or "").strip(), (r.stderr or "").strip()
    except subprocess.TimeoutExpired:
        return -2, "", "tempo esgotado"
    except OSError as e:
        return -1, "", str(e)


def adb_probe_port(port, timeout=2.5):
    """What answers on a forwarded local port: HTTP first, then a raw TCP banner."""
    import socket
    try:
        with http_open(f"http://127.0.0.1:{port}/", timeout) as r:
            return {"http": {"status": r.status, "content_type": r.headers.get("Content-Type", ""), "snippet": r.read(200).decode("utf-8", "replace")},
                    "banner": None, "error": None}
    except Fail:
        pass
    res = {"http": None, "banner": None, "error": None}
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout) as sk:
            sk.settimeout(timeout)
            try:
                data = sk.recv(200)
            except socket.timeout:
                data = b""
            res["banner"] = data.decode("utf-8", "replace") if data else "(conectou, sem resposta)"
    except OSError as e:
        res["error"] = str(e)[:150]
    return res


def cmd_adb_access(a):
    """
    Connects to a TV 3.0 receiver with adb, forwards the port of its media-player API, reads `currentMedia` (an address on the device's
    own loopback) and forwards that port too, so the stream can be captured from this computer. With --watch it repeats every
    --interval seconds and redoes the forward when the address changes (new channel, signal loss). Stop = Ctrl+C / stop command.
    """
    set_steps([("connect", "Conectar ao aparelho"), ("watch", "Acompanhar o endereço")])
    set_stages({"connect": 30, "watch": 70})
    step("connect", "running")
    adb = find_adb(a.adb)
    if not adb:
        raise Fail(f"adb não encontrado. Instale o Android platform-tools ({PLATFORM_TOOLS_URL}) ou informe o caminho com --adb.")
    serial, net = adb_serial(a.device)
    say(f"adb: {adb}", "ok")
    made = {"api": False, "media": 0, "scan": set()}          # forwards this run created (removed when it ends)
    state = {"device_state": "", "action": "", "api_ok": False, "player": None, "media": "", "media_port": 0, "local_url": "", "changed": False,
             "manifest": "", "forwards": [], "ports": [], "error": "", "cycle": 0, "serial": serial}

    def emit():
        ev(type="adb", **{k: v for k, v in state.items()})

    def device_state():
        rc, out, err = adb_run(adb, ["devices", "-l"], 15)
        if rc != 0:
            return "", err or out or "o adb não respondeu"
        for d in parse_adb_devices(out):
            if d["serial"] == serial:
                return d["state"], ""
        return "absent", ""

    def ensure_connected(deadline):
        """Waits until the device is in state 'device'; says what to do on the device otherwise."""
        tried = False
        while True:
            if net and not tried:
                tried = True
                rc, out, err = adb_run(adb, ["connect", a.device if ":" in a.device else a.device], 20)
                log(f"adb connect: {out or err}")
            st, err = device_state()
            state["device_state"], state["error"] = st, err
            if st == "device":
                state["action"] = ""
                return True
            state["action"] = {"unauthorized": "No aparelho: toque em “Permitir depuração” para autorizar este computador.",
                               "offline": "No aparelho: desative e ative de novo a depuração (ou reconecte o cabo).",
                               "absent": "No aparelho: ative a depuração por Wi-Fi/USB e confira o endereço IP."}.get(st, "")
            emit()
            if time.time() > deadline:
                return False
            if net and st in ("absent", "offline"):
                tried = False
            time.sleep(min(2.0, a.interval))

    def forward(local, remote):
        rc, out, err = adb_run(adb, ["-s", serial, "forward", f"tcp:{local}", f"tcp:{remote}"], 15)
        if rc != 0:
            raise Fail(f"Falha no adb forward tcp:{local}: {err or out}")

    def unforward(port):
        adb_run(adb, ["-s", serial, "forward", "--remove", f"tcp:{port}"], 10)

    def list_forwards():
        rc, out, err = adb_run(adb, ["-s", serial, "forward", "--list"], 10)
        return [f for f in parse_forward_list(out) if f["serial"] == serial] if rc == 0 else []

    def fetch_player():
        with http_open(f"http://127.0.0.1:{a.api_port}/tv3/mediaplayers/{a.player_id}", 5) as r:
            return json.loads(r.read(2_000_000).decode("utf-8", "replace"))

    def scan(exclude):
        listening = []
        for cmd, parser in ((["shell", "cat", "/proc/net/tcp"], parse_proc_net_tcp), (["shell", "netstat", "-tln"], parse_netstat_listen),
                            (["shell", "ss", "-tln"], parse_netstat_listen)):
            rc, out, err = adb_run(adb, ["-s", serial, *cmd], 15)
            if rc == 0 and out:
                listening = parser(out)
                if listening:
                    break
        seen, res = set(), []
        for ip, port in listening:
            if port in seen or port in exclude:
                continue
            seen.add(port)
            try:
                forward(port, port)
                made["scan"].add(port)
                probe = adb_probe_port(port)
                ok = True
            except Fail:
                probe, ok = {"http": None, "banner": None, "error": "forward falhou"}, False
            res.append({"port": port, "address": ip, "forwarded": ok, "probe": probe})
        return sorted(res, key=lambda r: r["port"])

    last_media = None
    try:
        if not ensure_connected(time.time() + (a.connect_timeout if a.watch else a.connect_timeout)):
            raise Fail(f"O aparelho {serial} não ficou disponível (estado: {state['device_state'] or 'desconhecido'}). {state['action']}".strip())
        say(f"Aparelho conectado: {serial}", "ok")
        step("connect", "done", serial)
        step("watch", "running")
        while True:
            state["cycle"] += 1
            state["changed"], state["error"] = False, ""
            st, err = device_state()
            state["device_state"] = st
            if st != "device":
                state["action"], state["error"] = "No aparelho: confira a conexão e a depuração.", err
                emit()
                if not a.watch:
                    raise Fail("O aparelho deixou de responder.")
                if not ensure_connected(time.time() + 3600):
                    raise Fail("O aparelho não voltou.")
                continue
            state["action"] = ""
            try:
                if not made["api"] or not any(f["local"] == f"tcp:{a.api_port}" for f in list_forwards()):
                    forward(a.api_port, a.api_port)
                    made["api"] = True
                data = fetch_player()
                state["api_ok"], state["player"] = True, data
            except Fail as e:
                state["api_ok"], state["error"] = False, f"API do player ({a.api_port}): {e}"
                log(f"ERRO ao consultar a API: {e}")
                emit()
                if not a.watch:
                    raise
                time.sleep(a.interval)
                continue
            media = data.get("currentMedia") or ""
            state["media"] = media
            if not media:
                state["local_url"], state["media_port"] = "", 0
                state["action"] = "No aparelho: coloque um canal para tocar (o player está sem mídia)."
            elif media != last_media:
                try:
                    port, url = media_local_url(media)
                    if made["media"] and made["media"] != port and made["media"] != a.api_port:
                        unforward(made["media"])
                    forward(port, port)
                    made["media"] = port
                    state.update(media_port=port, local_url=url, changed=last_media is not None)
                    last_media = media
                    say(f"Mídia disponível localmente em: {url}", "ok")
                except Fail as e:
                    state["error"] = str(e)
                    log(f"ERRO ao fazer forward da mídia: {e}")
            if state["local_url"]:
                try:
                    with http_open(state["local_url"], 4) as r:
                        head = r.read(400).decode("utf-8", "replace")
                    state["manifest"] = "mpd" if "<MPD" in head.upper() else f"http {r.status}"
                except Fail as e:
                    state["manifest"] = f"falhou: {e}"
            else:
                state["manifest"] = ""
            if a.scan_ports:
                try:
                    state["ports"] = scan({a.api_port, state["media_port"]})
                except Fail as e:
                    note(f"Varredura de portas: {e}")
            state["forwards"] = list_forwards()
            emit()
            if not a.watch:
                break
            time.sleep(a.interval)
    except KeyboardInterrupt:
        say("Acompanhamento encerrado.", "ok")
    finally:
        if not a.keep_forwards:
            for port in ([a.api_port] if made["api"] else []) + ([made["media"]] if made["media"] else []) + sorted(made["scan"]):
                unforward(port)
    step("watch", "done")
    rows = [["Aparelho", serial], ["Mídia no aparelho", state["media"] or "—"], ["Endereço local", state["local_url"] or "—"]]
    ev(type="result", out=state["local_url"], outputs=[], notes=NOTES, rows=rows, adb={k: state[k] for k in ("serial", "media", "local_url", "media_port")})
    return 0
# BUILD-REMOVE-END-ADB


def cmd_sdr(a):
    """Redo only the SDR image from a saved *_hdr.png (fast way to tune the look; no decoding)."""
    set_steps([("sdr", "Converter para SDR"), ("report", "Resumo")])
    set_stages({"sdr": 99, "report": 1})
    src = Path(a.input).resolve()
    if not src.is_file():
        raise Fail(f"Arquivo não encontrado: {src}")
    fmts = [x.strip() for x in a.sdr.split(",")]
    if any(x not in ("png", "jpg") for x in fmts):
        raise Fail("--sdr aceita png,jpg.")
    step("sdr", "running")
    rgb, hdr = read_png16_hdr(src)
    img8 = tonemap_sdr(rgb, hdr, a.tonemap, a.white_nits, float(a.peak_nits or hdr["maxcll"] or hdr["lmax"]), a.exposure, a.desat, a.saturation)
    base = Path(a.output) if a.output else src.with_name(re.sub(r"_hdr$", "", src.stem))
    made = []
    for f in fmts:
        p = unique_path(base.with_name(base.stem + f"_sdr.{f}"), a.overwrite)
        save_sdr(img8, p, f, a.jpg_quality)
        made.append({"kind": "frame-sdr", "title": f"Imagem SDR ({f.upper()}, BT.709/sRGB)", "path": str(p.resolve()), "size": p.stat().st_size})
    step("sdr", "done", f"{len(made)} arquivo(s)")
    step("report", "running")
    say(bold("Resultado:"))
    for m in made:
        say(f"  {m['title']}: {m['path']} ({human(m['size'])})")
    ev(type="result", out=made[0]["path"], outputs=made, notes=NOTES, rows=[["Tone mapping SDR", tone_args_summary(a)]])
    step("report", "done")
    progress("report", 1.0, force=True)
    return 0


# ──────────────────────────── CLI ────────────────────────────
_MP_HELP = {"--normalize": "normalize the volume of the rendered audio: off (default), ebu (-23 LUFS), atsc (-24), streaming (-16) or a LUFS value (-40..-5); "
                           "one constant gain, never above --true-peak",
            "--true-peak": "highest true peak after --normalize, in dBTP (default -1)",
            "--drc-effect": "decoder DRC effect type (-dse; default 0)",
            "--target-loudness": "decoder target loudness (-rl); empty = decoder default",
            "--sleep-mode": "how interactivity commands are timed (advanced; default relative)",
            "--uim-version": "UI manager protocol version (advanced; default 11.0)"}
HELP_TEXTS = {
    "*": {"--overwrite": "replace existing output files instead of using a free name",
          "-v": "show the external commands that are run", "--no-color": "plain output, no colours",
          "--ascii": "plain output, no Unicode symbols", "--keep-temp": "keep the intermediate files",
          "--outdir": "output folder (default: next to the input)",
          "--cache-dir": "folder of the MPEG-H analysis cache (default: <system temp>/dtv_audio_cache)"},
    "ts-tv3": {"--outputs": "single, dual or single,dual (default: both)", "--program": "program number (default: the first in the PAT)",
               "--vvc-pid": "force the VVC PID (decimal or hex such as 0x100)", "--lcevc-pid": "force the LCEVC PID (decimal or hex)",
               "--mpegh-pid": "force the MPEG-H PID (decimal or hex)",
               "--lcevc-size": "frame size written in the lvc1 track of the dual-track file (default: 3840x2160)",
               "--outdir": "output folder (default: the folder of the input)", "--name": "base name of the outputs (default: the input name)",
               "--dry-run": "only list the programs, PIDs and stream types; write nothing"},
    "mpegh-audio": {"--option": "preset:ID or group:ID, optionally =FORMAT[:BITRATE] and @LAYOUT (2.0 or 5.1; the track's own layout is the default), e.g. group:3=aac@2.0 (repeatable; default: all presets)",
                    "--jobs": "parallel decoders (0 = automatic)",
                    "--mux": "none (audio files only), add (files and a copy of the MP4 with the audio tracks) or only (just the MP4)",
                    "--audio-delay": "delay in ms for the tracks added by --mux (default 0)", **_MP_HELP},
    "transcode": {"--crf": "quality: x265 CRF (default 18), NVENC CQ (22), SVT-AV1 CRF (28)", "--container": "auto (MP4, or MKV when PCM/FLAC tracks need it), mp4 or mkv", **_MP_HELP},
    "frame": {"--jpg-quality": "JPEG quality, 1-95 (default 95)", "--avif-crf": "AVIF quality (libaom CRF; default 18)"},
    "sdr": {"-o": "base name of the output (default: the PNG name without _hdr)", "--jpg-quality": "JPEG quality, 1-95 (default 95)"},
}


def fill_missing_help(sub):
    """One place for the help of the options that are shared or self-explanatory."""
    for name, sp in sub.choices.items():
        table = {**HELP_TEXTS["*"], **HELP_TEXTS.get(name, {})}
        for act in sp._actions:
            if act.option_strings and act.help is None and act.dest != "help":
                txt = next((table[o] for o in act.option_strings if o in table), None)
                if txt:
                    act.help = txt


def build_parser():
    p = argparse.ArgumentParser(prog="dtv_tools.py", description="DTV+ (TV 3.0) tools",
                                epilog="Run 'dtv_tools.py doctor' to check the external programs.")
    p.add_argument("--version", action="version", version=f"dtv_tools {__version__}")
    sub = p.add_subparsers(dest="cmd")

    def tool_flags(sp, which=("mp4box", "ffmpeg")):
        if "mp4box" in which:
            sp.add_argument("--mp4box", "--mp4box-binary-path", default="", metavar="PATH", help="MP4Box executable")
        if "ffmpeg" in which:
            sp.add_argument("--ffmpeg", "--ffmpeg-binary-path", default="", metavar="PATH", help="ffmpeg executable (DTV+/LCEVC build for the enhancement check)")
        if "ffplay" in which:
            sp.add_argument("--ffplay", default="", metavar="PATH", help="ffplay executable (video preview; a build with the lcevc filter shows the enhancement)")
        if "mpegh" in which:
            sp.add_argument("--mpegh-ui", default="", metavar="PATH", help="mpeghUiManager executable")
            sp.add_argument("--mpegh-decoder", default="", metavar="PATH", help="mpeghDecoder executable")

    def machine_flags(sp):
        sp.add_argument("--json", action="store_true", help="protocol v2: stdout carries only JSON Lines events (see docs/PROTOCOL.md)")
        sp.add_argument("--control", action="store_true", help="read control commands on stdin (STOP / {\"cmd\":\"stop\"}); closing stdin also stops")
        sp.add_argument("--progress-json", action="store_true", help=argparse.SUPPRESS)   # legacy: '@@DTV {json}' lines mixed with text

    def common_flags(sp):
        tool_flags(sp)
        sp.add_argument("--ffprobe", default="", help=argparse.SUPPRESS)  # accepted for compatibility, not used
        sp.add_argument("--tmpdir", "--tmp-dir", default="", help="base folder for intermediates (a private sub-folder is created inside it)")
        sp.add_argument("--keep-temp", action="store_true", help="keep the intermediate files")
        machine_flags(sp)
        sp.add_argument("-v", "--verbose", action="store_true")
        sp.add_argument("--no-color", action="store_true")
        sp.add_argument("--ascii", action="store_true")

    s_ts = sub.add_parser("ts-tv3", help="process a TV 3.0 MPEG-TS capture into MP4")
    s_ts.add_argument("input")
    s_ts.add_argument("--outputs", default="single,dual")
    s_ts.add_argument("--program", type=int, default=None)
    s_ts.add_argument("--vvc-pid", default="")
    s_ts.add_argument("--lcevc-pid", default="")
    s_ts.add_argument("--mpegh-pid", default="")
    s_ts.add_argument("--fps", type=float, default=None, help="force a constant frame rate for the MP4Box input (default: measured)")
    s_ts.add_argument("--lcevc-size", default="3840x2160")
    s_ts.add_argument("--outdir", default="")
    s_ts.add_argument("--name", default="")
    s_ts.add_argument("--overwrite", action="store_true")
    s_ts.add_argument("--dry-run", action="store_true")
    common_flags(s_ts)

    s_scan = sub.add_parser("mpegh-scan", help="analyse the MPEG-H scene and audio catalog")
    s_scan.add_argument("input")
    s_scan.add_argument("--mpegh-ui", default="", metavar="PATH", help="mpeghUiManager executable")
    s_scan.add_argument("--mpegh-decoder", default="", help=argparse.SUPPRESS)  # accepted for compatibility, not used
    s_scan.add_argument("--native-layout", type=int, default=0, help="0=auto (detect from the stream), 2=2.0, 6=5.1, 16=5.1.4, 19=7.1.4")
    s_scan.add_argument("--compat-51", action="store_true", help="compatibility mode: whatever is CICP 16 (5.1.4) is rendered as 5.1")
    s_scan.add_argument("--cache-dir", default="")
    common_flags(s_scan)

    s_aud = sub.add_parser("mpegh-audio", help="extract and convert MPEG-H presets and groups")
    s_aud.add_argument("input")
    s_aud.add_argument("--option", action="append", default=[])
    s_aud.add_argument("--mpegh-ui", default="", metavar="PATH", help="mpeghUiManager executable")
    s_aud.add_argument("--mpegh-decoder", default="", metavar="PATH", help="mpeghDecoder executable")
    s_aud.add_argument("--native-layout", type=int, default=0, help="0=auto (detect from the stream), 2=2.0, 6=5.1, 16=5.1.4, 19=7.1.4")
    s_aud.add_argument("--compat-51", action="store_true", help="compatibility mode: whatever is CICP 16 (5.1.4) is rendered as 5.1")
    s_aud.add_argument("--jobs", type=int, default=0)
    s_aud.add_argument("--mux", default="none")
    s_aud.add_argument("--audio-delay", default="0")
    s_aud.add_argument("--normalize", default="off", metavar="off|ebu|atsc|streaming|LUFS",
                       help="normalize the volume of the generated audio files (EBU R128 measurement, one constant gain); default off")
    s_aud.add_argument("--true-peak", type=float, default=-1.0, metavar="DBTP", help="highest true peak after --normalize (default -1)")
    s_aud.add_argument("--drc-effect", default="0")
    s_aud.add_argument("--target-loudness", default="")
    s_aud.add_argument("--sleep-mode", default="relative")
    s_aud.add_argument("--uim-version", default="11.0")
    s_aud.add_argument("--outdir", default="")
    s_aud.add_argument("--cache-dir", default="")
    s_aud.add_argument("--overwrite", action="store_true")
    common_flags(s_aud)

    s_cut = sub.add_parser("mp4-cut", help="lossless cut of an MP4 at IDR frames (VVC + LCEVC + MPEG-H preserved)")
    s_cut.add_argument("input")
    s_cut.add_argument("-s", "--start", action="append", help="wanted start time (repeat -s/-e for several cuts)")
    s_cut.add_argument("-e", "--end", action="append", help="wanted end time")
    s_cut.add_argument("-o", "--output", default="", help="output file (several cuts get _01, _02... suffixes)")
    s_cut.add_argument("--snap", choices=("nearest", "prev", "next"), default="nearest", help="which IDR to use for the start (and the end unless --end-snap)")
    s_cut.add_argument("--end-snap", choices=("nearest", "prev", "next", "eof"), default="", help="which point to use for the end")
    s_cut.add_argument("--list-idr", action="store_true", help="only list the cut points (result.scan.idr)")
    s_cut.add_argument("--no-av-fix", "--sem-ajuste-av", dest="no_av_fix", action="store_true",
                       help="only measure the A/V sync after the cut, do not fix the output edit list")
    s_cut.add_argument("--overwrite", action="store_true")
    machine_flags(s_cut)
    s_cut.add_argument("-v", "--verbose", action="store_true")
    s_cut.add_argument("--no-color", action="store_true")
    s_cut.add_argument("--ascii", action="store_true")
    tool_flags(s_cut)

    s_dash = sub.add_parser("dash-capture", help="record a live/static DASH stream (VVC + LCEVC + MPEG-H) into MP4")
    s_dash.add_argument("mpd_url", metavar="URL", help="manifest (MPD) URL")
    s_dash.add_argument("--save-dir", default=".", help="folder of the final file (default: .)")
    s_dash.add_argument("--save-name", help="final file name, without extension")
    s_dash.add_argument("--thread-count", type=int, default=8, help="parallel downloads (default: 8)")
    s_dash.add_argument("--live-record-limit", type=parse_hms, metavar="HH:mm:ss", help="stop recording after this duration")
    s_dash.add_argument("--live-take-count", type=int, default=1, metavar="N", help="start from the last N segments of the window (default: 1)")
    s_dash.add_argument("--live-wait-time", type=float, default=0.4, metavar="SEC", help="manifest refresh interval in seconds (default: 0.4)")
    s_dash.add_argument("--cleanup-after", type=int, default=30, metavar="SEC", help="delete each .m4s SEC seconds after it was processed (default: 30)")
    s_dash.add_argument("--del-after-done", nargs="?", const=True, default=True, type=str2bool, metavar="BOOL",
                        help="delete the temporary files after the MP4 is made (default: true)")
    s_dash.add_argument("--log-level", type=str.upper, choices=list(DASH_LOG_LEVELS), default="INFO", help="OFF, ERROR, WARN, INFO or DEBUG")
    s_dash.add_argument("--header", action="append", metavar="'Name: value'", help="extra HTTP header (repeatable)")
    s_dash.add_argument("--user-agent", default="", help="HTTP User-Agent (default: dtv_tools/<version>)")
    s_dash.add_argument("--proxy", default="", help="HTTP(S) proxy URL")
    s_dash.add_argument("--subtitles", action="store_true",
                        help="also record the manifest's first subtitle/caption track (TTML/stpp) into the MP4 (default: subtitles are only listed)")
    s_dash.add_argument("--max-errors", type=int, default=150, metavar="N",
                        help="stop (and package) after N consecutive manifest failures; 0 = never (default: 150)")
    s_dash.add_argument("--overwrite", action="store_true")
    s_info = sub.add_parser("dash-info", help="read a DASH manifest and list its tracks, subtitles included (no media is downloaded)")
    s_info.add_argument("mpd_url", metavar="URL", help="manifest (MPD) URL")
    s_info.add_argument("--header", action="append", metavar="'Name: value'", help="extra HTTP header (repeatable)")
    s_info.add_argument("--user-agent", default="", help="HTTP User-Agent (default: dtv_tools/<version>)")
    s_info.add_argument("--proxy", default="", help="HTTP(S) proxy URL")
    s_info.add_argument("--timeout", type=float, default=10.0, metavar="SEC", help="manifest download timeout (default 10)")
    machine_flags(s_info)
    s_info.add_argument("-v", "--verbose", action="store_true")
    s_info.add_argument("--no-color", action="store_true")
    s_info.add_argument("--ascii", action="store_true")
    s_dash.add_argument("--tmpdir", "--tmp-dir", default="", help="base folder for temporary files (default: --save-dir)")
    s_dash.add_argument("--keep-temp", action="store_true")
    machine_flags(s_dash)
    s_dash.add_argument("-v", "--verbose", action="store_true")
    s_dash.add_argument("--no-color", action="store_true")
    s_dash.add_argument("--ascii", action="store_true")
    tool_flags(s_dash)

    def tone_flags(sp):
        sp.add_argument("--tonemap", choices=("mobius", "hable", "reinhard", "clip"), default="mobius", help="HDR -> SDR curve")
        sp.add_argument("--white-nits", type=float, default=203.0, help="HDR nits that become SDR white (higher = darker; default 203)")
        sp.add_argument("--peak-nits", type=float, default=0, help="HDR peak for the roll-off (default: MaxCLL, else mastering max)")
        sp.add_argument("--exposure", type=float, default=0.0, help="stops added before tone mapping (negative = darker)")
        sp.add_argument("--desat", type=float, default=0.0, help="highlight desaturation, 0 = off (default 0; ffmpeg tonemap uses 2)")
        sp.add_argument("--saturation", type=float, default=1.0, help="final saturation multiplier (e.g. 0.85 = less vivid)")
        sp.add_argument("--jpg-quality", type=int, default=95)

    def video_input_flags(sp):
        sp.add_argument("input", help="single-track MP4 made by ts-tv3 / mp4-cut (VVC + LCEVC SEI + MPEG-H)")
        sp.add_argument("--no-lcevc", action="store_true", help="source without LCEVC (plain HDR10 file): skip the lcevc filter")
        sp.add_argument("--size", default="", metavar="WxH", help="force the output size (default: the one measured by the LCEVC verification)")
        sp.add_argument("--no-size-fix", action="store_true", help="never force a size, even if the verification found one")
        sp.add_argument("--outdir", default="")
        sp.add_argument("--overwrite", action="store_true")

    s_tc = sub.add_parser("transcode", help="single-track MP4 -> HEVC or AV1 10-bit HDR10 with every MPEG-H preset as an audio track")
    video_input_flags(s_tc)
    s_tc.add_argument("-o", "--output", default="", help="output file (default: <name>_hevc_hdr10.mp4 / <name>_av1_hdr10.mp4, or .mkv)")
    s_tc.add_argument("--no-subtitles", action="store_true",
                      help="do not carry the source's subtitle track(s) over to the output (by default they are copied into MP4 outputs)")
    s_tc.add_argument("--encoder", choices=tuple(ENCODERS), default="x265",
                      help="video encoder: x265 (CPU, default), nvenc (HEVC, NVIDIA GPU), av1 (SVT-AV1, CPU) or av1-nvenc (AV1, NVIDIA RTX 40+)")
    s_tc.add_argument("--crf", type=float, default=None, help="quality: x265 CRF (default 18), NVENC CQ (22), SVT-AV1 CRF (28)")
    s_tc.add_argument("--preset", default="", help="x265: ultrafast..placebo; NVENC: p1..p7; SVT-AV1: 0..13 (default: x265 decides, NVENC p5, SVT-AV1 6)")
    s_tc.add_argument("--max-bitrate", type=float, default=0, metavar="MBPS",
                      help="cap the peak video bitrate, in Mbit/s, keeping the --crf quality target (0 = no cap; reference: HEVC 20, AV1 15). "
                           "All four encoders support it")
    s_tc.add_argument("--encoder-params", default="", metavar="PARAMS",
                      help="extra encoder parameters: x265 'k=v:k=v', SVT-AV1 'k=v:k=v', NVENC '-opt val ...' (appended as-is)")
    s_tc.add_argument("--nvenc-params", default="", metavar="'-opt val ...'", help="same as --encoder-params with an NVENC encoder (older name)")
    s_tc.add_argument("--x265-params", default="", metavar="K=V:K=V", help="same as --encoder-params with x265 (older name)")
    s_tc.add_argument("--verify-frames", type=int, default=48, help="frames decoded by the LCEVC verification (default: 48)")
    s_tc.add_argument("--limit", type=float, default=0, metavar="SEC", help="encode only the first SEC seconds (quick test; the audio is still rendered in full)")
    s_tc.add_argument("--dry-run", action="store_true", help="show the ffmpeg command and stop")
    s_tc.add_argument("--audio", choices=("render", "copy", "none"), default="render",
                      help="render = every preset as its own track (like mpegh-audio); copy = original track(s) only; none")
    s_tc.add_argument("--option", action="append", default=[], metavar="SPEC",
                      help="what to render, e.g. preset:1 or group:3=aac:192k@2.0 (@LAYOUT = 2.0 or 5.1; repeat or comma list; default: all presets, no groups)")
    s_tc.add_argument("--audio-format", choices=("auto", "aac", "eac3", "flac", "wav"), default="auto", help="auto = AAC for 2.0, E-AC-3 for 5.1, WAV above (per option)")
    s_tc.add_argument("--audio-delay", default="auto", metavar="auto|MS", help="rendered audio offset; auto = start of the original track")
    s_tc.add_argument("--normalize", default="off", metavar="off|ebu|atsc|streaming|LUFS",
                      help="normalize the volume of the rendered audio tracks (EBU R128 measurement, one constant gain); default off")
    s_tc.add_argument("--true-peak", type=float, default=-1.0, metavar="DBTP", help="highest true peak after --normalize (default -1)")
    s_tc.add_argument("--original-audio", choices=("keep", "drop"), default="keep", help="also keep the original MPEG-H track (last)")
    s_tc.add_argument("--container", choices=("auto", "mp4", "mkv"), default="auto")
    s_tc.add_argument("--native-layout", type=int, default=0, help="0=auto (detect from the stream), 2=2.0, 6=5.1, 16=5.1.4, 19=7.1.4")
    s_tc.add_argument("--compat-51", action="store_true", help="compatibility mode: whatever is CICP 16 (5.1.4) is rendered as 5.1")
    s_tc.add_argument("--jobs", type=int, default=0, help="parallel decoders (0 = automatic)")
    s_tc.add_argument("--drc-effect", default="0")
    s_tc.add_argument("--target-loudness", default="")
    s_tc.add_argument("--sleep-mode", default="relative")
    s_tc.add_argument("--uim-version", default="11.0")
    s_tc.add_argument("--cache-dir", default="")
    common_flags(s_tc)
    tool_flags(s_tc, ("mpegh",))

    s_fr = sub.add_parser("frame", help="save one frame as HDR-native and/or tone-mapped SDR image")
    video_input_flags(s_fr)
    g = s_fr.add_mutually_exclusive_group(required=True)
    g.add_argument("--time", help="HH:MM:SS.mmm, MM:SS or seconds (nearest frame)")
    g.add_argument("--frame", type=int, help="frame number in display order, starting at 1")
    s_fr.add_argument("-o", "--output", default="", help="base name (_hdr.png / _sdr.png ... are appended)")
    s_fr.add_argument("--hdr", default="png", help="HDR native: png (16-bit, cICP) and/or avif (10-bit); comma list or none")
    s_fr.add_argument("--sdr", default="png", help="SDR tone-mapped: png and/or jpg; comma list or none")
    s_fr.add_argument("--avif-crf", type=int, default=18)
    s_fr.add_argument("--avif-chroma", choices=("444", "420"), default="444",
                      help="AVIF chroma format: 444 keeps all the colour detail (AV1 High profile); 420 is the most compatible (Main profile)")
    s_fr.add_argument("--slow-seek", action="store_true", help="decode from the start of the file (slow, no IDR lookup)")
    tone_flags(s_fr)
    common_flags(s_fr)

    def preview_flags(sp):
        sp.add_argument("input", help="MP4 (VVC + LCEVC + MPEG-H)")
        sp.add_argument("--no-lcevc", action="store_true", help="source without LCEVC: skip the lcevc filter")
        sp.add_argument("--no-tonemap", action="store_true", help="do not convert HDR to SDR (colours look washed out)")
        sp.add_argument("--width", type=int, default=960, help="width of the picture (default 960)")

    s_pv = sub.add_parser("preview", help="extract still frames (scaled, SDR) at some instants, for the Cut MP4 preview")
    preview_flags(s_pv)
    s_pv.add_argument("--time", action="append", required=True, metavar="T", help="instant (HH:MM:SS.mmm, MM:SS or seconds); repeat for more frames")
    s_pv.add_argument("--outdir", default="", help="folder for the PNG files (default: a temporary one)")
    tool_flags(s_pv, ("ffmpeg",))
    machine_flags(s_pv)
    s_pv.add_argument("-v", "--verbose", action="store_true")
    s_pv.add_argument("--no-color", action="store_true")
    s_pv.add_argument("--ascii", action="store_true")

    s_pl = sub.add_parser("play", help="play a stretch of the video in an ffplay window (no audio)")
    preview_flags(s_pl)
    s_pl.add_argument("--start", default="", help="start (default: beginning)")
    s_pl.add_argument("--end", default="", help="end (default: end of the file)")
    s_pl.set_defaults(width=1280)
    tool_flags(s_pl, ("ffmpeg", "ffplay"))
    machine_flags(s_pl)
    s_pl.add_argument("-v", "--verbose", action="store_true")
    s_pl.add_argument("--no-color", action="store_true")
    s_pl.add_argument("--ascii", action="store_true")

    s_sdr = sub.add_parser("sdr", help="redo the SDR image from a saved *_hdr.png (no decoding)")
    s_sdr.add_argument("input", help="16-bit PNG made by 'frame'")
    s_sdr.add_argument("-o", "--output", default="")
    s_sdr.add_argument("--sdr", default="png", help="png and/or jpg")
    s_sdr.add_argument("--overwrite", action="store_true")
    machine_flags(s_sdr)
    s_sdr.add_argument("-v", "--verbose", action="store_true")
    s_sdr.add_argument("--no-color", action="store_true")
    s_sdr.add_argument("--ascii", action="store_true")
    tone_flags(s_sdr)

    s_doc = sub.add_parser("doctor", help="check the external programs and Python modules")
    machine_flags(s_doc)
    s_doc.add_argument("-v", "--verbose", action="store_true")
    s_doc.add_argument("--no-color", action="store_true")
    s_doc.add_argument("--ascii", action="store_true")
    tool_flags(s_doc, ("mp4box", "ffmpeg", "mpegh", "ffplay"))

    s_up = sub.add_parser("check-update", help="check on GitHub whether a newer release exists (never downloads or installs anything)")
    s_up.add_argument("--repo", default="", metavar="OWNER/NAME", help=f"GitHub repository (default {UPDATE_REPO}, or $DTV_UPDATE_REPO)")
    s_up.add_argument("--timeout", type=float, default=8.0, metavar="SEC", help="network timeout (default 8)")
    s_up.add_argument("--api-url", default="", help=argparse.SUPPRESS)           # tests / mirrors: URL that returns the release JSON
    machine_flags(s_up)
    s_up.add_argument("-v", "--verbose", action="store_true")
    s_up.add_argument("--no-color", action="store_true")
    s_up.add_argument("--ascii", action="store_true")

    s_ft = sub.add_parser("fetch-tool", help="download an external program (ffmpeg, MP4Box...), check its SHA-256 and unpack it; releases do not bundle them")
    s_ft.add_argument("tool", choices=list(TOOL_DEFS), help="program to fetch")
    s_ft.add_argument("--url", default="", help="address of the .zip/.tar (default: the known source for ffmpeg/ffplay on this system)")
    s_ft.add_argument("--sha256", default="", metavar="HEX", help="expected SHA-256 of the file")
    s_ft.add_argument("--sha256-url", default="", metavar="URL", help="checksum file ('<hash>  <name>' lines) to read the expected SHA-256 from")
    s_ft.add_argument("--allow-unverified", action="store_true", help="accept a file with no SHA-256 to check against (not recommended)")
    s_ft.add_argument("--accept-license", action="store_true", help="confirm that you read the license notice (required)")
    s_ft.add_argument("--dest", default="", metavar="DIR", help="folder where the program is installed under <DIR>/<tool> (default: the tools folder)")
    s_ft.add_argument("--max-size", type=float, default=1500.0, metavar="MB", help="refuse downloads larger than this (default 1500)")
    s_ft.add_argument("--timeout", type=float, default=30.0, metavar="SEC", help="network timeout (default 30)")
    machine_flags(s_ft)
    s_ft.add_argument("-v", "--verbose", action="store_true")
    s_ft.add_argument("--no-color", action="store_true")
    s_ft.add_argument("--ascii", action="store_true")

    # BUILD-REMOVE-START-ADB
    s_adb = sub.add_parser("adb-access", help="connect to a TV 3.0 receiver by adb, forward its media port and report the stream address (source only)")
    s_adb.add_argument("device", help="IP (or IP:port) of the device, or the adb serial of a USB one")
    s_adb.add_argument("--adb", default="", metavar="PATH", help="adb executable (default: PATH, Android SDK or ./platform-tools)")
    s_adb.add_argument("--api-port", type=int, default=ADB_API_PORT, help=f"port of the media-player API on the device (default {ADB_API_PORT})")
    s_adb.add_argument("--player-id", default="1", help="media player id (default 1)")
    s_adb.add_argument("--watch", action="store_true", help="keep checking and redo the forward whenever currentMedia changes")
    s_adb.add_argument("--interval", type=float, default=5.0, metavar="SEC", help="seconds between checks with --watch (default 5)")
    s_adb.add_argument("--connect-timeout", type=float, default=30.0, metavar="SEC", help="how long to wait for the device (default 30)")
    s_adb.add_argument("--scan-ports", action="store_true",
                       help="also list the other TCP ports listening on the device, forward them and probe each with GET / (may have side effects on unknown services)")
    s_adb.add_argument("--keep-forwards", action="store_true", help="do not remove the forwards when finishing")
    machine_flags(s_adb)
    s_adb.add_argument("-v", "--verbose", action="store_true")
    s_adb.add_argument("--no-color", action="store_true")
    s_adb.add_argument("--ascii", action="store_true")
    # BUILD-REMOVE-END-ADB

    s_desc = sub.add_parser("describe", help="print the capabilities of this script as JSON (commands, options, tools, formats, layouts)")
    s_desc.add_argument("--no-color", action="store_true", help=argparse.SUPPRESS)
    s_desc.add_argument("--ascii", action="store_true", help=argparse.SUPPRESS)
    s_desc.add_argument("-v", "--verbose", action="store_true", help=argparse.SUPPRESS)

    fill_missing_help(sub)
    return p


def _term_to_interrupt(signum, frame):
    raise KeyboardInterrupt


def _fix_stdio():
    """A frozen Windows executable without a console may have a None/null sys.stdout or sys.stdin even with inherited pipes."""
    def broken(f):
        return f is None or not hasattr(f, "fileno") or type(f).__name__ == "NullWriter"
    try:
        if broken(sys.stdout):
            sys.stdout = io.TextIOWrapper(os.fdopen(1, "wb", buffering=0, closefd=False), encoding="utf-8", errors="replace", line_buffering=True)
        if broken(sys.stderr):
            sys.stderr = io.TextIOWrapper(os.fdopen(2, "wb", buffering=0, closefd=False), encoding="utf-8", errors="replace", line_buffering=True)
        if broken(sys.stdin):
            sys.stdin = io.TextIOWrapper(os.fdopen(0, "rb", closefd=False), encoding="utf-8", errors="replace")
    except OSError:
        pass


def _utf8_stdio():
    for f in (sys.stdout, sys.stderr):
        try:
            f.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def _frozen_env():
    """PyInstaller's Linux bootloader rewrites LD_LIBRARY_PATH; MP4Box/ffmpeg must not inherit that."""
    if getattr(sys, "frozen", False) and sys.platform.startswith("linux"):
        orig = os.environ.get("LD_LIBRARY_PATH_ORIG")
        if orig is not None:
            os.environ["LD_LIBRARY_PATH"] = orig
        else:
            os.environ.pop("LD_LIBRARY_PATH", None)


def _control_command(line):
    """'STOP' or {"cmd":"stop"} -> "STOP"; anything else -> ""."""
    t = line.strip()
    if not t:
        return ""
    if t.startswith("{"):
        try:
            return str(json.loads(t).get("cmd", "")).upper()
        except ValueError:
            return ""
    return t.upper()


def _control_poll(peek, read, sleep=time.sleep, interval=0.1):
    """Windows: never block in ReadFile on the stdin pipe. A pending synchronous read keeps that handle locked, and anything else
    that touches it in the meantime (importing numpy, starting a child process...) hangs. So: ask how many bytes are waiting
    (PeekNamedPipe) and read only those. peek() -> bytes available, raises EOFError when the other end closed; read(n) -> bytes."""
    buf = b""
    while True:
        try:
            n = peek()
        except EOFError:
            return
        if n:
            buf += read(n)
            *lines, buf = buf.split(b"\n")
            if any(_control_command(ln.decode("utf-8", "replace")) == "STOP" for ln in lines):
                return
        else:
            sleep(interval)


def start_control_reader():
    """--control: a controlling program talks to us on stdin. 'STOP' or {"cmd":"stop"} = Ctrl+C (clean stop);
    end of stdin = the controller died = the same."""
    import _thread
    main_id = threading.main_thread().ident

    def interrupt():
        if hasattr(signal, "pthread_kill"):     # POSIX: a real SIGINT to the main thread also wakes sleep()/waitpid()
            signal.pthread_kill(main_id, signal.SIGINT)
        else:
            _thread.interrupt_main()

    def watch_posix():
        try:
            for line in sys.stdin:
                if _control_command(line) == "STOP":
                    break
        except Exception:
            pass
        interrupt()

    def watch_windows():
        import msvcrt
        import _winapi
        try:
            fd = sys.stdin.fileno()
            handle = msvcrt.get_osfhandle(fd)
        except (OSError, ValueError, AttributeError):
            return

        def peek():
            try:
                return _winapi.PeekNamedPipe(handle, 0)[0]
            except OSError as e:
                if getattr(e, "winerror", 0) == 109:        # ERROR_BROKEN_PIPE: the controller closed its end
                    raise EOFError from e
                raise
        try:
            peek()
        except (OSError, EOFError):
            return          # not a pipe (a console, a file): nobody is controlling us through it
        try:
            _control_poll(peek, lambda n: os.read(fd, n))
        except OSError:
            pass
        interrupt()

    threading.Thread(target=watch_windows if os.name == "nt" else watch_posix, daemon=True, name="dtv-control").start()


def describe():
    """Everything a front end would otherwise have to hard-code."""
    parser = build_parser()
    sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    helps = {c.dest: c.help for c in sub._choices_actions}
    commands = {}
    for name, sp in sub.choices.items():
        opts = []
        for act in sp._actions:
            if act.dest == "help" or act.help == argparse.SUPPRESS:
                continue
            kind = ("flag" if isinstance(act, (argparse._StoreTrueAction, argparse._StoreFalseAction))
                    else "append" if isinstance(act, argparse._AppendAction) else "value")
            opts.append({"flags": list(act.option_strings), "dest": act.dest, "kind": kind, "positional": not act.option_strings,
                         "help": act.help or "", "choices": list(act.choices) if act.choices else None,
                         "default": act.default if isinstance(act.default, (str, int, float, bool, type(None))) else None})
        commands[name] = {"help": helps.get(name, ""), "options": opts}
    return {"version": __version__, "protocol": PROTOCOL_VERSION, "features": list(FEATURES),
            "tools": {k: {"label": d["label"], "names": list(d["names"]), "flag": d["flag"], "optional": bool(d.get("optional")), "source": tool_source_info(k)} for k, d in TOOL_DEFS.items()},
            "formats": {k: {"label": f["label"], "ext": f["ext"], "default_bitrate": f["br"], "max_channels": f["max"]} for k, f in FORMATS.items()},
            "layouts": catalog_public(), "encoders": ENCODERS, "commands": commands}


# ──────────────────────────── update check ────────────────────────────
UPDATE_REPO = "limaalef/pyDTVTools"          # GitHub "owner/name" whose latest Release is compared with __version__


def parse_version(v):
    """'v1.2.10-rc1' -> ((1, 2, 10), 0) ; final releases sort after their pre-releases. None when unparseable."""
    m = re.match(r"^\s*[vV]?(\d+(?:\.\d+){0,3})(?:[-+._ ]?([A-Za-z].*))?\s*$", str(v or ""))
    if not m:
        return None
    nums = tuple(int(x) for x in m.group(1).split("."))
    nums = (nums + (0, 0, 0))[:max(3, len(nums))]
    return nums, (0 if m.group(2) else 1)


def version_newer(latest, current) -> bool:
    a, b = parse_version(latest), parse_version(current)
    if a is None or b is None:
        return False
    n = max(len(a[0]), len(b[0]))
    pad = lambda t: t[0] + (0,) * (n - len(t[0]))      # noqa: E731
    return (pad(a), a[1]) > (pad(b), b[1])


def pick_asset(assets, platform=None):
    """The release file that looks made for this OS (by name), or None."""
    plat = platform or sys.platform
    keys = ("win", ".exe", ".msi") if plat.startswith("win") else ("mac", "darwin", "osx", ".dmg") if plat == "darwin" else ("linux", ".appimage", ".deb")
    other = ("linux", "mac", "darwin", "osx", ".dmg") if plat.startswith("win") else ("win", ".exe", "linux", ".appimage", ".deb") if plat == "darwin" else ("win", ".exe", "mac", "darwin", "osx", ".dmg")
    for a_ in assets:
        n = a_.get("name", "").lower()
        if n.endswith((".sha256", ".sha256sum", ".sig", ".asc", ".txt", ".md")):
            continue
        if any(k in n for k in keys) and not any(k in n for k in other):
            return a_
    return None


def fetch_latest_release(repo, api_url=None, timeout=8.0):
    import urllib.error
    import urllib.request
    url = api_url or f"https://api.github.com/repos/{repo}/releases/latest"
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": f"dtv_tools/{__version__}",
                                               "X-GitHub-Api-Version": "2022-11-28"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise Fail(f"Nenhuma versão publicada em github.com/{repo} (ou o repositório é privado/não existe).")
        if e.code in (403, 429):
            raise Fail("O GitHub limitou as consultas (muitas verificações seguidas). Tente de novo mais tarde.")
        raise Fail(f"O GitHub respondeu HTTP {e.code}.")
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        raise Fail(f"Não foi possível consultar o GitHub (sem internet?): {getattr(e, 'reason', e)}")
    except ValueError:
        raise Fail("Resposta inválida do GitHub.")


def cmd_check_update(a):
    set_steps([("check", "Consultar a última versão publicada")])
    set_stages({"check": 100})
    step("check", "running")
    repo = (a.repo or os.environ.get("DTV_UPDATE_REPO") or UPDATE_REPO).strip().strip("/")
    if not api_url_ok(a.api_url) and not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise Fail(f"Repositório inválido: '{repo}' (use dono/nome).")
    progress("check", 0.2, f"github.com/{repo}", force=True)
    rel = fetch_latest_release(repo, a.api_url or os.environ.get("DTV_UPDATE_URL"), timeout=a.timeout)
    tag = str(rel.get("tag_name") or rel.get("name") or "")
    latest = tag.lstrip("vV")
    if parse_version(tag) is None:
        raise Fail(f"A versão publicada ('{tag}') não tem um número reconhecível.")
    assets = [{"name": x.get("name", ""), "size": int(x.get("size") or 0), "url": x.get("browser_download_url", "")}
              for x in rel.get("assets", []) if x.get("browser_download_url")]
    newer = version_newer(tag, __version__) and not rel.get("draft")
    info = {"repo": repo, "current": __version__, "latest": latest, "update_available": bool(newer), "url": rel.get("html_url", ""),
            "published": rel.get("published_at", ""), "prerelease": bool(rel.get("prerelease")),
            "notes": str(rel.get("body") or "")[:4000], "assets": assets, "suggested": pick_asset(assets)}
    if newer:
        say(f"Nova versão disponível: {latest} (instalada: {__version__})  {info['url']}", "ok")
    else:
        say(f"Você está na versão mais recente ({__version__}).", "ok")
    progress("check", 1.0, force=True)
    step("check", "done", f"{latest} disponível" if newer else "atualizado")
    ev(type="result", out="", outputs=[], notes=NOTES, rows=[["Instalada", __version__], ["Última publicada", latest]], update=info)
    return 0


def api_url_ok(u):
    return bool(u or os.environ.get("DTV_UPDATE_URL"))


def main(argv=None):
    global PJ, JSONL, VERBOSE, CMD
    _fix_stdio()
    _frozen_env()
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.cmd:
        parser.print_help()
        return 0
    if args.cmd == "describe":
        _utf8_stdio()
        print(json.dumps(describe(), ensure_ascii=False, indent=1))
        return 0
    CMD = args.cmd
    JSONL = bool(getattr(args, "json", False))
    PJ = JSONL or bool(getattr(args, "progress_json", False))
    if PJ:
        _utf8_stdio()
    VERBOSE = bool(getattr(args, "verbose", False))
    init_console(getattr(args, "no_color", False), getattr(args, "ascii", False))
    NOTES.clear()
    install_exit_hooks()
    if JSONL:
        ev(type="hello", protocol=PROTOCOL_VERSION, version=__version__, command=args.cmd, pid=os.getpid(), features=list(FEATURES))
    if getattr(args, "control", False):
        start_control_reader()
    if args.cmd == "dash-capture":   # stop request (SIGTERM / Ctrl+Break) = stop recording and package, like Ctrl+C
        for name in ("SIGTERM", "SIGBREAK"):
            if hasattr(signal, name):
                try:
                    signal.signal(getattr(signal, name), _term_to_interrupt)
                except (ValueError, OSError):
                    pass
    global DASH_LOG, ORIG_LOOK
    DASH_LOG, ORIG_LOOK = None, False
    handlers = {"ts-tv3": cmd_ts_tv3, "mpegh-scan": cmd_mpegh_scan, "mpegh-audio": cmd_mpegh_audio,
                "mp4-cut": cmd_mp4_cut, "dash-capture": cmd_dash_capture, "doctor": cmd_doctor, "check-update": cmd_check_update,
                "transcode": cmd_transcode, "frame": cmd_frame, "sdr": cmd_sdr,
                "preview": cmd_preview, "play": cmd_play, "fetch-tool": cmd_fetch_tool, "dash-info": cmd_dash_info}
    # BUILD-REMOVE-START-ADB
    handlers["adb-access"] = cmd_adb_access
    # BUILD-REMOVE-END-ADB
    try:
        return handlers[args.cmd](args) or 0
    except Fail as e:
        ev(type="error", kind="fail", message=str(e), step=CUR_STEP)
        if CUR_STEP:
            step(CUR_STEP, "error", str(e))
        say(f"ERRO: {e}", "err")
        return 2
    except KeyboardInterrupt:
        ev(type="error", kind="interrupted", message="Interrompido pelo usuário.", step=CUR_STEP)
        say("Interrompido.", "err")
        return 130
    except Exception as e:
        ev(type="error", kind="unexpected", message=str(e), step=CUR_STEP)
        say(f"ERRO inesperado: {e}", "err")
        if VERBOSE:
            import traceback
            traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
