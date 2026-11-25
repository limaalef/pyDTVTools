"""Aviso nativo do sistema (Windows: central de notificações; macOS: Central de Notificações; Linux: D-Bus) quando uma fila de tarefas termina.

`summarize` não depende do Qt. `Notifier` usa QSystemTrayIcon.showMessage, o recurso nativo do Qt para isso: o ícone da bandeja só aparece
enquanto o aviso é mostrado.
"""
from __future__ import annotations

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QStyle, QSystemTrayIcon


def _short(text: str, n: int = 180) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def summarize(jobs: list, all_ok: bool, stopped: bool):
    """jobs: [{"title", "ok", "quiet", "err"}]. -> (título, texto, "info"|"warning"|"error"), ou None se não há o que avisar.

    Tarefas silenciosas (análises, verificação de ferramentas) não geram aviso, a não ser que falhem."""
    shown = [j for j in jobs if not j.get("quiet") or not j.get("ok")]
    if not shown:
        return None
    if stopped:
        done = sum(1 for j in jobs if j.get("ok"))
        return "Interrompido", f"{done} de {len(jobs)} tarefa(s) concluída(s) antes de parar.", "warning"
    if all_ok:
        text = shown[0]["title"] if len(shown) == 1 else f"{len(shown)} tarefas concluídas."
        return "Concluído", _short(text), "info"
    failed = [j for j in jobs if not j.get("ok")]
    first = failed[0] if failed else shown[0]
    text = first["title"] + (f": {first['err']}" if first.get("err") else " falhou.")
    if len(failed) > 1:
        text += f" (+{len(failed) - 1} com erro)"
    return "Erro", _short(text), "error"


class Notifier(QObject):
    clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._tray = None
        if QSystemTrayIcon.isSystemTrayAvailable():
            self._tray = QSystemTrayIcon(self._icon(), self)
            self._tray.messageClicked.connect(self.clicked)

    @staticmethod
    def _icon() -> QIcon:
        icon = QApplication.windowIcon()
        if icon.isNull():
            icon = QApplication.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon)
        return icon

    def available(self) -> bool:
        return self._tray is not None and QSystemTrayIcon.supportsMessages()

    def notify(self, title: str, text: str, kind: str = "info") -> bool:
        """Mostra o aviso. False se o sistema não tiver como (sem bandeja ou sem serviço de notificações)."""
        if not self.available():
            return False
        self._deliver(title, text, kind)
        return True

    def _deliver(self, title: str, text: str, kind: str):
        icon = {"info": QSystemTrayIcon.MessageIcon.Information, "warning": QSystemTrayIcon.MessageIcon.Warning,
                "error": QSystemTrayIcon.MessageIcon.Critical}.get(kind, QSystemTrayIcon.MessageIcon.Information)
        self._tray.setIcon(self._icon())
        self._tray.show()                                    # no Windows o ícone precisa estar visível para o aviso aparecer
        self._tray.showMessage(f"DTV Tools - {title}", text, icon, 10000)
        QTimer.singleShot(20000, self._tray.hide)            # some da bandeja depois de o aviso já ter sido exibido
