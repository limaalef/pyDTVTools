"""Texto do aviso nativo ao terminar uma fila (a parte sem janela)."""
import sys
from pathlib import Path

import pytest

pytest.importorskip("PySide6")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "gui"))
from dtv_gui_notify import summarize  # noqa: E402


def J(title, ok=True, quiet=False, err=""):
    return {"title": title, "ok": ok, "quiet": quiet, "err": err}


def test_single_success_names_the_task():
    assert summarize([J("Transcodificar · jogo.mp4")], True, False) == ("Concluído", "Transcodificar · jogo.mp4", "info")


def test_several_successes_are_counted():
    t, text, k = summarize([J("a"), J("b"), J("c")], True, False)
    assert t == "Concluído" and text == "3 tarefas concluídas." and k == "info"


def test_quiet_tasks_alone_do_not_notify():
    assert summarize([J("Analisando áudio", quiet=True)], True, False) is None
    assert summarize([], True, False) is None


def test_quiet_tasks_are_not_counted_when_others_ran():
    assert summarize([J("scan", quiet=True), J("Cortar · x.mp4")], True, False)[1] == "Cortar · x.mp4"


def test_a_quiet_task_that_fails_does_notify():
    t, text, k = summarize([J("Verificar ferramentas", ok=False, quiet=True, err="ffmpeg não encontrado")], False, False)
    assert t == "Erro" and "ffmpeg não encontrado" in text and k == "error"


def test_failure_names_the_task_and_the_reason_and_counts_the_rest():
    t, text, k = summarize([J("a.mp4", ok=False, err="disco cheio"), J("b.mp4"), J("c.mp4", ok=False, err="x")], False, False)
    assert t == "Erro" and text == "a.mp4: disco cheio (+1 com erro)" and k == "error"


def test_stopped_reports_how_many_finished():
    t, text, k = summarize([J("a"), J("b", ok=False), J("c", ok=False)], False, True)
    assert t == "Interrompido" and text.startswith("1 de 3") and k == "warning"


def test_long_messages_are_shortened_and_flattened():
    t, text, _ = summarize([J("x", ok=False, err="linha um\n" + "palavra " * 80)], False, False)
    assert "\n" not in text and len(text) <= 180 and text.endswith("…")
