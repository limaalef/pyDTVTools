"""Captura DASH: cartão "Captura em andamento" (trilhas do manifesto + status ao vivo). Só desenha o que o dtv_tools informa
nos eventos `tracks` (uma vez, quando o manifesto é lido) e `live` (cerca de uma vez por segundo)."""
from __future__ import annotations

from collections import deque

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QFrame, QGridLayout, QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget

from dtv_gui_style import DangerButton
from dtv_gui_widgets import CardBox, Colors, fmt_hms, human, muted_label

KIND_NAME = {"vvc_base": "Vídeo VVC", "lcevc_enh": "Camada LCEVC", "audio": "Áudio", "subtitle": "Legenda", "other": "Outra trilha"}
KIND_COLOR = {"vvc_base": 0, "lcevc_enh": 1, "audio": 2, "subtitle": 3, "other": 5}
STATE_PT = {"Waiting": "Aguardando", "Downloading": "Baixando"}
HISTORY = 90            # amostras (≈ segundos) no gráfico


def fmt_bitrate(bps: float) -> str:
    bps = float(bps or 0)
    if bps >= 1_000_000:
        return f"{bps / 1_000_000:.1f} Mbps"
    if bps >= 1000:
        return f"{bps / 1000:.0f} kbps"
    return f"{bps:.0f} bps" if bps else "—"


def describe_track(t: dict) -> str:
    """Resumo de uma trilha do manifesto: resolução · taxa · codec · idioma · canais · função."""
    parts = []
    if t.get("width") and t.get("height"):
        parts.append(f"{t['width']}×{t['height']}")
    if t.get("bandwidth"):
        parts.append(fmt_bitrate(t["bandwidth"]))
    if t.get("codecs"):
        parts.append(str(t["codecs"]))
    if t.get("lang"):
        parts.append(str(t["lang"]))
    if t.get("channels"):
        parts.append(f"{t['channels']} canais" if str(t["channels"]).isdigit() else str(t["channels"]))
    if t.get("role"):
        parts.append(str(t["role"]).capitalize())
    if t.get("label"):
        parts.append(f"“{t['label']}”")
    return " · ".join(parts) or "sem detalhes no manifesto"


def summarize_extra(extra: list) -> tuple[list, str]:
    """(trilhas que existem mas não são gravadas, texto das outras qualidades ignoradas)."""
    others = [e for e in extra if not str(e.get("kind", "")).startswith("alt_")]
    alts = [e for e in extra if str(e.get("kind", "")).startswith("alt_")]
    text = ""
    if alts:
        text = "Outras qualidades no manifesto, ignoradas (só a primeira de cada trilha é gravada): " + ", ".join(
            f"{KIND_NAME.get(e['kind'][4:], e['kind'][4:])} " + (f"{e['width']}×{e['height']} " if e.get("width") else "") + fmt_bitrate(e.get("bandwidth", 0))
            for e in alts[:6]) + (f" e mais {len(alts) - 6}" if len(alts) > 6 else "")
    return others, text


