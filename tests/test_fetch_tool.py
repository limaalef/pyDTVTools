"""Assisted download of programs: SHA-256, licence confirmation, safe unpacking and where the executables end up."""
import functools
import hashlib
import http.server
import io
import json
import os
import subprocess
import sys
import tarfile
import threading
import zipfile
from pathlib import Path

import pytest

import dtv_tools as d
from conftest import ROOT

SCRIPT = str(ROOT / "dtv_tools.py")
EXE = ".exe" if os.name == "nt" else ""


def make_zip(path: Path, files: dict):
    with zipfile.ZipFile(path, "w") as z:
        for n, body in files.items():
            z.writestr(n, body)
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def server(tmp_path):
    root = tmp_path / "srv"
    root.mkdir()
    h = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(root))
    h.log_message = lambda *a, **k: None
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), h)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield root, f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def run(*args):
    r = subprocess.run([sys.executable, SCRIPT, "fetch-tool", *args, "--json"], capture_output=True, text=True, timeout=120)
    evs = [json.loads(x) for x in r.stdout.splitlines() if x.startswith("{")]
    err = " ".join(json.dumps(e, ensure_ascii=False) for e in evs if e["type"] == "error")
    return r.returncode, evs, err


def pack(root):
    sha = make_zip(root / "ff.zip", {f"ffmpeg-x/bin/ffmpeg{EXE}": "#!/bin/sh\n", f"ffmpeg-x/bin/ffplay{EXE}": "#!/bin/sh\n", "ffmpeg-x/LICENSE": "GPL"})
    return sha


def test_download_verify_unpack_and_find(server, tmp_path):
    root, base = server
    sha = pack(root)
    dest = tmp_path / "inst"
    rc, evs, err = run("ffmpeg", "--url", f"{base}/ff.zip", "--sha256", sha, "--accept-license", "--dest", str(dest))
    assert rc == 0, err
    res = next(e for e in evs if e["type"] == "result")["installed"]
    assert res["verified"] and res["sha256"] == sha and Path(res["path"]).is_file()
    assert set(res["found"]) == {"ffmpeg", "ffplay"} and os.access(res["path"], os.X_OK)
    assert (dest / "ffmpeg" / ".dtv_fetch.json").is_file() and not list((dest / ".download").glob("*.zip"))
    assert any(e["type"] == "progress" and e["stage"] == "download" for e in evs)


def test_wrong_hash_discards_the_file(server, tmp_path):
    root, base = server
    pack(root)
    dest = tmp_path / "inst"
    rc, evs, err = run("ffmpeg", "--url", f"{base}/ff.zip", "--sha256", "0" * 64, "--accept-license", "--dest", str(dest))
    assert rc != 0 and "diferente do esperado" in err
    assert not (dest / "ffmpeg").exists() and not list((dest / ".download").glob("*"))


def test_requires_license_confirmation_and_a_hash(server, tmp_path):
    root, base = server
    sha = pack(root)
    rc, _, err = run("ffmpeg", "--url", f"{base}/ff.zip", "--sha256", sha, "--dest", str(tmp_path / "i"))
    assert rc != 0 and "--accept-license" in err and "GPL" not in "" and not (tmp_path / "i").exists()
    rc, _, err = run("ffmpeg", "--url", f"{base}/ff.zip", "--accept-license", "--dest", str(tmp_path / "i"))
    assert rc != 0 and "SHA-256" in err and "--allow-unverified" in err
    rc, evs, err = run("ffmpeg", "--url", f"{base}/ff.zip", "--accept-license", "--allow-unverified", "--dest", str(tmp_path / "i"))
    assert rc == 0, err
    assert next(e for e in evs if e["type"] == "result")["installed"]["verified"] is False


