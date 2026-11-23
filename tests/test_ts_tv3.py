"""ts-tv3 end to end on a synthetic TS (H.264 stands in for VVC/LCEVC, SEI insertion is bypassed)."""
import shutil
import struct
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import ROOT, need_mp4box


def _crc(data):
    crc = 0xFFFFFFFF
    for b in data:
        crc ^= b << 24
        for _ in range(8):
            crc = ((crc << 1) ^ 0x04C11DB7) & 0xFFFFFFFF if crc & 0x80000000 else (crc << 1) & 0xFFFFFFFF
    return crc


def _patch_pmt(src, dst):
    d = bytearray(Path(src).read_bytes())
    pmt = None
    for i in range(0, len(d), 188):
        if (((d[i + 1] & 0x1F) << 8) | d[i + 2]) == 0 and d[i + 1] & 0x40:
            p = i + 5 + d[i + 4]
            pmt = ((d[p + 10] & 0x1F) << 8) | d[p + 11]
            break
    for i in range(0, len(d), 188):
        if (((d[i + 1] & 0x1F) << 8) | d[i + 2]) != pmt or not d[i + 1] & 0x40:
            continue
        p = i + 5 + d[i + 4]
        end = p + 3 + (((d[p + 1] & 0x0F) << 8) | d[p + 2]) - 4
        q = p + 12 + (((d[p + 10] & 0x0F) << 8) | d[p + 11])
        while q + 5 <= end:
            pid = ((d[q + 1] & 0x1F) << 8) | d[q + 2]
            d[q] = {0x100: 0x33, 0x101: 0x36}.get(pid, d[q])
            q += 5 + (((d[q + 3] & 0x0F) << 8) | d[q + 4])
        d[end:end + 4] = struct.pack(">I", _crc(bytes(d[p:end])))
    Path(dst).write_bytes(d)


HARNESS = '''
import sys, shutil
sys.path.insert(0, %r)
import dtv_tools as d
d.insert_sei_before_first_slice = lambda au, sei: au
real = d.run_mp4box
def fake(cmd, label, tmpdir, on_pct):
    cmd = list(cmd)
    for i, c in enumerate(cmd):
        if isinstance(c, str) and c.split(":fps")[0].split("#")[0].endswith(".vvc"):
            src = c.split(":fps")[0].split("#")[0]
            shutil.copy(src, src[:-4] + ".h264")
            cmd[i] = c.replace(".vvc", ".h264", 1)
    return real(cmd, label, tmpdir, on_pct)
d.run_mp4box = fake
sys.exit(d.main(sys.argv[1:]))
''' % str(ROOT)


@pytest.fixture
def tv3_ts(tmp_path):
    raw = tmp_path / "cap.ts"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=duration=8:size=320x240:rate=25",
                    "-f", "lavfi", "-i", "testsrc2=duration=8:size=160x120:rate=25", "-map", "0:v", "-map", "1:v",
                    "-c:v:0", "libx264", "-bf", "2", "-g", "25", "-keyint_min", "25", "-sc_threshold", "0", "-pix_fmt", "yuv420p",
                    "-c:v:1", "libx264", "-bf", "0", "-g", "25", "-pix_fmt", "yuv420p", "-f", "mpegts", "-mpegts_start_pid", "0x100", str(raw)],
                   check=True)
    out = tmp_path / "cap_tv3.ts"
    _patch_pmt(raw, out)
    h = tmp_path / "harness.py"
    h.write_text(HARNESS)
    return out, h


def _run(h, *args):
    r = subprocess.run([sys.executable, str(h), *map(str, args)], capture_output=True, text=True, encoding="utf-8", errors="replace")
    return r.returncode, r.stdout + r.stderr


@need_mp4box
def test_ts_tv3_tmpdir_pointing_at_an_existing_folder_is_never_wiped(tv3_ts, tmp_path):
    ts, h = tv3_ts
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    (downloads / "important.txt").write_text("keep me")
    rc, text = _run(h, "ts-tv3", ts, "--outdir", tmp_path / "out", "--tmpdir", downloads, "--no-color")
    assert rc == 0, text
    assert (downloads / "important.txt").read_text() == "keep me"
    assert list(downloads.iterdir()) == [downloads / "important.txt"]          # its own sub-folder was cleaned up
    assert (tmp_path / "out" / "cap_tv3_single_track_4k.mp4").stat().st_size > 0
    assert (tmp_path / "out" / "cap_tv3_dual_track.mp4").stat().st_size > 0


@need_mp4box
def test_ts_tv3_timing_from_real_pts_and_resume(tv3_ts, tmp_path):
    ts, h = tv3_ts
    out = tmp_path / "o"
    rc, text = _run(h, "ts-tv3", ts, "--outdir", out, "--outputs", "single", "--keep-temp", "--no-color")
    assert rc == 0, text
    mp4 = out / "cap_tv3_single_track_4k.mp4"
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "packet=pts_time", "-of", "csv=p=0", str(mp4)],
                       capture_output=True, text=True)
    pts = sorted(float(x.strip(",")) for x in r.stdout.split() if x.strip(","))
    assert len(pts) == 200 and pts[0] == pytest.approx(0.0, abs=1e-3) and pts[-1] == pytest.approx(7.96, abs=0.02)
    rc, text = _run(h, "ts-tv3", ts, "--outdir", out, "--outputs", "single", "--keep-temp", "--no-color", "--overwrite")
    assert "reaproveitado" in text                                              # same source + parameters: checkpoint reused
    rc, text = _run(h, "ts-tv3", ts, "--outdir", out, "--outputs", "single", "--keep-temp", "--no-color", "--overwrite", "--fps", "30")
    assert "discarding the old intermediate files" in text                     # parameters changed: checkpoint thrown away


@need_mp4box
def test_ts_tv3_no_stray_print_in_json_mode(tv3_ts, tmp_path):
    ts, h = tv3_ts
    rc, text = _run(h, "ts-tv3", ts, "--outdir", tmp_path / "o", "--outputs", "single", "--progress-json")
    assert rc == 0
    assert not [ln for ln in text.splitlines() if ln.startswith("[") and "single" in ln and "MP4Box" in ln]
