"""DASH subtitles: reading the manifest, aligning the segments to the video, packaging with MP4Box, and keeping/removing them in cut and transcode."""
import functools
import http.server
import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import pytest

import dtv_tools as d
from conftest import ROOT

SCRIPT = str(ROOT / "dtv_tools.py")
MPD = ROOT / "tests" / "data" / "dash_subtitle.mpd"
need_tools = pytest.mark.skipif(not (shutil.which("MP4Box") and shutil.which("ffmpeg")), reason="precisa de MP4Box e ffmpeg")
TTML = """<?xml version="1.0" encoding="UTF-8"?>
<tt xmlns="http://www.w3.org/ns/ttml" xml:lang="por"><body><div>
<p begin="00:00:01.000" end="00:00:03.000">Ola mundo</p>
<p begin="00:00:04.000" end="00:00:06.000">Segunda</p>
<p begin="00:00:08.000" end="00:00:09.500">Terceira</p>
</div></body></tt>
"""


def sh(*cmd, cwd=None):
    r = subprocess.run([str(c) for c in cmd], capture_output=True, text=True, cwd=cwd, timeout=300)
    assert r.returncode == 0, (cmd, r.stdout[-400:], r.stderr[-400:])
    return r


@pytest.fixture(scope="module")
def media(tmp_path_factory):
    t = tmp_path_factory.mktemp("subs")
    (t / "a.ttml").write_text(TTML, encoding="utf-8")
    sh("MP4Box", "-add", "a.ttml:lang=por", "-new", "s.mp4", cwd=t)
    sh("MP4Box", "-dash", "4000", "-frag", "4000", "-rap", "-segment-name", "seg_$Number$", "-out", "m.mpd", "s.mp4", cwd=t)
    sh("ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=320x180:rate=30:duration=12", "-f", "lavfi", "-i", "sine=duration=12",
       "-c:v", "libx264", "-pix_fmt", "yuv420p", "-g", "30", "-c:a", "aac", "-shortest", "v.mp4", cwd=t)
    sh("MP4Box", "-add", "v.mp4", "-add", "s.mp4:lang=por", "-new", "vs.mp4", cwd=t)
    return t


def test_manifest_subtitle_track_is_returned_only_when_asked(tmp_path):
    xml = MPD.read_bytes()
    tracks, dyn, skipped, info = d.parse_mpd_tasks(xml, "http://h/live/manifest.mpd", str(tmp_path))
    assert dyn and tracks["subtitle"]["meta"] is None
    assert [e["kind"] for e in info["extra"]] == ["subtitle"] and info["extra"][0]["lang"] == "por" and "stpp" in info["extra"][0]["codecs"]
    tracks, _, _, info = d.parse_mpd_tasks(xml, "http://h/live/manifest.mpd", str(tmp_path), subs=True)
    sub = tracks["subtitle"]
    assert sub["meta"]["lang"] == "por" and sub["meta"]["timescale"] == 60000 and sub["meta"]["role"] == "caption"
    assert len(sub["segs"]) == 31 and sub["init"][0] == "http://h/live/Data3_4-init.mp4"
    assert sub["segs"][0][1] == "http://h/live/Data3_4-68493506081.m4s" and sub["segs"][0][3] == pytest.approx(120120 / 60000)
    assert info["extra"] == [] and tracks["vvc_base"]["meta"]["rep_id"] == "v1_1" and tracks["audio"]["meta"]["rep_id"] == "a2_3"   # the others are untouched


@need_tools
def test_segments_are_moved_to_the_video_origin(media, tmp_path):
    ts = d.parse_init_file(str(media / "seg_init.mp4"))[0]
    assert ts == 1000
    segs = [(i * 4000, str(media / f"seg_{i + 1}.m4s"), 4.0) for i in range(3)]
    out = tmp_path / "subs.mp4"
    r = d.build_subtitle_mp4(str(media / "seg_init.mp4"), segs, ts, 4.0, None, out)
    assert r == {"kept": 2, "dropped": 1, "first_gap": 0.0}                       # the first segment ends at 4 s: before the origin
    firsts = [d.m4s_shift_tfdt((media / f"seg_{i}.m4s").read_bytes(), 0)[1] for i in (1, 2, 3)]
    assert firsts == [0, 4000, 8000]
    pk = json.loads(sh("ffprobe", "-v", "error", "-show_packets", "-select_streams", "s", "-of", "json", out).stdout)["packets"]
    d.build_subtitle_mp4(str(media / "seg_init.mp4"), segs, ts, 0.0, None, tmp_path / "all.mp4")
    allp = json.loads(sh("ffprobe", "-v", "error", "-show_packets", "-select_streams", "s", "-of", "json", tmp_path / "all.mp4").stdout)["packets"]
    base = [float(p["pts_time"]) for p in allp]
    assert base and [float(p["pts_time"]) for p in pk] == [t - 4.0 for t in base if t >= 4.0]      # what is at 4 s or later, moved by 4 s
    r = d.build_subtitle_mp4(str(media / "seg_init.mp4"), segs, ts, 3.0, 8.0, tmp_path / "b.mp4")
    assert r["kept"] == 2 and r["dropped"] == 1 and r["first_gap"] == pytest.approx(3.0)      # starts 3 s before the origin: clamped to 0, reported
    assert d.m4s_shift_tfdt(b"", 5) == (b"", None)


