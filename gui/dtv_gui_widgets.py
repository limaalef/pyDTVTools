"""
dtv_gui_widgets.py - widgets reutilizáveis da interface do DTV Tools.

Tudo usa a paleta e o estilo do sistema (nada de folha de estilo global): o claro/escuro vem do Qt e os gráficos
pintados à mão leem as cores da paleta a cada repaint, então acompanham a mudança de tema sem reiniciar.
"""
from __future__ import annotations

import bisect
import math
import os
import re
import sys
import time

from pathlib import Path
from dtv_gui_style import RADIUS, AccentButton, accent_of, on_color, set_compact
from PySide6.QtCore import QEvent, QObject, QPointF, QRectF, QSize, QSettings, Qt, QTimer, QUrl, Signal, QProcess
from PySide6.QtGui import (QTextCursor, QTextOption, QColor, QDesktopServices, QFont, QFontDatabase, QFontMetrics, QGuiApplication, QPainter,
                           QPainterPath, QPalette, QPen, QPixmap, QIcon)
from PySide6.QtWidgets import (QAbstractButton, QStyledItemDelegate, QStyleOptionViewItem, QAbstractItemView, QAbstractSpinBox, QApplication, QFormLayout, QTableWidget, QScrollArea, QCheckBox, QComboBox, QFileDialog, QFrame, QGridLayout,
                               QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget, QListWidgetItem,
                               QPlainTextEdit, QProgressBar, QPushButton, QSizePolicy, QStyle, QTabWidget, QToolButton,
                               QTextEdit, QToolTip, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)


# ───────────────────────────── cores / formatação ─────────────────────────────
def mix(a: QColor, b: QColor, t: float) -> QColor:
    return QColor(round(a.red() * t + b.red() * (1 - t)), round(a.green() * t + b.green() * (1 - t)),
                  round(a.blue() * t + b.blue() * (1 - t)))


class Colors:
    """Cores derivadas da paleta atual (claro/escuro). Categóricas: Okabe-Ito, legíveis nos dois temas."""

    def __init__(self, w: QWidget):
        pal = w.palette()
        R = QPalette.ColorRole
        self.dark = pal.color(R.Window).lightness() < 128
        self.window = pal.color(R.Window)
        self.bg = pal.color(R.Base)
        self.fg = pal.color(R.Text)
        self.muted = mix(self.fg, self.bg, 0.55)
        self.grid = mix(self.fg, self.bg, 0.13)
        self.border = mix(self.fg, self.window, 0.22)
        self.accent = pal.color(R.Highlight)
        base = ["#0072B2", "#E69F00", "#009E73", "#CC79A7", "#D55E00", "#56B4E9"]
        self.cat = [QColor(c).lighter(135) if self.dark else QColor(c) for c in base]
        self.ok = QColor("#4cc38a" if self.dark else "#1f8a4c")
        self.warn = QColor("#f2b84b" if self.dark else "#b36b00")
        self.err = QColor("#ff7b72" if self.dark else "#c62828")


def fmt_hms(s) -> str:
    s = max(0, int(round(float(s or 0))))
    h, r = divmod(s, 3600)
    m, sec = divmod(r, 60)
    return f"{h:02d}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"


def fmt_clock(sec) -> str:
    sec = max(0.0, float(sec))
    return f"{int(sec // 3600):02d}:{int(sec % 3600 // 60):02d}:{sec % 60:06.3f}"


def human(n) -> str:
    n = float(n or 0)
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or u == "TB":
            return f"{n:.0f} {u}" if u == "B" else f"{n:.1f} {u}"
        n /= 1024


def nice_step(span: float, target: int = 7) -> float:
    for s in (1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 14400, 21600, 43200, 86400):
        if span / s <= target:
            return s
    return 86400


def draw_text(p: QPainter, x: float, yc: float, text: str, align: str = "left"):
    fm = p.fontMetrics()
    base = yc + (fm.ascent() - fm.descent()) / 2
    if align == "right":
        x -= fm.horizontalAdvance(text)
    elif align == "center":
        x -= fm.horizontalAdvance(text) / 2
    p.drawText(QPointF(x, base), text)


class MutedLabel(QLabel):
    """Texto secundário: cor derivada da paleta e recalculada quando o tema muda."""

    def __init__(self, text: str = "", small: bool = True):
        super().__init__(text)
        self.setWordWrap(True)
        self._busy = False
        if small:
            f = self.font()
            f.setPointSizeF(max(f.pointSizeF() - 0.5, 7))
            self.setFont(f)
        self._recolor()

    def _recolor(self):
        self._busy = True
        pal = self.palette()
        pal.setColor(QPalette.ColorRole.WindowText, mix(pal.color(QPalette.ColorRole.WindowText), pal.color(QPalette.ColorRole.Window), 0.62))
        self.setPalette(pal)
        self._busy = False

    def changeEvent(self, e):
        if e.type() == e.Type.PaletteChange and not self._busy:
            self.setPalette(QPalette())  # volta ao padrão do tema atual
            self._recolor()
        super().changeEvent(e)


def muted_label(text: str = "", small: bool = True) -> QLabel:
    return MutedLabel(text, small)


class CardBox(QFrame):
    """Cartão no estilo das configurações do sistema: fundo e borda vêm da paleta (claro/escuro), cabeçalho pequeno."""

    def __init__(self, title: str = "", parent=None, expand: bool = False):
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.NoFrame)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 10, 16, 14)
        lay.setSpacing(8)
        self.outer = lay
        self.head = MutedLabel(title)
        self.head.setWordWrap(False)
        f = self.head.font()
        f.setBold(True)
        self.head.setFont(f)
        self.head.setVisible(bool(title))
        lay.addWidget(self.head)
        self.body = QVBoxLayout()
        self.body.setSpacing(8)
        if expand:
            lay.addLayout(self.body, 1)      # o conteúdo ocupa a altura que sobrar (ex.: diagrama ao lado de uma lista alta)
        else:
            lay.addLayout(self.body)
            lay.addStretch(1)                # cartões esticados pelo vizinho mantêm o conteúdo no topo

    def form(self) -> QFormLayout:
        f = QFormLayout()
        f.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        f.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        f.setHorizontalSpacing(14)
        f.setVerticalSpacing(8)
        self.body.addLayout(f)
        return f

    @staticmethod
    def colors(w: QWidget):
        pal = w.palette()
        R = QPalette.ColorRole
        win, fg = pal.color(R.Window), pal.color(R.WindowText)
        dark = win.lightness() < 128
        # Translúcidos (como as camadas do Windows 11): o que está atrás (Mica ou a cor da janela) aparece através do cartão
        fill = QColor(255, 255, 255, 14) if dark else QColor(255, 255, 255, 150)
        border = QColor(255, 255, 255, 26) if dark else QColor(0, 0, 0, 24)
        return fill, border

    def paintEvent(self, _):
        fill, border = self.colors(self)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(QPen(border, 1))
        p.setBrush(fill)
        p.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 8, 8)


