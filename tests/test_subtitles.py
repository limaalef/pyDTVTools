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


HAS_X265 = bool(shutil.which("ffmpeg")) and "libx265" in subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True).stdout


def tracks_of(path):
    return [(t["fourcc"], t["lang"]) for t in d.mp4_subtitle_tracks(path)]


@need_tools
@pytest.mark.skipif(not HAS_X265, reason="ffmpeg sem libx265")
def test_transcode_converts_ttml_to_tx3g_by_default(media, tmp_path):
    out, res = run_tc(media, tmp_path, "--subtitle-sidecar")
    assert tracks_of(out) == [("tx3g", "por")] and any(r[0] == "Legendas" and "tx3g" in r[1] and "convertida" in r[1] for r in res["rows"])
    assert (tmp_path / f"{out.stem}.por.srt").read_text(encoding="utf-8").startswith("1\n00:00:01,000 --> 00:00:03,000\nOla mundo\n")
    r = sh("ffmpeg", "-v", "error", "-i", out, "-map", "0:s:0", "-f", "srt", "-")                      # ffmpeg, and so most players, can read it
    assert "Ola mundo" in r.stdout and "Terceira" in r.stdout


@need_tools
@pytest.mark.skipif(not HAS_X265, reason="ffmpeg sem libx265")
def test_transcode_keeps_stpp_or_both_or_removes(media, tmp_path):
    (tmp_path / "a").mkdir(); (tmp_path / "b").mkdir(); (tmp_path / "c").mkdir(); (tmp_path / "e").mkdir()
    out, _ = run_tc(media, tmp_path / "a", "--subtitle-format", "stpp")
    assert tracks_of(out) == [("stpp", "por")]
    out, _ = run_tc(media, tmp_path / "b", "--subtitle-format", "both")
    assert sorted(tracks_of(out)) == [("stpp", "por"), ("tx3g", "por")]
    out, res = run_tc(media, tmp_path / "c", "--no-subtitles")
    assert tracks_of(out) == [] and any(r[0] == "Legendas" and "removidas" in r[1] for r in res["rows"])
    mkv, resm = run_tc(media, tmp_path / "e", "-o", str(tmp_path / "e" / "o.mkv"))
    assert mkv.suffix == ".mkv" and any("não a recebe" in n for n in resm["notes"])


# ───────── TTML → cues ─────────
def tt(body, attrs=""):
    return f'<?xml version="1.0"?><tt xmlns="http://www.w3.org/ns/ttml" xmlns:tts="http://www.w3.org/ns/ttml#styling" xmlns:ttp="http://www.w3.org/ns/ttml#parameter" {attrs}>{body}</tt>'


def test_ttml_times_styles_and_breaks():
    xml = tt('<head><styling><style xml:id="it" tts:fontStyle="italic"/></styling></head><body><div begin="10s">'
             '<p begin="00:00:01.500" end="00:00:03.000">Linha  um<br/>linha <span style="it">dois</span></p>'
             '<p begin="500ms" dur="2s"><span tts:fontWeight="bold">Negrito</span> normal</p>'
             '<p begin="30f" end="60f">Quadros</p><p begin="0s" end="0s">vazio</p><p begin="1s" end="2s">   </p></div></body>', 'ttp:frameRate="30"')
    c = d.parse_ttml_cues(xml)
    assert c[0] == (11.5, 13.0, "Linha um\nlinha <i>dois</i>")
    assert c[1] == (10.5, 12.5, "<b>Negrito</b> normal")
    assert c[2] == (11.0, 12.0, "Quadros")
    assert len(c) == 3 and d.ttml_seconds("1200ms") == 1.2 and d.ttml_seconds("10t", tick=10) == 1.0 and d.ttml_seconds("xx") is None
    assert d.parse_ttml_cues(tt("<body/>")) == []
    with pytest.raises(Exception):
        d.parse_ttml_cues("<tt")


def doc(begin, end, text="Oi"):
    begin, end = f"{begin}s", f"{end}s"
    return tt(f'<body><div><p begin="{begin}" end="{end}">{text}</p></div></body>')


def test_docs_to_cues_detects_the_time_base_and_merges_repeats():
    # times on the track's own timeline (inside the sample window)
    c, mode = d.ttml_docs_to_cues([(100.0, 2.0, doc("100.5", "103.0")), (102.0, 2.0, doc("100.5", "103.0")), (104.0, 2.0, doc("105", "105.5", "Outra"))])
    assert mode == "aligned" and c == [(100.5, 103.0, "Oi"), (105.0, 105.5, "Outra")]            # the repeated cue became one
    # relative to each sample
    c, mode = d.ttml_docs_to_cues([(100.0, 2.0, doc("0.5", "1.5")), (102.0, 2.0, doc("0.2", "1")), (104.0, 2.0, "<tt")])
    assert mode == "relative" and c == [(100.5, 101.5, "Oi"), (102.2, 103.0, "Oi")]
    # another epoch: the constant difference seen in the first document is removed
    c, mode = d.ttml_docs_to_cues([(0.0, 2.0, doc("5000.0", "5001.0")), (2.0, 2.0, doc("5002.0", "5003.0", "Dois"))])
    assert mode == "offset" and c == [(0.0, 1.0, "Oi"), (2.0, 3.0, "Dois")]
    assert d.ttml_docs_to_cues([(0.0, 2.0, tt("<body/>"))]) == ([], "aligned")


def test_srt_text():
    assert d.srt_text([(0.0, 1.5, "A"), (3661.25, 3662.0, "B\nC")]) == "1\n00:00:00,000 --> 00:00:01,500\nA\n\n2\n01:01:01,250 --> 01:01:02,000\nB\nC\n\n"


@need_tools
def test_samples_are_read_from_segments_and_files(media):
    smp = d.m4s_samples((media / "seg_1.m4s").read_bytes())
    assert smp and smp[0][0] == 0 and any(b"Ola mundo" in pl for _, _, pl in smp)
    docs = [(t / 1000, du / 1000, pl.decode()) for f in ("seg_1.m4s", "seg_2.m4s", "seg_3.m4s") for t, du, pl in d.m4s_samples((media / f).read_bytes())]
    cues, mode = d.ttml_docs_to_cues(docs)
    assert [c[2] for c in cues] == ["Ola mundo", "Segunda", "Terceira"] and cues[0][:2] == (1.0, 3.0) and mode == "aligned"
    tid = d.mp4_subtitle_tracks(media / "vs.mp4")[0]["id"]
    fdocs = d.mp4_stpp_docs(media / "vs.mp4", tid)
    assert fdocs and d.ttml_docs_to_cues(fdocs)[0][1][2] == "Segunda"


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
