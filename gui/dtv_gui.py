#!/usr/bin/env python3
"""
dtv_gui.py - interface gráfica nativa (Qt 6 / PySide6) para o dtv_tools.py.

  python gui/dtv_gui.py             abre a interface
  python gui/dtv_gui.py --cli ...   (só no app congelado) executa o dtv_tools.py embutido: é como as tarefas rodam em segundo plano
  python gui/dtv_gui.py --selftest  abre e fecha a janela (teste de fumaça para a CI)

A GUI não importa o dtv_tools: ela monta a linha de comando, roda o subcomando como processo filho com
`--json --control` (protocolo v2, docs/PROTOCOL.md) e desenha os eventos (etapas e pesos vêm do evento `begin`).
"""
import os
import sys

APP_NAME = "DTV Tools"
APP_VERSION = "1.3.0"


if __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] == "--cli":
    # app congelado: o executável é a própria GUI, então ele roda o script embutido (em modo fonte o Runner chama o script direto)
    import dtv_tools as _dtv
    sys.exit(_dtv.main(sys.argv[2:]))

import json  # noqa: E402
import re  # noqa: E402
from pathlib import Path  # noqa: E402

from PySide6.QtCore import QRectF, QSettings, QSize, QStandardPaths, Qt, QTime, QTimer, Signal  # noqa: E402
from PySide6.QtGui import QAction, QActionGroup, QColor, QGuiApplication, QIcon, QPainter, QPalette  # noqa: E402
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout,  # noqa: E402
                               QFrame, QGridLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMainWindow,
                               QMessageBox, QPushButton, QRadioButton, QScrollArea, QSizePolicy, QSpinBox, QSplitter,
                               QStackedWidget, QStyle, QStyledItemDelegate, QTableWidget, QTableWidgetItem, QTimeEdit, QTreeWidget,
                               QTreeWidgetItem, QVBoxLayout, QWidget, QListWidget, QListWidgetItem, QFileDialog, QInputDialog, QMenu)

import cutmath  # noqa: E402
from dtv_gui_notify import Notifier, summarize
from dtv_gui_presets import PresetStore  # noqa: E402
from dtv_gui_runner import PROTOCOL, Job, Runner, program_and_prefix  # noqa: E402
from dtv_gui_style import AccentButton, FluentStyle, accent_of, apply_backdrop, apply_fluent, mica_supported  # noqa: E402
from dtv_gui_widgets import (CardBox, WheelGuard, svg_icon, fit_view, AvailabilityChart, Collapsible, Colors, CutTimeline, FileEdit, ImagePreview, ItemList,  # noqa: E402
                             SpeakerDiagram, StatusIcon, TaskPanel, TracksChart, fmt_clock, fmt_hms, human, muted_label, mix)


def left(*widgets) -> QHBoxLayout:
    """Botões com largura natural, alinhados à esquerda (em vez de esticados pela coluna do formulário)."""
    h = QHBoxLayout()
    h.setContentsMargins(0, 0, 0, 0)
    for w in widgets:
        h.addWidget(w)
    h.addStretch(1)
    return h


class UserError(Exception):
    """Entrada incompleta/inválida: mostrada junto ao botão, sem abrir diálogo."""


VIDEO_FILTER = "Vídeo MP4 (*.mp4 *.m4v *.mov);;Todos os arquivos (*)"
TMPDIR_CMDS = {"ts-tv3", "mpegh-scan", "mpegh-audio", "transcode", "frame", "dash-capture"}
CACHE_CMDS = {"mpegh-scan", "mpegh-audio", "transcode"}
NATIVE_LAYOUTS = [("Detectar do stream", 0), ("2.0 (estéreo)", 2), ("5.1", 6), ("5.1.4", 16), ("7.1.4", 19)]
TOOL_ROWS = [("mp4box", "MP4Box"), ("ffmpeg", "ffmpeg (build DTV+/LCEVC)"), ("mpegh_ui", "mpeghUiManager"), ("mpegh_decoder", "mpeghDecoder")]


# usado só se o dtv_tools não informar a tabela de codificadores no `describe` (versão antiga)
FALLBACK_ENCODERS = {"x265": {"label": "x265 (CPU)", "crf": 18, "crf_range": [0, 51], "step": 0.5, "preset_default": "", "params_hint": "key=value:key=value",
                              "presets": ["ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow", "placebo"]}}


def bundled_tools_dir() -> Path:
    if getattr(sys, "frozen", False):
        exe = Path(sys.executable).parent
        cands = [exe / "tools", exe / "_internal" / "tools", exe.parent / "Resources" / "tools", exe.parent / "Frameworks" / "tools"]
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            cands.insert(0, Path(meipass) / "tools")
    else:
        cands = [Path(__file__).resolve().parent.parent / "tools"]      # modo fonte: tools/ na raiz do projeto, ao lado do dtv_tools.py
    return next((c for c in cands if c.is_dir()), cands[0])


def presets_path() -> Path:
    return config_path().parent / "transcode_presets.json"


def config_path() -> Path:
    d = Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppConfigLocation) or Path.home() / ".dtv_tools")
    d.mkdir(parents=True, exist_ok=True)
    return d / "dtv_tools.json"


# ═════════════════════════════ base das páginas ═════════════════════════════
class Page(QWidget):
    title = ""
    subtitle = ""

    def __init__(self, win):
        super().__init__()
        self.win = win
        self._busy = False
        self.action_buttons: list = []
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        head = QWidget()
        hl = QVBoxLayout(head)
        hl.setContentsMargins(26, 20, 26, 8)
        hl.setSpacing(2)
        t = QLabel(self.title)
        f = t.font()
        f.setPointSizeF(f.pointSizeF() * 1.45)
        f.setBold(True)
        t.setFont(f)
        hl.addWidget(t)
        hl.addWidget(muted_label(self.subtitle, small=False))
        root.addWidget(head)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.viewport().setAutoFillBackground(False)
        self.body = QWidget()
        self.body_lay = QVBoxLayout(self.body)
        self.body_lay.setContentsMargins(26, 8, 26, 16)
        self.body_lay.setSpacing(12)
        self.scroll.setWidget(self.body)
        self.body.setAutoFillBackground(False)      # QScrollArea.setWidget liga o preenchimento: sem desligar, o conteúdo fica opaco e esconde o Mica
        root.addWidget(self.scroll, 1)
        foot = QFrame()
        foot.setFrameShape(QFrame.Shape.NoFrame)
        self.footer = QHBoxLayout(foot)
        self.footer.setContentsMargins(26, 10, 26, 12)
        self.msg = QLabel("")
        self.msg.setWordWrap(True)
        self.footer.addWidget(self.msg, 1)
        root.addWidget(foot)
        self.build(self.body_lay)
        self.body_lay.addStretch(1)

    # ── a implementar ──
    def build(self, lay):
        pass

    def handle_result(self, job, res):
        pass

    def handle_log(self, job, line):
        pass

    def handle_error(self, job, msg):
        pass

    # ── comum ──
    def primary(self, text: str, slot, default: bool = True) -> QPushButton:
        b = AccentButton(text)
        b.clicked.connect(slot)
        if default:
            b.setDefault(True)
        self.footer.addWidget(b)
        self.action_buttons.append(b)
        return b

    def show_error(self, text: str):
        c = Colors(self)
        pal = self.msg.palette()
        pal.setColor(QPalette.ColorRole.WindowText, c.err)
        self.msg.setPalette(pal)
        self.msg.setText(text)

    def set_busy(self, busy: bool):
        self._busy = busy
        self.body.setEnabled(not busy)
        self.update_buttons()

    def update_buttons(self):
        for b in self.action_buttons:
            b.setEnabled(not self._busy)

    def run(self, jobs):
        self.show_error("")
        self.win.run(jobs)

    def run_built(self, builder):
        try:
            jobs = builder()
        except UserError as e:
            self.show_error(str(e))
            return
        self.run(jobs)

    @staticmethod
    def group(title: str) -> tuple:
        g = CardBox(title)
        return g, g.form()

    @staticmethod
    def spin(lo, hi, val, step=1, dec=0, special="", suffix=""):
        s = QDoubleSpinBox() if dec else QSpinBox()
        if dec:
            s.setDecimals(dec)
        s.setRange(lo, hi)
        s.setValue(val)
        s.setSingleStep(step)
        if special:
            s.setSpecialValueText(special)
        if suffix:
            s.setSuffix(suffix)
        s.setMaximumWidth(200)
        return s

    def extra(self, cmd: str) -> list:
        return self.win.extra_args(cmd)