def test_hash_read_from_checksum_file(server, tmp_path):
    root, base = server
    sha = pack(root)
    (root / "checksums.sha256").write_text(f"{'a' * 64}  other.zip\n{sha}  ff.zip\n")
    rc, evs, err = run("ffmpeg", "--url", f"{base}/ff.zip", "--sha256-url", f"{base}/checksums.sha256", "--accept-license", "--dest", str(tmp_path / "i"))
    assert rc == 0, err
    (root / "checksums.sha256").write_text(f"{'a' * 64}  other.zip\n")
    rc, _, err = run("ffmpeg", "--url", f"{base}/ff.zip", "--sha256-url", f"{base}/checksums.sha256", "--accept-license", "--dest", str(tmp_path / "j"))
    assert rc != 0 and "não lista" in err


def test_unsafe_archive_is_refused(server, tmp_path):
    root, base = server
    sha = make_zip(root / "bad.zip", {"../evil.txt": "x", f"bin/ffmpeg{EXE}": "x"})
    rc, _, err = run("ffmpeg", "--url", f"{base}/bad.zip", "--sha256", sha, "--accept-license", "--dest", str(tmp_path / "i"))
    assert rc != 0 and "suspeito" in err and not (tmp_path / "evil.txt").exists() and not (tmp_path / "i" / "evil.txt").exists()


def test_tar_xz_and_missing_program(server, tmp_path):
    root, base = server
    with tarfile.open(root / "m.tar.xz", "w:xz") as t:
        data = b"#!/bin/sh\n"
        ti = tarfile.TarInfo(f"pkg/MP4Box{EXE}")
        ti.size, ti.mode = len(data), 0o755
        t.addfile(ti, io.BytesIO(data))
    sha = hashlib.sha256((root / "m.tar.xz").read_bytes()).hexdigest()
    rc, evs, err = run("mp4box", "--url", f"{base}/m.tar.xz", "--sha256", sha, "--accept-license", "--dest", str(tmp_path / "i"))
    assert rc == 0, err
    assert next(e for e in evs if e["type"] == "result")["installed"]["path"].endswith(f"MP4Box{EXE}")
    rc, _, err = run("ffmpeg", "--url", f"{base}/m.tar.xz", "--sha256", sha, "--accept-license", "--dest", str(tmp_path / "k"))
    assert rc != 0 and "Não achei" in err


def test_reinstall_replaces_only_its_own_folder(server, tmp_path):
    root, base = server
    sha = pack(root)
    dest = tmp_path / "i"
    assert run("ffmpeg", "--url", f"{base}/ff.zip", "--sha256", sha, "--accept-license", "--dest", str(dest))[0] == 0
    assert run("ffmpeg", "--url", f"{base}/ff.zip", "--sha256", sha, "--accept-license", "--dest", str(dest))[0] == 0
    (dest / "mp4box").mkdir()                                   # a folder this program did not create is never wiped
    rc, _, err = run("mp4box", "--url", f"{base}/ff.zip", "--sha256", sha, "--accept-license", "--dest", str(dest))
    assert rc != 0 and "já existe" in err


def test_https_only_and_no_auto_source_message(tmp_path):
    rc, _, err = run("ffmpeg", "--url", "http://example.com/x.zip", "--accept-license", "--dest", str(tmp_path))
    assert rc != 0 and "https" in err
    rc, _, err = run("mp4box", "--accept-license", "--dest", str(tmp_path))
    assert rc != 0 and "--url" in err


def test_helpers():
    assert d.parse_checksums("abc", "x") is None
    assert d.parse_checksums("A" * 64 + " *f.zip\n", "f.zip") == "a" * 64
    assert d.parse_checksums("b" * 64 + "\n", "any.zip") == "b" * 64
    assert d.url_allowed("https://x.org/a") and not d.url_allowed("http://x.org/a") and d.url_allowed("http://127.0.0.1:8/a")
    info = d.tool_source_info("ffmpeg")
    assert info["license"] and info["notice"] and {"ffmpeg", "ffplay"} <= set(info["provides"])
    assert d.tool_source_info("mp4box")["auto"] is False and d.tool_source_info("mp4box")["notice"]
    assert d.describe()["tools"]["ffplay"]["source"]["license_url"].startswith("https://")


