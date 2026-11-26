"""Acesso via ADB (comando `adb-access` do núcleo): funções puras, adb falso + API local do player, e a remoção do recurso na compilação."""
import ast
import http.server
import json
import os
import subprocess
import sys
import threading
import time

import pytest

import dtv_tools as d
from conftest import ROOT

SCRIPT = str(ROOT / "dtv_tools.py")
posix = pytest.mark.skipif(os.name == "nt", reason="o adb falso é um script posix")

DEVICES = """List of devices attached
192.168.0.20:5555      device product:tv model:TV3 device:tv transport_id:1
R58M123ABC             unauthorized usb:1-1 transport_id:2
emulator-5554          offline

"""
PROC = """  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode
   0: 0100007F:AC62 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 11 1
   1: 0100007F:1F90 0100007F:D000 01 00000000:00000000 00:00000000 00000000  1000        0 12 1
   2: 00000000:1388 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 13 1
"""


def test_serial_and_devices():
    assert d.adb_serial("192.168.0.20") == ("192.168.0.20:5555", True)
    assert d.adb_serial("192.168.0.20:6000") == ("192.168.0.20:6000", True)
    assert d.adb_serial("R58M123ABC") == ("R58M123ABC", False)
    dv = d.parse_adb_devices(DEVICES)
    assert [(x["serial"], x["state"]) for x in dv] == [("192.168.0.20:5555", "device"), ("R58M123ABC", "unauthorized"), ("emulator-5554", "offline")]
    assert d.parse_adb_devices("* daemon started successfully *\nList of devices attached\n") == []


def test_media_url_forward_list_and_ports():
    assert d.media_local_url("http://127.0.0.1:34567/ch1/manifest.mpd?t=9") == (34567, "http://127.0.0.1:34567/ch1/manifest.mpd?t=9")
    with pytest.raises(d.Fail):
        d.media_local_url("http://tv/a.mpd")
    assert d.parse_forward_list("S1 tcp:44642 tcp:44642\nS2 tcp:1 tcp:2\n")[1] == {"serial": "S2", "local": "tcp:1", "remote": "tcp:2"}
    assert d.parse_proc_net_tcp(PROC) == [("127.0.0.1", 0xAC62), ("0.0.0.0", 0x1388)]       # só LISTEN (0A)
    assert d.parse_netstat_listen("tcp 0 0 127.0.0.1:44642 0.0.0.0:* LISTEN\nudp 0 0 0.0.0.0:53 0.0.0.0:*\nLISTEN 0 4 *:8080 *:*\n")[:1] == [("127.0.0.1", 44642)]


def test_find_adb(tmp_path, monkeypatch):
    exe = tmp_path / ("adb.exe" if sys.platform == "win32" else "adb")
    exe.write_text("x")
    assert d.find_adb(str(exe)) == str(exe) and d.find_adb(str(tmp_path / "nope")) == ""
    monkeypatch.setenv("PATH", str(tmp_path / "vazio"))
    monkeypatch.setenv("ANDROID_HOME", str(tmp_path / "sdk"))
    (tmp_path / "sdk" / "platform-tools").mkdir(parents=True)
    sdk = tmp_path / "sdk" / "platform-tools" / exe.name
    sdk.write_text("x")
    assert d.find_adb("") == str(sdk)


# ───────── adb falso + API local do player ─────────
class Env:
    def __init__(self, tmp):
        self.dir = tmp
        self.fake = str(tmp / "adb")
        (tmp / "state").write_text("device\n")
        self.media = http.server.ThreadingHTTPServer(("127.0.0.1", 0), self._mh())
        self.cur = {"media": ""}
        self.api = http.server.ThreadingHTTPServer(("127.0.0.1", 0), self._ah())
        for sv in (self.media, self.api):
            threading.Thread(target=sv.serve_forever, daemon=True).start()
        self.mport, self.aport = self.media.server_address[1], self.api.server_address[1]
        self.set_media("/ch1/manifest.mpd")
        (tmp / "adb").write_text(f"""#!{sys.executable}
import os, sys
d = {str(tmp)!r}
a = sys.argv[1:]
if a[:1] == ["-s"]: a = a[2:]
p = lambda n: os.path.join(d, n)
rd = lambda n, df="": open(p(n)).read() if os.path.exists(p(n)) else df
if a[:1] == ["devices"]:
    print("List of devices attached"); st = rd("state", "device").strip()
    if st != "none": print("192.168.0.20:5555      " + st + " product:tv model:TV3")
elif a[:1] == ["connect"]: print("connected to " + a[1])
elif a[:2] == ["forward", "--remove"]:
    open(p("removed"), "a").write(a[2] + "\\n")
elif a[:2] == ["forward", "--list"]:
    for ln in rd("forwards").splitlines(): print("192.168.0.20:5555 " + ln)
elif a[:1] == ["forward"]:
    open(p("forwards"), "a").write(a[1] + " " + a[2] + "\\n")
elif a[:2] == ["shell", "cat"]: sys.stdout.write(rd("proc"))
else: sys.exit(1)
""")
        os.chmod(self.fake, 0o755)

    def _mh(self):
        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(s):
                body = b'<?xml version="1.0"?><MPD type="dynamic"></MPD>'
                s.send_response(200); s.send_header("Content-Length", str(len(body))); s.end_headers(); s.wfile.write(body)
            def log_message(s, *a): pass
        return H

    def _ah(self):
        env = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(s):
                if s.path != "/tv3/mediaplayers/1":
                    s.send_response(404); s.send_header("Content-Length", "0"); s.end_headers(); return
                body = json.dumps({"currentMedia": env.cur["media"], "state": "playing", "currTime": 12.5, "duration": 0}).encode()
                s.send_response(200); s.send_header("Content-Length", str(len(body))); s.end_headers(); s.wfile.write(body)
            def log_message(s, *a): pass
        return H

    def set_media(self, path):
        self.cur["media"] = f"http://127.0.0.1:{self.mport}{path}" if path else ""

    def args(self, *extra):
        return [sys.executable, SCRIPT, "adb-access", "192.168.0.20", "--adb", self.fake, "--api-port", str(self.aport), "--interval", "1", "--json", *extra]

    def removed(self):
        f = self.dir / "removed"
        return f.read_text() if f.exists() else ""

    def close(self):
        for sv in (self.media, self.api):
            threading.Thread(target=sv.shutdown, daemon=True).start()


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    yield e
    e.close()


