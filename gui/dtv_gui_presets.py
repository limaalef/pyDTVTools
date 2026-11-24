"""Predefinições da tela Recodificar: guardadas num JSON ao lado da configuração do dtv_tools, com algumas já embutidas.

Sem dependência do Qt (testável sozinho). Uma predefinição é um dicionário de ajustes (chaves em KEYS); o que não está nele fica como está na tela.
Não entram arquivos de entrada/saída nem a seleção de itens do áudio: isso depende de cada arquivo.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

SCHEMA = 1

# chave -> (tipo, valores aceitos ou None)
KEYS = {
    "encoder": (str, None), "crf": (float, None), "max_bitrate": (float, None), "preset": (str, None), "container": (str, ("auto", "mp4", "mkv")),
    "size": (str, None), "limit": (int, None), "no_lcevc": (bool, None), "params": (str, None), "verify": (int, None),
    "audio_mode": (str, ("render", "copy", "none")), "keep_orig": (bool, None), "audio_format": (str, ("auto", "aac", "eac3", "flac", "wav")),
    "audio_delay": (str, None), "native_layout": (int, (0, 2, 6, 16, 19)), "compat_51": (bool, None),
}

BUILTIN = {
    "HEVC 10 bits · qualidade (x265)": {"encoder": "x265", "crf": 18, "preset": "slow", "max_bitrate": 20, "container": "auto", "audio_mode": "render", "keep_orig": True},
    "HEVC 10 bits · equilibrado (x265)": {"encoder": "x265", "crf": 20, "preset": "medium", "max_bitrate": 20, "container": "auto", "audio_mode": "render", "keep_orig": True},
    "HEVC rápido (NVENC, GPU NVIDIA)": {"encoder": "nvenc", "crf": 22, "preset": "p5", "max_bitrate": 20, "container": "auto", "audio_mode": "render", "keep_orig": True},
    "AV1 · arquivo (SVT-AV1)": {"encoder": "av1", "crf": 30, "preset": "6", "max_bitrate": 15, "container": "auto", "audio_mode": "render", "keep_orig": True},
    "Teste rápido (30 s, áudio original)": {"encoder": "x265", "crf": 24, "preset": "veryfast", "limit": 30, "audio_mode": "copy"},
}


def clean(d) -> dict:
    """Mantém só as chaves conhecidas, com o tipo certo (um arquivo editado à mão ou de outra versão não derruba a tela)."""
    out = {}
    if not isinstance(d, dict):
        return out
    for k, (typ, allowed) in KEYS.items():
        if k not in d:
            continue
        v = d[k]
        try:
            if typ is bool:
                if not isinstance(v, bool):
                    continue
            elif typ is str:
                if not isinstance(v, str):
                    continue
            elif typ is float:
                if isinstance(v, bool) or not isinstance(v, (int, float)):
                    continue
                v = float(v)
            elif typ is int:
                if isinstance(v, bool) or not isinstance(v, (int, float)):
                    continue
                v = int(v)
        except (TypeError, ValueError):
            continue
        if allowed is not None and v not in allowed:
            continue
        out[k] = v
    return out


def check_name(name: str, taken_builtin=BUILTIN) -> str:
    """Nome válido (devolvido sem espaços nas pontas) ou ValueError com a razão."""
    n = re.sub(r"\s+", " ", (name or "")).strip()
    if not n:
        raise ValueError("Dê um nome à predefinição.")
    if len(n) > 60:
        raise ValueError("O nome pode ter no máximo 60 caracteres.")
    if n in taken_builtin:
        raise ValueError("Este nome é de uma predefinição que já vem no programa. Escolha outro.")
    return n


def _unique(name: str, used) -> str:
    if name not in used:
        return name
    i = 2
    while f"{name} ({i})" in used:
        i += 1
    return f"{name} ({i})"


class PresetStore:
    def __init__(self, path):
        self.path = Path(path)
        self.user: dict = {}
        self.error = ""
        self.load()

    def load(self):
        self.user, self.error = {}, ""
        if not self.path.is_file():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            raw = data.get("presets", {}) if isinstance(data, dict) else {}
            if not isinstance(raw, dict):
                raise ValueError("formato inesperado")
            self.user = {str(n): clean(v) for n, v in raw.items() if str(n) not in BUILTIN and clean(v)}
        except (OSError, ValueError) as e:
            bad = self.path.with_suffix(".json.bad")
            try:
                os.replace(self.path, bad)
                self.error = f"O arquivo de predefinições estava ilegível ({e}); foi guardado como {bad.name} e a lista recomeçou vazia."
            except OSError:
                self.error = f"O arquivo de predefinições está ilegível ({e})."
            self.user = {}

    def _write(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"version": SCHEMA, "presets": self.user}, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)                                  # só um arquivo completo toma o lugar do anterior

    def builtin_names(self) -> list:
        return list(BUILTIN)

    def user_names(self) -> list:
        return sorted(self.user, key=str.casefold)

    def is_builtin(self, name) -> bool:
        return name in BUILTIN

    def get(self, name) -> dict | None:
        d = BUILTIN.get(name) if name in BUILTIN else self.user.get(name)
        return dict(d) if d is not None else None

    def put(self, name, settings) -> str:
        n = check_name(name)
        self.user[n] = clean(settings)
        self._write()
        return n

    def delete(self, name) -> bool:
        if name in BUILTIN or name not in self.user:
            return False
        del self.user[name]
        self._write()
        return True

    def export_to(self, path, names=None):
        names = list(names) if names is not None else self.user_names()
        data = {"version": SCHEMA, "presets": {n: self.get(n) for n in names if self.get(n)}}
        Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        return len(data["presets"])

    def import_from(self, path) -> list:
        """Acrescenta as predefinições de um arquivo exportado; nome repetido ganha ' (2)'. Devolve os nomes criados."""
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise ValueError(f"Não consegui ler o arquivo: {e}")
        raw = data.get("presets") if isinstance(data, dict) else None
        if not isinstance(raw, dict) or not raw:
            raise ValueError("O arquivo não tem predefinições.")
        added = []
        for n, v in raw.items():
            s = clean(v)
            if not s:
                continue
            base = re.sub(r"\s+", " ", str(n)).strip()[:60] or "Predefinição"
            name = _unique(base, set(self.user) | set(BUILTIN))
            self.user[name] = s
            added.append(name)
        if not added:
            raise ValueError("Nenhuma predefinição válida no arquivo.")
        self._write()
        return added
