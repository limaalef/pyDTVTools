"""MP4Box with hostile file/folder names (spaces, '+', '%', '$', '#', ':', unicode): every call goes through MP4BoxJob."""
import shutil
import subprocess

import pytest

import dtv_tools as d
from conftest import need_ffmpeg, need_mp4box

NAMES = ["with space + plus.mp4", "100%25 $x$ #1.mp4", "ação ñ (1) & [2].mp4"]


def _make(tmp_path, name, audio=True):
    folder = tmp_path / "Pasta A+B %41 $d$"
    folder.mkdir(exist_ok=True)
    f = folder / name
    args = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=duration=4:size=160x120:rate=25"]
    if audio:
        args += ["-f", "lavfi", "-i", "sine=duration=4"]
    args += ["-c:v", "libx264", "-g", "25", "-pix_fmt", "yuv420p"] + (["-c:a", "aac"] if audio else []) + [str(f)]
    subprocess.run(args, check=True)
    return f


@need_ffmpeg
@need_mp4box
@pytest.mark.parametrize("name", NAMES)
def test_cut_and_splitx(tmp_path, name):
    mp4box = shutil.which("MP4Box") or shutil.which("mp4box")
    src = _make(tmp_path, name)
    out = tmp_path / "Saída A+B %41" / f"cut {name}"
    ok, tail = d.cut_segment(mp4box, src, out, 1.0, 3.0, 4.0)
    assert ok, tail
    assert out.is_file() and out.stat().st_size > 1000
    out2 = tmp_path / "Saída A+B %41" / f"split {name}"
    assert d.mp4box_splitx(mp4box, src, 0.5, 2.5, out2)
    assert out2.is_file()


@need_ffmpeg
@need_mp4box
@pytest.mark.parametrize("name", NAMES)
def test_add_with_titles_and_info(tmp_path, name):
    mp4box = shutil.which("MP4Box") or shutil.which("mp4box")
    src = _make(tmp_path, name)
    aud = _make(tmp_path, "áudio: 2.0 + título.mp4")
    out = tmp_path / "out dir +" / f"mux {name}"
    with d.MP4BoxJob(src) as box:
        cmd = [mp4box, "-add", box.inp(src), "-add",
               f"{box.inp(aud)}#audio:name={d.mp4box_title('Preset 1: Diálogo #2 100% $x$')}:delay=40", "-new", box.out(out)]
        box.run(cmd, "mux", lambda f, t: None)
    assert out.is_file() and out.stat().st_size > 1000
    with d.MP4BoxJob(out) as box:
        r = subprocess.run([mp4box, "-info", box.inp(out)], capture_output=True, text=True, cwd=str(box.dir))
    assert r.returncode == 0
    pr = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream_tags=handler_name", "-of", "csv=p=0", str(out)],
                        capture_output=True, text=True)
    assert "Diálogo" in pr.stdout, pr.stdout


@need_ffmpeg
@need_mp4box
def test_prepare_audio_hostile_paths(tmp_path):
    mp4box = shutil.which("MP4Box") or shutil.which("mp4box")
    src = _make(tmp_path, "vídeo + áudio %41 $d$ #.mp4")
    root = tmp_path / "cache dir +"
    root.mkdir()
    dd = d.prepare_audio(src, root, mp4box)
    assert (dd / "audio.mp4").stat().st_size > 100
