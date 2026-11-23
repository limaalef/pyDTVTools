"""Protocol v2 (docs/PROTOCOL.md): --json, begin, progress stage/frac, --control, describe, legacy mode, GUI contract."""
import json
import re
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "dtv_tools.py"


def run_json(*args, timeout=120):
    p = subprocess.run([sys.executable, str(SCRIPT), *args, "--json"], capture_output=True, text=True, timeout=timeout)
    events = [json.loads(line) for line in p.stdout.splitlines()]      # every stdout line must be JSON
    return p, events


def test_doctor_stream_is_pure_jsonl():
    p, ev = run_json("doctor")
    assert ev[0]["type"] == "hello" and ev[0]["protocol"] == 2
    assert ev[1]["type"] == "begin" and ev[1]["steps"][0] == {"id": "check", "label": "Check the external programs", "weight": 100}
    res = ev[-1]
    assert res["type"] == "result" and res["command"] == "doctor" and res["protocol"] == 2 and "features" in res
    assert all("type" in e for e in ev)


def test_error_event_has_kind_and_exit_code(tmp_path):
    p, ev = run_json("frame", str(tmp_path / "missing.mp4"), "--time", "1")
    assert p.returncode == 2
    err = [e for e in ev if e["type"] == "error"]
    assert err and err[-1]["kind"] == "fail" and err[-1]["message"]


def test_legacy_progress_json_still_works():
    p = subprocess.run([sys.executable, str(SCRIPT), "doctor", "--progress-json"], capture_output=True, text=True, timeout=120)
    lines = [ln for ln in p.stdout.splitlines() if ln.startswith("@@DTV ")]
    assert lines and json.loads(lines[-1][6:])["type"] == "result"


def test_progress_carries_stage_and_fraction(capsys):
    import dtv_tools as d
    old = (d.PJ, d.JSONL, d.CMD)
    d.PJ = d.JSONL = True
    try:
        d.STEP_DEFS[:] = [("a", "A"), ("b", "B")]
        d.set_stages({"a": 30, "b": 70})
        d.progress("b", 0.5, "half", force=True)
    finally:
        d.PJ, d.JSONL, d.CMD = old
    ev = [json.loads(x) for x in capsys.readouterr().out.splitlines()]
    assert ev[0]["type"] == "begin" and [s["weight"] for s in ev[0]["steps"]] == [30, 70]
    assert ev[1] == {"type": "progress", "stage": "b", "frac": 0.5, "pct": 65.0, "detail": "half"}


def test_json_mode_keeps_text_out_of_stdout(capsys):
    import dtv_tools as d
    old = (d.PJ, d.JSONL)
    d.PJ = d.JSONL = True
    try:
        d.say("hello", "warn")
        d.log("dbg")
        d._out("plain\x1b[31m")
    finally:
        d.PJ, d.JSONL = old
    ev = [json.loads(x) for x in capsys.readouterr().out.splitlines()]
    assert [(e["level"], e["msg"]) for e in ev] == [("warn", "hello"), ("debug", "dbg"), ("info", "plain")]


CONTROL_SNIPPET = textwrap.dedent("""
    import sys, time
    sys.path.insert(0, %r)
    import dtv_tools as d
    d.start_control_reader()
    print("ready", flush=True)
    try:
        time.sleep(30)
        print("not stopped")
    except KeyboardInterrupt:
        print("stopped")
""")


