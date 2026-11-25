import os
import subprocess
import time

import pytest

import dtv_tools as d
from conftest import need_ffmpeg

MPD_TIMELINE = b"""<?xml version="1.0"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="dynamic" availabilityStartTime="1970-01-01T00:00:00Z">
 <BaseURL>https://cdn.example/live/</BaseURL>
 <Period id="1" start="PT0S">
  <AdaptationSet contentType="video" mimeType="video/mp4">
   <Representation id="v1" codecs="vvc1.1.L51" bandwidth="9000000" width="1920" height="1080">
    <SegmentTemplate timescale="90000" initialization="init-$RepresentationID$.mp4" media="v/$RepresentationID$/$Number%05d$/$Time$.m4s" startNumber="7">
     <SegmentTimeline><S t="900000" d="180000" r="2"/></SegmentTimeline>
    </SegmentTemplate>
   </Representation>
  </AdaptationSet>
  <AdaptationSet contentType="video" mimeType="video/mp4">
   <Representation id="e1" codecs="lvc1" dependencyId="v1" bandwidth="1000000">
    <SegmentTemplate timescale="90000" initialization="init-e.mp4" media="e/$Time$.m4s">
     <SegmentTimeline><S t="900000" d="180000" r="2"/></SegmentTimeline>
    </SegmentTemplate>
   </Representation>
  </AdaptationSet>
  <AdaptationSet contentType="audio" mimeType="audio/mp4"><Role value="main"/>
   <Representation id="a1" codecs="mhm1.0x0D" bandwidth="128000" lang="pt">
    <AudioChannelConfiguration value="6"/>
    <SegmentTemplate timescale="48000" initialization="init-a.mp4" media="a/$Time$.m4s">
     <SegmentTimeline><S t="480000" d="96000" r="2"/></SegmentTimeline>
    </SegmentTemplate>
   </Representation>
  </AdaptationSet>
 </Period>
</MPD>"""


def test_parse_timeline_template_with_formatted_number(tmp_path):
    tracks, dyn, skipped, info = d.parse_mpd_tasks(MPD_TIMELINE, "https://x.test/a/live.mpd", str(tmp_path))
    assert dyn and not info["drm"] and not info["unsupported"]
    v = tracks["vvc_base"]
    assert [s[0] for s in v["segs"]] == [900000, 1080000, 1260000]
    assert v["segs"][0][1] == "https://cdn.example/live/v/v1/00007/900000.m4s"       # $Number%05d$ honoured, BaseURL used
    assert v["init"][0] == "https://cdn.example/live/init-v1.mp4"
    assert tracks["lcevc_enh"]["meta"]["rep_id"] == "e1" and tracks["audio"]["meta"]["channels"] == "6"
    assert tracks["audio"]["segs"][0][3] == pytest.approx(2.0)


def test_parse_duration_template_live_and_static(tmp_path):
    live = b"""<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="dynamic" availabilityStartTime="2024-01-01T00:00:00Z">
     <Period start="PT0S"><AdaptationSet contentType="video" mimeType="video/mp4"><Representation id="v" codecs="vvc1" bandwidth="1">
      <SegmentTemplate timescale="1000" duration="2000" startNumber="1" initialization="i.mp4" media="s-$Number$.m4s"/>
     </Representation></AdaptationSet></Period></MPD>"""
    now = 1704067200 + 100.5                      # 100.5 s after availabilityStartTime
    tracks, dyn, _, info = d.parse_mpd_tasks(live, "http://h/m.mpd", str(tmp_path), now=now, window=5)
    urls = [s[1] for s in tracks["vvc_base"]["segs"]]
    assert urls[-1].endswith("s-50.m4s") and len(urls) == 5          # last COMPLETE segment, a window of 5
    static = live.replace(b'type="dynamic" availabilityStartTime="2024-01-01T00:00:00Z"', b'type="static" mediaPresentationDuration="PT10S"')
    tracks, dyn, _, _ = d.parse_mpd_tasks(static, "http://h/m.mpd", str(tmp_path))
    assert not dyn and len(tracks["vvc_base"]["segs"]) == 5


