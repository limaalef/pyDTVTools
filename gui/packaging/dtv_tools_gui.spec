# -*- mode: python ; coding: utf-8 -*-
# PyInstaller (a partir da raiz do projeto): pyinstaller gui/packaging/dtv_tools_gui.spec --noconfirm
#   DTV_TOOLS_SRC=<pasta>   (opcional) pasta com MP4Box/ffmpeg/mpegh* a embutir como tools/
#   DTV_VERSION=1.0.0       (opcional) versão exibida no bundle do macOS
import os
import sys
from pathlib import Path

GUI = Path(SPECPATH).resolve().parent      # gui/
ROOT = GUI.parent                           # raiz do projeto (dtv_tools.py)
NAME = "DTV-Tools"
VERSION = os.environ.get("DTV_VERSION", "1.2.1")

datas = [(str(GUI / "packaging" / "icon.png"), "packaging"), (str(GUI / "icons"), "icons")]
tools_src = os.environ.get("DTV_TOOLS_SRC", "")
if tools_src and Path(tools_src).is_dir() and any(Path(tools_src).iterdir()):
    datas.append((tools_src, "tools"))

icon = str(GUI / "packaging" / ("icon.ico" if sys.platform == "win32" else "icon.icns" if sys.platform == "darwin" else "icon.png"))

a = Analysis(
    [str(GUI / "dtv_gui.py")],
    pathex=[str(GUI), str(ROOT)],
    datas=datas,
    # módulos que o dtv_tools importa de forma opcional/tardia (doctor usa __import__)
    hiddenimports=["dtv_tools", "cutmath", "requests", "rich", "numpy", "PIL", "PIL.Image", "PIL.ImageCms"],
    excludes=["tkinter", "unittest", "test", "pydoc_data", "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets",
              "PySide6.Qt3DCore", "PySide6.QtQuick", "PySide6.QtQml", "PySide6.QtMultimedia", "PySide6.QtPdf"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name=NAME,
    console=False,            # GUI sem janela de terminal; o modo --cli reabre stdout/stdin (dtv_tools._fix_stdio)
    icon=icon,
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name=NAME, upx=False)

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name=NAME + ".app",
        icon=icon,
        bundle_identifier="br.dtv.tools",
        info_plist={
            "CFBundleName": "DTV Tools",
            "CFBundleDisplayName": "DTV Tools",
            "CFBundleShortVersionString": VERSION,
            "CFBundleVersion": VERSION,
            "NSHighResolutionCapable": True,
            "NSRequiresAquaSystemAppearance": False,   # segue claro/escuro do sistema
            "LSMinimumSystemVersion": "12.0",
        },
    )
