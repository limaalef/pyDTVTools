"""Delay of the audio relative to the video, as the transcode must apply it to the rendered tracks."""
import subprocess
from pathlib import Path

import pytest

import dtv_tools as d
from conftest import need_ffmpeg


def _lav(args):
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True)


@pytest.fixture(scope="module")
def media(tmp_path_factory):
    t = tmp_path_factory.mktemp("off")
    _lav(["-f", "lavfi", "-i", "testsrc=d=3:s=160x120:r=25", "-c:v", "libx264", "-g", "25", str(t / "v.mp4")])
    _lav(["-f", "lavfi", "-i", "sine=d=3", "-c:a", "ac3", str(t / "a.ac3")])
    return t


def _mux(t, name, shift):
    out = t / name
    _lav(["-i", str(t / "v.mp4"), "-itsoffset", str(shift), "-i", str(t / "a.ac3"), "-map", "0:v", "-map", "1:a", "-c", "copy",
          "-avoid_negative_ts", "disabled", str(out)])
    return out


@need_ffmpeg
@pytest.mark.parametrize("shift", [0.26, 0.5, -0.1, 0.0])
def test_offset_read_from_edit_lists(media, shift):
    f = _mux(media, f"s{shift}.mp4", shift)
    off = d.mp4_audio_offset(str(f))
    assert off == pytest.approx(shift, abs=0.002)


@need_ffmpeg
def test_offset_none_without_audio(media):
    assert d.mp4_audio_offset(str(media / "v.mp4")) is None


def _cmd(offset, n=2):
    import argparse
    a = argparse.Namespace(encoder="x265", audio="render", limit=0, container="mkv", max_bitrate=0, crf=None, preset=None,
                           encoder_params="", x265_params="", nvenc_params="", normalize="off")
    items = [{"path": f"/t/a{i}.ac3", "title": f"P{i}"} for i in range(n)]
    return d.build_transcode_cmd("ffmpeg", "/t/in.mp4", Path("/t/out.mkv"), a, dict(d.DEFAULT_HDR10), "1920x1080", False, 1, items, offset, True)


@pytest.mark.parametrize("n", [1, 3])
def test_positive_offset_goes_before_every_rendered_input(n):
    c = _cmd(0.26, n)
    assert c.count("-itsoffset") == n and "-ss" not in c
    for i, x in enumerate(c):
        if x == "-itsoffset":
            assert c[i + 1] == "0.260" and c[i + 2] == "-i" and c[i + 3].endswith(".ac3")


def test_negative_offset_trims_the_rendered_input():
    c = _cmd(-0.1)
    assert "-itsoffset" not in c and c.count("-ss") == 2
    i = c.index("-ss")
    assert c[i + 1] == "0.100" and c[i + 2] == "-i"


def test_zero_offset_adds_nothing():
    c = _cmd(0.0)
    assert "-itsoffset" not in c and "-ss" not in c