# ───────────────────────────── gráfico de taxa de download ─────────────────────────────
class SpeedGraph(QWidget):
    """Taxa de download por trilha nos últimos ~90 s (linhas com área suave); a escala acompanha o maior valor."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(120)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.series: dict[str, deque] = {}

    def clear(self):
        self.series.clear()
        self.update()

    def push(self, kind_speed: dict):
        for k in set(self.series) | set(kind_speed):
            d = self.series.setdefault(k, deque([0.0] * 0, maxlen=HISTORY))
            d.append(float(kind_speed.get(k, 0)) * 8)          # bytes/s -> bits/s
        self.update()

    @staticmethod
    def nice_max(v: float) -> float:
        if v <= 0:
            return 1_000_000
        import math
        mag = 10 ** math.floor(math.log10(v))
        for m in (1, 2, 2.5, 5, 10):
            if v <= m * mag:
                return m * mag
        return 10 * mag

    def paintEvent(self, e):
        c = Colors(self)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setPen(QPen(c.border, 1))
        p.setBrush(c.bg)
        p.drawRoundedRect(r, 6, 6)
        plot = r.adjusted(66, 12, -12, -22)
        top = self.nice_max(max((max(d) for d in self.series.values() if d), default=0) * 1.1)
        f = QFont(self.font())
        f.setPointSizeF(max(f.pointSizeF() - 1.5, 6.5))
        p.setFont(f)
        for i in range(4):                                        # grade horizontal + escala
            y = plot.top() + plot.height() * i / 3
            p.setPen(QPen(c.grid, 1))
            p.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
            p.setPen(c.muted)
            p.drawText(QRectF(r.left() + 4, y - 8, 56, 16), int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
                       fmt_bitrate(top * (1 - i / 3)) if i < 3 else "0")
        p.setPen(c.muted)
        p.drawText(QRectF(plot.left(), plot.bottom() + 3, 120, 16), int(Qt.AlignmentFlag.AlignLeft), f"últimos {HISTORY} s")
        p.drawText(QRectF(plot.right() - 40, plot.bottom() + 3, 40, 16), int(Qt.AlignmentFlag.AlignRight), "agora")
        step = plot.width() / (HISTORY - 1)
        for k, d in self.series.items():
            if len(d) < 2:
                continue
            col = QColor(c.cat[KIND_COLOR.get(k, 5) % len(c.cat)])
            pts = [QPointF(plot.right() - (len(d) - 1 - i) * step, plot.bottom() - min(v / top, 1.0) * plot.height()) for i, v in enumerate(d)]
            path = QPainterPath(pts[0])
            for q in pts[1:]:
                path.lineTo(q)
            area = QPainterPath(path)
            area.lineTo(QPointF(pts[-1].x(), plot.bottom()))
            area.lineTo(QPointF(pts[0].x(), plot.bottom()))
            area.closeSubpath()
            fill = QColor(col)
            fill.setAlpha(46)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(fill)
            p.drawPath(area)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(col, 2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            p.drawPath(path)
        if not any(len(d) > 1 for d in self.series.values()):
            p.setPen(c.muted)
            p.drawText(plot, int(Qt.AlignmentFlag.AlignCenter), "Aguardando os primeiros segmentos…")
        p.end()


# ───────────────────────────── peças pequenas ─────────────────────────────
class LiveBadge(QWidget):
    """Ponto que pulsa + texto (AO VIVO / ESTÁTICO / EMPACOTANDO / CONCLUÍDO…)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._text, self._kind, self._on = "", "idle", True
        f = QFont(self.font())
        f.setBold(True)
        self.setFont(f)
        self._t = QTimer(self, interval=650)
        self._t.timeout.connect(self._blink)
        self.setFixedHeight(26)

    def set_state(self, kind: str, text: str):
        self._kind, self._text, self._on = kind, text, True
        (self._t.start if kind == "live" else self._t.stop)()
        self.setFixedWidth(self.fontMetrics().horizontalAdvance(text) + 38)
        self.update()

    def _blink(self):
        self._on = not self._on
        self.update()

    def paintEvent(self, e):
        c = Colors(self)
        col = {"live": QColor("#e5484d" if c.dark else "#c42b1c"), "post": c.warn, "ok": c.ok, "err": c.err}.get(self._kind, c.muted)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        bg = QColor(col)
        bg.setAlpha(34)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(bg)
        p.drawRoundedRect(QRectF(self.rect()), 13, 13)
        dot = QColor(col)
        if self._kind == "live" and not self._on:
            dot.setAlpha(70)
        p.setBrush(dot)
        p.drawEllipse(QPointF(14, self.height() / 2), 4.5, 4.5)
        p.setPen(col)
        p.drawText(QRectF(26, 0, self.width() - 26, self.height()), int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft), self._text)
        p.end()


