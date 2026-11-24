"""Normalização de volume (--normalize): alvo, ganho, medição e o resultado real medido de novo."""
import argparse
import subprocess

import pytest

from conftest import need_ffmpeg
import dtv_tools as d


def NS(**kw):
    return argparse.Namespace(**{"normalize": "off", "true_peak": -1.0, **kw})


def test_target_parsing():
    assert d.loudness_target(NS()) is None
    for off in ("off", "OFF", "none", "0"):
        assert d.loudness_target(NS(normalize=off)) is None
    assert d.loudness_target(NS(normalize="ebu")) == -23.0
    assert d.loudness_target(NS(normalize="atsc")) == -24.0
    assert d.loudness_target(NS(normalize="streaming")) == -16.0
    assert d.loudness_target(NS(normalize="-18")) == -18.0
    assert d.loudness_target(NS(normalize="-20,5")) == -20.5


@pytest.mark.parametrize("bad", ["loud", "-50", "-3", "ebu2"])
def test_invalid_target(bad):
    with pytest.raises(d.Fail):
        d.loudness_target(NS(normalize=bad))


@pytest.mark.parametrize("tp", [1.0, -10.0])
def test_invalid_true_peak(tp):
    with pytest.raises(d.Fail):
        d.loudness_target(NS(normalize="ebu", true_peak=tp))


def test_gain_brings_the_programme_to_the_target():
    assert d.loudness_gain({"i": -30.0, "tp": -12.0}, -23.0, -1.0) == (7.0, False)
    assert d.loudness_gain({"i": -16.0, "tp": -2.0}, -23.0, -1.0) == (-7.0, False)       # quieter is always allowed
    assert d.loudness_gain({"i": -23.02, "tp": -5.0}, -23.0, -1.0) == (0.0, False)       # under 0.05 dB: untouched


def test_gain_is_limited_by_the_true_peak():
    g, limited = d.loudness_gain({"i": -30.0, "tp": -4.0}, -23.0, -1.0)
    assert limited and g == 3.0                                                          # -4 + 3 = -1 dBTP, not the +7 dB the target asked for


def test_a_resumed_job_does_not_reuse_a_file_made_without_normalization():
    src = open(d.__file__, encoding="utf-8").read()
    assert 'f"|N{norm_target:g}/{a.true_peak:g}"' in src                                  # the plan key carries the normalization setting


def _tone(path, level_db, channels="stereo", seconds=6):
    layout = {"stereo": "stereo", "5.1": "5.1"}[channels]
    pan = ("pan=stereo|FL=c0|FR=c0" if channels == "stereo" else "pan=5.1|FL=c0|FR=c0|FC=c0|LFE=c0|BL=c0|BR=c0")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"sine=f=330:sample_rate=48000:duration={seconds}",
                    "-af", f"volume={level_db}dB,{pan}", "-c:a", "pcm_s24le", str(path)], check=True)
    assert layout


@need_ffmpeg
@pytest.mark.parametrize("channels", ["stereo", "5.1"])
def test_measure_then_apply_reaches_the_target(tmp_path, channels):
    wav = tmp_path / "in.wav"
    _tone(wav, -22, channels)
    m = d.measure_loudness("ffmpeg", wav)
    gain, limited = d.loudness_gain(m, -23.0, -1.0)
    out = tmp_path / "out.flac"
    d.encode_audio("ffmpeg", wav, out, "flac", None, None, gain_db=gain)
    m2 = d.measure_loudness("ffmpeg", out)
    assert not limited and abs(m2["i"] - (-23.0)) < 0.6, (m, gain, m2)
    assert m2["tp"] <= -1.0 + 0.3


@need_ffmpeg
def test_gain_changes_nothing_but_the_level(tmp_path):
    wav = tmp_path / "in.wav"
    _tone(wav, -22)
    base, loud = tmp_path / "a.flac", tmp_path / "b.flac"
    d.encode_audio("ffmpeg", wav, base, "flac", None, None)
    d.encode_audio("ffmpeg", wav, loud, "flac", None, None, gain_db=6.0)
    a, b = d.measure_loudness("ffmpeg", base), d.measure_loudness("ffmpeg", loud)
    assert abs((b["i"] - a["i"]) - 6.0) < 0.2 and abs((b["tp"] - a["tp"]) - 6.0) < 0.3


@need_ffmpeg
def test_silence_is_not_measurable(tmp_path):
    wav = tmp_path / "s.wav"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-t", "3", "-c:a", "pcm_s24le", str(wav)], check=True)
    assert d.measure_loudness("ffmpeg", wav) is None


@need_ffmpeg
def test_unreadable_wav_fails_clearly(tmp_path):
    bad = tmp_path / "x.wav"
    bad.write_bytes(b"not audio")
    with pytest.raises(d.Fail):
        d.measure_loudness("ffmpeg", bad)


def test_both_commands_take_the_options():
    p = d.build_parser()
    for cmd, extra in (("mpegh-audio", []), ("transcode", [])):
        a = p.parse_args([cmd, "x.mp4", *extra, "--normalize", "ebu", "--true-peak", "-2"])
        assert a.normalize == "ebu" and a.true_peak == -2.0
        assert p.parse_args([cmd, "x.mp4"]).normalize == "off"
