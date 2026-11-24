#!/usr/bin/env python3
"""Gera o executável do DTV Tools (interface + dtv_tools.py) com PyInstaller, SEM embutir ffmpeg/MP4Box/mpegh*.

Uso (a partir de qualquer pasta):
    python gui/packaging/build.py              # build + teste de fumaça + pacote em release/
    python gui/packaging/build.py --no-test    # pula o teste de fumaça
    python gui/packaging/build.py --no-package # só gera dist/
    python gui/packaging/build.py --version 1.2.2
    python gui/packaging/build.py --onedir     # pasta DTV-Tools/ com _internal/ em vez de um único arquivo
    python gui/packaging/build.py --tools DIR  # (opcional) embute os programas de DIR como tools/

Windows/Linux: um único arquivo (dist/DTV-Tools.exe ou dist/DTV-Tools); macOS: DTV-Tools.app (sempre uma pasta por dentro).
Saída: dist/…  e  release/DTV-Tools-<versão>-<plataforma>.{exe,zip,tar.gz,dmg} + .sha256
Os programas externos são configurados pelo usuário na tela "Ferramentas e pastas" (ou ficam em tools/ ao lado do app).
"""
from __future__ import annotations

import argparse, hashlib, os, platform, shutil, subprocess, sys, tarfile, zipfile
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
    ap.add_argument("--no-package", action="store_true")
    ap.add_argument("--keep", action="store_true", help="não apaga build/ e dist/ antes")
    a = ap.parse_args()

    try:
        import PyInstaller  # noqa: F401
        import PySide6  # noqa: F401
    except ImportError:
        print("Instale antes:  pip install -r gui/requirements.txt pyinstaller", file=sys.stderr)
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
    if not a.no_test:
        smoke(exe)
    if not a.no_package:
        package(a.version, a.onedir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
