import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import dtv_tools as d  # noqa: E402

HAVE_FFMPEG = shutil.which("ffmpeg") is not None
HAVE_MP4BOX = shutil.which("MP4Box") is not None or shutil.which("mp4box") is not None
need_ffmpeg = pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg not installed")
need_mp4box = pytest.mark.skipif(not (HAVE_FFMPEG and HAVE_MP4BOX), reason="ffmpeg + MP4Box needed")


@pytest.fixture(autouse=True)
def reset_state(monkeypatch):
    d.NOTES.clear()
    d._TOOL_CACHE.clear()
    d._CFG_CACHE.clear()
    d._FF_CAPS.clear()
    monkeypatch.setattr(d, "PJ", False)
    yield


@pytest.fixture
def fake_tool(tmp_path):
    """Creates an executable file named `name` in a fresh folder and returns its path."""
    def make(name, folder=None):
        folder = Path(folder or tmp_path / "bin")
        folder.mkdir(parents=True, exist_ok=True)
        p = folder / (name + (".bat" if os.name == "nt" else ""))
        p.write_text("#!/bin/sh\nexit 0\n")
        p.chmod(0o755)
        return p
    return make


def run_cli(*args, cwd=None):
    """Runs dtv_tools.py as a subprocess; returns (returncode, stdout, events[@@DTV])."""
    import json
    r = subprocess.run([sys.executable, str(ROOT / "dtv_tools.py"), *map(str, args)], capture_output=True, text=True,
                       cwd=cwd, encoding="utf-8", errors="replace")
    events = [json.loads(ln[6:]) for ln in r.stdout.splitlines() if ln.startswith("@@DTV ")]
    return r.returncode, r.stdout + r.stderr, events


@pytest.fixture
def sample_mp4(tmp_path):
    """20 s, 25 fps H.264 with B-frames, one IDR every 2 s, AAC audio delayed by 0.5 s."""
    out = tmp_path / "src.mp4"
    raw = tmp_path / "raw.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=duration=20:size=320x240:rate=25",
                    "-itsoffset", "0.5", "-f", "lavfi", "-i", "sine=frequency=440:duration=20:sample_rate=48000",
                    "-c:v", "libx264", "-bf", "2", "-g", "50", "-keyint_min", "50", "-sc_threshold", "0",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "64k", str(raw)], check=True)
    subprocess.run(["MP4Box", "-add", str(raw), "-new", str(out)], check=True, capture_output=True)
    return out
