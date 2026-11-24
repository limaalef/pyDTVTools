"""A lógica de assinatura do build.py (sem certificados reais: usa comandos de mentira)."""
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("dtv_build", ROOT / "gui" / "packaging" / "build.py")
build = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build)

SIGN_ENV = ("DTV_SIGN_COMMAND", "DTV_SIGN_PFX", "DTV_SIGN_PFX_BASE64", "DTV_SIGN_PASSWORD", "DTV_MAC_IDENTITY",
            "DTV_NOTARY_APPLE_ID", "DTV_NOTARY_TEAM_ID", "DTV_NOTARY_PASSWORD")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for k in SIGN_ENV:
        monkeypatch.delenv(k, raising=False)


def test_without_certificate_nothing_is_signed(tmp_path):
    f = tmp_path / "app.exe"
    f.write_bytes(b"x")
    assert build.sign_configured() is False
    assert build.sign_file(f) is False


def test_custom_command_runs_with_the_file(tmp_path, monkeypatch):
    f = tmp_path / "DTV Tools.exe"
    f.write_bytes(b"x")
    marker = tmp_path / "marker.txt"
    script = tmp_path / "sign.py"
    script.write_text("import sys, pathlib\npathlib.Path(sys.argv[2]).write_text(sys.argv[1])\n")
    monkeypatch.setenv("DTV_SIGN_COMMAND", f'"{sys.executable}" "{script}" "{{file}}" "{marker}"')
    assert build.sign_configured() is True
    assert build.sign_file(f) is True
    assert marker.read_text() == str(f)


def test_failing_sign_command_stops_the_build(tmp_path, monkeypatch):
    monkeypatch.setenv("DTV_SIGN_COMMAND", f'"{sys.executable}" -c "import sys; sys.exit(3)"')
    with pytest.raises(Exception):
        build.sign_file(tmp_path / "x.exe")


def test_windows_signtool_command():
    c = build.windows_sign_cmd("signtool.exe", "c.pfx", "pw", "http://ts.example", "a.exe")
    assert c[:2] == ["signtool.exe", "sign"] and c[c.index("/f") + 1] == "c.pfx" and c[c.index("/fd") + 1] == "SHA256"
    assert c[c.index("/tr") + 1] == "http://ts.example" and c[c.index("/td") + 1] == "SHA256" and c[-1] == "a.exe"


def test_notarization_needs_all_the_variables(tmp_path, monkeypatch):
    monkeypatch.setenv("DTV_MAC_IDENTITY", "Developer ID Application: X (ABC)")
    monkeypatch.setenv("DTV_NOTARY_APPLE_ID", "a@b.c")
    assert build.notarize(tmp_path / "x.dmg") is False          # faltam o time e a senha (e, fora do macOS, nunca roda)


def test_spec_only_signs_macos_when_asked():
    s = (ROOT / "gui" / "packaging" / "dtv_tools_gui.spec").read_text()
    assert 'os.environ.get("DTV_MAC_IDENTITY") or None' in s and "codesign_identity=MAC_ID" in s
    assert (ROOT / "gui" / "packaging" / "entitlements.plist").is_file()


def test_workflow_signing_is_optional():
    wf = (ROOT / ".github" / "workflows" / "build.yml").read_text()
    for name in ("WINDOWS_CERT_PFX_BASE64", "MACOS_CERT_P12_BASE64", "MACOS_NOTARY_PASSWORD", "REQUIRE_SIGNING", "attest-build-provenance"):
        assert name in wf
    assert "--require-signing" in wf and "id-token: write" in wf
