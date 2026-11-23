import json
import subprocess

import pytest

import dtv_tools as d
from conftest import need_ffmpeg, need_mp4box, run_cli


def _pts(path, stream="v:0"):
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", stream, "-show_entries", "packet=pts_time",
                        "-of", "csv=p=0", str(path)], capture_output=True, text=True)
    return sorted(float(x.strip(",")) for x in r.stdout.split() if x.strip(","))


@need_mp4box
def test_retime_from_real_pts_after_mp4box_constant_fps(tmp_path):
    """MP4Box makes a constant-fps file from a raw stream; retime must apply a gap + offset taken from the source clock."""
    raw = tmp_path / "v.h264"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=duration=4:size=320x240:rate=25",
                    "-c:v", "libx264", "-bf", "0", "-g", "25", "-pix_fmt", "yuv420p", "-f", "h264", str(raw)], check=True)
    out = tmp_path / "o.mp4"
    subprocess.run(["MP4Box", "-add", f"{raw}:fps=25", "-new", str(out)], check=True, capture_output=True)
    n = len(d.mp4_tables(str(out))[0]["sizes"])
    assert n == 100
    dts = [100.0 + i / 25 + (0.5 if i >= 50 else 0) for i in range(n)]       # a half-second hole after frame 50
    res = d.retime_mp4_tracks(str(out), {1: (dts, list(dts))}, dts[0])
    assert res == {1: "ok"}
    p = _pts(out)
    assert p[0] == pytest.approx(0.0, abs=1e-6)
    assert p[50] == pytest.approx(2.5, abs=1e-3)                               # hole preserved
    assert subprocess.run(["ffmpeg", "-v", "error", "-i", str(out), "-f", "null", "-"], capture_output=True).returncode == 0


@need_mp4box
def test_retime_refuses_sample_count_mismatch(tmp_path):
    raw = tmp_path / "v.h264"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=duration=2:size=160x120:rate=25",
                    "-c:v", "libx264", "-g", "25", "-f", "h264", str(raw)], check=True)
    out = tmp_path / "o.mp4"
    subprocess.run(["MP4Box", "-add", f"{raw}:fps=25", "-new", str(out)], check=True, capture_output=True)
    before = out.read_bytes()
    res = d.retime_mp4_tracks(str(out), {1: ([0.0, 0.04], [0.0, 0.04])}, 0.0)
    assert "differs" in res[1]
    assert out.read_bytes() == before                                           # untouched


@need_mp4box
def test_cut_timeline_and_list_idr(sample_mp4):
    idr, total, n, ts, notes = d.cut_timeline(str(sample_mp4))
    assert n == 500 and total == pytest.approx(20.0)
    assert [round(x[0], 2) for x in idr[:3]] == [0.0, 2.0, 4.0]
    assert {x[2] for x in idr} == {"IDR"}
    rc, out, ev = run_cli("mp4-cut", sample_mp4, "--list-idr", "--progress-json")
    res = [e for e in ev if e["type"] == "result"][0]
    assert rc == 0 and len(res["scan"]["idr"]) == 10 and res["scan"]["duration"] == pytest.approx(20.0)


@need_mp4box
def test_cut_lossless_snap_and_av_fix(sample_mp4, tmp_path):
    out = tmp_path / "cut.mp4"
    rc, text, ev = run_cli("mp4-cut", sample_mp4, "-s", "7.3", "-e", "15.2", "-o", out, "--progress-json")
    assert rc == 0, text
    res = [e for e in ev if e["type"] == "result"][0]
    c = res["outputs"][0]
    assert (c["kind"], c["start"], c["end"]) == ("cut", 8.0, 16.0) and c["path"] == str(out)
    assert "FIXED" in text or "in sync" in text                                # the 0.5 s audio delay is kept in sync
    assert "cut starts exactly at the chosen IDR" in text
    v = _pts(out)
    assert v[0] == pytest.approx(0.0, abs=0.1) and v[-1] == pytest.approx(7.96, abs=0.1)
    # --snap
    out2 = tmp_path / "cut2.mp4"
    rc, text, ev = run_cli("mp4-cut", sample_mp4, "-s", "7.3", "-e", "15.2", "-o", out2, "--snap", "prev", "--end-snap", "next", "--progress-json")
    c = [e for e in ev if e["type"] == "result"][0]["outputs"][0]
    assert (c["start"], c["end"]) == (6.0, 16.0)


@need_mp4box
def test_cut_never_overwrites_without_flag(sample_mp4, tmp_path):
    out = tmp_path / "x.mp4"
    run_cli("mp4-cut", sample_mp4, "-s", "2", "-e", "6", "-o", out)
    first = out.read_bytes()
    rc, text, ev = run_cli("mp4-cut", sample_mp4, "-s", "2", "-e", "8", "-o", out)
    assert rc == 0 and out.read_bytes() == first and (tmp_path / "x (1).mp4").exists()
    rc, text, ev = run_cli("mp4-cut", sample_mp4, "-s", "2", "-e", "8", "-o", out, "--overwrite")
    assert out.read_bytes() != first
    rc, text, ev = run_cli("mp4-cut", sample_mp4, "-s", "2", "-e", "8", "-o", sample_mp4, "--overwrite")
    assert rc == 2 and "overwrite the input" in text


@need_mp4box
def test_cut_validations_and_odd_names(sample_mp4, tmp_path):
    rc, text, ev = run_cli("mp4-cut", sample_mp4, "-s", "30", "-e", "40", "--progress-json")
    assert rc == 2 and any(e["type"] == "error" for e in ev)
    rc, text, ev = run_cli("mp4-cut", sample_mp4, "-s", "1", "--progress-json")
    assert rc == 2 and "pairs" in next(e for e in ev if e["type"] == "error")["message"]
    odd = tmp_path / "a#b:c.mp4"
    odd.write_bytes(sample_mp4.read_bytes())
    rc, text, ev = run_cli("mp4-cut", odd, "-s", "2", "-e", "6", "--progress-json")
    assert rc == 0, text
    assert [e for e in ev if e["type"] == "result"][0]["outputs"][0]["size"] > 0
