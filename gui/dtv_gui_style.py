"""
dtv_gui_style.py - ajustes de estilo no espírito do Windows 11 (Fluent) SEM folha de estilo (QSS).

Por que não QSS: uma folha de estilo no app troca o estilo nativo por um que guarda a paleta de cada widget na hora em que ele
é "polido"; quando o tema claro/escuro muda depois, os widgets ficam com as cores antigas. Aqui o estilo continua o nativo
(windows11 / fusion / macos) embrulhado em um QProxyStyle que só aumenta medidas, e o botão de destaque se pinta sozinho lendo a
paleta a cada repintura, então segue o tema em tempo real.

Medidas (Fluent): controles de 32 px de altura, texto de 14 px (10 pt), cantos de 4 px nos controles, 8 px nos cartões.
"""
import sys

from PySide6.QtCore import QPoint, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QCursor, QFont, QPainter, QPalette, QPen
from PySide6.QtWidgets import (QApplication, QComboBox, QHeaderView, QLineEdit, QProxyStyle, QPushButton,
                               QStyle, QStyleOptionViewItem, QTableView, QTreeView)

CONTROL_H = 32          # altura mínima de botão, campo de texto e lista suspensa
RADIUS = 4              # canto dos controles
ROW_H = 34              # altura mínima de uma linha de tabela/lista
HEADER_H = 34           # altura do cabeçalho de tabela
CELL_PAD = 8            # respiro horizontal do texto nas células


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
        elif ct == C.CT_ItemViewItem and isinstance(widget, (QTableView, QTreeView)):
            s = QSize(s.width(), max(s.height(), ROW_H))
        elif ct == C.CT_HeaderSection and isinstance(widget, QHeaderView):
            s = QSize(s.width(), max(s.height(), HEADER_H))
        return s

    def pixelMetric(self, m, opt=None, widget=None):
        if isinstance(widget, (QTableView, QTreeView, QHeaderView)):
            if m == QStyle.PixelMetric.PM_FocusFrameHMargin:
                return CELL_PAD - 1                       # o texto das células não encosta na borda (soma 1 px do estilo)
            if m == QStyle.PixelMetric.PM_HeaderMargin and isinstance(widget, QHeaderView):
                return CELL_PAD
        return super().pixelMetric(m, opt, widget)

    # ── tabelas e árvores: sem grade, cabeçalho plano, seleção em destaque suave (tudo lido da paleta a cada pintura) ──
    @staticmethod
    def _tint(widget, strength):
        pal = widget.palette()
        R = QPalette.ColorRole
        return mix(accent_of(pal), pal.color(R.Base), strength)

    def _row_fill(self, p, rect, widget, selected, hover, pos, radius=RADIUS):
        """Preenche a parte de uma linha que cabe em `rect`; os cantos só arredondam nas pontas da linha."""
        pal = widget.palette()
        R = QPalette.ColorRole
        base = pal.color(R.Base)
        if selected:
            col = mix(accent_of(pal), base, 0.30 if base.lightnessF() < 0.5 else 0.20)
        elif hover:
            col = mix(pal.color(R.Text), base, 0.07)
        else:
            return
        r = QRectF(rect).adjusted(0, 1, 0, -1)
        left = pos in (QStyleOptionViewItem.ViewItemPosition.Beginning, QStyleOptionViewItem.ViewItemPosition.OnlyOne)
        right = pos in (QStyleOptionViewItem.ViewItemPosition.End, QStyleOptionViewItem.ViewItemPosition.OnlyOne)
        if left:
            r.adjust(3, 0, 0, 0)
        if right:
            r.adjust(0, 0, -3, 0)
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(col)
        # retângulo arredondado nas pontas e reto no meio: desenha o arredondado e cobre o lado que deve ser reto
        p.drawRoundedRect(r, radius, radius)
        if not left:
            p.drawRect(QRectF(r.left(), r.top(), radius + 1, r.height()))
        if not right:
            p.drawRect(QRectF(r.right() - radius - 1, r.top(), radius + 1, r.height()))
        p.restore()

    def drawControl(self, el, opt, p, widget=None):
        CE = QStyle.ControlElement
        S = QStyle.StateFlag
        if el == CE.CE_ItemViewItem and isinstance(widget, (QTableView, QTreeView)):
            o = QStyleOptionViewItem(opt)
            sel, hov = bool(o.state & S.State_Selected), bool(o.state & S.State_MouseOver)
            if isinstance(widget, QTableView):       # tabela: cada célula pinta o seu pedaço; árvore: a linha toda (PE_PanelItemViewRow)
                self._row_fill(p, o.rect, widget, sel, hov and not sel, o.viewItemPosition)
            o.state &= ~(S.State_Selected | S.State_MouseOver | S.State_HasFocus)
            o.features &= ~QStyleOptionViewItem.ViewItemFeature.Alternate
            o.showDecorationSelected = False
            return super().drawControl(el, o, p, widget)
        if el == CE.CE_HeaderSection and isinstance(widget, QHeaderView):
            pal = opt.palette
            R = QPalette.ColorRole
            p.save()
            p.fillRect(opt.rect, pal.color(R.Base))
            p.setPen(QPen(mix(pal.color(R.Text), pal.color(R.Base), 0.16), 1))
            y = opt.rect.bottom()
            p.drawLine(opt.rect.left(), y, opt.rect.right(), y)
            p.restore()
            return
        super().drawControl(el, opt, p, widget)

    def drawPrimitive(self, pe, opt, p, widget=None):
        PE = QStyle.PrimitiveElement
        S = QStyle.StateFlag
        if isinstance(widget, (QTableView, QTreeView)):
            if pe == PE.PE_PanelItemViewRow:
                if isinstance(widget, QTreeView):          # chamado por pedaço da linha (ramo, colunas): pinta a linha como um bloco só
                    P = QStyleOptionViewItem.ViewItemPosition
                    vp = widget.viewport()
                    idx = widget.indexAt(QPoint(max(opt.rect.left(), 0) + 1, opt.rect.center().y()))
                    sm = widget.selectionModel()
                    sel = bool(idx.isValid() and sm is not None and sm.isRowSelected(idx.row(), idx.parent()))
                    pos_y = vp.mapFromGlobal(QCursor.pos()).y() if vp.underMouse() else -1
                    hov = (not sel) and opt.rect.top() <= pos_y <= opt.rect.bottom()
                    pos = (P.Beginning if opt.rect.left() <= 0 else P.End if opt.rect.right() + 1 >= vp.width() else P.Middle)
                    self._row_fill(p, opt.rect, widget, sel, hov, pos)
                return                                     # sem faixas alternadas nem fundo de linha do estilo
            if pe == PE.PE_Frame:
                pal = widget.palette()
                R = QPalette.ColorRole
                p.save()
                p.setPen(QPen(mix(pal.color(R.Text), pal.color(R.Window), 0.18), 1))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawRect(QRectF(opt.rect).adjusted(0.5, 0.5, -0.5, -0.5))
                p.restore()
                return
            if pe == PE.PE_IndicatorBranch and isinstance(widget, QTreeView):
                o = type(opt)(opt)
                o.state &= ~(S.State_Selected | S.State_MouseOver)
                return super().drawPrimitive(pe, o, p, widget)
        super().drawPrimitive(pe, opt, p, widget)

    def subControlRect(self, cc, opt, sc, widget=None):
        r = super().subControlRect(cc, opt, sc, widget)
        if cc == QStyle.ComplexControl.CC_ComboBox and sc == QStyle.SubControl.SC_ComboBoxEditField and r.width() > 24:
            r.adjust(6, 0, 0, 0)            # o texto da lista suspensa não encosta na borda
        return r

    def polish(self, w):
        super().polish(w)
        if isinstance(w, QLineEdit) and w.parent() is not None and not isinstance(w.parent(), QComboBox):
            w.setTextMargins(4, 0, 4, 0)    # idem nos campos de texto
        if isinstance(w, (QTableView, QTreeView)):
            w.viewport().setAttribute(Qt.WidgetAttribute.WA_Hover, True)        # realce da linha sob o mouse
            w.setAlternatingRowColors(False)
            if isinstance(w, QTableView):
                w.setShowGrid(False)
                w.horizontalHeader().setHighlightSections(False)
                w.horizontalHeader().setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
                f = w.horizontalHeader().font()
                f.setWeight(QFont.Weight.DemiBold)
                w.horizontalHeader().setFont(f)
            else:
                f = w.header().font()
                f.setWeight(QFont.Weight.DemiBold)
                w.header().setFont(f)
                w.header().setHighlightSections(False)
                w.header().setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)


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

    def base_color(self, pal: QPalette) -> QColor:
        return accent_of(pal)

    def paintEvent(self, e):
        pal = self.palette()
        acc = self.base_color(pal)
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


class DangerButton(AccentButton):
    """Mesmo botão de destaque, em vermelho (parar/cancelar uma execução em andamento)."""

    def base_color(self, pal: QPalette) -> QColor:
        dark = pal.color(QPalette.ColorRole.Window).lightness() < 128
        return QColor("#e5484d" if dark else "#c42b1c")


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
