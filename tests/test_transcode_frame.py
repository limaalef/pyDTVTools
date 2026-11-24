import argparse
import math
import subprocess

import pytest

from conftest import HAVE_FFMPEG, need_ffmpeg
import dtv_tools as d

pytestmark = pytest.mark.filterwarnings("ignore")


def test_version():
    assert d.__version__ == "1.3.0"


def test_every_option_has_help():
    p = d.build_parser()
    for name, sp in p._subparsers._group_actions[0].choices.items():
        for a in sp._actions:
            if a.option_strings and a.dest != "help":
                assert a.help, f"{name} {a.option_strings[0]} has no help text"


def test_x265_master_format():
    h = {"g": (0.17, 0.797), "b": (0.131, 0.046), "r": (0.708, 0.292), "wp": (0.3127, 0.329), "lmax": 1000, "lmin": 0.005}
    assert d.x265_master(h) == "G(8500,39850)B(6550,2300)R(35400,14600)WP(15635,16450)L(10000000,50)"


def test_pq_roundtrip_and_monotonic():
    np = pytest.importorskip("numpy")
    x = np.linspace(0, 1, 11)
    y = d.pq_eotf(x)
    assert y[0] == pytest.approx(0, abs=1e-6) and y[-1] == pytest.approx(10000, rel=1e-3)
    assert all(b > a for a, b in zip(y, y[1:]))


@pytest.mark.parametrize("name", ["mobius", "hable", "reinhard", "clip"])
def test_tone_curve_monotonic(name):
    np = pytest.importorskip("numpy")
    x = np.linspace(0, 4, 50)
    y = d.tone_curve(name, x, 4.0)
    assert all(b >= a - 1e-9 for a, b in zip(y, y[1:]))


def test_same_file(tmp_path):
    f = tmp_path / "a.mp4"
    f.write_bytes(b"x")
    assert d.same_file(f, tmp_path / "." / "a.mp4")
    assert not d.same_file(f, tmp_path / "b.mp4")


@pytest.fixture
def hdr_mp4(tmp_path):
    if not HAVE_FFMPEG:
        pytest.skip("ffmpeg missing")
    out = tmp_path / "in.mp4"
    r = subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=25:duration=1", "-f", "lavfi",
         "-i", "sine=d=1", "-c:v", "libx265", "-pix_fmt", "yuv420p10le", "-x265-params",
         "hdr10=1:repeat-headers=1:colorprim=bt2020:transfer=smpte2084:colormatrix=bt2020nc:"
         "master-display=G(8500,39850)B(6550,2300)R(35400,14600)WP(15635,16450)L(10000000,50):max-cll=1000,300",
         "-c:a", "aac", "-shortest", str(out)], capture_output=True)
    if r.returncode:
        pytest.skip("libx265 missing")
    return out


def run(*args):
    return subprocess.run(["python3", str(d.__file__), *map(str, args), "--no-color"], capture_output=True, text=True)


@need_ffmpeg
def test_transcode_refuses_same_file_and_bad_ext(hdr_mp4, tmp_path):
    r = run("transcode", hdr_mp4, "-o", hdr_mp4, "--overwrite", "--no-lcevc", "--audio", "copy")
    assert r.returncode != 0 and hdr_mp4.stat().st_size > 1000
    r = run("transcode", hdr_mp4, "-o", tmp_path / "x.txt", "--no-lcevc", "--audio", "copy")
    assert r.returncode != 0 and not (tmp_path / "x.txt").exists()


@need_ffmpeg
def test_transcode_ok_leaves_no_part(hdr_mp4, tmp_path):
    out = tmp_path / "o.mp4"
    r = run("transcode", hdr_mp4, "-o", out, "--no-lcevc", "--audio", "copy", "--crf", "30")
    assert r.returncode == 0, r.stderr + r.stdout
    assert out.stat().st_size > 1000 and not list(tmp_path.glob("*.part"))


@need_ffmpeg
def test_frame_png_sdr_and_out_of_range(hdr_mp4, tmp_path):
    pytest.importorskip("numpy")
    r = run("frame", hdr_mp4, "--time", "99", "--no-lcevc", "--outdir", tmp_path)
    assert r.returncode == 0, r.stderr + r.stdout
    assert "fora do vídeo" in r.stdout
    hdr = next(tmp_path.glob("*_hdr.png"))
    assert hdr.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n" and b"cICP" in hdr.read_bytes()
    assert next(tmp_path.glob("*_sdr.png")).exists()


@need_ffmpeg
def test_sdr_from_hdr_png(hdr_mp4, tmp_path):
    pytest.importorskip("numpy")
    run("frame", hdr_mp4, "--frame", "3", "--no-lcevc", "--outdir", tmp_path)
    hdr = next(tmp_path.glob("*_hdr.png"))
    out = tmp_path / "again"
    r = run("sdr", hdr, "-o", out, "--tonemap", "hable")
    assert r.returncode == 0, r.stderr + r.stdout