# ═════════════════════════════ TS → MP4 ═════════════════════════════
class TsPage(Page):
    title = "MPEG-TS → MP4"
    subtitle = "Padroniza uma captura MPEG-TS -> MP4 com áudio MPEG-H em duas opções de formato: single-track (LCEVC embutido na trilha VVC) e/ou dual-track (VVC e LCEVC de trilhas separadas)"

    def build(self, lay):
        g = CardBox("Capturas de entrada")
        gl = g.body
        self.files = ItemList("files", "Capturas TS (*.ts *.mts *.m2ts *.trp *.mpg);;Todos os arquivos (*)", key="ts_in")
        gl.addWidget(self.files)
        gl.addWidget(muted_label("Se vários arquivos forem selecionados, serão processados em sequência. Arraste arquivos para a lista."))
        lay.addWidget(g)

        g, f = self.group("Saída")
        self.chk_single = QCheckBox("Single-track (VVC com LCEVC embutido no padrão SEI T.35)")
        self.chk_dual = QCheckBox("Dual-track  (VVC e LCEVC em trilhas separadas)")
        self.chk_single.setChecked(True)
        self.chk_dual.setChecked(False)
        f.addRow("Gerar", self.chk_single)
        f.addRow("", self.chk_dual)
        self.outdir = FileEdit("dir", placeholder="Mesma pasta do arquivo de entrada", key="ts_out")
        f.addRow("Pasta de saída", self.outdir)
        self.name = QLineEdit()
        self.name.setPlaceholderText("Nome do arquivo de entrada")
        f.addRow("Nome base", self.name)
        self.overwrite = QCheckBox("Substituir arquivos existentes")
        f.addRow("", self.overwrite)
        lay.addWidget(g)
        self.files.changed.connect(lambda: self.name.setEnabled(len(self.files.values()) <= 1))

        adv = Collapsible("Avançado: programa, PIDs e frame rate")
        row = QHBoxLayout()
        self.inspect_btn = QPushButton("Inspecionar PIDs do 1º arquivo")
        self.inspect_btn.clicked.connect(self.inspect)
        row.addWidget(self.inspect_btn)
        row.addStretch(1)
        adv.content.addLayout(row)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["PID", "stream_type", "Provável", "Papel"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        fit_view(self.table, 60)
        adv.content.addWidget(self.table)
        adv.content.addWidget(muted_label("Caso não seja possível identificar corretamente os PIDs de cada trilha, escolha o 'Papel' correto de cada trilha"))
        f2 = QFormLayout()
        self.program = self.spin(0, 65535, 0, special="Automático (primeiro da PAT)")
        self.fps = self.spin(0, 240, 0, 0.01, 3, special="Medido da captura")
        self.lcevc_size = QLineEdit("3840x2160")
        f2.addRow("Programa", self.program)
        f2.addRow("Frame rate", self.fps)
        f2.addRow("Tamanho da trilha lvc1", self.lcevc_size)
        adv.content.addLayout(f2)
        lay.addWidget(adv)
        self.primary("Converter", lambda: self.run_built(self.build_jobs))
        self.action_buttons.append(self.inspect_btn)
        self._inspect_lines: list = []

    def _role_args(self) -> list:
        a = []
        for r in range(self.table.rowCount()):
            cb = self.table.cellWidget(r, 3)
            flag = {"VVC": "--vvc-pid", "LCEVC": "--lcevc-pid", "MPEG-H": "--mpegh-pid"}.get(cb.currentText())
            if flag:
                a += [flag, self.table.item(r, 0).text()]
        return a

    def inspect(self):
        files = self.files.values()
        if not files:
            self.show_error("Adicione ao menos um arquivo TS para inspecionar.")
            return
        self._inspect_lines = []
        self.run([Job("Inspecionando PIDs", ["ts-tv3", files[0], "--dry-run"], kind="ts-inspect", page=self, meta={"quiet": True})])

    def build_jobs(self):
        files = self.files.values()
        if not files:
            raise UserError("Adicione ao menos um arquivo TS.")
        outs = [k for k, c in (("single", self.chk_single), ("dual", self.chk_dual)) if c.isChecked()]
        if not outs:
            raise UserError("Marque ao menos uma saída (single-track ou dual-track).")
        jobs = []
        for fpath in files:
            a = ["ts-tv3", fpath, "--outputs", ",".join(outs), "--lcevc-size", self.lcevc_size.text().strip() or "3840x2160"]
            if self.outdir.text():
                a += ["--outdir", self.outdir.text()]
            if self.name.text().strip() and len(files) == 1:
                a += ["--name", self.name.text().strip()]
            if self.program.value():
                a += ["--program", str(self.program.value())]
            if self.fps.value():
                a += ["--fps", str(self.fps.value())]
            if self.overwrite.isChecked():
                a.append("--overwrite")
            a += self._role_args() + self.extra("ts-tv3")
            jobs.append(Job(f"TS → MP4 · {os.path.basename(fpath)}", a, kind="ts-tv3", page=self))
        return jobs

    def handle_log(self, job, line):
        if job.kind == "ts-inspect":
            self._inspect_lines.append(line)

    def handle_result(self, job, res):
        if job.kind == "ts-inspect":
            self.table.setRowCount(0)
            guess = {0x33: "VVC", 0x36: "LCEVC", 0x2D: "MPEG-H", 0x2E: "MPEG-H"}
            for ln in self._inspect_lines:
                m = re.search(r"PID 0x([0-9A-Fa-f]+)\s+stream_type 0x([0-9A-Fa-f]+)", ln)
                if not m:
                    continue
                r = self.table.rowCount()
                self.table.insertRow(r)
                pid, st = int(m.group(1), 16), int(m.group(2), 16)
                for c, txt in enumerate((f"0x{pid:04X}", f"0x{st:02X}", guess.get(st, "—"))):
                    self.table.setItem(r, c, QTableWidgetItem(txt))
                self.table.item(r, 0).setText(f"0x{pid:04X}")
                cb = QComboBox()
                cb.addItems(["Automático", "VVC", "LCEVC", "MPEG-H", "Ignorar"])
                self.table.setCellWidget(r, 3, cb)
            return
        tracks = res.get("tracks")
        if tracks:
            ch = TracksChart()
            ch.set_tracks(tracks)
            self.win.panel.result.add_extra(ch, "Trilhas encontradas · " + os.path.basename(job.args[1]))


# ═════════════════════════════ catálogo de áudio MPEG-H ═════════════════════════════
class CatalogView(QWidget):
    """Árvore de presets / grupos / pares com seleção (caixas), formato e bitrate por linha."""
    def __init__(self):
        super().__init__()
        self._cb_sel = []
        self._cb_cur = []
        self.tree = QTreeWidget()
        self.tree.setColumnCount(4)
        self.tree.setHeaderLabels(["Item", "Layout", "Formato", "Bitrate"])
        self.tree.setUniformRowHeights(False)
        self.tree.setAlternatingRowColors(True)
        h = self.tree.header()
        h.setStretchLastSection(False)
        h.setMinimumSectionSize(80)
        h.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for c_, w_ in ((1, 135), (2, 120), (3, 78)):
            h.setSectionResizeMode(c_, QHeaderView.ResizeMode.Fixed)
            h.resizeSection(c_, w_)
        self.tree.setTextElideMode(Qt.TextElideMode.ElideRight)
        fit_view(self.tree, 80)
        self.b_presets = QPushButton("Todos os presets")
        self.b_all = QPushButton("Marcar tudo")
        self.b_none = QPushButton("Desmarcar tudo")
        self.count = muted_label("")
        self.count.setWordWrap(False)
        row = QHBoxLayout()
        for b in (self.b_presets, self.b_all, self.b_none):
            row.addWidget(b)
        row.addStretch(1)
        row.addWidget(self.count)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.tree)
        lay.addLayout(row)
        self.b_presets.clicked.connect(lambda: self._mark(lambda o: o["group"] == "presets"))
        self.b_all.clicked.connect(lambda: self._mark(lambda o: True))
        self.b_none.clicked.connect(lambda: self._mark(lambda o: False))
        self.tree.itemChanged.connect(lambda *_: self._changed())
        self.tree.currentItemChanged.connect(self._current)
        self._rows: dict = {}
        self._updating = False
        self.scan: dict | None = None

    def on_selection(self, fn):
        self._cb_sel.append(fn)

    def on_current(self, fn):
        self._cb_cur.append(fn)

    def clear(self):
        self.tree.clear()
        self._rows = {}
        self.scan = None
        self.count.setText("")

    def set_scan(self, scan: dict):
        self._updating = True
        self.tree.clear()
        self._rows = {}
        self.scan = scan
        fmts, layouts = scan["formats"], scan["layouts"]
        names = {"presets": "Presets", "groups": "Grupos (Audio Elements)", "pairs": "Pares estéreo L/R"}
        tops = {}
        for o in scan["options"]:
            sec = "presets" if o["group"] == "presets" else ("pairs" if o["id"].startswith("pair:") else "groups")
            if sec not in tops:
                top = QTreeWidgetItem([names[sec]])
                top.setFlags(Qt.ItemFlag.ItemIsEnabled)
                f = top.font(0)
                f.setBold(True)
                top.setFont(0, f)
                self.tree.addTopLevelItem(top)
                top.setExpanded(True)
                tops[sec] = top
            lname = lambda c: layouts.get(str(c), {}).get("name", "?")
            orig = o.get("layout_orig", o["layout"])
            choices = o.get("layout_choices") or [o["layout"]]
            it = QTreeWidgetItem(tops[sec], [o["title"], f"{lname(o['layout'])} · original" if len(choices) < 2 else "", "", ""])
            it.setToolTip(1, f"{lname(o['layout']) } · {o['out_ch']} canais" + (f"\n{o['composition']}" if o.get("composition") else ""))
            it.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsSelectable)
            it.setCheckState(0, Qt.CheckState.Checked if sec == "presets" else Qt.CheckState.Unchecked)
            it.setToolTip(0, o["desc"])
            if o.get("isolation") == "edit":
                it.setIcon(0, self.style().standardIcon(QStyle.StandardPixmap.SP_MessageBoxInformation))
                it.setToolTip(0, re.sub(r'·\s(\S)', lambda m: "\n" + m.group(1).upper(), o["desc"]))
            fcb = QComboBox()
            for k in o["formats"]:
                fcb.addItem(fmts[k]["label"].split(" (")[0], k)
                fcb.setItemData(fcb.count() - 1, fmts[k]["label"], Qt.ItemDataRole.ToolTipRole)
            fcb.setCurrentIndex(max(0, fcb.findData(o["fmt"])))
            bcb = QComboBox()
            bcb.setEditable(True)
            bcb.addItems(["", "96k", "128k", "192k", "256k", "320k", "384k", "448k", "640k"])
            bcb.setMinimumContentsLength(4)
            bcb.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            fcb.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            fcb.setMinimumContentsLength(6)
            self.tree.setItemWidget(it, 2, fcb)
            self.tree.setItemWidget(it, 3, bcb)
            fcb.currentIndexChanged.connect(lambda *_, a=fcb, b=bcb, fm=fmts: self._fmt_changed(a, b, fm))
            self._fmt_changed(fcb, bcb, fmts)
            row = {"item": it, "opt": o, "fmt": fcb, "br": bcb, "lay": None, "orig": orig, "deffmt": o["fmt"]}
            if len(choices) > 1:
                # Layout de saída da trilha: 2.0 / 5.1 (…), sempre dizendo qual é o original da trilha
                lcb = QComboBox()
                for c in choices:
                    lcb.addItem(f"{lname(c)} · original" if c == orig else f"{lname(c)}", c)
                    lcb.setItemData(lcb.count() - 1, "Layout original da trilha" if c == orig else f"A trilha é {lname(orig)}; será renderizada em {lname(c)}",
                                    Qt.ItemDataRole.ToolTipRole)
                lcb.setCurrentIndex(max(0, lcb.findData(o["layout"])))
                lcb.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
                lcb.setMinimumContentsLength(6)
                self.tree.setItemWidget(it, 1, lcb)
                lcb.currentIndexChanged.connect(lambda *_, oid=o["id"]: self._layout_changed(oid))
                row["lay"] = lcb
            self._rows[o["id"]] = row
        self._updating = False
        self._changed()

    def _layout_changed(self, oid):
        """Trocou o layout da trilha: os formatos aceitos e o padrão dependem do número de canais."""
        r = self._rows[oid]
        lay = r["lay"].currentData()
        n = len(self.scan["layouts"][str(lay)]["channels"])
        fmts = self.scan["formats"]
        fcb = r["fmt"]
        fcb.blockSignals(True)
        fcb.clear()
        for k, f in fmts.items():
            if n <= f.get("max", 64):
                fcb.addItem(f["label"].split(" (")[0], k)
                fcb.setItemData(fcb.count() - 1, f["label"], Qt.ItemDataRole.ToolTipRole)
        r["deffmt"] = "aac" if n <= 2 else ("eac3" if n <= 6 else "wav")
        fcb.setCurrentIndex(max(0, fcb.findData(r["deffmt"])))
        fcb.blockSignals(False)
        self._fmt_changed(fcb, r["br"], fmts)
        if self.tree.currentItem() is r["item"]:
            self._current(r["item"], None)

    def layout_of(self, oid) -> int:
        r = self._rows[oid]
        return r["lay"].currentData() if r["lay"] is not None else r["opt"]["layout"]

    @staticmethod
    def _fmt_changed(fcb, bcb, fmts):
        br = fmts[fcb.currentData()]["br"]
        bcb.setEnabled(bool(br))
        bcb.lineEdit().setPlaceholderText(br if br else "—")
        bcb.setToolTip(f"Bitrate (padrão {br})" if br else "Este formato não usa bitrate")
        if not br:
            bcb.setCurrentText("")

    def _mark(self, pred):
        self._updating = True
        for r in self._rows.values():
            if r["item"].flags() & Qt.ItemFlag.ItemIsUserCheckable:
                r["item"].setCheckState(0, Qt.CheckState.Checked if pred(r["opt"]) else Qt.CheckState.Unchecked)
        self._updating = False
        self._changed()

    def _changed(self):
        if self._updating:
            return
        n = len(self.selected_ids())
        self.count.setText(f"{n} de {len(self._rows)} selecionado(s)" if self._rows else "")
        for fn in self._cb_sel:
            fn()

    def _current(self, it, _prev):
        row = next((r for r in self._rows.values() if r["item"] is it), None)
        opt = None
        if row:
            opt = dict(row["opt"])
            lay = self.layout_of(opt["id"])
            if lay != opt["layout"] and self.scan:                  # mostra no diagrama de caixas o layout escolhido
                opt["layout"] = lay
                opt["highlight"] = [list(range(len(self.scan["layouts"][str(lay)]["channels"])))]
        for fn in self._cb_cur:
            fn(opt)

    def selected_ids(self) -> list:
        return [k for k, r in self._rows.items() if r["item"].checkState(0) == Qt.CheckState.Checked]

    def highlight_keys(self) -> set:
        keys = set()
        for k in self.selected_ids():
            o = self._rows[k]["opt"]
            if k.startswith("pair:"):
                keys |= {f"group:{e}" for e in o.get("elements", [])}
            else:
                keys.add(k)
        return keys

    def specs(self) -> list:
        out = []
        for k in self.selected_ids():
            r = self._rows[k]
            o = r["opt"]
            fmt, br = r["fmt"].currentData(), r["br"].currentText().strip() if r["br"].isEnabled() else ""
            if br and not re.fullmatch(r"\d{2,4}k", br):
                raise UserError(f"Bitrate inválido em “{o['title']}”: use algo como 192k.")
            spec = o["id"]
            if fmt != r["deffmt"] or br:
                spec += f"={fmt}" + (f":{br}" if br else "")
            lay = self.layout_of(k)
            if lay != r["orig"]:
                spec += "@" + self.scan["layouts"][str(lay)]["name"]
            out.append(spec)
        return out


