"""Update check: when to ask, what to say, and the dialog. The network call itself is `dtv_tools.py check-update`."""
from __future__ import annotations

import html
import time

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QMessageBox

INTERVAL_S = 24 * 3600


def due(last_ts: float, now: float | None = None, interval: float = INTERVAL_S) -> bool:
    """True when an automatic check should run (never checked, clock went back, or the interval elapsed)."""
    now = time.time() if now is None else now
    return not last_ts or last_ts > now or (now - last_ts) >= interval


def worth_showing(info: dict, skipped: str, manual: bool) -> bool:
    """An automatic check stays silent unless there is a version newer than the one the user chose to skip."""
    if not info.get("update_available"):
        return manual
    return manual or info.get("latest") != skipped


def human_size(n: int) -> str:
    f = float(n or 0)
    for u in ("B", "KB", "MB", "GB"):
        if f < 1024 or u == "GB":
            return f"{f:.0f} {u}" if u == "B" else f"{f:.1f} {u}"
        f /= 1024
    return ""


def message(info: dict) -> tuple[str, str]:
    """(title, rich-text body)."""
    cur, new = info.get("current", "?"), info.get("latest", "?")
    if not info.get("update_available"):
        return "Atualizações", f"Você está na versão mais recente ({html.escape(cur)})."
    body = f"A versão <b>{html.escape(new)}</b> está disponível. Você tem a <b>{html.escape(cur)}</b>."
    if info.get("prerelease"):
        body += "<br>Esta é uma versão de teste."
    notes = (info.get("notes") or "").strip()
    if notes:
        short = notes if len(notes) <= 600 else notes[:600].rstrip() + "…"
        body += "<br><br>" + html.escape(short).replace("\n", "<br>")
    sug = info.get("suggested")
    if sug:
        body += f"<br><br>Arquivo para este sistema: {html.escape(sug.get('name', ''))} ({human_size(sug.get('size', 0))})"
    body += "<br><br>O programa não se atualiza sozinho: baixe a nova versão e substitua a atual."
    return "Nova versão disponível", body


def build_dialog(parent, info: dict, on_skip) -> QMessageBox:
    title, body = message(info)
    box = QMessageBox(parent)
    box.setWindowTitle(title)
    box.setTextFormat(box.textFormat().RichText)
    box.setText(body)
    box.setIcon(QMessageBox.Icon.Information)
    if info.get("update_available"):
        sug, page = info.get("suggested"), info.get("url")
        if sug and sug.get("url"):
            b = box.addButton("Baixar", QMessageBox.ButtonRole.AcceptRole)
            b.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(sug["url"])))
        if page:
            b = box.addButton("Ver novidades", QMessageBox.ButtonRole.ActionRole)
            b.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(page)))
        s = box.addButton("Ignorar esta versão", QMessageBox.ButtonRole.DestructiveRole)
        s.clicked.connect(lambda: on_skip(info.get("latest", "")))
    box.addButton("Fechar", QMessageBox.ButtonRole.RejectRole)
    return box