def svg_icon(path, normal: QColor, selected: QColor, size: int = 18) -> QIcon:
    """Ícone SVG (traço em `currentColor`) pintado com a cor do texto e, quando selecionado, com a cor do texto selecionado."""
    from PySide6.QtCore import QByteArray
    from PySide6.QtSvg import QSvgRenderer
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError:
        return QIcon()
    ratio = max(1.0, QGuiApplication.primaryScreen().devicePixelRatio() if QGuiApplication.primaryScreen() else 1.0)
    icon = QIcon()
    for mode, col in ((QIcon.Mode.Normal, normal), (QIcon.Mode.Selected, selected), (QIcon.Mode.Active, normal)):
        r = QSvgRenderer(QByteArray(raw.replace("currentColor", col.name()).encode("utf-8")))
        pm = QPixmap(int(size * ratio), int(size * ratio))
        pm.fill(Qt.GlobalColor.transparent)
        pm.setDevicePixelRatio(ratio)
        p = QPainter(pm)
        r.render(p, QRectF(0, 0, size, size))
        p.end()
        icon.addPixmap(pm, mode)
    return icon


def fit_view(view, min_h: int = 0):
    """Faz a lista/tabela/árvore crescer com o conteúdo, sem barra de rolagem própria (só a página rola).
    Devolve a função que recalcula; chame depois de mudar o conteúdo."""
    view.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    view.setSizeAdjustPolicy(QAbstractItemView.SizeAdjustPolicy.AdjustIgnored)

    def apply():
        h = 2 * view.frameWidth() + 2
        if isinstance(view, QTreeWidget):
            view.doItemsLayout()
            h += view.header().height() if not view.isHeaderHidden() else 0
            from PySide6.QtWidgets import QTreeWidgetItemIterator
            it = QTreeWidgetItemIterator(view)
            while it.value():
                h += view.visualItemRect(it.value()).height()
                it += 1
        elif isinstance(view, QTableWidget):
            h += view.horizontalHeader().height() + view.verticalHeader().length()
        elif isinstance(view, QListWidget):
            h += sum(max(view.sizeHintForRow(i), 0) for i in range(view.count()))
        view.setFixedHeight(max(min_h, h))

    def later(*_):
        QTimer.singleShot(0, apply)

    m = view.model()
    for sig in (m.rowsInserted, m.rowsRemoved, m.modelReset, m.layoutChanged):
        sig.connect(later)
    if isinstance(view, QTreeWidget):
        view.itemExpanded.connect(later)
        view.itemCollapsed.connect(later)
    later()
    view._refit = later
    return later


class WheelGuard(QObject):
    """Combos e spin boxes só reagem à roda do mouse quando têm o foco; senão a roda rola a página."""

    def eventFilter(self, o, e):
        if e.type() == QEvent.Type.Wheel and isinstance(o, (QComboBox, QAbstractSpinBox)) and not o.hasFocus():
            e.ignore()
            return True
        return False


# ───────────────────────────── ícone de estado ─────────────────────────────
class StatusIcon(QWidget):
    PENDING, RUNNING, DONE, ERROR, SKIPPED = range(5)

    def __init__(self, size: int = 18, parent=None):
        super().__init__(parent)
        self.setFixedSize(size, size)
        self._s, self._angle = self.PENDING, 0
        self._t = QTimer(self, interval=50)
        self._t.timeout.connect(self._tick)

    def set_state(self, s: int):
        self._s = s
        if s == self.RUNNING:
            self._t.start()
        else:
            self._t.stop()
        self.update()

    def _tick(self):
        self._angle = (self._angle - 24) % 360
        self.update()

    def paintEvent(self, _):
        c = Colors(self)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(2.5, 2.5, -2.5, -2.5)
        s = self._s
        if s == self.PENDING:
            p.setPen(QPen(c.muted, 1.4))
            p.drawEllipse(r)
        elif s == self.RUNNING:
            p.setPen(QPen(c.grid.darker(110) if not c.dark else c.border, 2))
            p.drawEllipse(r)
            pen = QPen(c.accent, 2.4)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            p.setPen(pen)
            p.drawArc(r, self._angle * 16, 100 * 16)
        elif s in (self.DONE, self.ERROR):
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(c.ok if s == self.DONE else c.err)
            p.drawEllipse(r)
            pen = QPen(QColor("#ffffff") if not c.dark else QColor("#101418"), 1.8)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            p.setPen(pen)
            cx, cy, w = r.center().x(), r.center().y(), r.width()
            path = QPainterPath()
            if s == self.DONE:
                path.moveTo(cx - w * 0.22, cy + w * 0.01)
                path.lineTo(cx - w * 0.05, cy + w * 0.19)
                path.lineTo(cx + w * 0.24, cy - w * 0.17)
            else:
                path.moveTo(cx - w * 0.17, cy - w * 0.17)
                path.lineTo(cx + w * 0.17, cy + w * 0.17)
                path.moveTo(cx + w * 0.17, cy - w * 0.17)
                path.lineTo(cx - w * 0.17, cy + w * 0.17)
            p.drawPath(path)
        else:
            pen = QPen(c.muted, 1.2)
            pen.setStyle(Qt.PenStyle.DotLine)
            p.setPen(pen)
            p.drawEllipse(r)
            p.drawLine(QPointF(r.center().x() - r.width() * 0.2, r.center().y()), QPointF(r.center().x() + r.width() * 0.2, r.center().y()))


# ───────────────────────────── seções e entradas ─────────────────────────────
class CollapseHeader(QAbstractButton):
    """Cabeçalho de seção recolhível: linha inteira clicável, seta + título, destaque suave arredondado ao passar o mouse
    (o QToolButton marcado ficava preenchido com a cor de ênfase só em volta do texto)."""

    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        self.setText(title)
        self.setCheckable(True)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        f = self.font()
        f.setBold(True)
        self.setFont(f)

    def sizeHint(self):
        return QSize(160, max(self.fontMetrics().height() + 14, 32))

    def paintEvent(self, _):
        pal = self.palette()
        fg = pal.color(QPalette.ColorRole.WindowText)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        if self.isDown() or self.underMouse():
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(fg.red(), fg.green(), fg.blue(), 26 if self.isDown() else 16))
            p.drawRoundedRect(r, 4, 4)
        if self.hasFocus() and self.focusPolicy() != Qt.FocusPolicy.NoFocus and self.property("kbd"):
            p.setPen(QPen(fg, 1.2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(r.adjusted(1, 1, -1, -1), 3, 3)
        # seta (chevron): > fechado, v aberto
        cx, cy = 18.0, self.height() / 2.0
        pen = QPen(fg, 1.6)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        path = QPainterPath()
        if self.isChecked():
            path.moveTo(cx - 4, cy - 2); path.lineTo(cx, cy + 2); path.lineTo(cx + 4, cy - 2)
        else:
            path.moveTo(cx - 2, cy - 4); path.lineTo(cx + 2, cy); path.lineTo(cx - 2, cy + 4)
        p.drawPath(path)
        p.setPen(fg)
        p.drawText(QRectF(34, 0, self.width() - 40, self.height()), int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft), self.text())
        p.end()


class Collapsible(QWidget):
    """Seção recolhível. flat=False: vira um cartão próprio; flat=True: vai dentro de outro cartão."""

    def __init__(self, title: str, expanded: bool = False, flat: bool = False, parent=None):
        super().__init__(parent)
        self.flat = flat
        self.btn = CollapseHeader(title)
        self.btn.setChecked(expanded)
        self.body = QWidget()
        self.content = QVBoxLayout(self.body)
        self.content.setContentsMargins(20, 4, 0, 4)
        lay = QVBoxLayout(self)
        m = 0 if flat else 12
        lay.setContentsMargins(m - 4 if not flat else 0, 4 if not flat else 0, m - 4 if not flat else 0, 4 if not flat else 0)
        lay.setSpacing(2)
        lay.addWidget(self.btn)
        lay.addWidget(self.body)
        self.btn.toggled.connect(self._sync)
        self._sync(expanded)

    def paintEvent(self, _):
        if self.flat:
            return
        fill, border = CardBox.colors(self)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(QPen(border, 1))
        p.setBrush(fill)
        p.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 8, 8)

    def _sync(self, on: bool):
        self.btn.update()
        self.body.setVisible(on)