def test_manifest_warnings_drm_periods_unsupported(tmp_path):
    drm = MPD_TIMELINE.replace(b'<Representation id="v1"', b'<ContentProtection schemeIdUri="urn:uuid:x"/><Representation id="v1"', 1)
    drm = drm.replace(b'<AdaptationSet contentType="video" mimeType="video/mp4">\n   <Rep', b'<AdaptationSet contentType="video" mimeType="video/mp4"><ContentProtection schemeIdUri="u"/>\n   <Rep', 1)
    _, _, _, info = d.parse_mpd_tasks(drm, "http://h/m.mpd", str(tmp_path))
    assert info["drm"]
    seglist = b"""<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static"><Period><AdaptationSet contentType="video" mimeType="video/mp4">
       <Representation id="v" codecs="vvc1"><SegmentList><SegmentURL media="a.m4s"/></SegmentList></Representation></AdaptationSet></Period></MPD>"""
    _, _, _, info = d.parse_mpd_tasks(seglist, "http://h/m.mpd", str(tmp_path))
    assert info["unsupported"]


def test_substitution_edge_cases():
    assert d._subst("a/$$/$Number%03d$/$RepresentationID$/$Time$", "r", "9", 5, 77) == "a/$/005/r/77"


def _au(tag, rap=False):
    tag = tag % 256
    nut = 8 if rap else 1
    return b"\x00\x00\x00\x01" + bytes([0x00, nut << 3 | 1]) + bytes([tag])


def test_merger_keeps_order_and_never_drops_mid_stream(monkeypatch):
    written = []
    merger = d.LcevcMerger(lambda pts, dts, au: written.append(pts), max_wait=5)
    # frames 0..19 ; LCEVC lost for 8 and 9, and for 12 it arrives after the wait limit
    for i in range(20):
        merger.add_video(i, i, _au(i, rap=(i % 10 == 0)))
    for i in range(20):
        if i not in (8, 9, 12):
            merger.add_lcevc(i, bytes([0xFF, 0xFF, i % 256]))
    merger.pump()
    merger.add_lcevc(12, b"\xFF\xFF\x0c")
    merger.pump()
    merger.pump(final=True)
    assert written == sorted(written)                                    # strictly decode order
    assert merger.unpaired == 3 and merger.written == 20                  # 8, 9 and 12 kept without LCEVC, nothing dropped
    assert {8, 9, 12} <= set(written)


def test_merger_starts_on_first_rap_with_lcevc():
    out = []
    started = []
    m = d.LcevcMerger(lambda p, dd, au: out.append(p), lambda p, dd: started.append(p), max_wait=100)
    for i in range(6):
        m.add_video(i, i, _au(i, rap=(i == 3)))
        m.add_lcevc(i, b"\xFF\xFF\x00")
    m.pump(final=True)
    assert started == [3] and out == [3, 4, 5] and m.skipped_pre == 3


def test_merger_discards_only_the_tail():
    out = []
    m = d.LcevcMerger(lambda p, dd, au: out.append(p), max_wait=100)
    for i in range(4):
        m.add_video(i, i, _au(i, rap=(i == 0)))
    m.add_lcevc(0, b"\xFF\xFF\x00")
    m.add_lcevc(1, b"\xFF\xFF\x00")
    m.pump(final=True)
    assert out == [0, 1] and m.dropped_tail == 2


def test_merger_static_burst_does_not_lose_lcevc():
    """A whole static stream arrives at once: the LCEVC buffer must not be trimmed under the frames waiting for it."""
    out = []
    m = d.LcevcMerger(lambda p, dd, au: out.append(p), max_wait=240)
    for i in range(3000):
        m.add_lcevc(i, b"\xFF\xFF\x00")
    for i in range(3000):
        m.add_video(i, i, _au(i, rap=(i == 0)))
    m.pump(final=True)
    assert len(out) == 3000 and m.unpaired == 0