class NormalizeRow(QWidget):
    """Normalização de volume dos áudios gerados (--normalize / --true-peak): desligado, uma das metas padrão ou um valor em LUFS."""
    changed = Signal()
    CHOICES = (("Desligado", "off"), ("EBU R128 (−23 LUFS)", "ebu"), ("ATSC A/85 (−24 LUFS)", "atsc"), ("Streaming (−16 LUFS)", "streaming"), ("Outro valor…", "custom"))

    def __init__(self):
        super().__init__()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.combo = QComboBox()
        for t, v in self.CHOICES:
            self.combo.addItem(t, v)
        self.target = QDoubleSpinBox()
        self.target.setDecimals(1)
        self.target.setRange(-40, -5)
        self.target.setValue(-23)
        self.target.setSuffix(" LUFS")
        self.peak_lb = QLabel("Pico máx.")
        self.peak = QDoubleSpinBox()
        self.peak.setDecimals(1)
        self.peak.setRange(-9, 0)
        self.peak.setValue(-1)
        self.peak.setSuffix(" dBTP")
        tip = ("Mede o volume de cada áudio gerado (EBU R128: volume integrado e pico verdadeiro) e aplica um único ganho para chegar à meta, sem mexer na "
               "dinâmica. O ganho nunca leva o pico acima do máximo indicado; se levaria, o arquivo fica um pouco abaixo da meta e isso aparece no registro. "
               "Não substitui o “Loudness alvo (-rl)” do decoder, que usa os metadados do MPEG-H.")
        for w in (self.combo, self.target, self.peak):
            w.setToolTip(tip)
        lay.addWidget(self.combo, 1)
        lay.addWidget(self.target)
        lay.addWidget(self.peak_lb)
        lay.addWidget(self.peak)
        self.combo.currentIndexChanged.connect(self._sync)
        for sig in (self.combo.currentIndexChanged, self.target.valueChanged, self.peak.valueChanged):
            sig.connect(lambda *_: self.changed.emit())
        self._sync()

    def _sync(self):
        mode = self.combo.currentData()
        self.target.setVisible(mode == "custom")
        self.peak_lb.setVisible(mode != "off")
        self.peak.setVisible(mode != "off")

    def value(self) -> str:
        mode = self.combo.currentData()
        return f"{self.target.value():g}" if mode == "custom" else mode

    def set_value(self, v: str, peak=None):
        i = self.combo.findData(v)
        if i >= 0:
            self.combo.setCurrentIndex(i)
        else:
            try:
                self.target.setValue(float(str(v).replace(",", ".")))
                self.combo.setCurrentIndex(self.combo.findData("custom"))
            except ValueError:
                self.combo.setCurrentIndex(0)
        if peak is not None:
            self.peak.setValue(peak)

    def args(self) -> list:
        return [] if self.combo.currentData() == "off" else ["--normalize", self.value(), "--true-peak", f"{self.peak.value():g}"]


def native_combo() -> QComboBox:
    cb = QComboBox()
    for t, v in NATIVE_LAYOUTS:
        cb.addItem(t, v)
    return cb


# ═════════════════════════════ Áudio MPEG-H ═════════════════════════════
class AudioPage(Page):
    title = "Extração de áudio MPEG-H"
    subtitle = "Analisa os presets e grupos do áudio MPEG-H, mostrando a disponibilidade em cada momento do vídeo e permite a extração em áudios separados"

    def build(self, lay):
        g, f = self.group("Arquivo")
        self.input = FileEdit("open", VIDEO_FILTER + ";;Transport stream (*.ts *.mts *.m2ts)", "MP4 com áudio MPEG-H (mhm1)", key="audio_in")
        self.layout_cb = native_combo()
        self.compat = QCheckBox("Modo de compatibilidade: renderizar 5.1.4 como 5.1")
        f.addRow("Entrada", self.input)
        f.addRow("Layout nativo", self.layout_cb)
        f.addRow("", self.compat)
        self.scan_btn = QPushButton("Analisar áudio")
        self.scan_btn.clicked.connect(self.scan)
        self.action_buttons.append(self.scan_btn)
        f.addRow("", left(self.scan_btn))
        lay.addWidget(g)
        self.input.changed.connect(self._input_changed)

        self.result_box = QWidget()
        rb = QVBoxLayout(self.result_box)
        rb.setContentsMargins(0, 0, 0, 0)
        rb.setSpacing(10)
        self.warn = QLabel()
        self.warn.setWordWrap(True)
        self.warn.setVisible(False)
        rb.addWidget(self.warn)
        mid = QHBoxLayout()
        gsel = CardBox("O que gerar")
        gl = gsel.body
        self.catalog = CatalogView()
        gl.addWidget(self.catalog)
        mid.addWidget(gsel, 3)
        gsp = CardBox("Layout de caixas", expand=True)
        sl = gsp.body
        self.speakers = SpeakerDiagram()
        sl.addWidget(self.speakers, 1)
        self.speaker_note = muted_label("")
        self.speaker_note.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        sl.addWidget(self.speaker_note)
        mid.addWidget(gsp, 1)
        rb.addLayout(mid)
        gav = CardBox("Disponibilidade ao longo do vídeo")
        al = gav.body
        self.chart = AvailabilityChart()
        al.addWidget(self.chart)
        al.addWidget(muted_label("Selecione itens acima para destacá-los. Faixas vazias = o item não existe naquele trecho."))
        rb.addWidget(gav)
        g2, f2 = self.group("Saída")
        self.outdir = FileEdit("dir", placeholder="<nome do arquivo>_audios ao lado do original", key="audio_out")
        self.mux = QComboBox()
        for t, v in (("Somente arquivos de áudio", "none"), ("Áudios + cópia do MP4 com as faixas acrescentadas", "add"), ("Somente o MP4 com as faixas acrescentadas", "only")):
            self.mux.addItem(t, v)
        self.delay = self.spin(-60000, 60000, 0, 10, suffix=" ms")
        self.jobs = self.spin(0, 64, 0, special="Automático")
        self.overwrite = QCheckBox("Substituir arquivos existentes")
        f2.addRow("Pasta de saída", self.outdir)
        f2.addRow("Juntar ao vídeo", self.mux)
        f2.addRow("Atraso das faixas juntadas", self.delay)
        self.norm = NormalizeRow()
        f2.addRow("", self.overwrite)
        adv = Collapsible("Avançado: decoder", flat=True)
        f3 = QFormLayout()
        self.drc = QLineEdit("0")
        self.loud = QLineEdit()
        self.loud.setPlaceholderText("Padrão do decoder")
        f3.addRow("Efeito DRC (-dse)", self.drc)  
        f3.addRow("Normalizar volume", self.norm)
        f3.addRow("Loudness alvo (-rl)", self.loud)
        f3.addRow("Decodificações paralelas", self.jobs)
        adv.content.addLayout(f3)
        f2.addRow(adv)
        rb.addWidget(g2)
        self.result_box.setVisible(False)
        lay.addWidget(self.result_box)

        self.gen_btn = self.primary("Gerar áudios", lambda: self.run_built(self.build_jobs))
        self.catalog.on_selection(self._selection)
        self.catalog.on_current(self._current)
        self.scan_data = None

    def update_buttons(self):
        self.scan_btn.setEnabled(not self._busy)
        self.gen_btn.setEnabled(not self._busy and bool(self.catalog.selected_ids()))

    def _input_changed(self):
        self.scan_data = None
        self.catalog.clear()
        self.result_box.setVisible(False)
        self.update_buttons()

    def _scan_args(self):
        a = ["--native-layout", str(self.layout_cb.currentData())]
        if self.compat.isChecked():
            a.append("--compat-51")
        return a

    def scan(self):
        if not self.input.text() or not os.path.isfile(self.input.text()):
            self.show_error("Escolha o arquivo de entrada antes de analisar.")
            return
        self.run([Job("Analisando áudio MPEG-H", ["mpegh-scan", self.input.text()] + self._scan_args() + self.extra("mpegh-scan"),
                      kind="mpegh-scan", page=self, meta={"quiet": True})])

    def handle_result(self, job, res):
        if job.kind != "mpegh-scan":
            return
        scan = res["scan"]
        self.scan_data = scan
        self.catalog.set_scan(scan)
        self.chart.set_data(scan)
        self.result_box.setVisible(True)
        w = scan.get("warnings") or []
        self.warn.setVisible(bool(w))
        if w:
            c = Colors(self)
            pal = self.warn.palette()
            pal.setColor(QPalette.ColorRole.WindowText, c.warn)
            self.warn.setPalette(pal)
            self.warn.setText("⚠ " + "\n⚠ ".join(w))
        self._selection()
        first = next((r for r in self.catalog._rows.values()), None)
        if first:
            self.catalog.tree.setCurrentItem(first["item"])

    def _selection(self):
        self.chart.set_selected(self.catalog.highlight_keys())
        self.update_buttons()

    def _current(self, opt):
        if not opt or not self.scan_data:
            self.speakers.set_layout("", [])
            return
        lay = self.scan_data["layouts"].get(str(opt["layout"]))
        if lay:
            hi = set(opt["highlight"][0]) if opt.get("highlight") else None
            self.speakers.set_layout(f"{lay['name']} · {opt['title']}", lay["channels"], hi)
            self.speaker_note.setText(opt.get("composition") or "")

    def build_jobs(self):
        if not self.scan_data:
            raise UserError("Analise o áudio primeiro.")
        specs = self.catalog.specs()
        if not specs:
            raise UserError("Marque ao menos um item para gerar.")
        a = ["mpegh-audio", self.input.text()]
        for s in specs:
            a += ["--option", s]
        a += self._scan_args() + ["--mux", self.mux.currentData(), "--audio-delay", str(self.delay.value()), "--drc-effect", self.drc.text().strip() or "0"] + self.norm.args()
        if self.outdir.text():
            a += ["--outdir", self.outdir.text()]
        if self.jobs.value():
            a += ["--jobs", str(self.jobs.value())]
        if self.loud.text().strip():
            a += ["--target-loudness", self.loud.text().strip()]
        if self.overwrite.isChecked():
            a.append("--overwrite")
        a += self.extra("mpegh-audio")
        return [Job(f"Gerar áudios · {os.path.basename(self.input.text())}", a, kind="mpegh-audio", page=self)]