@pytest.mark.parametrize("how", ["json", "plain", "eof"])
def test_control_channel_stops(how):
    p = subprocess.Popen([sys.executable, "-c", CONTROL_SNIPPET % str(ROOT)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    assert p.stdout.readline().strip() == "ready"
    t0 = time.time()
    if how == "eof":
        p.stdin.close()
        p.stdin = None            # communicate() would try to flush the closed pipe (ValueError on Python 3.12)
    else:
        p.stdin.write('{"cmd":"stop"}\n' if how == "json" else "STOP\n")
        p.stdin.flush()
    out, _ = p.communicate(timeout=10)
    assert "stopped" in out and time.time() - t0 < 8


def test_describe_is_complete():
    p = subprocess.run([sys.executable, str(SCRIPT), "describe"], capture_output=True, text=True, timeout=60)
    d = json.loads(p.stdout)
    assert d["protocol"] == 2 and "describe" in d["features"]
    assert set(d["tools"]) == {"mp4box", "ffmpeg", "mpegh_ui", "mpegh_decoder"}
    assert {"aac", "eac3", "flac", "wav"} <= set(d["formats"]) and "16" in d["layouts"]
    assert {"ts-tv3", "mpegh-scan", "mpegh-audio", "mp4-cut", "dash-capture", "transcode", "frame", "sdr", "doctor"} <= set(d["commands"])
    for name, c in d["commands"].items():
        if name != "describe":
            flags = {f for o in c["options"] for f in o["flags"]}
            assert "--json" in flags and "--control" in flags, name


def test_gui_only_uses_flags_and_commands_the_script_has():
    """Contract: every --flag and subcommand written in the GUI exists in `describe`."""
    d = json.loads(subprocess.run([sys.executable, str(SCRIPT), "describe"], capture_output=True, text=True, timeout=60).stdout)
    known = {f for c in d["commands"].values() for o in c["options"] for f in o["flags"]}
    src = (ROOT / "gui" / "dtv_gui.py").read_text(encoding="utf-8")
    used = set(re.findall(r'"(--[a-z0-9][a-z0-9-]*)"', src)) - {"--cli", "--selftest"}
    assert used, "no flags found: the regex is stale"
    assert used <= known, sorted(used - known)
    kinds = set(re.findall(r'kind="([a-z0-9-]+)"', src)) - {"ts-inspect", "mp4-list", "x"}
    assert kinds <= set(d["commands"]), sorted(kinds - set(d["commands"]))


def test_gui_does_not_import_the_script():
    for f in (ROOT / "gui").glob("*.py"):
        assert not re.search(r"^\s*(import dtv_tools|from dtv_tools)", f.read_text(encoding="utf-8"), re.M) or f.name == "dtv_gui.py"
    src = (ROOT / "gui" / "dtv_gui.py").read_text(encoding="utf-8")
    assert src.count("import dtv_tools") == 1      # only the frozen `--cli` entry point


def test_cutmath_matches_script():
    sys.path.insert(0, str(ROOT / "gui"))
    import cutmath
    import dtv_tools as d
    for t in ("10", "1:30", "01:02:03.5", "0,25", "59:59.999"):
        assert cutmath.parse_time(t) == pytest.approx(d.parse_time_input(t))
    for bad in ("", "a", "1:2:3:4", "-1"):
        with pytest.raises(ValueError):
            cutmath.parse_time(bad)
        with pytest.raises(d.Fail):
            d.parse_time_input(bad)
    idr = [(0.0, 1, "IDR"), (2.0, 61, "IDR"), (4.1, 123, "IDR"), (6.0, 181, "IDR")]
    for target in (0, 1.0, 2.0, 3.0, 5.9, 9.0):
        assert cutmath.idr_candidates(idr, target) == d.idr_candidates(idr, target)


def test_every_stage_reports_progress():
    """doctor (and any command without fine-grained progress) still moves its bars: edges + per-item progress."""
    p, ev = run_json("doctor")
    prog = [e for e in ev if e["type"] == "progress"]
    assert len(prog) >= 5 and prog[0]["frac"] == 0.0 and prog[-1]["pct"] == 100.0
    assert [e["pct"] for e in prog] == sorted(e["pct"] for e in prog)


def test_frame_end_to_end_json(tmp_path):
    import shutil
    pytest.importorskip("numpy")
    pytest.importorskip("PIL")
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not installed")
    src = tmp_path / "a.mp4"
    r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=25:duration=2",
                        "-pix_fmt", "yuv420p10le", "-c:v", "libx265", "-x265-params", "log-level=none", str(src)], capture_output=True)
    if r.returncode:
        pytest.skip("ffmpeg without libx265")
    p, ev = run_json("frame", str(src), "--time", "1", "--no-lcevc", "--outdir", str(tmp_path / "o"))
    assert p.returncode == 0, p.stderr
    res = ev[-3:] and [e for e in ev if e["type"] == "result"][-1]
    assert {o["kind"] for o in res["outputs"]} == {"frame-hdr", "frame-sdr"}
    assert all(Path(o["path"]).is_file() for o in res["outputs"])
    pcts = [e["pct"] for e in ev if e["type"] == "progress"]
    assert pcts == sorted(pcts) and pcts[-1] == 100.0


def test_windows_control_poll_never_blocks_and_stops():
    """The Windows reader polls (PeekNamedPipe) instead of a blocking read; simulate the pipe."""
    import dtv_tools as d
    chunks = [b"", b"", b'{"cmd":"noop"}\nST', b"OP\n"]
    avail = []
    state = {"i": 0}

    def peek():
        i = state["i"]
        return len(chunks[i]) if i < len(chunks) else 1 / 0

    def read(n):
        c = chunks[state["i"]]
        state["i"] += 1
        return c[:n]
    slept = []

    # peek must return 0 for empty chunks without consuming them: advance on sleep
    def sleep(_):
        slept.append(1)
        state["i"] += 1
    d._control_poll(peek, read, sleep=sleep, interval=0)
    assert len(slept) == 2 and state["i"] == 4


def test_windows_control_poll_eof():
    import dtv_tools as d

    def peek():
        raise EOFError
    d._control_poll(peek, lambda n: b"", sleep=lambda _: None)      # returns instead of looping forever


def test_gui_bundled_tools_dir_is_project_tools():
    sys.path.insert(0, str(ROOT / "gui"))
    src = (ROOT / "gui" / "dtv_gui.py").read_text(encoding="utf-8")
    assert 'Path(__file__).resolve().parent.parent / "tools"' in src