class DropLineEdit(QLineEdit):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dragMoveEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        urls = e.mimeData().urls()
        if urls and urls[0].isLocalFile():
            self.setText(urls[0].toLocalFile())
            self.editingFinished.emit()


class FileEdit(QWidget):
    changed = Signal(str)

    def __init__(self, mode: str = "open", filters: str = "", placeholder: str = "", key: str = "", parent=None):
        super().__init__(parent)
        self.mode, self.filters, self.key = mode, filters, key or mode
        self.edit = DropLineEdit()
        self.edit.setPlaceholderText(placeholder)
        self.edit.setClearButtonEnabled(True)
        self.btn = QPushButton("Procurar…")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.edit, 1)
        lay.addWidget(self.btn)
        self.btn.clicked.connect(self.browse)
        self.edit.textChanged.connect(self.changed)

    def text(self) -> str:
        return self.edit.text().strip()

    def setText(self, t: str):
        self.edit.setText(t)

    def browse(self):
        st = QSettings()
        start = self.text() or st.value("dirs/" + self.key, "", str)
        if self.mode == "dir":
            path = QFileDialog.getExistingDirectory(self, "Escolher pasta", start)
        elif self.mode == "save":
            path, _ = QFileDialog.getSaveFileName(self, "Salvar como", start, self.filters)
        else:
            path, _ = QFileDialog.getOpenFileName(self, "Escolher arquivo", start, self.filters)
        if path:
            self.setText(path)
            st.setValue("dirs/" + self.key, path if self.mode == "dir" else os.path.dirname(path))


class _DropList(QListWidget):
    dropped = Signal(list)

    def __init__(self):
        super().__init__()
        self.setAcceptDrops(True)

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dragMoveEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        self.dropped.emit([u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()])
        e.acceptProposedAction()

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            for it in self.selectedItems():
                self.takeItem(self.row(it))
            self.model().layoutChanged.emit()
        else:
            super().keyPressEvent(e)


class ItemList(QWidget):
    """Lista com adicionar/remover. mode='files' abre o seletor de arquivos (e aceita arrastar e soltar);
    mode='text' adiciona o que foi digitado."""
    changed = Signal()

    def __init__(self, mode: str = "files", filters: str = "", placeholder: str = "", key: str = "files", rows: int = 4, parent=None):
        super().__init__(parent)
        self.mode, self.filters, self.key = mode, filters, key
        self.list = _DropList()
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.list.setAlternatingRowColors(True)
        fit_view(self.list, self.list.fontMetrics().height() * rows + 14)
        self.list.dropped.connect(self.add_paths)
        self.list.model().rowsRemoved.connect(lambda *_: self.changed.emit())
        self.list.model().layoutChanged.connect(lambda *_: self.changed.emit())
        self.add_btn = QPushButton("Adicionar arquivos…" if mode == "files" else "Adicionar")
        self.rm_btn = QPushButton("Remover")
        self.clear_btn = QPushButton("Limpar")
        self.edit = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        if mode == "text":
            self.edit = QLineEdit()
            self.edit.setPlaceholderText(placeholder)
            row = QHBoxLayout()
            row.addWidget(self.edit, 1)
            row.addWidget(self.add_btn)
            lay.addLayout(row)
            self.edit.returnPressed.connect(self._add_text)
        lay.addWidget(self.list)
        btns = QHBoxLayout()
        if mode == "files":
            btns.addWidget(self.add_btn)
        btns.addWidget(self.rm_btn)
        btns.addWidget(self.clear_btn)
        btns.addStretch(1)
        self.count_lb = muted_label("")
        self.count_lb.setWordWrap(False)
        btns.addWidget(self.count_lb)
        lay.addLayout(btns)
        self.add_btn.clicked.connect(self._add_clicked)
        self.rm_btn.clicked.connect(self.remove_selected)
        self.clear_btn.clicked.connect(self.clear)
        self.list.itemSelectionChanged.connect(self._sync)
        self.changed.connect(self._sync)
        self._sync()

    def _sync(self):
        n = self.list.count()
        self.rm_btn.setEnabled(bool(self.list.selectedItems()))
        self.clear_btn.setEnabled(n > 0)
        self.count_lb.setText(f"{n} item(ns)" if n else "")

    def _add_clicked(self):
        if self.mode == "text":
            self._add_text()
            return
        st = QSettings()
        paths, _ = QFileDialog.getOpenFileNames(self, "Adicionar arquivos", st.value("dirs/" + self.key, "", str), self.filters)
        if paths:
            st.setValue("dirs/" + self.key, os.path.dirname(paths[0]))
            self.add_paths(paths)

    def _add_text(self):
        t = self.edit.text().strip()
        if t:
            self.add_item(t, t)
            self.edit.clear()

    def add_paths(self, paths):
        for p in paths:
            if os.path.isfile(p):
                self.add_item(os.path.basename(p) + "   —   " + os.path.dirname(p), p)

    def add_item(self, text: str, value: str):
        if value in self.values():
            return
        it = QListWidgetItem(text)
        it.setData(Qt.ItemDataRole.UserRole, value)
        it.setToolTip(value)
        self.list.addItem(it)
        self.changed.emit()

    def values(self) -> list:
        return [self.list.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.list.count())]

    def remove_selected(self):
        for it in self.list.selectedItems():
            self.list.takeItem(self.list.row(it))
        self.changed.emit()

    def clear(self):
        self.list.clear()
        self.changed.emit()


# ───────────────────────────── gráficos ─────────────────────────────
class Card(QWidget):
    """Base: cartão com a cor de fundo de campos de texto do sistema e borda sutil."""

    def paint_card(self, p: QPainter, c: Colors):
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(QPen(c.border, 1))
        p.setBrush(c.bg)
        p.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 6, 6)


