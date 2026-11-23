"""
dtv_gui_style.py - ajustes de estilo no espírito do Windows 11 (Fluent) SEM folha de estilo (QSS).

Por que não QSS: uma folha de estilo no app troca o estilo nativo por um que guarda a paleta de cada widget na hora em que ele
é "polido"; quando o tema claro/escuro muda depois, os widgets ficam com as cores antigas. Aqui o estilo continua o nativo
(windows11 / fusion / macos) embrulhado em um QProxyStyle que só aumenta medidas, e o botão de destaque se pinta sozinho lendo a
paleta a cada repintura, então segue o tema em tempo real.

Medidas (Fluent): controles de 32 px de altura, texto de 14 px (10 pt), cantos de 4 px nos controles, 8 px nos cartões.
"""
import sys

from PySide6.QtCore import QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPalette, QPen
from PySide6.QtWidgets import QApplication, QComboBox, QLineEdit, QProxyStyle, QPushButton, QStyle

CONTROL_H = 32          # altura mínima de botão, campo de texto e lista suspensa
RADIUS = 4              # canto dos controles


def mix(a: QColor, b: QColor, t: float) -> QColor:
    return QColor(int(a.red() * t + b.red() * (1 - t)), int(a.green() * t + b.green() * (1 - t)), int(a.blue() * t + b.blue() * (1 - t)))


def accent_of(pal: QPalette) -> QColor:
    R = QPalette.ColorRole
    return pal.color(R.Accent) if hasattr(R, "Accent") else pal.color(R.Highlight)


def on_color(bg: QColor) -> QColor:
    """Texto legível sobre a cor de ênfase (branco em fundo escuro, preto em fundo claro)."""
    return QColor("#ffffff") if bg.lightnessF() < 0.6 else QColor("#000000")


def set_compact(b: QPushButton, compact: bool = True) -> QPushButton:
    """Botão um pouco menos gordo (listas de ações dentro de um painel); lido pelo FluentStyle e pelo AccentButton."""
    b.setProperty("compact", compact)
    return b


class FluentStyle(QProxyStyle):
    """O estilo nativo, só que com controles mais altos e com respiro interno nos campos."""

    def sizeFromContents(self, ct, opt, size, widget=None):
        s = super().sizeFromContents(ct, opt, size, widget)
        C = QStyle.ContentsType
        compact = bool(widget is not None and widget.property("compact"))
        if ct == C.CT_PushButton:
            s = QSize(s.width() + (10 if compact else 16), max(s.height() + (4 if compact else 6), 28 if compact else CONTROL_H))
        elif ct in (C.CT_LineEdit, C.CT_ComboBox, C.CT_SpinBox):
            s = QSize(s.width() + 8, max(s.height() + 4, CONTROL_H))
        return s

    def subControlRect(self, cc, opt, sc, widget=None):
        r = super().subControlRect(cc, opt, sc, widget)
        if cc == QStyle.ComplexControl.CC_ComboBox and sc == QStyle.SubControl.SC_ComboBoxEditField and r.width() > 24:
            r.adjust(6, 0, 0, 0)            # o texto da lista suspensa não encosta na borda
        return r

    def polish(self, w):
        super().polish(w)
        if isinstance(w, QLineEdit) and w.parent() is not None and not isinstance(w.parent(), QComboBox):
            w.setTextMargins(4, 0, 4, 0)    # idem nos campos de texto


class AccentButton(QPushButton):
    """Botão de ação principal: preenchido com a cor de ênfase do sistema, com hover, pressionado e desabilitado."""

    def __init__(self, text: str = "", compact: bool = False, parent=None):
        super().__init__(text, parent)
        self.setProperty("compact", compact)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setCursor(Qt.CursorShape.ArrowCursor)
        f = self.font()
        f.setWeight(QFont.Weight.DemiBold)
        self.setFont(f)

    def paintEvent(self, e):
        pal = self.palette()
        acc = accent_of(pal)
        win = pal.color(QPalette.ColorRole.Window)
        if not self.isEnabled():
            fill, fg = mix(acc, win, 0.30), mix(pal.color(QPalette.ColorRole.WindowText), win, 0.55)
        else:
            fill = acc.darker(118) if self.isDown() else (acc.lighter(112) if acc.lightnessF() < 0.8 else acc.darker(108)) if self.underMouse() else acc
            fg = on_color(fill)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setBrush(fill)
        p.setPen(QPen(mix(fill, QColor("#000000"), 0.88), 1))
        p.drawRoundedRect(r, RADIUS, RADIUS)
        if self.hasFocus() and self.isEnabled():       # anel de foco (teclado)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(pal.color(QPalette.ColorRole.Text), 1.5))
            p.drawRoundedRect(r.adjusted(2, 2, -2, -2), RADIUS - 1, RADIUS - 1)
        p.setPen(fg)
        p.drawText(self.rect(), int(Qt.AlignmentFlag.AlignCenter), self.text())
        p.end()


def apply_fluent(app: QApplication):
    """Chamar logo depois de criar o QApplication."""
    app.setStyle(FluentStyle(app.style().name()))
    if sys.platform == "win32":
        f = app.font()
        if f.family().startswith("Segoe UI"):
            f.setPointSizeF(10.0)           # 14 px: o "Body" do Fluent (o padrão do Qt é 9 pt)
            app.setFont(f)


# ── Mica (fundo da janela translúcido, Windows 11) ───────────────────────────────────────────────────────────────
def mica_supported() -> bool:
    """O backdrop do sistema (DWMWA_SYSTEMBACKDROP_TYPE) existe a partir do Windows 11 22H2 (build 22621)."""
    return sys.platform == "win32" and getattr(sys.getwindowsversion(), "build", 0) >= 22621


def apply_backdrop(window, on: bool, dark: bool) -> bool:
    """Liga/desliga o Mica nativo (DWM) na janela. A janela precisa ter WA_TranslucentBackground antes de ser exibida; os
    widgets que não devem ser translúcidos simplesmente se pintam com a cor da janela. Devolve False se o sistema recusar."""
    if not mica_supported():
        return False
    import ctypes
    from ctypes import wintypes
    try:
        dwm = ctypes.windll.dwmapi
        dwm.DwmSetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, wintypes.LPCVOID, wintypes.DWORD]
        dwm.DwmExtendFrameIntoClientArea.argtypes = [wintypes.HWND, ctypes.c_void_p]

        class MARGINS(ctypes.Structure):
            _fields_ = [("l", ctypes.c_int), ("r", ctypes.c_int), ("t", ctypes.c_int), ("b", ctypes.c_int)]
        hwnd = wintypes.HWND(int(window.winId()))
        dwm.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(ctypes.c_int(1 if dark else 0)), 4)    # DWMWA_USE_IMMERSIVE_DARK_MODE
        ok = dwm.DwmSetWindowAttribute(hwnd, 38, ctypes.byref(ctypes.c_int(2 if on else 1)), 4)  # DWMWA_SYSTEMBACKDROP_TYPE: 2 = Mica, 1 = nenhum
        m = MARGINS(-1, -1, -1, -1) if on else MARGINS(0, 0, 0, 0)
        dwm.DwmExtendFrameIntoClientArea(hwnd, ctypes.byref(m))
        return ok == 0
    except (OSError, AttributeError):
        return False