def events(out):
    res = []
    for ln in out.splitlines():
        try:
            res.append(json.loads(ln))
        except ValueError:
            pass
    return res


@posix
def test_one_shot_reports_address_and_cleans_up(env):
    r = subprocess.run(env.args(), capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
    ev = [e for e in events(r.stdout) if e.get("type") == "adb"]
    assert ev and ev[-1]["device_state"] == "device" and ev[-1]["api_ok"] is True
    assert ev[-1]["local_url"] == f"http://127.0.0.1:{env.mport}/ch1/manifest.mpd" and ev[-1]["manifest"] == "mpd"
    assert ev[-1]["player"]["state"] == "playing"
    fw = (env.dir / "forwards").read_text()
    assert f"tcp:{env.aport} tcp:{env.aport}" in fw and f"tcp:{env.mport} tcp:{env.mport}" in fw
    assert f"tcp:{env.mport}" in env.removed() and f"tcp:{env.aport}" in env.removed()      # removidos ao terminar


@posix
def test_watch_follows_channel_change_and_stops(env):
    p = subprocess.Popen(env.args("--watch", "--control"), stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    seen = []
    new_port = None

    def wait_for(cond, secs=30):
        end = time.time() + secs
        while time.time() < end:
            ln = p.stdout.readline()
            if not ln:
                return None
            try:
                e = json.loads(ln)
            except ValueError:
                continue
            seen.append(e)
            if cond(e):
                return e
        return None
    try:
        first = wait_for(lambda e: e.get("type") == "adb" and e.get("local_url"))
        assert first and first["changed"] is False
        env.set_media("/ch2/manifest.mpd?t=9")
        ch = wait_for(lambda e: e.get("type") == "adb" and e.get("local_url", "").endswith("/ch2/manifest.mpd?t=9"))
        assert ch and ch["changed"] is True
        env.set_media("")
        empty = wait_for(lambda e: e.get("type") == "adb" and not e.get("media"))
        assert empty and "canal" in empty["action"]
        p.stdin.write(json.dumps({"cmd": "stop"}) + "\n"); p.stdin.flush()
        res = wait_for(lambda e: e.get("type") == "result")
        assert res and res["adb"]["serial"] == "192.168.0.20:5555"
        assert p.wait(timeout=20) == 0
    finally:
        if p.poll() is None:
            p.kill()
    assert f"tcp:{env.aport}" in env.removed()


@posix
def test_unauthorized_device_says_what_to_do_on_the_device(env):
    (env.dir / "state").write_text("unauthorized\n")
    r = subprocess.run(env.args("--connect-timeout", "2"), capture_output=True, text=True, timeout=60)
    assert r.returncode != 0
    ev = [e for e in events(r.stdout) if e.get("type") == "adb"]
    assert ev and ev[-1]["device_state"] == "unauthorized" and "Permitir" in ev[-1]["action"]


@posix
def test_scan_ports_lists_and_probes(env):
    (env.dir / "proc").write_text(PROC.replace("AC62", format(env.mport, "04X")).replace("1388", "0001"))
    r = subprocess.run(env.args("--scan-ports"), capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout
    ev = [e for e in events(r.stdout) if e.get("type") == "adb"][-1]
    assert {x["port"] for x in ev["ports"]} == {1}              # a porta da mídia e a da API são excluídas da varredura
    assert f"tcp:1 tcp:1" in (env.dir / "forwards").read_text() and "tcp:1" in env.removed()


def test_missing_adb_explains_where_to_get_it(tmp_path):
    r = subprocess.run([sys.executable, SCRIPT, "adb-access", "1.2.3.4", "--adb", str(tmp_path / "nao"), "--json"], capture_output=True, text=True, timeout=30)
    assert r.returncode != 0 and "platform-tools" in r.stdout + r.stderr


# ───────── fora da compilação ─────────
def test_build_strips_the_feature_from_core_and_gui():
    sys.path.insert(0, str(ROOT / "gui" / "packaging"))
    import prebuild
    core = (ROOT / "dtv_tools.py").read_text(encoding="utf-8")
    out = prebuild.strip(core, prebuild.REQUIRED["dtv_tools.py"])
    ast.parse(out)
    assert "adb-access" not in out and "cmd_adb_access" not in out and "find_adb" not in out and "BUILD-REMOVE" not in out
    assert "def cmd_sdr" in out and '"fetch-tool": cmd_fetch_tool' in out
    gui = (ROOT / "gui" / "dtv_gui.py").read_text(encoding="utf-8")
    out = prebuild.strip(gui, prebuild.REQUIRED["gui/dtv_gui.py"])
    ast.parse(out)
    assert "AdbPage" not in out and "Acesso via ADB" not in out and "StepRow" not in out and "BUILD-REMOVE" not in out
    assert 'add("dash", DashPage)' in out and "class NavDelegate" in out
    assert "gui/icons/adb.svg" in prebuild.REMOVE_FILES and not (ROOT / "gui" / "dtv_gui_adb.py").exists()
    with pytest.raises(RuntimeError):
        prebuild.strip("x = 1\n", ["-ADB"])
