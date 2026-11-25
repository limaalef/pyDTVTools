from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
file = ROOT / "gui" / "dtv_gui.py"
code = file.read_text(encoding="utf-8")

blocks = [
    ("        # BUILD-REMOVE-START",   "        # BUILD-REMOVE-END"),
    ("        # BUILD-REMOVE-START-2", "        # BUILD-REMOVE-END-2"),
]

for start, end in blocks:
    if start not in code or end not in code:
        raise RuntimeError(f"Marcadores não encontrados: {start} / {end}")

    before, rest = code.split(start, 1)
    removed, after = rest.split(end, 1)

    code = before + after

file.write_text(code, encoding="utf-8")