class AvailabilityChart(Card):
    """Presets e grupos MPEG-H ao longo do tempo (faixas = onde existem na cena de áudio)."""
    ROW_H = 24

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self._rows: list = []
        self._dur = 1.0
        self._sel: set = set()
        self.setMinimumHeight(90)

    def set_data(self, scan: dict):
        dur = float(scan.get("duration") or 0)
        tl = scan.get("timeline") or []
        if not dur:
            dur = max([(t.get("end_s") or t.get("start_s") or 0) for t in tl] + [1.0])
        rows = [{"key": f"preset:{p['id']}", "label": f"P{p['id']}  {p['name']}", "kind": 0, "segs": []} for p in scan.get("presets", [])]
        rows += [{"key": f"group:{g['id']}", "label": f"G{g['id']}  {g['name']}", "kind": 1, "segs": []} for g in scan.get("groups", [])]
        idx = {r["key"]: r for r in rows}

        def add(r, a, b):
            if r["segs"] and abs(r["segs"][-1][1] - a) < 0.05:
                r["segs"][-1][1] = b
            else:
                r["segs"].append([a, b])

        for t in tl:
            if not t.get("has_scene"):
                continue
            a = float(t.get("start_s") or 0)
            b = t.get("end_s")
            b = dur if b is None else float(b)
            for pid in t.get("presets", {}):
                if f"preset:{pid}" in idx:
                    add(idx[f"preset:{pid}"], a, b)
            for gid in t.get("groups", {}):
                if f"group:{gid}" in idx:
                    add(idx[f"group:{gid}"], a, b)
        self._rows, self._dur = rows, dur
        self.setFixedHeight(len(rows) * self.ROW_H + 44)
        self.update()

    def set_selected(self, keys: set):
        self._sel = set(keys)
        self.update()

    def _geom(self):
        fm = self.fontMetrics()
        lw = min(230, max([fm.horizontalAdvance(r["label"]) for r in self._rows] + [60]) + 16)
        return lw, QRectF(lw + 8, 8, self.width() - lw - 20, len(self._rows) * self.ROW_H)

    def paintEvent(self, _):
        c = Colors(self)
        p = QPainter(self)
        self.paint_card(p, c)
        if not self._rows:
            p.setPen(c.muted)
            draw_text(p, self.width() / 2, self.height() / 2, "Analise o áudio para ver a disponibilidade dos presets e grupos.", "center")
            return
        lw, plot = self._geom()
        fm = p.fontMetrics()
        step = nice_step(self._dur, max(2, int(plot.width() // 90)))
        p.setPen(QPen(c.grid, 1))
        t = 0.0
        while t <= self._dur + 1e-6:
            x = plot.left() + plot.width() * t / self._dur
            p.drawLine(QPointF(x, plot.top()), QPointF(x, plot.bottom()))
            p.setPen(c.muted)
            tl_ = fmt_hms(t)
            hw = p.fontMetrics().horizontalAdvance(tl_) / 2
            draw_text(p, min(max(x, hw + 2), self.width() - hw - 2), plot.bottom() + 14, tl_, "center")
            p.setPen(QPen(c.grid, 1))
            t += step
        any_sel = bool(self._sel)
        for i, r in enumerate(self._rows):
            y = plot.top() + i * self.ROW_H
            on = (not any_sel) or r["key"] in self._sel
            p.setPen(c.fg if on else c.muted)
            p.drawText(QPointF(12, y + self.ROW_H / 2 + (fm.ascent() - fm.descent()) / 2), fm.elidedText(r["label"], Qt.TextElideMode.ElideRight, int(lw - 14)))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(c.grid)
            p.drawRoundedRect(QRectF(plot.left(), y + 5, plot.width(), self.ROW_H - 10), 3, 3)
            col = QColor(c.cat[2 if r["kind"] else 0])
            col.setAlpha(255 if on else 70)
            p.setBrush(col)
            for a, b in r["segs"]:
                x0 = plot.left() + plot.width() * a / self._dur
                x1 = plot.left() + plot.width() * b / self._dur
                p.drawRoundedRect(QRectF(x0, y + 5, max(2.0, x1 - x0), self.ROW_H - 10), 3, 3)

    def mouseMoveEvent(self, e):
        if not self._rows:
            return
        _, plot = self._geom()
        i = int((e.position().y() - plot.top()) // self.ROW_H)
        if 0 <= i < len(self._rows):
            r = self._rows[i]
            segs = ", ".join(f"{fmt_hms(a)}–{fmt_hms(b)}" for a, b in r["segs"][:6]) + (" …" if len(r["segs"]) > 6 else "")
            QToolTip.showText(e.globalPosition().toPoint(), f"{r['label']}\n{segs or 'indisponível'}", self)
        else:
            QToolTip.hideText()


class CutTimeline(Card):
    """Linha do tempo do vídeo: pontos de corte (IDR), cortes escolhidos e seleção por arrastar."""
    rangeDragged = Signal(float, float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setFixedHeight(120)
        self._dur, self._idr, self._cuts, self._times = 0.0, [], [], []
        self._x0 = None
        self._x1 = None
        self._hover = None

    def set_data(self, duration: float, idr: list):
        """idr: [(tempo, nº do quadro, tipo)]"""
        self._dur, self._idr = float(duration or 0), list(idr)
        self._times = [x[0] for x in self._idr]
        self.update()

    def set_cuts(self, cuts: list):
        self._cuts = list(cuts)
        self.update()

    def _plot(self) -> QRectF:
        return QRectF(14, 26, self.width() - 28, 52)

    def _t(self, x: float) -> float:
        pl = self._plot()
        return max(0.0, min(self._dur, (x - pl.left()) / pl.width() * self._dur))

    def paintEvent(self, _):
        c = Colors(self)
        p = QPainter(self)
        self.paint_card(p, c)
        if self._dur <= 0:
            p.setPen(c.muted)
            draw_text(p, self.width() / 2, self.height() / 2, "Leia os pontos de corte para ver a linha do tempo.", "center")
            return
        pl = self._plot()
        p.setPen(QPen(c.grid, 1))
        p.setBrush(c.window if c.dark else mix(c.window, c.bg, 0.5))
        p.drawRoundedRect(pl, 4, 4)
        # cortes
        for i, (a, b) in enumerate(self._cuts, 1):
            x0, x1 = pl.left() + pl.width() * a / self._dur, pl.left() + pl.width() * b / self._dur
            col = QColor(c.accent)
            col.setAlpha(70)
            p.setPen(QPen(c.accent, 1.5))
            p.setBrush(col)
            p.drawRect(QRectF(x0, pl.top(), max(2.0, x1 - x0), pl.height()))
            p.setPen(c.accent if not c.dark else c.fg)
            draw_text(p, x0 + 4, pl.top() - 9, f"#{i}")
        # arraste em andamento
        if self._x0 is not None and self._x1 is not None and abs(self._x1 - self._x0) > 2:
            pen = QPen(c.accent, 1.4)
            pen.setStyle(Qt.PenStyle.DashLine)
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(QRectF(min(self._x0, self._x1), pl.top(), abs(self._x1 - self._x0), pl.height()))
        # IDRs
        w = max(1, int(pl.width()))
        if len(self._times) * 2 <= w:
            p.setPen(QPen(c.cat[0], 1))
            for t in self._times:
                x = pl.left() + pl.width() * t / self._dur
                p.drawLine(QPointF(x, pl.top() + 16), QPointF(x, pl.bottom() - 4))
        else:
            cols = [0] * (w + 1)
            for t in self._times:
                cols[int(t / self._dur * w)] += 1
            mx = max(cols) or 1
            for i, n in enumerate(cols):
                if n:
                    col = QColor(c.cat[0])
                    col.setAlphaF(0.25 + 0.75 * n / mx)
                    p.setPen(QPen(col, 1))
                    p.drawLine(QPointF(pl.left() + i, pl.top() + 16), QPointF(pl.left() + i, pl.bottom() - 4))
        # eixo
        step = nice_step(self._dur, max(2, int(pl.width() // 90)))
        t = 0.0
        while t <= self._dur + 1e-6:
            x = pl.left() + pl.width() * t / self._dur
            p.setPen(QPen(c.muted, 1))
            p.drawLine(QPointF(x, pl.bottom()), QPointF(x, pl.bottom() + 4))
            p.setPen(c.muted)
            tl_ = fmt_hms(t)
            hw = p.fontMetrics().horizontalAdvance(tl_) / 2
            draw_text(p, min(max(x, hw + 2), self.width() - hw - 2), pl.bottom() + 15, tl_, "center")
            t += step
        if self._hover is not None and self._x0 is None:
            x = pl.left() + pl.width() * self._hover / self._dur
            p.setPen(QPen(c.fg, 1))
            p.drawLine(QPointF(x, pl.top() - 4), QPointF(x, pl.bottom()))

    def mousePressEvent(self, e):
        if self._dur > 0 and e.button() == Qt.MouseButton.LeftButton:
            self._x0 = self._x1 = e.position().x()

    def mouseMoveEvent(self, e):
        if self._dur <= 0:
            return
        if self._x0 is not None:
            self._x1 = e.position().x()
            self.update()
            return
        self._hover = self._t(e.position().x())
        i = bisect.bisect_left(self._times, self._hover)
        near = min((j for j in (i - 1, i) if 0 <= j < len(self._times)), key=lambda j: abs(self._times[j] - self._hover), default=None)
        txt = fmt_clock(self._hover)
        if near is not None:
            t, fr, kind = self._idr[near]
            txt += f"\nIDR mais próximo: {fmt_clock(t)}  (quadro {fr}{', ' + kind if kind not in ('', '?') else ''})"
        QToolTip.showText(e.globalPosition().toPoint(), txt, self)
        self.update()

    def mouseReleaseEvent(self, e):
        if self._x0 is not None:
            a, b = self._t(self._x0), self._t(e.position().x())
            moved = abs(e.position().x() - self._x0) > 4
            self._x0 = self._x1 = None
            self.update()
            if moved:
                self.rangeDragged.emit(min(a, b), max(a, b))

    def leaveEvent(self, _):
        self._hover = None
        self.update()


class TracksChart(Card):
    """Quadros por PID de uma captura TS, com erros de continuidade (cada linha: rótulo, contagem e barra)."""
    ROW_H = 44

    def __init__(self, parent=None):
        super().__init__(parent)
        self._tracks: list = []

    def set_tracks(self, tracks: list):
        self._tracks = list(tracks)
        self.setFixedHeight(max(1, len(self._tracks)) * self.ROW_H + 12)
        self.update()

    def paintEvent(self, _):
        c = Colors(self)
        p = QPainter(self)
        self.paint_card(p, c)
        mx = max([t.get("frames", 0) for t in self._tracks] + [1])
        kinds = {"VVC": 0, "LCEVC": 1, "MPEGH": 2}
        for i, t in enumerate(self._tracks):
            y = 6 + i * self.ROW_H
            p.setPen(c.fg)
            draw_text(p, 12, y + 11, f"PID 0x{int(t['pid']):04X}  ·  {t.get('kind', '')}")
            n = f"{t.get('frames', 0):,}".replace(",", ".")
            if t.get("cc_errors"):
                p.setPen(c.err)
                n += f" · {t['cc_errors']} erro(s) CC"
            else:
                p.setPen(c.muted)
                n += " quadros"
            draw_text(p, self.width() - 12, y + 11, n, "right")
            bar = QRectF(12, y + 24, self.width() - 24, 8)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(c.grid)
            p.drawRoundedRect(bar, 4, 4)
            p.setBrush(c.cat[kinds.get(t.get("kind"), 5)])
            p.drawRoundedRect(QRectF(bar.left(), bar.top(), max(6.0, bar.width() * t.get("frames", 0) / mx), bar.height()), 4, 4)


class SpeakerDiagram(Card):
    """Vista de cima do layout de caixas: círculo externo = plano horizontal, interno = canais de altura."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(200, 230)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._ch: list = []
        self._hi: set = set()
        self._name = ""

    def set_layout(self, name: str, channels: list, highlight: set | None = None):
        self._name, self._ch, self._hi = name, list(channels), set(highlight or [])
        self.update()

    def paintEvent(self, _):
        c = Colors(self)
        p = QPainter(self)
        self.paint_card(p, c)
        if not self._ch:
            p.setPen(c.muted)
            draw_text(p, self.width() / 2, self.height() / 2, "Selecione um item", "center")
            return
        cx, cy = self.width() / 2, self.height() / 2 + 10
        R = min(self.width() - 20, self.height() - 20) / 2 - 24
        p.setPen(QPen(c.grid.darker(115) if not c.dark else c.border, 1.2))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(QPointF(cx, cy), R, R)
        if any(ch["el"] == 1 for ch in self._ch):
            pen = QPen(c.border, 1)
            pen.setStyle(Qt.PenStyle.DashLine)
            p.setPen(pen)
            p.drawEllipse(QPointF(cx, cy), R * 0.55, R * 0.55)
        p.setPen(c.muted)
        draw_text(p, cx, cy - R - 30, "frente ▲", "center")
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(c.muted)
        p.drawEllipse(QPointF(cx, cy), 3, 3)
        for i, ch in enumerate(self._ch):
            on = (not self._hi) or i in self._hi
            if ch["el"] == -1:
                x, y, rad = cx + R * 0.0, cy - R * 0.22, 5
            else:
                r = R if ch["el"] == 0 else R * 0.55
                a = math.radians(ch["az"])
                x, y, rad = cx - r * math.sin(a), cy - r * math.cos(a), 6
            col = QColor(c.cat[0] if ch["el"] == 0 else (c.cat[1] if ch["el"] == 1 else c.cat[4]))
            col.setAlpha(255 if on else 70)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(col)
            if ch["el"] == 1:
                p.drawEllipse(QPointF(x, y), rad, rad)
                p.setBrush(c.bg)
                p.drawEllipse(QPointF(x, y), rad - 2.6, rad - 2.6)
            elif ch["el"] == -1:
                p.drawRect(QRectF(x - rad, y - rad, rad * 2, rad * 2))
            else:
                p.drawEllipse(QPointF(x, y), rad, rad)
            p.setPen(c.fg if on else c.muted)
            off = 15 if ch["el"] != 1 else -14
            dx, dy = (x - cx), (y - cy)
            d = math.hypot(dx, dy) or 1
            tx, ty = x + dx / d * off, y + dy / d * off
            if ch["el"] == -1:
                tx, ty = x + 17, y
            draw_text(p, tx, ty, ch["n"], "center")
        p.setPen(c.muted)
        draw_text(p, cx, self.height() - 10, self._name, "center")


class ImagePreview(Card):
    """Pré-visualização desenhada (proporção 16:9, ajustada à largura): nunca força a página a ficar mais larga."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._img = None
        sp = QSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        sp.setHeightForWidth(True)
        self.setSizePolicy(sp)
        self.setMinimumSize(120, 90)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, w):
        if self._img is not None and self._img.width():
            return min(380, max(90, int(w * self._img.height() / self._img.width())))
        return min(380, int(w * 9 / 16))

    def sizeHint(self):
        from PySide6.QtCore import QSize
        return QSize(320, int(320 * 9 / 16))

    def set_image(self, path: str) -> bool:
        from PySide6.QtGui import QImage
        img = QImage(path)
        if img.isNull():
            return False
        self._img = img
        self.updateGeometry()
        self.update()
        return True

    def clear_image(self):
        self._img = None
        self.update()

    def paintEvent(self, _):
        c = Colors(self)
        p = QPainter(self)
        self.paint_card(p, c)
        if self._img is None:
            p.setPen(c.muted)
            draw_text(p, self.width() / 2, self.height() / 2, "A pré-visualização aparece aqui", "center")
            return
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        r = QRectF(self.rect()).adjusted(4, 4, -4, -4)
        k = min(r.width() / self._img.width(), r.height() / self._img.height())
        w, h = self._img.width() * k, self._img.height() * k
        p.drawImage(QRectF(r.center().x() - w / 2, r.center().y() - h / 2, w, h), self._img)


# ───────────────────────────── painel de atividade (direita) ─────────────────────────────
def tint_frame(color: QColor, parent=None) -> QFrame:
    """Faixa de aviso/erro/sucesso: fundo = cor de estado bem diluída na cor da janela (vale para claro e escuro)."""
    fr = QFrame(parent)
    fr.setFrameShape(QFrame.Shape.NoFrame)
    fr.setAutoFillBackground(True)
    pal = fr.palette()
    pal.setColor(QPalette.ColorRole.Window, mix(color, pal.color(QPalette.ColorRole.Window), 0.16))
    fr.setPalette(pal)
    return fr


class StepRow(QWidget):
    """Um passo: ícone de estado, título, detalhe e (só enquanto roda) barra fina de progresso do passo."""

    def __init__(self, sid: str, label: str, weighted: bool, parent=None):
        super().__init__(parent)
        self.sid, self.weighted = sid, weighted
        self.icon = StatusIcon(20)
        self.label = QLabel(label)
        self.label.setWordWrap(True)
        self.detail = muted_label("")
        self.detail.setWordWrap(False)
        self.detail.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(5)
        self.bar.setVisible(False)
        col = QVBoxLayout()
        col.setSpacing(1)
        col.addWidget(self.label)
        col.addWidget(self.detail)
        col.addWidget(self.bar)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 7, 0, 7)
        lay.setSpacing(10)
        lay.addWidget(self.icon, 0, Qt.AlignmentFlag.AlignTop)
        lay.addLayout(col, 1)
        self._detail_full = ""
        self.state = StatusIcon.PENDING
        self.set_state(StatusIcon.PENDING)

    def set_state(self, state: int, detail: str = ""):
        self.state = state
        self.icon.set_state(state)
        f = self.label.font()
        f.setBold(state == StatusIcon.RUNNING)
        self.label.setFont(f)
        self.label.setEnabled(state != StatusIcon.PENDING)
        if detail or state in (StatusIcon.DONE, StatusIcon.ERROR, StatusIcon.SKIPPED, StatusIcon.PENDING):
            self.set_detail(detail)
        self.bar.setVisible(state == StatusIcon.RUNNING and self.weighted)
        if state == StatusIcon.DONE:
            self.bar.setValue(100)

    def set_detail(self, text: str):
        self._detail_full = text
        self.detail.setVisible(bool(text))
        self.detail.setToolTip(text)
        self._elide()

    def _elide(self):
        fm = QFontMetrics(self.detail.font())
        self.detail.setText(fm.elidedText(self._detail_full, Qt.TextElideMode.ElideRight, max(80, self.detail.width())))

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._elide()


class ResultItemDelegate(QStyledItemDelegate):
    """Lista de arquivos no estilo de lista do Windows 11: itens altos, seleção em retângulo arredondado com margem, sem zebra."""

    def sizeHint(self, opt, idx):
        sh = super().sizeHint(opt, idx)
        sh.setHeight(sh.height())                      # a altura mínima (34 px) já dá o respiro; sem folga extra no topo e na base
        sh.setWidth(sh.width() + 20)
        return sh

    def paint(self, p, option, idx):
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, idx)
        sel = bool(opt.state & QStyle.StateFlag.State_Selected)
        hov = bool(opt.state & QStyle.StateFlag.State_MouseOver)
        pal = opt.palette
        if sel or hov:                                  # cada coluna pinta o seu pedaço do mesmo retângulo arredondado (linha contínua)
            row = QRectF(0, option.rect.top(), opt.widget.viewport().width() if opt.widget else option.rect.width(), option.rect.height())
            p.save()
            p.setClipRect(option.rect)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(accent_of(pal) if sel else mix(pal.color(QPalette.ColorRole.Text), pal.color(QPalette.ColorRole.Base), 0.07))
            p.drawRoundedRect(row.adjusted(2, 2, -2, -2), RADIUS, RADIUS)
            p.restore()
        opt.state &= ~(QStyle.StateFlag.State_Selected | QStyle.StateFlag.State_MouseOver | QStyle.StateFlag.State_HasFocus)
        if sel:
            fg = on_color(accent_of(pal))
            pal.setColor(QPalette.ColorRole.Text, fg)
            pal.setColor(QPalette.ColorRole.WindowText, fg)
        opt.palette = pal
        opt.rect = opt.rect.adjusted(10 if idx.column() == 0 else 0, 0, -10 if idx.column() else 0, 0)
        QApplication.style().drawControl(QStyle.ControlElement.CE_ItemViewItem, opt, p, opt.widget)


class PageScroll(QScrollArea):
    """Área rolável sem moldura e sem rolagem horizontal; o conteúdo se adapta à largura."""

    def __init__(self, inner: QWidget):
        super().__init__()
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setWidget(inner)
        self.viewport().setAutoFillBackground(False)      # transparente: aparece o fundo da janela (Mica ou cor do tema)
        inner.setAutoFillBackground(False)

    def at_bottom(self) -> bool:
        sb = self.verticalScrollBar()
        return sb.value() >= sb.maximum() - 4

    def to_bottom(self):
        QTimer.singleShot(0, lambda: self.verticalScrollBar().setValue(self.verticalScrollBar().maximum()))


class AutoText(QTextEdit):
    """Registro somente leitura que cresce com o texto (sem barra própria)."""

    def __init__(self):
        super().__init__()
        self.setReadOnly(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        self.setWordWrapMode(QTextOption.WrapMode.WrapAnywhere)
        self.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.document().setMaximumBlockCount(3000)
        self.document().setDocumentMargin(2)
        self.document().documentLayout().documentSizeChanged.connect(self._fit)
        self.setMinimumHeight(24)

    def _fit(self, size):
        self.setFixedHeight(int(size.height()) + 6)

    def add_line(self, line: str):
        cur = self.textCursor()
        cur.movePosition(QTextCursor.MoveOperation.End)
        cur.insertText(line + "\n")

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._fit(self.document().size())


class ResultTab(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.banner_host = QVBoxLayout()
        self.banner_host.setContentsMargins(0, 0, 0, 0)
        self.files = QTreeWidget()
        self.files.setColumnCount(2)
        self.files.setHeaderHidden(True)
        self.files.setRootIsDecorated(False)
        self.files.setUniformRowHeights(True)
        self.files.header().setStretchLastSection(False)
        self.files.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.files.header().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.files.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.files.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.files.setAlternatingRowColors(False)
        self.files.setItemDelegate(ResultItemDelegate(self.files))
        self.files.setProperty("ownRowFill", True)          # o delegate pinta o destaque; o estilo não pinta o fundo da linha
        self.files.itemDoubleClicked.connect(lambda *_: self.open_selected())
        fit_view(self.files)
        self.where = muted_label("")
        self.where.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.open_btn = AccentButton("Abrir", compact=True)
        self.show_btn = QPushButton("Mostrar na pasta")
        self.copy_btn = QPushButton("Copiar")
        self.copy_btn.setToolTip("Copiar o caminho do arquivo")
        set_compact(self.show_btn)
        set_compact(self.copy_btn)
        self.open_btn.clicked.connect(self.open_selected)
        self.show_btn.clicked.connect(self.show_selected)
        self.copy_btn.clicked.connect(self.copy_selected)
        self.btn_row = QWidget()
        btns = QHBoxLayout(self.btn_row)
        btns.setContentsMargins(0, 0, 0, 0)
        for b in (self.open_btn, self.show_btn, self.copy_btn):
            btns.addWidget(b)
        btns.addStretch(1)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        self.summary.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.notes_host = QVBoxLayout()
        self.notes_host.setContentsMargins(0, 0, 0, 0)
        self.extra = QVBoxLayout()
        self.extra.setSpacing(6)
        self.empty = muted_label("Os arquivos gerados e o resumo aparecem aqui quando o processo terminar.", small=False)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 8, 0, 8)
        lay.setSpacing(10)
        lay.addLayout(self.banner_host)
        lay.addWidget(self.empty)
        lay.addWidget(self.files)
        lay.addWidget(self.where)
        lay.addWidget(self.btn_row)
        lay.addWidget(self.summary)
        lay.addLayout(self.notes_host)
        lay.addLayout(self.extra)
        lay.addStretch(1)
        self.files.itemSelectionChanged.connect(self._sync)
        self._notes: list = []
        self._banner = None
        self.reset()

    def _clear(self, layout):
        while layout.count():
            it = layout.takeAt(0)
            if it.widget():
                it.widget().deleteLater()

    def reset(self):
        self.files.clear()
        self._notes = []
        self._clear(self.notes_host)
        self._clear(self.banner_host)
        self.clear_extra()
        self.summary.setText("")
        self.summary.setVisible(False)
        self.empty.setVisible(True)
        self._sync()

    def clear_extra(self):
        self._clear(self.extra)

    def add_extra(self, widget: QWidget, caption: str = ""):
        if caption:
            self.extra.addWidget(muted_label(caption))
        self.extra.addWidget(widget)

    def set_banner(self, state: int, text: str):
        self._clear(self.banner_host)
        c = Colors(self)
        col = {StatusIcon.DONE: c.ok, StatusIcon.ERROR: c.err, StatusIcon.SKIPPED: c.warn}.get(state, c.muted)
        fr = tint_frame(col)
        h = QHBoxLayout(fr)
        h.setContentsMargins(10, 8, 10, 8)
        ic = StatusIcon(20)
        ic.set_state(state)
        lb = QLabel(text)
        lb.setWordWrap(True)
        lb.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        h.addWidget(ic, 0, Qt.AlignmentFlag.AlignTop)
        h.addWidget(lb, 1)
        self.banner_host.addWidget(fr)
        self.empty.setVisible(False)

    def add_result(self, res: dict):
        for o in res.get("outputs") or []:
            it = QTreeWidgetItem([os.path.basename(o["path"]), human(o.get("size", 0))])
            it.setData(0, Qt.ItemDataRole.UserRole, o["path"])
            it.setToolTip(0, f"{o.get('title', '')}\n{o['path']}".strip())
            it.setIcon(0, self.style().standardIcon(QStyle.StandardPixmap.SP_FileIcon))
            self.files.addTopLevelItem(it)
        if self.files.topLevelItemCount():
            if not self.files.selectedItems():
                self.files.topLevelItem(0).setSelected(True)
            self.where.setText("Pasta: " + os.path.dirname(self.files.topLevelItem(0).data(0, Qt.ItemDataRole.UserRole)))
            self.empty.setVisible(False)
        rows = res.get("rows") or []
        if rows:
            self.summary.setText("<br>".join(f"<b>{k}</b>: {v}" for k, v in rows))
            self.summary.setVisible(True)
        fresh = [n for n in (res.get("notes") or []) if n not in self._notes]
        if fresh:
            self._notes += fresh
            self._clear(self.notes_host)
            fr = tint_frame(Colors(self).warn)
            h = QVBoxLayout(fr)
            h.setContentsMargins(10, 8, 10, 8)
            lb = QLabel("⚠ " + "\n⚠ ".join(self._notes))
            lb.setWordWrap(True)
            lb.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            h.addWidget(lb)
            self.notes_host.addWidget(fr)
        self._sync()

    def _paths(self):
        its = self.files.selectedItems() or ([self.files.topLevelItem(0)] if self.files.topLevelItemCount() == 1 else [])
        return [i.data(0, Qt.ItemDataRole.UserRole) for i in its if i]

    def _sync(self):
        has = self.files.topLevelItemCount() > 0
        self.files.setVisible(has)
        self.where.setVisible(has)
        self.btn_row.setVisible(has)
        sel = bool(self._paths())
        for b in (self.open_btn, self.show_btn, self.copy_btn):
            b.setEnabled(sel)

    def open_selected(self):
        for p in self._paths():
            QDesktopServices.openUrl(QUrl.fromLocalFile(p))

    def show_selected(self):
        for p in self._paths():
            reveal_in_file_manager(p)

    def copy_selected(self):
        QGuiApplication.clipboard().setText("\n".join(self._paths()))


def reveal_in_file_manager(path: str):
    if sys.platform == "win32":
        QProcess.startDetached("explorer.exe", ["/select,", os.path.normpath(path)])
    elif sys.platform == "darwin":
        QProcess.startDetached("open", ["-R", path])
    else:
        QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.dirname(path)))


class TaskPanel(QWidget):
    """Painel “Atividade”: estado + progresso total do processo atual e abas Progresso / Resultado / Registro."""
    stopRequested = Signal(bool)

    def paintEvent(self, e):
        # camada levemente diferente do fundo (sem linha divisória); é translúcida, então o Mica continua aparecendo
        dark = self.palette().color(QPalette.ColorRole.Window).lightness() < 128
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(255, 255, 255, 12) if dark else QColor(255, 255, 255, 110))
        p.end()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedWidth(372)
        h = QLabel("Atividade")
        f = h.font()
        f.setBold(True)
        f.setPointSizeF(f.pointSizeF() * 1.15)
        h.setFont(f)
        self.stop_btn = QPushButton("Parar")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self._stop_clicked)
        top = QHBoxLayout()
        top.addWidget(h)
        top.addStretch(1)
        top.addWidget(self.stop_btn)

        self.card = CardBox()
        self.state_lb = QLabel("Ocioso")
        sf = self.state_lb.font()
        sf.setBold(True)
        sf.setPointSizeF(max(sf.pointSizeF() - 0.5, 7))
        self.state_lb.setFont(sf)
        self.title = QLabel("Nenhum processo em execução")
        tf = self.title.font()
        tf.setBold(True)
        self.title.setFont(tf)
        self.title.setWordWrap(True)
        self.title.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.overall = QProgressBar()
        self.overall.setRange(0, 100)
        self.overall.setValue(0)
        self.overall.setTextVisible(False)
        self.overall.setFixedHeight(6)
        self.time_lb = muted_label("")
        self.time_lb.setWordWrap(False)
        self.pct_lb = muted_label("")
        self.pct_lb.setWordWrap(False)
        tr = QHBoxLayout()
        tr.addWidget(self.time_lb, 1)
        tr.addWidget(self.pct_lb)
        for w in (self.state_lb, self.title, self.overall):
            self.card.body.addWidget(w)
        self.card.body.addLayout(tr)
        self.card.body.setSpacing(4)
        self.card.body.setContentsMargins(0, 0, 0, 0)

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.tabBar().setDrawBase(False)       # sem a faixa de fundo (e a linha) que o estilo desenha depois da última aba
        self.tabs.tabBar().setExpanding(False)
        self.steps_host = QWidget()
        self.steps_lay = QVBoxLayout(self.steps_host)
        self.steps_lay.setContentsMargins(0, 6, 0, 6)
        self.steps_lay.setSpacing(0)
        self.steps_lay.addStretch(1)
        self.steps_empty = muted_label("As etapas aparecem aqui quando você iniciar um processo.", small=False)
        self.steps_lay.insertWidget(0, self.steps_empty)
        self.steps_scroll = PageScroll(self.steps_host)
        self.result = ResultTab()
        self.result_scroll = PageScroll(self.result)
        self.log = AutoText()
        logw = QWidget()
        ll = QVBoxLayout(logw)
        ll.setContentsMargins(0, 8, 0, 8)
        ll.addWidget(self.log)
        lb = QHBoxLayout()
        cp, cl = QPushButton("Copiar"), QPushButton("Limpar")
        cp.clicked.connect(lambda: QGuiApplication.clipboard().setText(self.log.toPlainText()))
        cl.clicked.connect(self.log.clear)
        lb.addStretch(1)
        lb.addWidget(cp)
        lb.addWidget(cl)
        ll.addLayout(lb)
        ll.addStretch(1)
        self.log_scroll = PageScroll(logw)
        self.tabs.addTab(self.steps_scroll, "Progresso")
        self.tabs.addTab(self.result_scroll, "Resultado")
        self.tabs.addTab(self.log_scroll, "Registro")

        self.footer = muted_label("Pronto")
        self.footer.setWordWrap(False)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 14, 16, 8)
        lay.setSpacing(10)
        lay.addLayout(top)
        lay.addWidget(self.card)
        lay.addWidget(self.tabs, 1)
        lay.addWidget(self.footer)

        self._rows: dict = {}
        self._order: list = []
        self._weights: dict = {}
        self._job = None
        self._t0 = 0.0
        self._pct = 0.0
        self._running = False
        self._force = False
        self._state = "idle"
        self._clock = QTimer(self, interval=1000)
        self._clock.timeout.connect(self._tick)

    # ───── estado visual ─────
    def _paint_state(self, kind: str, text: str):
        """kind: idle | run | ok | err | stop"""
        self._state = kind
        c = Colors(self)
        col = {"idle": c.muted, "run": c.accent if not c.dark else QColor("#4cc2ff"), "ok": c.ok, "err": c.err, "stop": c.warn}[kind]
        pal = self.state_lb.palette()
        pal.setColor(QPalette.ColorRole.WindowText, col)
        self.state_lb.setPalette(pal)
        self.state_lb.setText(text)
        bp = self.overall.palette()
        bp.setColor(QPalette.ColorRole.Highlight, col if kind in ("err", "stop", "ok") else c.accent)
        self.overall.setPalette(bp)

    def set_live_hint(self, text: str):
        """Indicador simples de que a execução está em curso (ex.: “Gravando ao vivo”), sem detalhes."""
        if self._running:
            self._paint_state("run", text)

    def set_footer(self, text: str):
        self.footer.setText(text)

    # ───── ciclo ─────
    def begin_queue(self, n: int):
        self.result.reset()
        self.log.clear()

    def begin_job(self, index: int, total: int, job):
        self._job = job
        self._running, self._force, self._pct = True, bool(job.force_only), 0.0
        self._t0 = time.time()
        q = f" · item {index + 1} de {total}" if total > 1 else ""
        self.title.setText(job.title + q)
        self._paint_state("run", "Em execução")
        self.stop_btn.setText(job.stop_label)
        self.stop_btn.setEnabled(True)
        self.overall.setRange(0, 0) if job.indeterminate else self.overall.setRange(0, 100)
        self.overall.setValue(0)
        self.pct_lb.setText("")
        for r in self._rows.values():
            r.deleteLater()
        self._rows, self._order = {}, []
        self._weights = {}
        self.steps_empty.setText("Preparando…")
        self.steps_empty.setVisible(True)
        self.tabs.setCurrentIndex(0)
        self.log.add_line(f"─── {job.title} ───")
        self.set_footer("Em execução…")
        self._clock.start()
        self._tick()

    def set_plan(self, steps: list):
        """Evento `begin` do script: as etapas e os pesos vêm dele, a interface não tem tabela própria."""
        for r in self._rows.values():
            r.deleteLater()
        self._rows, self._order = {}, []
        self._weights = {s["id"]: float(s.get("weight", 0) or 0) for s in steps}
        self.steps_empty.setVisible(False)
        for s in steps:
            self._add_row(s["id"], s.get("label") or s["id"])

    def _add_row(self, sid: str, label: str):
        row = StepRow(sid, label, self._weights.get(sid, 0) > 0)
        self.steps_lay.insertWidget(self.steps_lay.count() - 1, row)
        self._rows[sid] = row
        self._order.append(sid)

    def set_step(self, sid: str, status: str, detail: str):
        if sid not in self._rows:
            self._add_row(sid, sid)
        st = {"running": StatusIcon.RUNNING, "done": StatusIcon.DONE, "error": StatusIcon.ERROR, "skipped": StatusIcon.SKIPPED}.get(status)
        if st is not None:
            self._rows[sid].set_state(st, detail)

    def set_progress(self, pct: float, detail: str, stage: str = "", frac: float = -1.0):
        self._pct = pct
        busy = self._job is not None and self._job.indeterminate and pct <= 0.1
        self.overall.setRange(0, 0) if busy else self.overall.setRange(0, 100)
        if not busy:
            self.overall.setValue(int(pct))
            self.pct_lb.setText(f"{int(pct)}%")
        row = self._rows.get(stage)
        if row is None:
            return
        if frac >= 0 and self._weights.get(stage, 0) > 0:
            row.bar.setValue(int(max(0.0, min(1.0, frac)) * 100))     # fração da etapa vem pronta do script
        if detail and row.state == StatusIcon.RUNNING:
            row.set_detail(detail)

    def end_job(self, job, code: int, ok: bool):
        self._running = False
        self._clock.stop()
        self.stop_btn.setEnabled(False)
        for sid in self._order:
            r = self._rows[sid]
            if r.state == StatusIcon.RUNNING:
                r.set_state(StatusIcon.DONE if ok else StatusIcon.ERROR)
            elif r.state == StatusIcon.PENDING:
                r.set_state(StatusIcon.SKIPPED, "não executada")
        self.overall.setRange(0, 100)
        if ok:
            self.overall.setValue(100)
            self.pct_lb.setText("100%")
        self._tick()

    def end_queue(self, all_ok: bool, stopped: bool):
        self.stop_btn.setEnabled(False)
        self.stop_btn.setText("Parar")
        if stopped:
            self._paint_state("stop", "Interrompido")
            self.set_footer("Interrompido")
        elif all_ok:
            self._paint_state("ok", "Concluído")
            self.set_footer("Concluído")
        else:
            self._paint_state("err", "Terminou com erro")
            self.set_footer("Terminou com erro")

    def _tick(self):
        if self._job is None:
            return
        el = time.time() - self._t0
        txt = f"decorrido {fmt_hms(el)}"
        if self._running and self._pct >= 3 and not self._job.indeterminate:
            txt += f" · restam ~{fmt_hms(el * (100 - self._pct) / self._pct)}"
        self.time_lb.setText(txt)

    def _stop_clicked(self):
        self.stopRequested.emit(self._force)
        self._force = True
        self.stop_btn.setText("Forçar parada")

    def append_log(self, line: str):
        follow = self.log_scroll.at_bottom()
        self.log.add_line(line)
        if follow and self.tabs.currentWidget() is self.log_scroll:
            self.log_scroll.to_bottom()
