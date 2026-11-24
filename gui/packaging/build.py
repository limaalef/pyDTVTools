#!/usr/bin/env python3
"""Gera o executável do DTV Tools (interface + dtv_tools.py) com PyInstaller, SEM embutir ffmpeg/MP4Box/mpegh*.

Uso (a partir de qualquer pasta):
    python gui/packaging/build.py              # build + teste de fumaça + pacote em release/
    python gui/packaging/build.py --no-test    # pula o teste de fumaça
    python gui/packaging/build.py --no-package # só gera dist/
    python gui/packaging/build.py --version 1.2.2
    python gui/packaging/build.py --onedir     # pasta DTV-Tools/ com _internal/ em vez de um único arquivo
    python gui/packaging/build.py --tools DIR  # (opcional) embute os programas de DIR como tools/
    python gui/packaging/build.py --require-signing   # falha se não houver certificado configurado

Assinatura (opcional; sem certificado o executável sai sem assinatura). Tudo por variáveis de ambiente:
  Windows   DTV_SIGN_PFX=<arquivo.pfx> ou DTV_SIGN_PFX_BASE64=<conteúdo em base64>, DTV_SIGN_PASSWORD, [DTV_SIGN_TIMESTAMP=<url>]
            (usa o signtool do Windows SDK) -- ou DTV_SIGN_COMMAND='comando "{file}"' para qualquer outra ferramenta (Azure Trusted Signing etc.)
  macOS     DTV_MAC_IDENTITY="Developer ID Application: Nome (TEAMID)" (o certificado já no chaveiro) e, para notarizar,
            DTV_NOTARY_APPLE_ID, DTV_NOTARY_TEAM_ID, DTV_NOTARY_PASSWORD (senha específica de app)
  qualquer  DTV_SIGN_COMMAND roda para o executável (Windows/Linux) em qualquer sistema

Windows/Linux: um único arquivo (dist/DTV-Tools.exe ou dist/DTV-Tools); macOS: DTV-Tools.app (sempre uma pasta por dentro).
Saída: dist/…  e  release/DTV-Tools-<versão>-<plataforma>.{exe,zip,tar.gz,dmg} + .sha256
Os programas externos são configurados pelo usuário na tela "Ferramentas e pastas" (ou ficam em tools/ ao lado do app).
"""
from __future__ import annotations

import argparse, base64, glob, hashlib, os, platform, shutil, subprocess, sys, tarfile, tempfile, zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPEC = ROOT / "gui" / "packaging" / "dtv_tools_gui.spec"
NAME = "DTV-Tools"


def plat() -> str:
    m = platform.machine().lower()
    arch = "arm64" if m in ("arm64", "aarch64") else "x64"
    os_ = {"win32": "windows", "darwin": "macos"}.get(sys.platform, "linux")
    return f"{os_}-{arch}"


def run(cmd, **kw):
    print("+", " ".join(map(str, cmd)), flush=True)
    return subprocess.run(cmd, check=True, cwd=ROOT, **kw)


def find_signtool() -> str | None:
    found = shutil.which("signtool")
    if found:
        return found
    cands = sorted(glob.glob(r"C:\Program Files (x86)\Windows Kits\10\bin\*\x64\signtool.exe"))
    return cands[-1] if cands else None


def windows_sign_cmd(signtool: str, pfx: str, password: str, timestamp: str, target) -> list:
    return [signtool, "sign", "/f", pfx, "/p", password, "/fd", "SHA256", "/tr", timestamp, "/td", "SHA256", "/d", "DTV Tools", str(target)]


def sign_configured() -> bool:
    if os.environ.get("DTV_SIGN_COMMAND"):
        return True
    if sys.platform == "win32":
        return bool(os.environ.get("DTV_SIGN_PFX") or os.environ.get("DTV_SIGN_PFX_BASE64"))
    if sys.platform == "darwin":
        return bool(os.environ.get("DTV_MAC_IDENTITY"))
    return False