@need_ffmpeg
def test_parse_m4s_matches_ffprobe(tmp_path):
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=duration=4:size=160x120:rate=25",
                    "-c:v", "libx264", "-bf", "2", "-g", "50", "-pix_fmt", "yuv420p", "-f", "dash", "-seg_duration", "2",
                    "-use_template", "1", "-use_timeline", "1", "-init_seg_name", "i.m4s",
                    "-media_seg_name", "c$Number%05d$.m4s", str(tmp_path / "m.mpd")], check=True)
    ts, nlen, params, ddur = d.parse_init_file(str(tmp_path / "i.m4s"))
    s1 = d.parse_m4s_file(str(tmp_path / "c00001.m4s"), ddur)
    s2 = d.parse_m4s_file(str(tmp_path / "c00002.m4s"), ddur)
    assert nlen == 4 and len(s1) + len(s2) == 100
    assert s2[0][1] > s1[-1][1]                                           # dts keeps counting across segments
    annexb = d.mp4_sample_to_annexb(s1[0][2], nlen)
    assert annexb.startswith(b"\x00\x00\x00\x01")


def test_download_rejects_truncated_body(tmp_path):
    class R:
        status_code = 200
        content = b"abc"
        headers = {"Content-Length": "10"}

    class S:
        def get(self, *a, **k):
            return R()
    n, err = d.download_to_disk(S(), "http://x/y", str(tmp_path / "f"))
    assert n == 0 and "incomplete" in err and not (tmp_path / "f").exists()


def test_drm_manifest_stops_the_capture(tmp_path):
    import http.server
    import threading
    drm = MPD_TIMELINE.replace(b'<AdaptationSet contentType="video" mimeType="video/mp4">',
                               b'<AdaptationSet contentType="video" mimeType="video/mp4"><ContentProtection schemeIdUri="urn:x"/>', 1)

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(drm)

        def log_message(self, *a):
            pass
    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    from conftest import run_cli
    rc, text, ev = run_cli("dash-capture", f"http://127.0.0.1:{srv.server_port}/m.mpd", "--save-dir", tmp_path, "--progress-json",
                           "--mp4box", "/bin/true")
    srv.shutdown()
    assert rc == 2 and "DRM" in next(e for e in ev if e["type"] == "error")["message"]


def test_mpd_lists_subtitles_and_alternative_representations(tmp_path):
    mpd = MPD_TIMELINE.replace(b"</Period>", b"""<AdaptationSet contentType="text" mimeType="application/mp4" lang="por"><Role value="subtitle"/>
<Representation id="s1" codecs="stpp" bandwidth="1000"><SegmentTemplate timescale="1" duration="2" media="s_$Number$.m4s"/></Representation></AdaptationSet>
<AdaptationSet contentType="video" mimeType="video/mp4"><Representation id="v_alt" codecs="vvc1" bandwidth="999" width="1280" height="720">
<SegmentTemplate timescale="1" duration="2" media="a_$Number$.m4s"/></Representation></AdaptationSet></Period>""", 1)
    tracks, _, skipped, info = d.parse_mpd_tasks(mpd, "https://x.test/a/live.mpd", str(tmp_path))
    kinds = {e["kind"] for e in info["extra"]}
    assert "subtitle" in kinds and "alt_vvc_base" in kinds
    sub = next(e for e in info["extra"] if e["kind"] == "subtitle")
    assert sub["lang"] == "por" and sub["codecs"] == "stpp" and sub["role"] == "subtitle"
    assert tracks["vvc_base"]["meta"]["codecs"]            # a trilha gravada continua a primeira representação
    assert not (tmp_path / "subtitle").exists()            # nada é baixado/criado para a legenda