def _tc_args(*extra):
    p = d.build_parser()
    return p.parse_args(["transcode", "x.mp4", *extra])


_HDR = {"g": (0.17, 0.797), "b": (0.131, 0.046), "r": (0.708, 0.292), "wp": (0.3127, 0.329), "lmax": 1000, "lmin": 0.005, "maxcll": 1000, "maxfall": 400}


def _tc_cmd(a):
    from pathlib import Path
    return d.build_transcode_cmd("ffmpeg", Path("x.mp4"), Path("o.mp4"), a, _HDR, None, False, 0)


def test_transcode_default_is_x265():
    c = _tc_cmd(_tc_args())
    assert "libx265" in c and "hevc_nvenc" not in c and "-x265-params" in c


def test_transcode_nvenc_command():
    c = _tc_cmd(_tc_args("--encoder", "nvenc", "--crf", "22", "--preset", "p7", "--nvenc-params", "-spatial-aq 1"))
    assert "hevc_nvenc" in c and "libx265" not in c and "-x265-params" not in c
    assert c[c.index("-cq") + 1] == "22" and c[c.index("-preset") + 1] == "p7" and c[c.index("-pix_fmt") + 1] == "p010le"
    assert c[c.index("-profile:v") + 1] == "main10" and "-spatial-aq" in c and "hvc1" in c
    # a x265 preset name is not valid for NVENC: falls back to p5
    c = _tc_cmd(_tc_args("--encoder", "nvenc", "--preset", "slow"))
    assert c[c.index("-preset") + 1] == "p5"


def test_encoder_table_is_consistent():
    for k, e in d.ENCODERS.items():
        assert e["codec"] in d.CODEC_NAMES and e["ffmpeg"] and e["crf_range"][0] <= e["crf"] <= e["crf_range"][1]
        assert e["preset_default"] in ("", *e["presets"]), k


def test_default_quality_depends_on_the_encoder():
    assert d.enc_crf(_tc_args()) == 18
    assert d.enc_crf(_tc_args("--encoder", "nvenc")) == 22
    assert d.enc_crf(_tc_args("--encoder", "av1")) == 28
    assert d.enc_crf(_tc_args("--encoder", "av1", "--crf", "30")) == 30


def test_svtav1_command_carries_hdr10():
    c = _tc_cmd(_tc_args("--encoder", "av1", "--preset", "4", "--encoder-params", "tune=0"))
    assert c[c.index("-c:v") + 1] == "libsvtav1" and "libx265" not in c and "-tag:v" not in c
    assert c[c.index("-crf") + 1] == "28" and c[c.index("-preset") + 1] == "4" and c[c.index("-pix_fmt") + 1] == "yuv420p10le"
    sp = c[c.index("-svtav1-params") + 1]
    assert "transfer-characteristics=16" in sp and "color-primaries=9" in sp and "content-light=1000,400" in sp
    assert "mastering-display=G(0.1700,0.7970)B(0.1310,0.0460)R(0.7080,0.2920)WP(0.3127,0.3290)L(1000.0000,0.0050)" in sp
    assert sp.endswith("tune=0")


def test_svtav1_preset_from_another_encoder_falls_back():
    c = _tc_cmd(_tc_args("--encoder", "av1", "--preset", "slow"))
    assert c[c.index("-preset") + 1] == "6"


def test_av1_nvenc_command():
    c = _tc_cmd(_tc_args("--encoder", "av1-nvenc", "--preset", "p6", "--encoder-params", "-spatial-aq 1"))
    assert c[c.index("-c:v") + 1] == "av1_nvenc" and c[c.index("-cq") + 1] == "28" and c[c.index("-preset") + 1] == "p6"
    assert c[c.index("-pix_fmt") + 1] == "p010le" and "-spatial-aq" in c and "-profile:v" not in c and "-tag:v" not in c


def test_old_param_options_still_work():
    assert "-spatial-aq" in _tc_cmd(_tc_args("--encoder", "nvenc", "--nvenc-params", "-spatial-aq 1"))
    xp = _tc_cmd(_tc_args("--x265-params", "aq-mode=3"))
    assert xp[xp.index("-x265-params") + 1].endswith("aq-mode=3")
    xp = _tc_cmd(_tc_args("--encoder-params", "aq-mode=2"))
    assert xp[xp.index("-x265-params") + 1].endswith("aq-mode=2")


def test_describe_lists_the_encoders():
    assert set(d.describe()["encoders"]) == {"x265", "nvenc", "av1", "av1-nvenc"}