class ThinBar(QWidget):
    def __init__(self, kind: str, parent=None):
        super().__init__(parent)
        self.kind, self.frac = kind, 0.0
        self.setFixedHeight(5)

    def set_frac(self, f: float):
        self.frac = max(0.0, min(1.0, f))
        self.update()

    def paintEvent(self, e):
        c = Colors(self)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(c.grid)
        p.drawRoundedRect(QRectF(self.rect()), 2.5, 2.5)
        if self.frac > 0:
            p.setBrush(c.cat[KIND_COLOR.get(self.kind, 5) % len(c.cat)])
            p.drawRoundedRect(QRectF(0, 0, self.width() * self.frac, self.height()), 2.5, 2.5)
        p.end()


class Chip(QLabel):
    def __init__(self, kind: str, parent=None):
        super().__init__(KIND_NAME.get(kind, kind), parent)
        self.kind = kind
        f = QFont(self.font())
        f.setBold(True)
        self.setFont(f)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMinimumWidth(128)
        self.setFixedHeight(24)

    def paintEvent(self, e):
        c = Colors(self)
        col = QColor(c.cat[KIND_COLOR.get(self.kind, 5) % len(c.cat)])
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        bg = QColor(col)
        bg.setAlpha(40)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(bg)
        p.drawRoundedRect(QRectF(self.rect()), 5, 5)
        p.setPen(col.lighter(125) if c.dark else col.darker(115))
        p.drawText(self.rect(), int(Qt.AlignmentFlag.AlignCenter), self.text())
        p.end()


class TrackRow(QFrame):
    """Uma trilha: etiqueta colorida, descrição do manifesto e, se for gravada, estado/velocidade/tamanho ao vivo."""

    def __init__(self, t: dict, captured: bool, parent=None):
        super().__init__(parent)
        self.kind = t.get("kind", "other")
        lay = QGridLayout(self)
        lay.setContentsMargins(0, 2, 0, 2)
        lay.setHorizontalSpacing(12)
        lay.setVerticalSpacing(4)
        self.chip = Chip(self.kind)
        self.desc = QLabel(describe_track(t))
        self.desc.setWordWrap(True)
        self.desc.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.stat = QLabel("")
        self.stat.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        lay.addWidget(self.chip, 0, 0, Qt.AlignmentFlag.AlignTop)
        lay.addWidget(self.desc, 0, 1)
        lay.addWidget(self.stat, 0, 2)
        lay.setColumnStretch(1, 1)
        self.bar = None
        if captured:
            self.bar = ThinBar(self.kind)
            lay.addWidget(self.bar, 1, 1, 1, 2)
            self.stat.setText("Aguardando…")
        else:
            self.stat.setText("Encontrada · não é gravada")
            self.chip.setEnabled(False)
            self.desc.setEnabled(False)
            self.stat.setEnabled(False)

    def set_live(self, s: dict, dynamic: bool):
        self.last = s
        done, seen = int(s.get("done", 0)), int(s.get("seen", 0))
        state = STATE_PT.get(s.get("state", ""), s.get("state", ""))
        spd = f" · {fmt_bitrate(s.get('speed', 0) * 8)}" if s.get("state") == "Downloading" else ""
        self.stat.setText(f"{state}{spd} · {done}/{seen} seg · {human(s.get('bytes', 0))}")
        if self.bar is not None and seen:
            self.bar.set_frac(done / seen)


