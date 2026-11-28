"""Prepara a árvore para a compilação (PyInstaller): remove de dtv_gui.py e de dtv_tools.py os blocos BUILD-REMOVE (recursos fora da build) e apaga os arquivos que só eles usam.

Marcador: uma linha `# BUILD-REMOVE-START[-nome]` até a linha `# BUILD-REMOVE-END[-nome]` (mesmo nome), com qualquer recuo.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# arquivo -> marcadores que PRECISAM existir nele (se sumirem, a build falha em vez de publicar o recurso por engano)
REQUIRED = {
    "gui/dtv_gui.py": ["", "-2"],       # "" e -2: recursos antigos; -3 e -4: Acesso via ADB (menu e página)
    "dtv_tools.py": [""],                        # Acesso via ADB (comando, parser e registro)
}
REMOVE_FILES = ["gui/icons/adb.svg"]

_BLOCK = re.compile(r"^[ \t]*# BUILD-REMOVE-START(?P<n>(?:-\w+)?)(?![-\w])[^\n]*\n.*?^[ \t]*# BUILD-REMOVE-END(?P=n)(?![-\w])[^\n]*\n?", re.S | re.M)


def strip(code: str, required=()) -> str:
    for n in required:
        if f"# BUILD-REMOVE-START{n}" not in code or f"# BUILD-REMOVE-END{n}" not in code:
            raise RuntimeError(f"Marcadores não encontrados: BUILD-REMOVE-START{n} / BUILD-REMOVE-END{n}")
    return _BLOCK.sub("", code)


def main():
    for rel, req in REQUIRED.items():
        f = ROOT / rel
        f.write_text(strip(f.read_text(encoding="utf-8"), req), encoding="utf-8")
    for f in REMOVE_FILES:
        (ROOT / f).unlink(missing_ok=True)


if __name__ == "__main__":
    main()