def sign_file(target: Path) -> bool:
    """Assina um arquivo (Windows: signtool com PFX; qualquer sistema: DTV_SIGN_COMMAND). False = nada configurado."""
    custom = os.environ.get("DTV_SIGN_COMMAND")
    if custom:
        cmd = custom.replace("{file}", str(target))
        print("+ (DTV_SIGN_COMMAND)", cmd, flush=True)
        subprocess.run(cmd, shell=True, check=True, cwd=ROOT)
        return True
    if sys.platform != "win32":
        return False
    pfx, b64, pw = os.environ.get("DTV_SIGN_PFX"), os.environ.get("DTV_SIGN_PFX_BASE64"), os.environ.get("DTV_SIGN_PASSWORD", "")
    if not (pfx or b64):
        return False
    tool = find_signtool()
    if not tool:
        raise SystemExit("signtool não encontrado (instale o Windows SDK ou ponha o signtool.exe no PATH).")
    tmp = None
    try:
        if not pfx:
            fd, tmp = tempfile.mkstemp(suffix=".pfx")
            with os.fdopen(fd, "wb") as f:
                f.write(base64.b64decode(b64))
            pfx = tmp
        ts = os.environ.get("DTV_SIGN_TIMESTAMP", "http://timestamp.digicert.com")
        subprocess.run(windows_sign_cmd(tool, pfx, pw, ts, target), check=True, cwd=ROOT)
        subprocess.run([tool, "verify", "/pa", str(target)], check=True, cwd=ROOT)
    finally:
        if tmp:
            try:
                os.remove(tmp)
            except OSError:
                pass
    return True


def sign_app(exe: Path) -> bool:
    """Assina o executável gerado. No macOS o PyInstaller já assinou o .app (DTV_MAC_IDENTITY); aqui só se confere."""
    if sys.platform == "darwin":
        ident = os.environ.get("DTV_MAC_IDENTITY")
        if not ident:
            return sign_file(exe)
        app = ROOT / "dist" / f"{NAME}.app"
        run(["codesign", "--verify", "--deep", "--strict", "--verbose=2", app])
        return True
    return sign_file(exe)


def notarize(dmg: Path) -> bool:
    """macOS: assina o .dmg, envia à Apple (notarytool) e grampeia o resultado. Exige as três variáveis DTV_NOTARY_*."""
    ident = os.environ.get("DTV_MAC_IDENTITY")
    aid, team, pw = (os.environ.get(k) for k in ("DTV_NOTARY_APPLE_ID", "DTV_NOTARY_TEAM_ID", "DTV_NOTARY_PASSWORD"))
    if sys.platform != "darwin" or not (ident and aid and team and pw):
        return False
    run(["codesign", "--force", "--timestamp", "--sign", ident, dmg])
    run(["xcrun", "notarytool", "submit", dmg, "--apple-id", aid, "--team-id", team, "--password", pw, "--wait"])
    run(["xcrun", "stapler", "staple", dmg])
    run(["xcrun", "stapler", "validate", dmg])
    return True


def exe_path(onedir: bool) -> Path:
    if sys.platform == "darwin":
        return ROOT / "dist" / f"{NAME}.app" / "Contents" / "MacOS" / NAME
    ext = ".exe" if sys.platform == "win32" else ""
    return ROOT / "dist" / (NAME if onedir else "") / f"{NAME}{ext}"


def smoke(exe: Path) -> None:
    env = dict(os.environ, QT_QPA_PLATFORM=os.environ.get("QT_QPA_PLATFORM", "offscreen" if sys.platform == "linux" else ""))
    env = {k: v for k, v in env.items() if v != ""}
    run([exe, "--selftest"], env=env, timeout=120)
    out = subprocess.run([str(exe), "--cli", "doctor", "--json"], cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                         capture_output=True, text=True, timeout=180)
    text = out.stdout + out.stderr
    assert '"type":"hello"' in text.replace(" ", "") and '"type":"result"' in text.replace(" ", ""), "doctor --json sem eventos:\n" + text
    desc = subprocess.run([str(exe), "--cli", "describe"], cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                          capture_output=True, text=True, timeout=120).stdout
    assert '"protocol": 2' in desc, "describe sem protocolo 2"
    print("teste de fumaça: ok")