# ═════════════════════════════ Cortar MP4 ═════════════════════════════
class CutPage(Page):
    title = "Cortar MP4"
    subtitle = "Corte de arquivos MP4 sem recodificar e preservando as camadas VVC + LCEVC + MPEG-H"

    def build(self, lay):
        g, f = self.group("Arquivo")
        self.input = FileEdit("open", VIDEO_FILTER, "MP4 gerado pelo ts-tv3", key="cut_in")
        f.addRow("Entrada", self.input)
        self.read_btn = QPushButton("Ler pontos de corte")
        self.read_btn.clicked.connect(self.read_points)
        self.action_buttons.append(self.read_btn)
        f.addRow("", left(self.read_btn))
        lay.addWidget(g)
        self.input.changed.connect(self._input_changed)

        self.work = QWidget()
        wl = QVBoxLayout(self.work)
        wl.setContentsMargins(0, 0, 0, 0)
        wl.setSpacing(10)
        self.info = muted_label("")
        gt = CardBox("Linha do tempo")
        tl = gt.body
        self.timeline = CutTimeline()
        tl.addWidget(self.timeline)
        tl.addWidget(self.info)
        tl.addWidget(muted_label("Arraste sobre a linha do tempo para preencher início e fim. As barras azuis são os pontos de corte válidos."))
        wl.addWidget(gt)
        gc, fc = self.group("Cortes")
        row = QHBoxLayout()
        self.start = QLineEdit()
        self.start.setPlaceholderText("Início  00:01:15.500")
        self.end = QLineEdit()
        self.end.setPlaceholderText("Fim  00:02:30")
        self.add_btn = QPushButton("Adicionar corte")
        self.add_btn.clicked.connect(self.add_cut)
        for w in (self.start, self.end, self.add_btn):
            row.addWidget(w)
        fc.addRow(row)
        self.snap = QComboBox()
        self.end_snap = QComboBox()
        for cb, items in ((self.snap, (("IDR mais próximo", "nearest"), ("IDR anterior", "prev"), ("IDR seguinte", "next"))),
                          (self.end_snap, (("IDR mais próximo", "nearest"), ("IDR anterior", "prev"), ("IDR seguinte", "next"), ("Fim do arquivo", "eof")))):
            for t, v in items:
                cb.addItem(t, v)
        fc.addRow("Ajustar o início para", self.snap)
        fc.addRow("Ajustar o fim para", self.end_snap)
        self.snap.currentIndexChanged.connect(self._resnap)
        self.end_snap.currentIndexChanged.connect(self._resnap)
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["#", "Início pedido", "Fim pedido", "Início (IDR)", "Fim", "Duração"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        fit_view(self.table, 60)
        fc.addRow(self.table)
        rr = QHBoxLayout()
        self.rm_btn = QPushButton("Remover selecionados")
        self.clear_btn = QPushButton("Limpar")
        self.rm_btn.clicked.connect(self.remove_cuts)
        self.clear_btn.clicked.connect(self.clear_cuts)
        rr.addWidget(self.rm_btn)
        rr.addWidget(self.clear_btn)
        rr.addStretch(1)
        fc.addRow(rr)
        wl.addWidget(gc)
        go, fo = self.group("Saída")
        self.output = FileEdit("save", VIDEO_FILTER, "<nome>_cut_<início>_<fim>.mp4 ao lado do original", key="cut_out")
        self.noav = QCheckBox("Só medir a sincronia A/V, sem corrigir")
        self.overwrite = QCheckBox("Substituir arquivos existentes")
        fo.addRow("Arquivo de saída", self.output)
        fo.addRow("", self.noav)
        fo.addRow("", self.overwrite)
        wl.addWidget(go)
        self.work.setVisible(False)
        lay.addWidget(self.work)
        self.cut_btn = self.primary("Cortar", lambda: self.run_built(self.build_jobs))
        self.timeline.rangeDragged.connect(self._dragged)
        self.cuts: list = []
        self.idr: list = []
        self.total = 0.0

    def update_buttons(self):
        self.read_btn.setEnabled(not self._busy)
        self.cut_btn.setEnabled(not self._busy and bool(self.cuts))

    def _input_changed(self):
        self.idr, self.cuts, self.total = [], [], 0.0
        self.work.setVisible(False)
        self._refresh()

    def read_points(self):
        if not self.input.text() or not os.path.isfile(self.input.text()):
            self.show_error("Escolha o arquivo MP4 antes de ler os pontos de corte.")
            return
        self.run([Job("Lendo pontos de corte", ["mp4-cut", self.input.text(), "--list-idr"] + self.extra("mp4-cut"),
                      kind="mp4-list", page=self, meta={"quiet": True})])

    def handle_result(self, job, res):
        if job.kind != "mp4-list":
            lines = res.get("sync") or []
            if lines:
                lb = QLabel("\n".join(lines))
                lb.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
                self.win.panel.result.add_extra(lb, "Sincronia A/V")
            return
        sc = res["scan"]
        self.total = float(sc["duration"])
        self.idr = [(x["time"], x["frame"], x["kind"]) for x in sc["idr"]]
        self.cuts = []
        self.timeline.set_data(self.total, self.idr)
        kinds = ", ".join(sorted({x[2] for x in self.idr}))
        step = self.total / max(1, len(self.idr))
        self.info.setText(f"Duração {fmt_clock(self.total)}  ·  {sc['frames']} quadros  ·  {len(self.idr)} pontos de corte (1 a cada ~{step:.2f} s)  ·  tipos: {kinds}")
        self.work.setVisible(True)
        self._refresh()

    def _dragged(self, a, b):
        self.start.setText(fmt_clock(a))
        self.end.setText(fmt_clock(b))

    def _resolve(self, raw_s, raw_e):
        try:
            s, e = cutmath.parse_time(raw_s), cutmath.parse_time(raw_e)
        except ValueError as ex:
            raise UserError(str(ex))
        if s < 0 or s >= self.total:
            raise UserError(f"O início deve estar entre 0 e {fmt_clock(self.total)}.")
        if e <= s:
            raise UserError("O fim deve ser depois do início.")
        e = min(e, self.total)
        a = cutmath.idr_candidates(self.idr, s)[self.snap.currentData()][0]
        later = [i for i in self.idr if i[0] > a + 0.001]
        es = self.end_snap.currentData()
        b = self.total if (es == "eof" or not later) else cutmath.idr_candidates(later, e)[es][0]
        if b <= a:
            raise UserError("O ponto de fim escolhido não fica depois do início.")
        return a, b

    def add_cut(self):
        try:
            if not self.idr:
                raise UserError("Leia os pontos de corte primeiro.")
            a, b = self._resolve(self.start.text(), self.end.text())
        except UserError as e:
            self.show_error(str(e))
            return
        self.show_error("")
        self.cuts.append({"s": self.start.text().strip(), "e": self.end.text().strip(), "a": a, "b": b})
        self.start.clear()
        self.end.clear()
        self._refresh()

    def _resnap(self):
        if not self.idr:
            return
        keep = []
        for c in self.cuts:
            try:
                c["a"], c["b"] = self._resolve(c["s"], c["e"])
                keep.append(c)
            except UserError:
                pass
        self.cuts = keep
        self._refresh()

    def remove_cuts(self):
        rows = sorted({i.row() for i in self.table.selectedIndexes()}, reverse=True)
        for r in rows:
            del self.cuts[r]
        self._refresh()

    def clear_cuts(self):
        self.cuts = []
        self._refresh()

    def _refresh(self):
        self.table.setRowCount(len(self.cuts))
        for r, c in enumerate(self.cuts):
            vals = (str(r + 1), c["s"], c["e"], fmt_clock(c["a"]), fmt_clock(c["b"]), fmt_hms(c["b"] - c["a"]))
            for k, v in enumerate(vals):
                self.table.setItem(r, k, QTableWidgetItem(v))
        self.timeline.set_cuts([(c["a"], c["b"]) for c in self.cuts])
        self.rm_btn.setEnabled(bool(self.cuts))
        self.clear_btn.setEnabled(bool(self.cuts))
        self.update_buttons()

    def build_jobs(self):
        if not self.cuts:
            raise UserError("Adicione ao menos um corte.")
        a = ["mp4-cut", self.input.text()]
        for c in self.cuts:
            a += ["-s", c["s"], "-e", c["e"]]
        a += ["--snap", self.snap.currentData(), "--end-snap", self.end_snap.currentData()]
        if self.output.text():
            a += ["-o", self.output.text()]
        if self.noav.isChecked():
            a.append("--no-av-fix")
        if self.overwrite.isChecked():
            a.append("--overwrite")
        a += self.extra("mp4-cut")
        return [Job(f"Cortar · {os.path.basename(self.input.text())} ({len(self.cuts)} corte(s))", a, kind="mp4-cut", page=self)]


# ═════════════════════════════ Transcodificar ═════════════════════════════
class TranscodePage(Page):
    title = "Recodificar"
    subtitle = "Recodificação de MP4 single-track (VVC com LCEVC embutido + MPEG-H) com opção de seleção de faixas de áudio do MPEG-H"

    def build(self, lay):
        g = CardBox("Arquivos de entrada")
        gl = g.body
        self.files = ItemList("files", VIDEO_FILTER, key="tc_in")
        gl.addWidget(self.files)
        lay.addWidget(g)

        gp, fp = self.group("Predefinição")
        self.store = PresetStore(presets_path())
        self.preset_cb = QComboBox()
        self.preset_cb.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.preset_cb.setMinimumContentsLength(24)
        self.preset_state = muted_label("")
        self.preset_state.setWordWrap(False)
        self.preset_save = QPushButton("Salvar como…")
        self.preset_del = QPushButton("Excluir")
        self.preset_more = QPushButton("Mais…")
        menu = QMenu(self.preset_more)
        menu.addAction("Exportar minhas predefinições…", self._export_presets)
        menu.addAction("Importar predefinições…", self._import_presets)
        self.preset_more.setMenu(menu)
        prow = QHBoxLayout()
        prow.setContentsMargins(0, 0, 0, 0)
        prow.addWidget(self.preset_cb, 1)
        prow.addSpacing(4)
        prow.addWidget(self.preset_state)
        prow.addSpacing(4)
        for b in (self.preset_save, self.preset_del, self.preset_more):
            prow.addWidget(b)
        fp.addRow("Usar", prow)
        fp.addRow("", muted_label("Guarda o codificador, a qualidade, o contêiner, o áudio e os demais ajustes desta tela (não os arquivos nem a seleção de faixas)."))
        lay.addWidget(gp)
        self.preset_cb.activated.connect(self._preset_chosen)
        self.preset_save.clicked.connect(self._save_preset)
        self.preset_del.clicked.connect(self._delete_preset)

        gv, fv = self.group("Vídeo")
        self.encs = (self.win.core_info or {}).get("encoders") or FALLBACK_ENCODERS     # tabela do dtv_tools (describe)
        self.crf = self.spin(0, 63, 18, 0.5, 1)
        self.encoder = QComboBox()
        for k, e in self.encs.items():
            self.encoder.addItem(e["label"], k)
        self.preset = QComboBox()
        self.encoder.currentIndexChanged.connect(self._encoder_changed)
        self._encoder_changed()
        self.container = QComboBox()
        for t, v in (("Automático", "auto"), ("MP4", "mp4"), ("MKV", "mkv")):
            self.container.addItem(t, v)
        self.size = QLineEdit()
        self.size.setPlaceholderText("Automático (medido pelo LCEVC)  ex.: 3840x2160")
        self.nolcevc = QCheckBox("A fonte não tem LCEVC (arquivo HDR10 comum)")
        self.limit = self.spin(0, 86400, 0, 5, special="Arquivo inteiro", suffix=" s")
        fv.addRow("Codificador", self.encoder)
        self.crf_lb = QLabel("Qualidade (CRF)")
        fv.addRow(self.crf_lb, self.crf)
        self.maxrate = self.spin(0, 500, 0, 1, 1, special="Sem limite", suffix=" Mbps")
        fv.addRow("Limite de bitrate (pico)", self.maxrate)
        self._encoder_changed()                                      # agora que o campo existe: dica com a referência do codec
        fv.addRow("Velocidade (preset)", self.preset)
        fv.addRow("Contêiner", self.container)
        fv.addRow("Tamanho de saída", self.size)
        fv.addRow("Codificar apenas os primeiros", self.limit)
        fv.addRow("", self.nolcevc)
        adv = Collapsible("Avançado: codificador e verificação", flat=True)
        f3 = QFormLayout()
        self.x265 = QLineEdit()
        self.verify = self.spin(1, 5000, 48)
        self.f3 = f3
        f3.addRow("Parâmetros extras do codificador", self.x265)
        f3.addRow("Quadros na verificação LCEVC", self.verify)
        adv.content.addLayout(f3)
        fv.addRow(adv)
        lay.addWidget(gv)

        ga, fa = self.group("Áudio")
        self.audio_mode = QComboBox()
        for t, v in (("Renderizar presets como faixas separadas", "render"), ("Manter só a faixa MPEG-H original (cópia)", "copy"), ("Sem áudio", "none")):
            self.audio_mode.addItem(t, v)
        self.keep_orig = QCheckBox("Manter também a faixa MPEG-H original (por último)")
        self.keep_orig.setChecked(True)
        self.afmt = QComboBox()
        for t, v in (("Automático (AAC 2.0, E-AC-3 5.1, WAV acima)", "auto"), ("AAC", "aac"), ("E-AC-3", "eac3"), ("FLAC", "flac"), ("WAV 24 bits", "wav")):
            self.afmt.addItem(t, v)
        self.adelay = QLineEdit("auto")
        self.adelay.setToolTip("auto = início da trilha original, ou um número em milissegundos")
        self.layout_cb = native_combo()
        self.compat = QCheckBox("Modo de compatibilidade: renderizar 5.1.4 como 5.1")
        fa.addRow("Modo", self.audio_mode)
        self.render_box = QWidget()
        rf = QFormLayout(self.render_box)
        rf.setContentsMargins(0, 0, 0, 0)
        rf.addRow("", self.keep_orig)
        rf.addRow("Formato", self.afmt)
        rf.addRow("Atraso (auto ou ms)", self.adelay)
        self.norm = NormalizeRow()
        rf.addRow("Normalizar volume", self.norm)
        rf.addRow("Layout nativo", self.layout_cb)
        rf.addRow("", self.compat)
        self.scan_btn = QPushButton("Analisar áudio do 1º arquivo")
        self.scan_btn.clicked.connect(self.scan)
        self.action_buttons.append(self.scan_btn)
        rf.addRow("", left(self.scan_btn))
        self.catalog = CatalogView()
        self.catalog.setVisible(False)
        rf.addRow(self.catalog)
        rf.addRow("", muted_label("Sem análise, todos os presets são renderizados (padrão do dtv_tools). A seleção vale para todos os arquivos da fila."))
        fa.addRow(self.render_box)
        self.audio_mode.currentIndexChanged.connect(lambda: self.render_box.setVisible(self.audio_mode.currentData() == "render"))
        lay.addWidget(ga)

        go, fo = self.group("Saída")
        self.outdir = FileEdit("dir", placeholder="Mesma pasta do arquivo de entrada", key="tc_out")
        self.output = FileEdit("save", "MP4/MKV (*.mp4 *.mkv)", "<nome>_hevc_hdr10.mp4 (só com um arquivo)", key="tc_outf")
        self.overwrite = QCheckBox("Substituir arquivos existentes")
        fo.addRow("Pasta de saída", self.outdir)
        fo.addRow("Arquivo de saída", self.output)
        fo.addRow("", self.overwrite)
        lay.addWidget(go)
        self.files.changed.connect(lambda: self.output.setEnabled(len(self.files.values()) <= 1))
        self._preset_wire()
        self.primary("Transcodificar", lambda: self.run_built(lambda: self.build_jobs(False)))
        dry = QPushButton("Mostrar comando")
        dry.clicked.connect(lambda: self.run_built(lambda: self.build_jobs(True)))
        self.footer.addWidget(dry)
        self.action_buttons.append(dry)

    def scan(self):
        files = self.files.values()
        if not files:
            self.show_error("Adicione ao menos um arquivo antes de analisar o áudio.")
            return
        a = ["mpegh-scan", files[0], "--native-layout", str(self.layout_cb.currentData())] + (["--compat-51"] if self.compat.isChecked() else [])
        self.run([Job("Analisando áudio MPEG-H", a + self.extra("mpegh-scan"), kind="mpegh-scan", page=self, meta={"quiet": True})])

    def handle_result(self, job, res):
        if job.kind == "mpegh-scan":
            self.catalog.set_scan(res["scan"])
            self.catalog.setVisible(True)

    def _encoder_changed(self):
        """Cada codificador tem a sua escala de qualidade, lista de presets e sintaxe de parâmetros (tabela ENCODERS do dtv_tools)."""
        e = self.encs[self.encoder.currentData()]
        self.preset.clear()
        d = e["preset_default"]
        self.preset.addItem(f"Padrão ({d})" if d else "Padrão do codificador", "")
        for p in e["presets"]:
            self.preset.addItem(p, p)
        self.crf.setRange(*e["crf_range"])
        self.crf.setSingleStep(e["step"])
        self.crf.setValue(e["crf"])
        if hasattr(self, "maxrate"):
            ref = e.get("ref_mbps")
            self.maxrate.setToolTip("Mantém a qualidade escolhida, mas não deixa o bitrate passar deste valor (pico)."
                                    + (f" Referência para este codec: {ref} Mbps." if ref else ""))
        if hasattr(self, "x265"):
            self.x265.clear()
            self.x265.setPlaceholderText(e["params_hint"])
        self.crf_label_set()

    def crf_label_set(self):
        lb = getattr(self, "crf_lb", None)
        if lb is not None:
            lb.setText("Qualidade (CQ)" if "nvenc" in self.encoder.currentData() else "Qualidade (CRF)")

    def on_tools(self, res: dict):
        """Desabilita codificadores que o ffmpeg configurado não tem (resultado do doctor)."""
        have = next((t.get("caps", {}).get("encoders") for t in res.get("tools", []) if t["key"] == "ffmpeg" and t["ok"]), None)
        model = self.encoder.model()
        for i in range(self.encoder.count()):
            k = self.encoder.itemData(i)
            ok = have is None or bool(have.get(k, True))
            it = model.item(i)
            if it is not None:
                it.setEnabled(ok)
                it.setToolTip("" if ok else "O ffmpeg configurado não tem este codificador")
        if have is not None and not have.get(self.encoder.currentData(), True):
            self.encoder.setCurrentIndex(next((i for i in range(self.encoder.count()) if have.get(self.encoder.itemData(i), True)), 0))

    # ── predefinições ──────────────────────────────────────────────────────────
    def settings(self) -> dict:
        """Os ajustes da tela, no formato das predefinições (dtv_gui_presets.KEYS)."""
        return {"encoder": self.encoder.currentData(), "crf": float(self.crf.value()), "max_bitrate": float(self.maxrate.value()), "preset": self.preset.currentData() or "",
                "container": self.container.currentData(), "size": self.size.text().strip(), "limit": int(self.limit.value()),
                "no_lcevc": self.nolcevc.isChecked(), "params": self.x265.text().strip(), "verify": int(self.verify.value()),
                "audio_mode": self.audio_mode.currentData(), "keep_orig": self.keep_orig.isChecked(), "audio_format": self.afmt.currentData(),
                "audio_delay": self.adelay.text().strip() or "auto", "native_layout": int(self.layout_cb.currentData()),
                "compat_51": self.compat.isChecked(), "normalize": self.norm.value(), "true_peak": float(self.norm.peak.value())}

    def apply_settings(self, s: dict) -> list:
        """Aplica uma predefinição; o que não existe neste computador (codificador, por exemplo) é ignorado e devolvido na lista de avisos."""
        warn = []
        self._applying = True
        try:
            def pick(cb, val):
                i = cb.findData(val)
                if i >= 0:
                    cb.setCurrentIndex(i)
                return i >= 0
            if "encoder" in s and not pick(self.encoder, s["encoder"]):
                warn.append(f"codificador “{s['encoder']}”")
            elif "encoder" in s and not (self.encoder.model().item(self.encoder.currentIndex()).isEnabled()):
                warn.append(f"o ffmpeg configurado não tem o codificador “{s['encoder']}”")
            if "crf" in s:
                self.crf.setValue(s["crf"])
            if "max_bitrate" in s:
                self.maxrate.setValue(s["max_bitrate"])
            if "preset" in s and s["preset"] and not pick(self.preset, s["preset"]):
                warn.append(f"preset “{s['preset']}”")
            if "preset" in s and not s["preset"]:
                self.preset.setCurrentIndex(0)
            for key, cb in (("container", self.container), ("audio_mode", self.audio_mode), ("audio_format", self.afmt), ("native_layout", self.layout_cb)):
                if key in s:
                    pick(cb, s[key])
            if "size" in s:
                self.size.setText(s["size"])
            if "limit" in s:
                self.limit.setValue(s["limit"])
            if "no_lcevc" in s:
                self.nolcevc.setChecked(s["no_lcevc"])
            if "params" in s:
                self.x265.setText(s["params"])
            if "verify" in s:
                self.verify.setValue(s["verify"])
            if "keep_orig" in s:
                self.keep_orig.setChecked(s["keep_orig"])
            if "audio_delay" in s:
                self.adelay.setText(s["audio_delay"])
            if "compat_51" in s:
                self.compat.setChecked(s["compat_51"])
            if "normalize" in s:
                self.norm.set_value(s["normalize"], s.get("true_peak"))
        finally:
            self._applying = False
        return warn

    def _preset_wire(self):
        self._applying = False
        for sig in (self.encoder.currentIndexChanged, self.crf.valueChanged, self.maxrate.valueChanged, self.preset.currentIndexChanged, self.container.currentIndexChanged,
                    self.size.textChanged, self.limit.valueChanged, self.nolcevc.toggled, self.x265.textChanged, self.verify.valueChanged,
                    self.audio_mode.currentIndexChanged, self.keep_orig.toggled, self.afmt.currentIndexChanged, self.adelay.textChanged,
                    self.layout_cb.currentIndexChanged, self.compat.toggled, self.norm.changed):
            sig.connect(self._preset_dirty)
        self._fill_presets(QSettings().value("tc_preset_last", "", str))
        name = self.preset_cb.currentData()
        if name:
            self.apply_settings(self.store.get(name) or {})
        self._preset_dirty()
        if self.store.error:
            self.preset_state.setText("lista recomeçou vazia")
            self.preset_state.setToolTip(self.store.error)

    def _fill_presets(self, select: str = ""):
        cb = self.preset_cb
        cb.blockSignals(True)
        cb.clear()
        cb.addItem("Configuração atual (sem predefinição)", "")
        cb.insertSeparator(cb.count())
        for n in self.store.builtin_names():
            cb.addItem(n, n)
            cb.setItemData(cb.count() - 1, "Já vem no programa", Qt.ItemDataRole.ToolTipRole)
        if self.store.user_names():
            cb.insertSeparator(cb.count())
            for n in self.store.user_names():
                cb.addItem(n, n)
        i = cb.findData(select) if select else 0
        cb.setCurrentIndex(max(0, i))
        cb.blockSignals(False)
        self._preset_buttons()

    def _preset_buttons(self):
        name = self.preset_cb.currentData() or ""
        self.preset_del.setEnabled(bool(name) and not self.store.is_builtin(name))
        self.preset_del.setToolTip("" if self.preset_del.isEnabled() else "As predefinições que já vêm no programa não podem ser excluídas")

    def _preset_dirty(self, *_):
        """Diz se a tela ainda corresponde à predefinição escolhida."""
        if getattr(self, "_applying", False) or not hasattr(self, "store"):
            return
        name = self.preset_cb.currentData() or ""
        saved = self.store.get(name) if name else None
        if saved is None:
            self.preset_state.setText("")
            return
        cur = self.settings()
        self.preset_state.setText("modificada" if any(cur.get(k) != v for k, v in saved.items()) else "")

    def _preset_chosen(self, *_):
        name = self.preset_cb.currentData() or ""
        QSettings().setValue("tc_preset_last", name)
        self._preset_buttons()
        if not name:
            self._preset_dirty()
            return
        warn = self.apply_settings(self.store.get(name) or {})
        self._preset_dirty()
        if warn:
            self.show_error("Parte da predefinição não pôde ser aplicada neste computador: " + "; ".join(warn) + ".")

    def save_preset_as(self, name: str) -> str:
        """Guarda os ajustes atuais com este nome (sem perguntar nada) e a seleciona. ValueError se o nome não servir."""
        n = self.store.put(name, self.settings())
        QSettings().setValue("tc_preset_last", n)
        self._fill_presets(n)
        self._preset_dirty()
        return n

    def _save_preset(self):
        cur = self.preset_cb.currentData() or ""
        suggest = cur if cur and not self.store.is_builtin(cur) else ""
        name, ok = QInputDialog.getText(self, "Salvar predefinição", "Nome da predefinição:", text=suggest)
        if not ok:
            return
        try:
            from dtv_gui_presets import check_name
            n = check_name(name)
            if n in self.store.user and QMessageBox.question(self, "Substituir predefinição", f"Já existe a predefinição “{n}”. Substituir?") != QMessageBox.StandardButton.Yes:
                return
            self.save_preset_as(n)
        except ValueError as e:
            self.show_error(str(e))

    def delete_preset(self, name: str) -> bool:
        ok = self.store.delete(name)
        if ok:
            QSettings().setValue("tc_preset_last", "")
            self._fill_presets("")
            self._preset_dirty()
        return ok

    def _delete_preset(self):
        name = self.preset_cb.currentData() or ""
        if not name or self.store.is_builtin(name):
            return
        if QMessageBox.question(self, "Excluir predefinição", f"Excluir a predefinição “{name}”?") == QMessageBox.StandardButton.Yes:
            self.delete_preset(name)

    def _export_presets(self):
        if not self.store.user:
            self.show_error("Você ainda não salvou nenhuma predefinição.")
            return
        path, _ = QFileDialog.getSaveFileName(self, "Exportar predefinições", "predefinicoes_dtv_tools.json", "Predefinições (*.json)")
        if path:
            try:
                self.store.export_to(path)
            except OSError as e:
                self.show_error(f"Não consegui gravar o arquivo: {e}")

    def _import_presets(self):
        path, _ = QFileDialog.getOpenFileName(self, "Importar predefinições", "", "Predefinições (*.json)")
        if path:
            try:
                added = self.store.import_from(path)
            except ValueError as e:
                self.show_error(str(e))
                return
            self._fill_presets(self.preset_cb.currentData() or "")
            self.preset_state.setText(f"{len(added)} importada(s)")

    def build_jobs(self, dry: bool):
        files = self.files.values()
        if not files:
            raise UserError("Adicione ao menos um arquivo MP4.")
        mode = self.audio_mode.currentData()
        specs = self.catalog.specs() if (mode == "render" and self.catalog.scan) else []
        if mode == "render" and self.catalog.scan and not specs:
            raise UserError("Marque ao menos um item de áudio, ou escolha outro modo de áudio.")
        jobs = []
        for fpath in files:
            a = ["transcode", fpath, "--crf", str(self.crf.value()), "--container", self.container.currentData(), "--audio", mode,
                 "--verify-frames", str(self.verify.value())]
            a += ["--encoder", self.encoder.currentData()]
            if self.preset.currentData():
                a += ["--preset", self.preset.currentData()]
            if self.size.text().strip():
                a += ["--size", self.size.text().strip()]
            if self.x265.text().strip():
                a += ["--encoder-params", self.x265.text().strip()]
            if self.maxrate.value():
                a += ["--max-bitrate", str(self.maxrate.value())]
            if self.limit.value():
                a += ["--limit", str(self.limit.value())]
            if self.nolcevc.isChecked():
                a.append("--no-lcevc")
            if mode == "render":
                for s in specs:
                    a += ["--option", s]
                a += self.norm.args()
                a += ["--audio-format", self.afmt.currentData(), "--audio-delay", self.adelay.text().strip() or "auto",
                      "--original-audio", "keep" if self.keep_orig.isChecked() else "drop", "--native-layout", str(self.layout_cb.currentData())]
                if self.compat.isChecked():
                    a.append("--compat-51")
            if self.outdir.text():
                a += ["--outdir", self.outdir.text()]
            if self.output.text() and len(files) == 1:
                a += ["-o", self.output.text()]
            if self.overwrite.isChecked():
                a.append("--overwrite")
            if dry:
                a.append("--dry-run")
            a += self.extra("transcode")
            jobs.append(Job(f"{'Comando' if dry else 'Transcodificar'} · {os.path.basename(fpath)}", a, kind="transcode", page=self,
                            meta={"quiet": dry}))
        return jobs


# ═════════════════════════════ Quadro / imagem ═════════════════════════════
class FramePage(Page):
    title = "Captura de frame"
    subtitle = "Salva frames do vídeo como imagem HDR nativa e/ou SDR com tone mapping"

    def build(self, lay):
        top = QHBoxLayout()
        top.setSpacing(12)
        left = QVBoxLayout()
        g, f = self.group("Quadro")
        self.input = FileEdit("open", VIDEO_FILTER, "MP4 single-track", key="fr_in")
        self.by_time = QRadioButton("Por tempo")
        self.by_frame = QRadioButton("Por nº do quadro")
        self.by_time.setChecked(True)
        self.time = QLineEdit("00:00:10.000")
        self.frame_no = self.spin(1, 10_000_000, 1)
        self.nolcevc = QCheckBox("A fonte não tem LCEVC")
        self.slow = QCheckBox("Seek lento (decodifica do início; mais seguro)")
        f.addRow("Entrada", self.input)
        f.addRow(self.by_time, self.time)
        f.addRow(self.by_frame, self.frame_no)
        f.addRow("", self.nolcevc)
        f.addRow("", self.slow)
        lay.addWidget(g)
        g, f = self.group("Arquivos")
        self.hdr_png = QCheckBox("HDR · PNG 16 bits (BT.2020 / PQ)")
        self.hdr_png.setChecked(True)
        self.hdr_avif = QCheckBox("HDR · AVIF 10 bits")
        self.sdr_png = QCheckBox("SDR · PNG")
        self.sdr_png.setChecked(True)
        self.sdr_jpg = QCheckBox("SDR · JPG")
        self.jpgq = self.spin(1, 95, 95)
        self.avif_crf = self.spin(0, 63, 18)
        self.avif_chroma = QComboBox()
        self.avif_chroma.addItem("4:4:4 (mais detalhe)", "444")
        self.avif_chroma.addItem("4:2:0 (mais compatível)", "420")
        self.outdir = FileEdit("dir", placeholder="Mesma pasta do arquivo de entrada", key="fr_out")
        self.overwrite = QCheckBox("Substituir arquivos existentes")
        for w in (self.hdr_png, self.hdr_avif, self.sdr_png, self.sdr_jpg):
            f.addRow("", w)
        f.addRow("Qualidade JPG", self.jpgq)
        f.addRow("AVIF · CRF", self.avif_crf)
        f.addRow("AVIF · croma", self.avif_chroma)
        f.addRow("Pasta de saída", self.outdir)
        f.addRow("", self.overwrite)
        left.addWidget(g)
        top.addLayout(left, 1)

        right = QVBoxLayout()
        g, f = self.group("Tone mapping SDR")
        self.tm = QComboBox()
        for t in ("mobius", "hable", "reinhard", "clip"):
            self.tm.addItem(t, t)
        self.white = self.spin(50, 1000, 203, 1, 0, suffix=" nits")
        self.peak = self.spin(0, 10000, 0, 50, 0, special="Automático (MaxCLL)", suffix=" nits")
        self.exposure = self.spin(-5, 5, 0, 0.25, 2, suffix=" EV")
        self.desat = self.spin(0, 10, 2, 0.25, 2, special="Desligado")
        self.sat = self.spin(0, 3, 1, 0.05, 2)
        f.addRow("Curva", self.tm)
        f.addRow("Branco SDR", self.white)
        f.addRow("Pico HDR", self.peak)
        f.addRow("Exposição", self.exposure)
        f.addRow("Dessaturar realces", self.desat)
        f.addRow("Saturação", self.sat)
        right.addWidget(g)
        gp = CardBox("Pré-visualização (SDR)")
        pl = gp.body
        self.preview = ImagePreview()
        pl.addWidget(self.preview)
        self.preview_info = muted_label("")
        pl.addWidget(self.preview_info)
        top.addLayout(right, 1)
        lay.addLayout(top)
        lay.addWidget(gp)
        self.primary("Extrair quadro", lambda: self.run_built(self.build_frame))
        self.redo = QPushButton("Refazer SDR com estes ajustes")
        self.redo.setEnabled(False)
        self.redo.clicked.connect(lambda: self.run_built(self.build_sdr))
        self.footer.addWidget(self.redo)
        self.action_buttons.append(self.redo)
        self.last_hdr = ""

    def update_buttons(self):
        for b in self.action_buttons:
            b.setEnabled(not self._busy)
        self.redo.setEnabled(not self._busy and bool(self.last_hdr) and os.path.isfile(self.last_hdr))

    def _tone(self):
        a = ["--tonemap", self.tm.currentData(), "--white-nits", str(self.white.value()), "--peak-nits", str(self.peak.value()),
             "--exposure", str(self.exposure.value()), "--desat", str(self.desat.value()), "--saturation", str(self.sat.value()),
             "--jpg-quality", str(self.jpgq.value())]
        return a

    def build_frame(self):
        if not self.input.text() or not os.path.isfile(self.input.text()):
            raise UserError("Escolha o arquivo de entrada.")
        hdr = [n for n, c in (("png", self.hdr_png), ("avif", self.hdr_avif)) if c.isChecked()]
        sdr = [n for n, c in (("png", self.sdr_png), ("jpg", self.sdr_jpg)) if c.isChecked()]
        if not hdr and not sdr:
            raise UserError("Marque ao menos um tipo de arquivo.")
        a = ["frame", self.input.text()]
        a += ["--time", self.time.text().strip() or "0"] if self.by_time.isChecked() else ["--frame", str(self.frame_no.value())]
        a += ["--hdr", ",".join(hdr) or "none", "--sdr", ",".join(sdr) or "none", "--avif-crf", str(self.avif_crf.value()),
              "--avif-chroma", self.avif_chroma.currentData()] + self._tone()
        if self.nolcevc.isChecked():
            a.append("--no-lcevc")
        if self.slow.isChecked():
            a.append("--slow-seek")
        if self.outdir.text():
            a += ["--outdir", self.outdir.text()]
        if self.overwrite.isChecked():
            a.append("--overwrite")
        a += self.extra("frame")
        return [Job("Extrair quadro · " + os.path.basename(self.input.text()), a, kind="frame", page=self)]

    def build_sdr(self):
        sdr = [n for n, c in (("png", self.sdr_png), ("jpg", self.sdr_jpg)) if c.isChecked()]
        if not sdr:
            raise UserError("Marque SDR · PNG e/ou JPG.")
        a = ["sdr", self.last_hdr, "--sdr", ",".join(sdr)] + self._tone() + ["--overwrite"] + self.extra("sdr")
        return [Job("Refazer SDR", a, kind="sdr", page=self)]

    def handle_result(self, job, res):
        outs = res.get("outputs") or []
        hdr_png = next((o["path"] for o in outs if o["kind"] == "frame-hdr" and o["path"].lower().endswith(".png")), "")
        if hdr_png:
            self.last_hdr = hdr_png
        sdr = next((o["path"] for o in outs if o["kind"] == "frame-sdr"), "")
        if sdr and self.preview.set_image(sdr):
            self.preview_info.setText(os.path.basename(sdr) + " · tone mapping " + self.tm.currentText())
        self.update_buttons()


# ═════════════════════════════ Captura DASH ═════════════════════════════
class DashPage(Page):
    title = "Captura DASH DTV+"
    subtitle = "Gravação de stream DASH ao vivo ou estático da DTV+"

    def build(self, lay):
        g, f = self.group("Stream")
        self.url = QLineEdit()
        self.url.setPlaceholderText("https://…/manifest.mpd")
        self.url.setClearButtonEnabled(True)
        self.save_dir = FileEdit("dir", placeholder="Pasta onde o MP4 será salvo", key="dash_out")
        movies = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.MoviesLocation)
        self.save_dir.setText(movies or str(Path.home()))
        self.save_name = QLineEdit()
        self.save_name.setPlaceholderText("capture_AAAAMMDD_HHMMSS")
        self.limit = QTimeEdit()
        self.limit.setDisplayFormat("HH:mm:ss")
        self.limit.setTime(QTime(0, 0, 0))
        self.limit.setSpecialValueText("Sem limite (até parar)")
        f.addRow("URL do manifesto (MPD)", self.url)
        f.addRow("Pasta de saída", self.save_dir)
        f.addRow("Nome do arquivo", self.save_name)
        f.addRow("Duração máxima", self.limit)
        self.overwrite = QCheckBox("Substituir arquivo existente")
        f.addRow("", self.overwrite)
        lay.addWidget(g)
        lay.addWidget(muted_label("Streams protegidos (DRM) não são suportados."))

        adv = Collapsible("Avançado: rede, cabeçalhos e limpeza")
        f2 = QFormLayout()
        self.threads = self.spin(1, 64, 8)
        self.take = self.spin(0, 100, 1, special="Desde o início da janela")
        self.wait = self.spin(0.05, 30, 0.4, 0.05, 2, suffix=" s")
        self.cleanup = self.spin(1, 3600, 30, suffix=" s")
        self.maxerr = self.spin(0, 100000, 150, special="Nunca parar")
        self.loglevel = QComboBox()
        self.loglevel.addItems(["INFO", "WARN", "ERROR", "DEBUG", "OFF"])
        self.ua = QLineEdit()
        self.ua.setPlaceholderText("dtv_tools/<versão>")
        self.proxy = QLineEdit()
        self.proxy.setPlaceholderText("http://host:porta")
        self.del_tmp = QCheckBox("Apagar arquivos temporários ao terminar")
        self.del_tmp.setChecked(True)
        f2.addRow("Downloads paralelos", self.threads)
        f2.addRow("Começar nos últimos N segmentos", self.take)
        f2.addRow("Atualizar manifesto a cada", self.wait)
        f2.addRow("Apagar segmento após", self.cleanup)
        f2.addRow("Parar após N falhas seguidas", self.maxerr)
        f2.addRow("Nível de registro", self.loglevel)
        f2.addRow("User-Agent", self.ua)
        f2.addRow("Proxy", self.proxy)
        f2.addRow("", self.del_tmp)
        adv.content.addLayout(f2)
        adv.content.addWidget(QLabel("Cabeçalhos HTTP extras"))
        self.headers = ItemList("text", placeholder="Nome: valor", rows=3)
        adv.content.addWidget(self.headers)
        lay.addWidget(adv)
        self.primary("Iniciar captura", lambda: self.run_built(self.build_jobs))

    def build_jobs(self):
        url = self.url.text().strip()
        if not re.match(r"^https?://", url, re.I):
            raise UserError("Informe a URL do manifesto (começando com http:// ou https://).")
        for h in self.headers.values():
            if ":" not in h:
                raise UserError(f"Cabeçalho inválido: “{h}”. Use o formato Nome: valor.")
        lim = self.limit.time()
        secs = lim.hour() * 3600 + lim.minute() * 60 + lim.second()
        a = ["dash-capture", url, "--save-dir", self.save_dir.text() or ".", "--thread-count", str(self.threads.value()),
             "--live-take-count", str(self.take.value()), "--live-wait-time", str(self.wait.value()), "--cleanup-after", str(self.cleanup.value()),
             "--max-errors", str(self.maxerr.value()), "--log-level", self.loglevel.currentText(),
             "--del-after-done", "true" if self.del_tmp.isChecked() else "false"]
        if self.save_name.text().strip():
            a += ["--save-name", self.save_name.text().strip()]
        if secs:
            a += ["--live-record-limit", lim.toString("HH:mm:ss")]
        if self.ua.text().strip():
            a += ["--user-agent", self.ua.text().strip()]
        if self.proxy.text().strip():
            a += ["--proxy", self.proxy.text().strip()]
        for h in self.headers.values():
            a += ["--header", h]
        if self.overwrite.isChecked():
            a.append("--overwrite")
        a += self.extra("dash-capture")
        return [Job("Captura DASH", a, kind="dash-capture", page=self, indeterminate=not secs, stop_label="Parar e empacotar")]


