import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

import dtv_tools as d
from conftest import ROOT


@pytest.mark.parametrize("latest,current,newer", [
    ("1.2.3", "1.2.2", True), ("v1.3", "1.2.9", True), ("1.2.10", "1.2.9", True), ("2.0.0", "1.99.99", True),
    ("1.2.2", "1.2.2", False), ("1.2.1", "1.2.2", False), ("1.2.2.0", "1.2.2", False),
    ("1.3.0-rc1", "1.2.2", True), ("1.2.2", "1.2.2-rc1", True), ("1.2.2-rc1", "1.2.2", False),
    ("lixo", "1.2.2", False), ("", "1.2.2", False),
])
def test_version_compare(latest, current, newer):
    assert d.version_newer(latest, current) is newer


def test_pick_asset_by_platform():
    assets = [{"name": n} for n in ("DTVTools-1.3.0-windows-x64.exe", "DTVTools-1.3.0-windows-x64.exe.sha256",
                                    "DTVTools-1.3.0-linux-x64", "DTVTools-1.3.0-macos-arm64.zip", "notes.txt")]
    assert d.pick_asset(assets, "win32")["name"].endswith("windows-x64.exe")
    assert d.pick_asset(assets, "linux")["name"].endswith("linux-x64")
    assert d.pick_asset(assets, "darwin")["name"].endswith("macos-arm64.zip")
    assert d.pick_asset([{"name": "src.tar.gz"}], "linux") is None


def _server(code, body):
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass
    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_port}/rel"


def _check(url, *extra):
    p = subprocess.run([sys.executable, str(ROOT / "dtv_tools.py"), "check-update", "--api-url", url, "--json", *extra],
                       capture_output=True, text=True, timeout=60)
    ev = [json.loads(x) for x in p.stdout.splitlines() if x.startswith("{")]
    return p.returncode, ev


def test_check_update_new_version():
    srv, url = _server(200, {"tag_name": "v9.0.0", "html_url": "https://github.com/x/y/releases/tag/v9.0.0", "body": "novidades",
                             "published_at": "2030-01-01T00:00:00Z",
                             "assets": [{"name": "app-linux", "size": 5, "browser_download_url": "https://x/app-linux"}]})
    try:
        rc, ev = _check(url)
    finally:
        srv.shutdown()
    assert rc == 0
    res = next(e for e in ev if e["type"] == "result")["update"]
    assert res["update_available"] and res["latest"] == "9.0.0" and res["current"] == d.__version__
    assert res["notes"] == "novidades" and res["assets"][0]["name"] == "app-linux"


def test_check_update_already_current():
    srv, url = _server(200, {"tag_name": "v" + d.__version__, "html_url": "u", "assets": []})
    try:
        rc, ev = _check(url)
    finally:
        srv.shutdown()
    assert rc == 0 and next(e for e in ev if e["type"] == "result")["update"]["update_available"] is False


@pytest.mark.parametrize("code,txt", [(404, "Nenhuma versão"), (403, "limitou")])
def test_check_update_http_errors(code, txt):
    srv, url = _server(code, {})
    try:
        rc, ev = _check(url)
    finally:
        srv.shutdown()
    assert rc == 2 and txt in next(e for e in ev if e["type"] == "error")["message"]


def test_check_update_bad_tag_and_repo():
    srv, url = _server(200, {"tag_name": "nightly"})
    try:
        rc, ev = _check(url)
    finally:
        srv.shutdown()
    assert rc == 2
    p = subprocess.run([sys.executable, str(ROOT / "dtv_tools.py"), "check-update", "--repo", "sem barra", "--json"],
                       capture_output=True, text=True, timeout=30)
    assert p.returncode == 2 and "inválido" in p.stdout