def test_github_latest_release_with_published_digest(server, tmp_path, monkeypatch):
    root, base = server
    sha = make_zip(root / "pkg-win64-static-limaalef.zip", {f"ffmpeg{EXE}": "x", f"ffplay{EXE}": "x", f"mpeghDecoder{EXE}": "x", f"mpeghUIManager{EXE}": "x"})
    api = root / "repos" / "o" / "r" / "releases"
    api.mkdir(parents=True)
    (api / "latest").write_text(json.dumps({"tag_name": "ffmpeg-v8-20261125-limaalef", "assets": [
        {"name": "outro.txt", "browser_download_url": f"{base}/x.txt"},
        {"name": "pkg-win64-static-limaalef.zip", "browser_download_url": f"{base}/pkg-win64-static-limaalef.zip", "digest": "sha256:" + sha.upper()}]}))
    monkeypatch.setenv("DTV_GITHUB_API", base)
    url, got, tag = d.resolve_github_asset("o/r", r"win64-static-limaalef\.zip$", 10)
    assert url.endswith("limaalef.zip") and got == sha and tag.startswith("ffmpeg-v8")
    (api / "latest").write_text(json.dumps({"tag_name": "t", "assets": [{"name": "pkg-win64-static-limaalef.zip", "browser_download_url": f"{base}/pkg-win64-static-limaalef.zip"}]}))
    assert d.resolve_github_asset("o/r", r"win64-static-limaalef\.zip$", 10)[1] == ""        # no digest: the caller must demand a hash
    (api / "latest").write_text(json.dumps({"tag_name": "t", "assets": []}))
    with pytest.raises(d.Fail):
        d.resolve_github_asset("o/r", r"win64-static-limaalef\.zip$", 10)


def test_lima_package_covers_the_four_programs_in_one_folder(server, tmp_path, monkeypatch):
    root, base = server
    sha = make_zip(root / "pkg-win64-static-limaalef.zip", {f"ffmpeg{EXE}": "x", f"ffplay{EXE}": "x", f"mpeghDecoder{EXE}": "x", f"mpeghUIManager{EXE}": "x"})
    api = root / "repos" / "o" / "r" / "releases"
    api.mkdir(parents=True)
    (api / "latest").write_text(json.dumps({"tag_name": "t1", "assets": [{"name": "pkg-win64-static-limaalef.zip", "browser_download_url": f"{base}/pkg-win64-static-limaalef.zip", "digest": "sha256:" + sha}]}))
    monkeypatch.setitem(d.TOOL_SOURCES["mpegh_decoder"]["assets"], d.source_platform_key(), dict(d._LIMA, github="o/r"))
    monkeypatch.setenv("DTV_GITHUB_API", base)
    env = dict(os.environ, DTV_GITHUB_API=base, PYTHONPATH=str(ROOT))
    code = ("import sys,dtv_tools as d; d.TOOL_SOURCES['mpegh_decoder']['assets'][d.source_platform_key()]=dict(d._LIMA, github='o/r'); "
            "sys.argv=['x','fetch-tool','mpegh_decoder','--accept-license','--dest',sys.argv[1],'--json']; sys.exit(d.main())")
    r = subprocess.run([sys.executable, "-c", code, str(tmp_path / "i")], capture_output=True, text=True, env=env, timeout=120)
    evs = [json.loads(x) for x in r.stdout.splitlines() if x.startswith("{")]
    res = next(e for e in evs if e["type"] == "result")["installed"]
    assert r.returncode == 0 and res["verified"] and set(res["found"]) == {"ffmpeg", "ffplay", "mpegh_ui", "mpegh_decoder"}
    assert Path(res["dir"]).name == "ffmpeg-lcevc-mpegh"


def test_catalog_for_windows_is_the_lima_package():
    assert d.TOOL_SOURCES["mpegh_ui"]["assets"]["win64"]["github"] == "limaalef/ffmpeg-autobuild-lcevc-mpegh"
    for k in ("ffmpeg", "ffplay", "mpegh_ui", "mpegh_decoder"):
        assert set(d.TOOL_SOURCES[k]["provides"]) == {"ffmpeg", "ffplay", "mpegh_ui", "mpegh_decoder"}
        assert d.TOOL_SOURCES[k]["assets"]["win64"]["asset"].endswith("limaalef\\.zip$")