def package(version: str, onedir: bool) -> Path:
    rel = ROOT / "release"
    rel.mkdir(exist_ok=True)
    base = f"{NAME}-{version}-{plat()}"
    if not onedir and sys.platform == "win32":
        out = rel / f"{base}.exe"                                      # arquivo único: entregue como está
        shutil.copy2(exe_path(False), out)
    elif not onedir and sys.platform != "darwin":
        out = rel / f"{base}.tar.gz"                                   # no Linux o tar.gz preserva a permissão de execução
        with tarfile.open(out, "w:gz") as t:
            t.add(exe_path(False), arcname=NAME)
    elif sys.platform == "win32":
        out = rel / f"{base}.zip"
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            for p in (ROOT / "dist" / NAME).rglob("*"):
                z.write(p, p.relative_to(ROOT / "dist"))
    elif sys.platform == "darwin":
        out = rel / f"{base}.dmg"
        run(["hdiutil", "create", "-volname", "DTV Tools", "-srcfolder", f"dist/{NAME}.app", "-ov", "-format", "UDZO", out])
    else:
        out = rel / f"{base}.tar.gz"
        with tarfile.open(out, "w:gz") as t:
            t.add(ROOT / "dist" / NAME, arcname=NAME)
    if out.suffix == ".dmg":
        print("notarização:", "ok" if notarize(out) else "pulada (DTV_NOTARY_* não definidas)")
    digest = hashlib.sha256(out.read_bytes()).hexdigest()
    (rel / f"{base}.sha256").write_text(f"{digest}  {out.name}\n")
    print("pacote:", out, f"({out.stat().st_size / 1e6:.1f} MB)")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", default=os.environ.get("DTV_VERSION", "1.2.2"))
    ap.add_argument("--tools", help="pasta com programas a embutir (opcional; por padrão nada é embutido)")
    ap.add_argument("--onedir", action="store_true", help="pasta com _internal/ em vez de arquivo único (Windows/Linux)")
    ap.add_argument("--no-test", action="store_true")
    ap.add_argument("--require-signing", action="store_true", help="falha se nenhum certificado de assinatura estiver configurado")
    ap.add_argument("--no-package", action="store_true")
    ap.add_argument("--keep", action="store_true", help="não apaga build/ e dist/ antes")
    a = ap.parse_args()

    try:
        import PyInstaller  # noqa: F401
        import PySide6  # noqa: F401
    except ImportError:
        print("Instale antes:  pip install -r gui/requirements.txt pyinstaller", file=sys.stderr)
        return 2
    if a.require_signing and not sign_configured():
        print("--require-signing: nenhum certificado configurado (veja a ajuda: DTV_SIGN_*, DTV_MAC_IDENTITY).", file=sys.stderr)
        return 2
    if not a.keep:
        for d in ("build", "dist"):
            shutil.rmtree(ROOT / d, ignore_errors=True)
    env = dict(os.environ, DTV_VERSION=a.version)
    env.pop("DTV_TOOLS_SRC", None)
    env.pop("DTV_ONEDIR", None)
    if a.onedir:
        env["DTV_ONEDIR"] = "1"
    if a.tools:
        env["DTV_TOOLS_SRC"] = str(Path(a.tools).resolve())
    run([sys.executable, "-m", "PyInstaller", str(SPEC), "--noconfirm", "--distpath", "dist", "--workpath", "build"], env=env)
    exe = exe_path(a.onedir)
    if not exe.exists():
        print("executável não encontrado:", exe, file=sys.stderr)
        return 1
    print("assinatura:", "ok" if sign_app(exe) else "nenhum certificado configurado: executável sem assinatura")
    if not a.no_test:                                                  # depois de assinar: garante que a assinatura não quebrou o executável
        smoke(exe)
    if not a.no_package:
        package(a.version, a.onedir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