class Tile(QFrame):
    """Número grande + legenda."""

    def __init__(self, caption: str, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.value = QLabel("—")
        f = QFont(self.value.font())
        f.setPointSizeF(f.pointSizeF() * 1.55)
        f.setBold(True)
        self.value.setFont(f)
        lay.addWidget(self.value)
        lay.addWidget(muted_label(caption))

    def set(self, text: str):
        self.value.setText(text)


# ───────────────────────────── o cartão ─────────────────────────────
class DashLiveCard(CardBox):
    stopRequested = Signal()

    def __init__(self, parent=None):
        super().__init__("Captura em andamento", parent)
        self.dynamic = True
        self._rows: dict[str, TrackRow] = {}
        self._extra_rows: list[QWidget] = []
        top = QHBoxLayout()
        self.badge = LiveBadge()
        self.badge.set_state("idle", "Conectando…")
        top.addWidget(self.badge)
        top.addStretch(1)
        self.stop_btn = DangerButton("Parar e empacotar")
        self.stop_btn.setMinimumWidth(176)
        self.stop_btn.clicked.connect(self._stop)
        top.addWidget(self.stop_btn)
        self.body.addLayout(top)
        tiles = QHBoxLayout()
        tiles.setSpacing(28)
        self.t_rec, self.t_frames, self.t_rate, self.t_err = Tile("Gravado"), Tile("Quadros 4K"), Tile("Download total"), Tile("Falhas de rede")
        for t in (self.t_rec, self.t_frames, self.t_rate, self.t_err):
            tiles.addWidget(t)
        tiles.addStretch(1)
        self.body.addLayout(tiles)
        self.graph = SpeedGraph()
        self.body.addWidget(self.graph)
        self.tracks_box = QVBoxLayout()
        self.tracks_box.setSpacing(6)
        self.body.addLayout(self.tracks_box)
        self.note = muted_label("")
        self.note.setWordWrap(True)
        self.note.setVisible(False)
        self.body.addWidget(self.note)
        self.setVisible(False)

    # ── ciclo ──
    def begin(self):
        self.graph.clear()
        for r in list(self._rows.values()) + self._extra_rows:
            r.deleteLater()
        self._rows, self._extra_rows = {}, []
        self.note.setVisible(False)
        self.badge.set_state("idle", "Lendo o manifesto…")
        for t in (self.t_rec, self.t_frames, self.t_rate, self.t_err):
            t.set("—")
        self.stop_btn.setText("Parar e empacotar")
        self.stop_btn.setEnabled(True)
        self.stop_btn.setVisible(True)
        self.setVisible(True)

    def set_tracks(self, info: dict):
        self.dynamic = bool(info.get("dynamic", True))
        self.badge.set_state("live" if self.dynamic else "post", "AO VIVO" if self.dynamic else "ESTÁTICO")
        self.t_rec.set("00:00:00" + (f" / {fmt_hms(info['limit'])}" if info.get("limit") else ""))
        for t in info.get("captured", []):
            row = TrackRow(t, True)
            self._rows[t["kind"]] = row
            self.tracks_box.addWidget(row)
        others, alts = summarize_extra(info.get("extra", []))
        for t in others:
            row = TrackRow(t, False)
            self._extra_rows.append(row)
            self.tracks_box.addWidget(row)
        if alts:
            self.note.setText(alts)
            self.note.setVisible(True)

    def set_live(self, s: dict):
        if s.get("phase") == "post":
            self.badge.set_state("post", "EMPACOTANDO")
            self.stop_btn.setVisible(False)
            return
        rec = fmt_hms(s.get("recorded", 0))
        self.t_rec.set(rec + (f" / {fmt_hms(s['limit'])}" if s.get("limit") else ""))
        self.t_frames.set(f"{int(s.get('frames', 0)):,}".replace(",", "."))
        tracks = s.get("tracks", [])
        self.t_rate.set(fmt_bitrate(sum(t.get("speed", 0) for t in tracks) * 8))
        errs = int(s.get("errors", 0))
        self.t_err.set(str(errs))
        self.graph.push({t["kind"]: t.get("speed", 0) for t in tracks})
        for t in tracks:
            row = self._rows.get(t["kind"])
            if row is not None:
                row.set_live(t, self.dynamic)

    def finish(self, ok: bool, stopped: bool):
        self.stop_btn.setVisible(False)
        if ok:
            self.badge.set_state("ok", "CONCLUÍDA")
        elif stopped:
            self.badge.set_state("post", "INTERROMPIDA")
        else:
            self.badge.set_state("err", "COM ERRO")
        for r in self._rows.values():
            last = getattr(r, "last", None)
            if last:
                r.stat.setText(f"Finalizado · {int(last.get('done', 0))}/{int(last.get('seen', 0))} seg · {human(last.get('bytes', 0))}")
            else:
                r.stat.setText("Sem dados")

    def _stop(self):
        self.stop_btn.setText("Parando…")
        self.stop_btn.setEnabled(False)
        self.stopRequested.emit()
