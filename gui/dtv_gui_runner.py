"""
dtv_gui_runner.py - executa os subcomandos do dtv_tools como processos filhos e traduz os eventos
(protocolo v2: `--json`, uma linha JSON por evento no stdout; docs/PROTOCOL.md) em sinais Qt.

O stdout carrega só eventos e o stderr só diagnóstico (traceback), então os canais não são misturados.
Parar: `--control` faz o script ler o stdin; `{"cmd":"stop"}` vira Ctrl+C dentro dele (em dash-capture a captura para *e empacota*
o MP4) e o fim do stdin (a GUI morreu) tem o mesmo efeito. Se o script não responder: terminate() e por fim kill().
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, QTimer, Signal

PROTOCOL = 2     # versão do protocolo que esta interface entende


@dataclass
class Job:
    title: str                                  # texto do cabeçalho do painel
    args: list                                  # argumentos do dtv_tools (subcomando + opções), sem --json/--control
    kind: str = ""                              # nome do subcomando
    page: Any = None                            # página que receberá resultado/erros/log
    indeterminate: bool = False                 # barra "ocupada" enquanto o progresso for 0 (captura ao vivo)
    stop_label: str = "Parar"
    force_only: bool = False                    # o botão do painel já é "forçar parada" (a parada normal fica na própria página)
    silent: bool = False                        # execução em segundo plano (verificação inicial)
    meta: dict = field(default_factory=dict)


def hide_console(proc: QProcess):
    """Windows: o filho (python.exe / o próprio app com --cli) não pode abrir uma janela de console, nem por um instante."""
    if os.name == "nt":
        try:
            proc.setCreateProcessArgumentsModifier(lambda a: setattr(a, "flags", a.flags | 0x08000000))   # CREATE_NO_WINDOW
        except Exception:
            pass


def core_fingerprint() -> str:
    """Identifica esta versão do dtv_tools (arquivo + tamanho + data): o `describe` só muda quando ela muda."""
    prog, prefix = program_and_prefix()
    target = prog if getattr(sys, "frozen", False) else prefix[0]
    try:
        st = os.stat(target)
        return f"{target}|{st.st_size}|{int(st.st_mtime)}"
    except OSError:
        return ""


def program_and_prefix():
    """(executável, argumentos iniciais) que rodam o dtv_tools: o app congelado (`--cli`), ou o script na raiz do projeto."""
    if getattr(sys, "frozen", False):
        return sys.executable, ["--cli"]
    exe = sys.executable
    if os.name == "nt" and exe.lower().endswith("pythonw.exe"):
        # pythonw não tem stdout/stdin: o filho não conseguiria falar. python.exe + CREATE_NO_WINDOW (abaixo) dá o mesmo sem janela.
        alt = exe[:-len("pythonw.exe")] + "python.exe"
        exe = alt if os.path.isfile(alt) else exe
    return exe, [str(Path(__file__).resolve().parent.parent / "dtv_tools.py")]


class Runner(QObject):
    queueStarted = Signal(int)
    jobStarted = Signal(int, int, object)          # índice, total, Job
    stepChanged = Signal(object, str, str, str)    # Job, id, status, detalhe
    progressChanged = Signal(object, float, str, str, float)   # Job, % total, detalhe, etapa, fração da etapa (0-1; -1 = desconhecida)
    tracksReported = Signal(object, dict)          # Job, evento `tracks` (trilhas do manifesto DASH)
    liveReported = Signal(object, dict)            # Job, evento `live` (estado da captura ao vivo)
    adbReported = Signal(object, dict)             # Job, evento `adb` (Acesso via ADB: estado do aparelho e do endereço)
    began = Signal(object, list)                   # Job, [{"id","label","weight"}]  (evento begin)
    planned = Signal(object, list)
    resultReady = Signal(object, dict)
    errorReported = Signal(object, str)
    logLine = Signal(object, str)
    jobFinished = Signal(object, int, bool)        # Job, código de saída, ok
    queueFinished = Signal(bool, bool)             # tudo ok, interrompido

    STOP_GRACE_MS = 7000
    HELLO_TIMEOUT_MS = 15000     # sem nenhum evento neste prazo, o registro avisa e mostra o comando para rodar à mão
    TERM_GRACE_MS = 4000

    def __init__(self, env_extra: dict | None = None, parent=None):
        super().__init__(parent)
        self.env_extra = dict(env_extra or {})
        self._proc: QProcess | None = None
        self._jobs: list[Job] = []
        self._i = -1
        self._buf = b""
        self._errbuf = b""
        self._stopped = False
        self._stop_sent = False
        self._all_ok = True
        self._done_emitted = False
        self._active = False
        self._watch = QTimer(self, singleShot=True)
        self._watch.timeout.connect(self._no_hello)
        self._esc = QTimer(self, singleShot=True)
        self._esc.timeout.connect(self._escalate)
        self._hard = QTimer(self, singleShot=True)
        self._hard.timeout.connect(self._hard_kill)

    # ───────────── controle ─────────────
    @property
    def busy(self) -> bool:
        return self._active

    def current_job(self) -> Job | None:
        return self._jobs[self._i] if 0 <= self._i < len(self._jobs) else None

    def start(self, jobs: list) -> bool:
        if self._proc is not None or not jobs:
            return False
        self._jobs, self._i = list(jobs), -1
        self._stopped = self._stop_sent = False
        self._all_ok = True
        self._active = True
        self.queueStarted.emit(len(self._jobs))
        self._next()
        return True

    def stop(self, force: bool = False):
        p = self._proc
        self._stopped = True
        if p is None:
            return
        if force:
            self._esc.stop()
            p.kill()
            return
        if not self._stop_sent:
            self._stop_sent = True
            p.write(b'{"cmd":"stop"}\n')
            self._esc.start(self.STOP_GRACE_MS)
        else:
            self._escalate()

    def _escalate(self):
        p = self._proc
        if p is not None and p.state() != QProcess.ProcessState.NotRunning:
            p.terminate()
            self._hard.start(self.TERM_GRACE_MS)

    def _hard_kill(self):
        p = self._proc
        if p is not None and p.state() != QProcess.ProcessState.NotRunning:
            p.kill()

    # ───────────── fila ─────────────
    def _next(self):
        self._i += 1
        if self._stopped or self._i >= len(self._jobs):
            self._proc = None
            self._active = False
            self.queueFinished.emit(self._all_ok and not self._stopped, self._stopped)
            return
        job = self._jobs[self._i]
        self._buf = self._errbuf = b""
        self._seen = set()          # tipos de evento recebidos nesta execução
        self._errtail: list = []
        self._done_emitted = False
        env = self.process_env()
        proc = QProcess(self)
        proc.setProcessEnvironment(env)
        proc.readyReadStandardOutput.connect(self._on_ready)
        proc.readyReadStandardError.connect(self._on_err_ready)
        proc.finished.connect(self._on_finished)
        proc.errorOccurred.connect(self._on_error)
        self._proc = proc
        hide_console(proc)         # sem janela de console piscando para cada tarefa
        self.jobStarted.emit(self._i, len(self._jobs), job)
        prog, prefix = program_and_prefix()
        argv = prefix + [str(a) for a in job.args] + ["--json", "--control"]
        self._watch.start(self.HELLO_TIMEOUT_MS)
        proc.start(prog, argv)

    # ───────────── saída do filho ─────────────
    def _on_ready(self):
        if self._proc is None:
            return
        self._buf += bytes(self._proc.readAllStandardOutput())
        *lines, self._buf = self._buf.split(b"\n")
        for raw in lines:
            self._handle_line(raw.decode("utf-8", "replace").rstrip("\r"))

    def process_env(self) -> QProcessEnvironment:
        env = QProcessEnvironment.systemEnvironment()
        env.insert("PYTHONUNBUFFERED", "1")
        env.insert("PYTHONIOENCODING", "utf-8")
        env.insert("PYTHONUTF8", "1")
        for k, v in self.env_extra.items():
            env.insert(k, v)
        return env

    def _no_hello(self):
        job = self.current_job()
        if job is not None and "hello" not in self._seen and self._proc is not None:
            self.logLine.emit(job, f"⚠ O dtv_tools não enviou nenhum evento em {self.HELLO_TIMEOUT_MS // 1000} s. "
                                   "Confira se o dtv_tools.py da pasta acima de gui/ é a versão 1.3.2 e rode o mesmo subcomando num terminal.")

    def _on_err_ready(self):
        if self._proc is None:
            return
        self._errbuf += bytes(self._proc.readAllStandardError())
        *lines, self._errbuf = self._errbuf.split(b"\n")
        job = self.current_job()
        for raw in lines:
            if raw.strip():
                self._errtail = (self._errtail + [raw.decode("utf-8", "replace").rstrip("\r")])[-6:]
                if job is not None:
                    self.logLine.emit(job, self._errtail[-1])

    def _handle_line(self, line: str):
        job = self.current_job()
        if job is None or not line:
            return
        try:
            ev = json.loads(line)
            t = ev["type"]
        except (ValueError, KeyError, TypeError):
            self.logLine.emit(job, line)        # algo que não é evento (ex.: uma biblioteca escrevendo no stdout)
            return
        self._seen.add(t)
        if t == "hello":
            self._watch.stop()
            if ev.get("protocol") != PROTOCOL:
                self.errorReported.emit(job, f"Protocolo incompatível: o script fala a versão {ev.get('protocol')}, a interface fala a {PROTOCOL}.")
                self.stop(force=True)
        elif t == "begin":
            self.began.emit(job, list(ev.get("steps") or []))
        elif t == "log":
            lvl = ev.get("level", "info")
            prefix = {"warn": "⚠ ", "error": "✖ ", "ok": "✔ "}.get(lvl, "")
            self.logLine.emit(job, prefix + str(ev.get("msg", "")))
        elif t == "step":
            self.stepChanged.emit(job, ev.get("id", ""), ev.get("status", ""), ev.get("detail", "") or "")
            if ev.get("status") in ("done", "error", "skipped") and ev.get("detail"):
                self.logLine.emit(job, f"[{ev.get('id')}] {ev.get('detail')}")
        elif t == "progress":
            fr = ev.get("frac")
            self.progressChanged.emit(job, float(ev.get("pct", 0) or 0), ev.get("detail", "") or "", ev.get("stage", "") or "",
                                      float(fr) if fr is not None else -1.0)
        elif t == "tracks":
            self.tracksReported.emit(job, ev)
        elif t == "live":
            self.liveReported.emit(job, ev)
        elif t == "adb":
            self.adbReported.emit(job, ev)
        elif t == "plan":
            self.planned.emit(job, list(ev.get("outs") or []))
        elif t == "result":
            self.resultReady.emit(job, ev)
        elif t == "error":
            self.errorReported.emit(job, str(ev.get("message", "")))

    def _on_error(self, err):
        if err == QProcess.ProcessError.FailedToStart and not self._done_emitted:
            job = self.current_job()
            if job is not None:
                self.errorReported.emit(job, "Não foi possível iniciar o processo de trabalho.")
            self._finish(-1)

    def _on_finished(self, code, _status):
        if self._proc is not None:
            self._on_ready()
            self._on_err_ready()
            if self._errbuf.strip():
                last = self._errbuf.decode("utf-8", "replace").rstrip("\r\n")
                self._errtail = (self._errtail + [last])[-6:]
                if self.current_job() is not None:
                    self.logLine.emit(self.current_job(), last)
            if self._buf.strip():
                self._handle_line(self._buf.decode("utf-8", "replace").rstrip("\r"))
        job = self.current_job()
        if job is not None and not ({"result", "error"} & self._seen) and not self._stopped:
            # o processo terminou sem dizer nada utilizável: nunca deixar a tela só em "Verificando…"
            if "hello" not in self._seen:
                why = ("O dtv_tools.py não respondeu no protocolo " + str(PROTOCOL) + " (é uma versão antiga? a interface precisa do dtv_tools 1.3.2 ou mais novo, "
                       "na pasta acima de gui/).")
            else:
                why = "O processo terminou antes de entregar o resultado."
            detail = " | ".join(self._errtail[-3:])
            self.errorReported.emit(job, f"{why} Código de saída {int(code)}." + (f" Saída de erro: {detail}" if detail else ""))
        self._finish(int(code))

    def _finish(self, code: int):
        if self._done_emitted:
            return
        self._done_emitted = True
        self._esc.stop()
        self._hard.stop()
        self._watch.stop()
        job = self.current_job()
        proc, self._proc = self._proc, None
        if proc is not None:
            proc.deleteLater()
        ok = code == 0
        if not ok:
            self._all_ok = False
        self._stop_sent = False
        if job is not None:
            self.jobFinished.emit(job, code, ok)
        QTimer.singleShot(0, self._next)