# ═════════════════════════════ Ferramentas ═════════════════════════════
class ToolsPage(Page):
    title = "Configurações"
    subtitle = "Definição dos programas externos usados pelo dtv_tools. Programas já instalados podem ser encontrados automaticamente se já estão definidos em PATH."

    NEEDED = {"mp4box": "ts-tv3, mp4-cut, dash-capture, áudio MPEG-H, transcodificação",
              "ffmpeg": "áudio MPEG-H, transcodificação, quadro/imagem; verificação do LCEVC exige o filtro lcevc",
              "mpegh_ui": "análise e renderização do áudio MPEG-H", "mpegh_decoder": "renderização do áudio MPEG-H"}

    def build(self, lay):
        self.rows: dict = {}
        for key, label in TOOL_ROWS:
            g = CardBox(label)
            gl = QGridLayout()
            g.body.addLayout(gl)
            icon = StatusIcon(20)
            st = QLabel("Verificando…")
            st.setWordWrap(True)
            st.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            fe = FileEdit("open", placeholder="Detectar automaticamente", key="tool_" + key)
            gl.addWidget(icon, 0, 0, Qt.AlignmentFlag.AlignTop)
            gl.addWidget(st, 0, 1)
            gl.addWidget(fe, 1, 0, 1, 2)
            gl.addWidget(muted_label("Necessário para: " + self.NEEDED[key]), 2, 0, 1, 2)
            gl.setColumnStretch(1, 1)
            lay.addWidget(g)
            self.rows[key] = {"icon": icon, "status": st, "edit": fe}
        gm = CardBox("Módulos Python")
        self.mods = QLabel("—")
        self.mods.setWordWrap(True)
        gm.body.addWidget(self.mods)
        lay.addWidget(gm)
        g, f = self.group("Pastas")
        self.tmpdir = FileEdit("dir", placeholder="Padrão: dentro da pasta de saída", key="tmp")
        self.cache = FileEdit("dir", placeholder="Padrão: <temp do sistema>/dtv_audio_cache", key="cache")
        st = QSettings()
        self.tmpdir.setText(st.value("paths/tmpdir", "", str))
        self.cache.setText(st.value("paths/cache", "", str))
        self.tmpdir.changed.connect(lambda t: QSettings().setValue("paths/tmpdir", t))
        self.cache.changed.connect(lambda t: QSettings().setValue("paths/cache", t))
        f.addRow("Arquivos temporários", self.tmpdir)
        f.addRow("Cache do áudio MPEG-H", self.cache)
        f.addRow("Programas embutidos", muted_label(str(bundled_tools_dir())))
        lay.addWidget(g)
        self._load_overrides()
        for r in self.rows.values():       # o que está escrito na tela vale já, sem depender de "Salvar"
            r["edit"].changed.connect(lambda _t: self.write_config())
        self.save_btn = self.primary("Salvar e verificar", self.save_and_check)
        self.check_btn = QPushButton("Só verificar")
        self.check_btn.clicked.connect(self.check)
        self.footer.addWidget(self.check_btn)
        self.action_buttons.append(self.check_btn)

    def _load_overrides(self):
        try:
            data = json.loads(config_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        for k, r in self.rows.items():
            r["edit"].setText(str(data.get(k, "")))

    def tool_paths(self) -> dict:
        return {k: r["edit"].text().strip() for k, r in self.rows.items() if r["edit"].text().strip()}

    def write_config(self) -> bool:
        try:
            config_path().write_text(json.dumps(self.tool_paths(), ensure_ascii=False, indent=2), encoding="utf-8")
            return True
        except OSError as e:
            self.show_error(f"Não foi possível salvar a configuração: {e}")
            return False

    def save_and_check(self):
        if self.write_config():
            self.check()

    def check(self):
        self.run([self.job(quiet=False)])

    def job(self, quiet: bool = True) -> Job:
        return Job("Verificando ferramentas", ["doctor"] + self.extra("doctor"), kind="doctor", page=self,
                   meta={"quiet": quiet})

    def handle_error(self, job, msg):
        """A verificação falhou por inteiro (script antigo, processo que não iniciou...): nunca ficar em 'Verificando…'."""
        if job.kind != "doctor":
            return
        for r in self.rows.values():
            r["icon"].set_state(StatusIcon.ERROR)
            r["status"].setText("Não foi possível verificar: " + msg)

    def handle_result(self, job, res):
        c = Colors(self)
        for t in res.get("tools", []):
            r = self.rows.get(t["key"])
            if not r:
                continue
            if t["ok"]:
                r["icon"].set_state(StatusIcon.DONE)
                extra = ""
                if t["key"] == "ffmpeg" and not t.get("caps", {}).get("lcevc_filter"):
                    extra = "\nSem o filtro 'lcevc': a verificação do enhancement será ignorada e a decodificação LCEVC não funciona."
                if t["key"] == "mp4box" and t.get("caps", {}).get("vvc") is False:
                    extra = "\nMP4Box anterior ao GPAC 2.0: provavelmente sem suporte a VVC."
                r["status"].setText(f"{t['path']}  [{t['source']}]\n{t['version']}{extra}")
            else:
                r["icon"].set_state(StatusIcon.ERROR)
                r["status"].setText("Não encontrado. Informe o caminho abaixo ou coloque o programa na pasta tools/.")
        mods = res.get("modules", {})
        use = {"requests": "captura DASH", "rich": "opcional: painel no terminal", "numpy": "quadro/imagem", "PIL": "imagens SDR"}
        self.mods.setText("    ".join(f"{'✔' if mods.get(m) else '✖'} {m} ({use[m]})" for m in use if m in mods))
        self.win.set_tools_status(res)


# ═════════════════════════════ janela principal ═════════════════════════════
class NavDelegate(QStyledItemDelegate):
    """Item de navegação 100% nativo (inclusive o destaque do item atual); só define a altura de linha do Fluent (40 px; seções 32 px)."""

    def sizeHint(self, opt, idx):
        sh = super().sizeHint(opt, idx)
        sh.setHeight(max(sh.height(), 40 if idx.flags() & Qt.ItemFlag.ItemIsSelectable else 32))
        return sh


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"{APP_NAME}")
        self.settings = QSettings()
        env = {"DTV_TOOLS_DIR": str(bundled_tools_dir()), "DTV_TOOLS_CONFIG": str(config_path())}
        self.runner = Runner(env, self)
        self.bg_runner = Runner(env, self)
        self._q_jobs = []
        self.notifier = Notifier(self)
        self.notifier.clicked.connect(self._bring_to_front)
        self.panel = TaskPanel()
        self._last_err = ""
        self._tools_res: dict | None = None
        self.core_version = ""
        self.core_protocol = 0
        self.core_info = self._describe_core()

        self.stack = QStackedWidget()
        self.pages: dict = {}
        self.nav = QListWidget()
        self.nav.setFrameShape(QFrame.Shape.NoFrame)
        self.nav.viewport().setAutoFillBackground(False)     # o menu usa o mesmo fundo da janela (e, com Mica, o do sistema)
        self.nav.setFixedWidth(212)
        self.nav.setSpacing(1)
        self.nav.setItemDelegate(NavDelegate(self.nav))
        f = self.nav.font()
        f.setPointSizeF(f.pointSizeF() * 1.05)
        self.nav.setFont(f)
        self._nav_page: dict = {}

        def section(text):
            it = QListWidgetItem(text.upper())
            it.setFlags(Qt.ItemFlag.NoItemFlags)
            ff = it.font()
            ff.setPointSizeF(max(ff.pointSizeF() * 0.78, 7))
            ff.setBold(True)
            it.setFont(ff)
            it.setForeground(mix(self.palette().color(QPalette.ColorRole.WindowText), self.palette().color(QPalette.ColorRole.Window), 0.55))
            self.nav.addItem(it)

        self.nav.setIconSize(QSize(18, 18))
        self._nav_icons: dict = {}

        def add(key, cls):
            page = cls(self)
            self.pages[key] = page
            idx = self.stack.addWidget(page)
            it = QListWidgetItem(cls.title)
            self.nav.addItem(it)
            self._nav_icons[key] = it
            self._nav_page[self.nav.row(it)] = idx

        section("Origem")
        add("dash", DashPage)
        add("ts", TsPage)
        section("Edição")
        add("cut", CutPage)
        add("transcode", TranscodePage)
        add("frame", FramePage)
        section("Áudio")
        add("audio", AudioPage)
        section("Sistema")
        add("tools", ToolsPage)
        self._paint_nav_icons()
        QGuiApplication.styleHints().colorSchemeChanged.connect(lambda *_: QTimer.singleShot(0, self._theme_changed))
        self.nav.currentRowChanged.connect(lambda r: self.stack.setCurrentIndex(self._nav_page[r]) if r in self._nav_page else None)

        self.banner = QFrame()
        self.banner.setFrameShape(QFrame.Shape.StyledPanel)
        bl = QHBoxLayout(self.banner)
        bl.setContentsMargins(12, 6, 12, 6)
        self.banner_lb = QLabel()
        self.banner_lb.setWordWrap(True)
        bl.addWidget(self.banner_lb, 1)
        bb = QPushButton("Configurar…")
        bb.clicked.connect(lambda: self.nav_to("tools"))
        bl.addWidget(bb)
        self.banner.setVisible(False)

        center = QWidget()
        rl = QVBoxLayout(center)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(0)
        rl.addWidget(self.banner)
        rl.addWidget(self.stack, 1)
        central = QWidget()
        cl = QHBoxLayout(central)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(0)
        cl.addWidget(self.nav)
        cl.addWidget(center, 1)
        cl.addWidget(self.panel)                     # sem linha: o painel é uma camada translúcida um pouco diferente do fundo
        self.central = central
        self.setCentralWidget(central)
        self._mica = mica_supported() and self.settings.value("ui/mica", True, bool)
        if self._mica:
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.central.setAutoFillBackground(not self._mica)
        self.setMinimumSize(1240, 640)
        self._menus()
        self._connect()
        self.resize(1360, 860)
        st = self.settings
        if st.value("ui/geometry"):
            self.restoreGeometry(st.value("ui/geometry"))
        self.nav.setCurrentRow(next(r for r in self._nav_page))
        QTimer.singleShot(0, self.startup_check)

    # ── menus / tema ──
    def _menus(self):
        mb = self.menuBar()
        m = mb.addMenu("&Arquivo")
        q = QAction("Sair", self)
        q.setMenuRole(QAction.MenuRole.QuitRole)
        q.setShortcut("Ctrl+Q")
        q.triggered.connect(self.close)
        m.addAction(q)
        v = mb.addMenu("E&xibir")
        nt = v.addMenu("Notificações")
        self.notify_act = QAction("Notificar quando as tarefas terminarem", self, checkable=True)
        self.notify_act.setChecked(self.settings.value("notify/enabled", True, bool))
        self.notify_act.toggled.connect(lambda on: self.settings.setValue("notify/enabled", on))
        self.notify_bg_act = QAction("Somente quando a janela não estiver em foco", self, checkable=True)
        self.notify_bg_act.setChecked(self.settings.value("notify/only_background", True, bool))
        self.notify_bg_act.toggled.connect(lambda on: self.settings.setValue("notify/only_background", on))
        test = QAction("Realizar teste de notificação", self)
        test.triggered.connect(self.notify_test)
        for a in (self.notify_act, self.notify_bg_act):
            nt.addAction(a)
        nt.addSeparator()
        sub = v.addMenu("Tema")
        grp = QActionGroup(self)
        cur = self.settings.value("ui/theme", "system", str)
        for text, key in (("Seguir o sistema", "system"), ("Claro", "light"), ("Escuro", "dark")):
            a = QAction(text, self, checkable=True)
            a.setChecked(key == cur)
            a.triggered.connect(lambda _=False, k=key: self.set_theme(k))
            grp.addAction(a)
            sub.addAction(a)
        self.set_theme(cur, save=False)
        v.addSeparator()
        nt.addAction(test)
        if mica_supported():
            v.addSeparator()
            self.mica_act = QAction("Fundo translúcido (nativo Windows)", self, checkable=True)
            self.mica_act.setChecked(self._mica)
            self.mica_act.triggered.connect(self.set_mica)
            v.addAction(self.mica_act)
        h = mb.addMenu("A&juda")
        a = QAction("Sobre o " + APP_NAME, self)
        a.setMenuRole(QAction.MenuRole.AboutRole)
        a.triggered.connect(self.about)
        h.addAction(a)
        c = QAction("Verificar ferramentas", self)
        c.triggered.connect(lambda: (self.nav_to("tools"), self.pages["tools"].check()))
        h.addAction(c)

    def set_theme(self, key: str, save: bool = True):
        hints = QGuiApplication.styleHints()
        if hasattr(hints, "setColorScheme"):
            hints.setColorScheme({"system": Qt.ColorScheme.Unknown, "light": Qt.ColorScheme.Light, "dark": Qt.ColorScheme.Dark}[key])
        if save:
            self.settings.setValue("ui/theme", key)

    def _theme_changed(self, again: bool = True):
        self._paint_nav_icons()
        if self.isVisible():
            self._apply_backdrop()
            for w in self.findChildren(QWidget):      # com widgets transparentes o cache de pintura não se invalida sozinho
                w.update()
            self.update()
        if again:
            QTimer.singleShot(250, lambda: self._theme_changed(False))     # o DWM aplica o novo backdrop um instante depois

    def _paint_nav_icons(self):
        pal = QApplication.palette()          # a paleta do app já é a do tema novo; a do menu pode ainda não ser
        R = QPalette.ColorRole
        st = QApplication.style()
        base = st.baseStyle() if hasattr(st, "baseStyle") and st.baseStyle() is not None else st
        # No estilo windows11 a seleção é um fundo neutro: o ícone selecionado usa a cor de destaque do sistema.
        # Nos outros estilos a seleção é preenchida com essa cor, então o ícone usa a cor do texto selecionado.
        sel = accent_of(pal) if base.name().lower().startswith("windows11") else pal.color(R.HighlightedText)
        for key, it in self._nav_icons.items():
            it.setIcon(svg_icon(Path(__file__).resolve().parent / "icons" / f"{key}.svg", pal.color(R.Text), sel))

    def _is_dark(self) -> bool:
        return QApplication.palette().color(QPalette.ColorRole.Window).lightness() < 128

    def _apply_backdrop(self):
        if mica_supported():
            apply_backdrop(self, self._mica, self._is_dark())

    def set_mica(self, on: bool):
        self.settings.setValue("ui/mica", bool(on))
        self._mica = bool(on)
        self.hide()                                   # o atributo de janela translúcida só vale com a janela recriada
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, self._mica)
        self.central.setAutoFillBackground(not self._mica)
        self.show()
        self._apply_backdrop()

    def showEvent(self, e):
        super().showEvent(e)
        self._apply_backdrop()

    def changeEvent(self, e):
        if e.type() == e.Type.PaletteChange and hasattr(self, "_nav_icons"):
            QTimer.singleShot(0, self._theme_changed)     # tema claro/escuro: ícones e título seguem o tema (depois que a paleta assentou)
        super().changeEvent(e)

    def about(self):
        QMessageBox.about(self, "Sobre", f"<b>{APP_NAME}</b> {self.core_version or '?'}<br>Conjunto de ferramentas direcionadas ao novo padrão de TV digital do Brasil, o DTV+ ou TV 3.0.<br><br>Criado por @limaalef • <a href='https://bsky.app/profile/limaalef.com'>Bluesky</a> • <a href='https://github.com/limaalef'>Github</a><br><br>"
                                         f"Qt {__import__('PySide6').__version__} · Python {sys.version.split()[0]}")

    def nav_to(self, key: str):
        idx = self.stack.indexOf(self.pages[key])
        row = next(r for r, i in self._nav_page.items() if i == idx)
        self.nav.setCurrentRow(row)

    # ── execução ──
    def _describe_core(self) -> dict | None:
        """`describe` do script (comandos, opções, ferramentas): lido uma vez, em modo síncrono (~0,2 s)."""
        from PySide6.QtCore import QProcess as _QP
        prog, prefix = program_and_prefix()
        p = _QP()
        p.setProcessEnvironment(self.runner.process_env())
        p.start(prog, prefix + ["describe"])
        if not p.waitForFinished(8000):
            p.kill()
            return None
        try:
            return json.loads(bytes(p.readAllStandardOutput()).decode("utf-8", "replace"))
        except ValueError:
            return None

    def extra_args(self, cmd: str) -> list:
        a = []
        # Programas escolhidos na tela Ferramentas vão como opção explícita (a de maior prioridade no script) para os comandos
        # que a aceitam; o `describe` do script diz quais são a opção de cada programa e quais comandos a têm.
        spec = (self.core_info or {})
        flags = {f for c in [spec.get("commands", {}).get(cmd, {})] for o in c.get("options", []) for f in o["flags"]}
        for key, path in self.pages["tools"].tool_paths().items():
            flag = (spec.get("tools", {}).get(key) or {}).get("flag")
            if flag and flag in flags:
                a += [flag, path]
        tmp, cache = self.pages["tools"].tmpdir.text(), self.pages["tools"].cache.text()
        if cmd in TMPDIR_CMDS and tmp:
            a += ["--tmpdir", tmp]
        if cmd in CACHE_CMDS and cache:
            a += ["--cache-dir", cache]
        return a

    def run(self, jobs: list):
        if self.runner.busy:
            return
        self.runner.start(jobs)

    def _connect(self):
        r, p = self.runner, self.panel
        r.queueStarted.connect(self._queue_started)
        r.jobStarted.connect(p.begin_job)
        r.stepChanged.connect(lambda job, i, s, d: p.set_step(i, s, d))
        r.began.connect(lambda job, steps: p.set_plan(steps))
        r.progressChanged.connect(lambda job, pct, d, stage, frac: p.set_progress(pct, d, stage, frac))
        r.resultReady.connect(self._result)
        r.errorReported.connect(self._error)
        r.logLine.connect(self._log)
        r.jobFinished.connect(self._job_finished)
        r.queueFinished.connect(self._queue_finished)
        p.stopRequested.connect(r.stop)
        self.bg_runner.resultReady.connect(lambda job, res: job.page.handle_result(job, res))
        self.bg_runner.errorReported.connect(lambda job, msg: job.page.handle_error(job, msg))

    def _quiet(self, job) -> bool:
        return bool(job.meta.get("quiet"))

    def _queue_started(self, n):
        self._last_err = ""
        self._q_jobs = []
        self.panel.begin_queue(n)
        for pg in self.pages.values():
            pg.set_busy(True)

    def _result(self, job, res):
        if not self._quiet(job):
            self.panel.result.add_result(res)
        if job.page is not None:
            job.page.handle_result(job, res)

    def _error(self, job, msg):
        self._last_err = msg
        self.panel.append_log(("" if self.runner._stopped else "ERRO: ") + msg)
        if job.page is not None and not self.runner._stopped:
            job.page.handle_error(job, msg)
            job.page.show_error(msg)

    def _log(self, job, line):
        self.panel.append_log(line)
        if job.page is not None:
            job.page.handle_log(job, line)

    def _job_finished(self, job, code, ok):
        self._q_jobs.append({"title": job.title, "ok": ok, "quiet": self._quiet(job), "err": "" if ok or code == 130 else self._last_err})
        self.panel.end_job(job, code, ok)
        if not ok and code == 130:
            self.panel.result.set_banner(StatusIcon.SKIPPED, "Interrompido pelo usuário.")
        elif not ok:
            self.panel.result.set_banner(StatusIcon.ERROR, "Falhou: " + (self._last_err or f"código de saída {code}"))
        elif not self._quiet(job):
            self.panel.result.set_banner(StatusIcon.DONE, "Concluído com sucesso.")

    def _queue_finished(self, all_ok, stopped):
        self.panel.end_queue(all_ok, stopped)
        for pg in self.pages.values():
            pg.set_busy(False)
        job = self.runner.current_job()
        quiet = job is not None and self._quiet(job)
        if (not quiet) or not all_ok:
            self.panel.tabs.setCurrentIndex(1)

        if not self.isActiveWindow():
            QApplication.alert(self)
        self._notify_done(all_ok, stopped)

    # ── aviso nativo do sistema ──
    def _notify_done(self, all_ok: bool, stopped: bool):
        if not self.settings.value("notify/enabled", True, bool):
            return
        if self.settings.value("notify/only_background", True, bool) and self.isActiveWindow():
            return
        msg = summarize(getattr(self, "_q_jobs", []), all_ok, stopped)
        if msg:
            self.notifier.notify(*msg)

    def _bring_to_front(self):
        self.setWindowState(self.windowState() & ~Qt.WindowState.WindowMinimized)
        self.show()
        self.raise_()
        self.activateWindow()

    def notify_test(self):
        if not self.notifier.notify("Aviso de teste", "É assim que você será avisado quando as tarefas terminarem.", "info"):
            QMessageBox.information(self, "Aviso ao terminar", "Este sistema não oferece notificações para o programa "
                                    "(sem área de notificação ou sem o serviço de notificações do desktop).")

    # ── ferramentas ──
    def startup_check(self):
        tp = self.pages["tools"]
        self.bg_runner.start([tp.job(quiet=True)])

    def set_tools_status(self, res: dict):
        self._tools_res = res
        for pg in self.pages.values():
            if hasattr(pg, "on_tools"):
                pg.on_tools(res)
        self.core_version, self.core_protocol = res.get("version", ""), int(res.get("protocol", 0) or 0)
        missing = [t["name"] for t in res.get("tools", []) if not t["ok"]]
        if self.core_protocol != PROTOCOL:
            c = Colors(self)
            pal = self.banner.palette()
            pal.setColor(QPalette.ColorRole.Window, mix(c.err, c.window, 0.22))
            self.banner.setPalette(pal)
            self.banner.setAutoFillBackground(True)
            self.banner_lb.setText(f"O dtv_tools.py tem o protocolo {self.core_protocol or '?'}, mas esta interface fala o protocolo {PROTOCOL}. "
                                   "Atualize os dois para a mesma versão.")
            self.banner.setVisible(True)
            return
        if missing:
            c = Colors(self)
            pal = self.banner.palette()
            pal.setColor(QPalette.ColorRole.Window, mix(c.warn, c.window, 0.22))
            self.banner.setPalette(pal)
            self.banner.setAutoFillBackground(True)
            self.banner_lb.setText("Programas não encontrados: " + ", ".join(missing) + ". Algumas funções ficam indisponíveis até você configurá-los.")
        self.banner.setVisible(bool(missing))

    def closeEvent(self, e):
        if self.runner.busy:
            ans = QMessageBox.question(self, "Processo em andamento", "Há um processo em andamento. Parar e sair?")
            if ans != QMessageBox.StandardButton.Yes:
                e.ignore()
                return
            self.runner.stop(force=True)
        self.bg_runner.stop(force=True)
        self.settings.setValue("ui/geometry", self.saveGeometry())
        super().closeEvent(e)


def make_app(argv) -> QApplication:
    QApplication.setOrganizationName("DTV")
    QApplication.setApplicationName(APP_NAME)
    QApplication.setApplicationVersion(APP_VERSION)
    app = QApplication(argv)
    apply_fluent(app)
    try:  # textos padrão do Qt (botões de diálogos, menus de edição) no idioma do sistema
        from PySide6.QtCore import QLibraryInfo, QLocale, QTranslator
        tr = QTranslator(app)
        if tr.load(QLocale.system(), "qtbase", "_", QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)):
            app.installTranslator(tr)
            app._qt_translator = tr
    except Exception:
        pass
    app._wheel_guard = WheelGuard(app)
    app.installEventFilter(app._wheel_guard)
    icon = Path(__file__).resolve().parent / "packaging" / "icon.png"
    if icon.is_file():
        app.setWindowIcon(QIcon(str(icon)))
    return app


def main() -> int:
    app = make_app(sys.argv)
    win = MainWindow()
    win.show()
    if "--selftest" in sys.argv:
        QTimer.singleShot(1500, win.close)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