@need_tools
def test_packaging_keeps_the_subtitle_track_and_the_retiming(media, tmp_path):
    ts = 1000
    segs = [(i * 4000, str(media / f"seg_{i + 1}.m4s"), 4.0) for i in range(3)]
    sub = tmp_path / "subs.mp4"
    d.build_subtitle_mp4(str(media / "seg_init.mp4"), segs, ts, 0.0, None, sub)
    with d.MP4BoxJob(media / "v.mp4") as box:
        out = tmp_path / "cap.mp4"
        cmd = ["MP4Box", "-add", f"{box.inp(media / 'v.mp4')}#video:id=1", "-add", f"{box.inp(sub)}:lang=por", "-new", box.out(out)]
        box.run(cmd, "package", lambda f, dd: None)
    tr = d.mp4_subtitle_tracks(out)
    assert [(t["id"], t["fourcc"], t["lang"]) for t in tr] == [(2, "stpp", "por")]
    v = next(t for t in d.mp4_tables(str(out)) if t["handler"] == "vide")
    dts = [x / v["ts"] for x in v["dts"]]
    pts = [d.mp4_present(v, k) for k in range(len(dts))]
    d.retime_mp4_tracks(str(out), {1: (dts, pts)}, 0.0)
    assert d.mp4_subtitle_tracks(out) == tr
    sh("ffmpeg", "-v", "error", "-i", out, "-map", "0:v", "-f", "null", "-")


@need_tools
def test_mp4_subtitle_tracks_ignores_other_files(media, tmp_path):
    assert [t["lang"] for t in d.mp4_subtitle_tracks(media / "vs.mp4")] == ["por"]
    assert d.mp4_subtitle_tracks(media / "v.mp4") == [] and d.mp4_subtitle_tracks(tmp_path / "nao_existe.mp4") == []
    (tmp_path / "x.mkv").write_bytes(b"\x1a\x45\xdf\xa3" + b"0" * 64)
    assert d.mp4_subtitle_tracks(tmp_path / "x.mkv") == []


@need_tools
def test_cut_keeps_the_subtitles(media, tmp_path):
    out = tmp_path / "cut.mp4"
    r = subprocess.run([sys.executable, SCRIPT, "mp4-cut", str(media / "vs.mp4"), "-s", "2", "-e", "7", "-o", str(out), "--overwrite"], capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stdout + r.stderr
    assert [t["lang"] for t in d.mp4_subtitle_tracks(out)] == ["por"]


def run_tc(media, tmp_path, *extra):
    r = subprocess.run([sys.executable, SCRIPT, "transcode", str(media / "vs.mp4"), "--no-lcevc", "--audio", "copy", "--preset", "ultrafast", "--size", "320x180",
                        "--outdir", str(tmp_path), "--overwrite", "--json", *extra], capture_output=True, text=True, timeout=600)
    assert r.returncode == 0, r.stdout[-800:] + r.stderr[-400:]
    res = [json.loads(x) for x in r.stdout.splitlines() if x.startswith("{") and '"type":"result"' in x.replace(" ", "")][-1]
    return Path(res["out"]), res


@need_tools
@pytest.mark.skipif("libx265" not in subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True).stdout if shutil.which("ffmpeg") else True,
                    reason="ffmpeg sem libx265")
def test_transcode_copies_or_removes_the_subtitles(media, tmp_path):
    out, res = run_tc(media, tmp_path)
    assert [t["lang"] for t in d.mp4_subtitle_tracks(out)] == ["por"] and ["Legendas", "por (copiadas da origem)"] in res["rows"]
    out2, res2 = run_tc(media, tmp_path / "x", "--no-subtitles") if (tmp_path / "x").mkdir() is None else (None, None)
    assert d.mp4_subtitle_tracks(out2) == [] and any(r[0] == "Legendas" and "removidas" in r[1] for r in res2["rows"])
    mkv, resm = run_tc(media, tmp_path, "-o", str(tmp_path / "o.mkv"))
    assert mkv.suffix == ".mkv" and any("não a recebe" in n for n in resm["notes"])


# ───────── dash-info ─────────
@pytest.fixture
def mpd_server(tmp_path):
    shutil.copy(MPD, tmp_path / "manifest.mpd")
    h = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(tmp_path))
    h.log_message = lambda *a, **k: None
    sv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), h)
    threading.Thread(target=sv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{sv.server_address[1]}"
    threading.Thread(target=sv.shutdown, daemon=True).start()


def test_dash_info_reports_the_subtitle(mpd_server):
    pytest.importorskip("requests")
    r = subprocess.run([sys.executable, SCRIPT, "dash-info", mpd_server + "/manifest.mpd", "--json"], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
    res = [json.loads(x) for x in r.stdout.splitlines() if x.startswith("{")][-1]
    s = res["stream"]
    assert s["dynamic"] and not s["drm"] and [x["lang"] for x in s["subtitles"]] == ["por"] and s["subtitles"][0]["recordable"]
    assert [v["kind"] for v in s["video"]] == ["vvc_base", "lcevc_enh"] and s["audio"][0]["codecs"] == "mhm1.0x10"
    r = subprocess.run([sys.executable, SCRIPT, "dash-info", mpd_server + "/nao.mpd", "--json"], capture_output=True, text=True, timeout=60)
    assert r.returncode != 0 and "404" in r.stdout + r.stderr
