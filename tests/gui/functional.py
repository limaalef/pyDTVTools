"""Testes funcionais da GUI: fila, erro, parada suave/forçada e integração real com o dtv_tools.py (protocolo v2)."""
import os, sys, tempfile, json
from pathlib import Path
os.environ["QT_QPA_PLATFORM"] = "offscreen"
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "gui"))
cfg = tempfile.mkdtemp(); os.environ["XDG_CONFIG_HOME"] = cfg; os.environ["HOME"] = cfg
from PySide6.QtCore import QEventLoop, QTimer, QSettings, Qt
import dtv_gui_runner as R
FAKE = os.path.join(ROOT, "tests", "gui", "fake_dtv.py")
REAL = R.program_and_prefix
import dtv_gui as G
QSettings.setPath(QSettings.Format.NativeFormat, QSettings.Scope.UserScope, cfg)
app = G.make_app(["x"])
QSettings().setValue("update/auto", False)       # os testes não vão à internet; a seção de atualização liga de propósito

def wait(ms):
    l = QEventLoop(); QTimer.singleShot(ms, l.quit); l.exec()
def until(pred, t=20000):
    n = 0
    while not pred() and n < t: wait(40); n += 40
    assert pred(), "timeout"
def done(w): until(lambda: not w.runner.busy and not w.bg_runner.busy)

ok = []
def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name); ok.append(bool(cond))

tmp = tempfile.mkdtemp(); f1 = os.path.join(tmp, "a.mp4"); f2 = os.path.join(tmp, "b.mp4")
for f in (f1, f2): open(f, "wb").write(b"x")

# --- 1. integração real: doctor rodando o dtv_tools.py de verdade
R.program_and_prefix = REAL
w = G.MainWindow(); w.show(); wait(500); done(w); wait(200)
tools = w._tools_res
check("integração real: doctor devolve 4 ferramentas", tools and len(tools.get("tools", [])) == 4)
check("integração real: versão e protocolo vindos do doctor", w.core_protocol == 2 and bool(w.core_version))
check("integração real: módulos python reportados", bool(tools and tools.get("modules")))
# real mpegh-scan sem arquivo → erro tratado, não trava
w.nav_to("audio"); pg = w.pages["audio"]; open(os.path.join(tmp, "v.mp4"), "wb").write(b"x"); pg.input.setText(os.path.join(tmp, "v.mp4")); pg.scan(); done(w)
check("integração real: scan sem ferramentas termina com erro legível", "MP4Box" in pg.msg.text() or len(pg.msg.text()) > 0)
print("   mensagem:", pg.msg.text()[:140])
w.close()

# --- 1b. script antigo (sem protocolo v2): a tela não pode ficar só em "Verificando…" nem em branco
R.program_and_prefix = lambda: (sys.executable, [os.path.join(ROOT, "tests", "gui", "old_dtv.py")])
w = G.MainWindow(); w.show(); wait(500); done(w); wait(200)
txt = [r["status"].text() for r in w.pages["tools"].rows.values()]
check("script antigo: a página Ferramentas não fica em 'Verificando…'", all("Verificando" not in t and "1.3.0" in t and "unrecognized" in t for t in txt))
w.close()

# --- 1c. ferramentas escolhidas na tela vão como opção explícita, sem precisar "Salvar"
R.program_and_prefix = REAL
w = G.MainWindow(); w.show(); wait(300); done(w)
tp = w.pages["tools"]
fake_ff = os.path.join(tmp, "meu_ffmpeg.exe"); open(fake_ff, "w").write("x")
tp.rows["ffmpeg"]["edit"].setText(fake_ff)
a = w.extra_args("frame")
check("ferramentas: ffmpeg da tela vai como --ffmpeg no frame", "--ffmpeg" in a and a[a.index("--ffmpeg") + 1] == fake_ff)
check("ferramentas: comando que não usa mpegh não recebe --mpegh-ui", "--mpegh-ui" not in w.extra_args("frame"))
check("ferramentas: config gravada na hora", json.load(open(G.config_path()))["ffmpeg"] == fake_ff)
jb = w.pages["frame"]; jb.input.setText(f1); jb.nolcevc.setChecked(True)
check("ferramentas: job de frame leva o --ffmpeg", "--ffmpeg" in jb.build_frame()[0].args)
check("ferramentas: doctor leva o --ffmpeg", "--ffmpeg" in tp.job().args)
w.close()

# --- 1d. ícones do menu seguem o tema ao forçar um e voltar ao outro
R.program_and_prefix = REAL
w = G.MainWindow(); w.show(); wait(300)
def icon_rgb():
    pm = w._nav_icons["ts"].icon().pixmap(18, 18)
    img = pm.toImage()
    best = max(((img.pixelColor(x, y).alpha(), img.pixelColor(x, y)) for x in range(img.width()) for y in range(img.height())), key=lambda t: t[0])[1]
    return best.lightness()
from PySide6.QtGui import QColor, QPalette
def dark_pal():
    pal = QPalette(); R = QPalette.ColorRole
    for role, col in ((R.Window, "#202124"), (R.WindowText, "#e8eaed"), (R.Base, "#171819"), (R.Text, "#e8eaed"), (R.Button, "#2b2d31"), (R.ButtonText, "#e8eaed"), (R.Highlight, "#2b7de0"), (R.HighlightedText, "#ffffff")):
        pal.setColor(role, QColor(col))
    return pal
# o plugin offscreen ignora o esquema de cores do SO: troca-se a paleta do app, que dispara o mesmo PaletteChange
check("páginas sem fundo próprio (deixam o Mica aparecer)", all(not pg.body.autoFillBackground() and not pg.scroll.viewport().autoFillBackground() for pg in w.pages.values()))
for key in ("dark", "light", "dark", "light"):
    app.setPalette(dark_pal() if key == "dark" else app.style().standardPalette()); wait(250)
    check(f"ícone acompanha o tema forçado ({key})", (icon_rgb() > 128) == (key == "dark"))
w.close()

# --- 2. fila de 2 arquivos
R.program_and_prefix = lambda: (sys.executable, [FAKE])
w = G.MainWindow(); w.show(); done(w)
w.nav_to("transcode"); pg = w.pages["transcode"]
pg.files.add_item("a", f1); pg.files.add_item("b", f2)
seen = []
w.runner.jobStarted.connect(lambda i, n, j: seen.append((i, n)))
pg.run_built(lambda: pg.build_jobs(False)); done(w)
check("fila: 2 jobs executados em sequência", seen == [(0, 2), (1, 2)])
check("fila: 2 resultados na aba Resultado", w.panel.result.files.topLevelItemCount() == 2)
check("fila: estado final 'Concluído'", w.panel.state_lb.text() == "Concluído")
check("fila: página reabilitada", pg.body.isEnabled())

# --- 3. erro
pg.run([G.Job("Falha", ["comando-inexistente"], kind="x", page=pg)]); done(w)
check("erro: estado 'Terminou com erro'", w.panel.state_lb.text() == "Terminou com erro")
check("erro: mensagem exibida na página", "desconhecido" in pg.msg.text())

# --- 4. parada suave
w.runner.env_extra["FAKE_HOLD"] = "30"
pg.run_built(lambda: pg.build_jobs(False)); until(lambda: w.panel._pct >= 25)
bars = [r.bar.value() for r in w.panel._rows.values() if r.state == G.StatusIcon.RUNNING]
check("progresso por etapa vem do `frac` do script", bool(bars) and 0 < bars[0] <= 100)
w.panel.stop_btn.click(); done(w)
check("parada suave: 'Interrompido'", w.panel.state_lb.text() == "Interrompido")
check("parada suave: segundo job da fila não roda", w.panel.title.text().endswith("item 1 de 2") or "Interrompido" in w.panel.state_lb.text())
check("parada suave: sem mensagem de erro vermelha", pg.msg.text() == "")

# --- 5. parada forçada (processo ignora STOP)
w.runner.env_extra["FAKE_HOLD"] = "30"; w.runner.env_extra["FAKE_IGNORE_STOP"] = "1"
pg.run_built(lambda: pg.build_jobs(False)); until(lambda: w.panel._pct >= 25)
w.panel.stop_btn.click(); wait(300)
check("parada forçada: botão vira 'Forçar parada'", w.panel.stop_btn.text() == "Forçar parada")
w.panel.stop_btn.click(); done(w)
check("parada forçada: processo encerrado", not w.runner.busy)

# --- 6. seleção/remoção de itens
cat = None
w.runner.env_extra.pop("FAKE_HOLD"); w.runner.env_extra.pop("FAKE_IGNORE_STOP")
w.nav_to("audio"); pg = w.pages["audio"]; pg.input.setText(f1); pg.scan(); done(w); cat = pg.catalog
n0 = len(cat.selected_ids())
check("catálogo: presets marcados por padrão", n0 == 4)
cat._mark(lambda o: False); check("catálogo: desmarcar tudo", len(cat.selected_ids()) == 0 and not pg.gen_btn.isEnabled())
cat._rows["group:2"]["item"].setCheckState(0, Qt.CheckState.Checked); check("catálogo: botão gerar habilita com 1 item", pg.gen_btn.isEnabled())
spec = cat.specs(); check("catálogo: spec do item marcado", spec == ["group:2"])
cat._rows["group:2"]["br"].setCurrentText("192k"); check("catálogo: bitrate vira spec", cat.specs() == ["group:2=aac:192k"] or cat.specs()[0].startswith("group:2="))
# --- layout por trilha (2.0 / 5.1), sempre indicando o original
cat._rows["group:2"]["br"].setCurrentText("")
lcb = cat._rows["group:2"]["lay"]
check("layout: seletor com o original indicado", lcb is not None and [lcb.itemText(i) for i in range(lcb.count())] == ["2.0 · original", "5.1"])
check("layout: par L/R não tem seletor (só 2.0)", cat._rows["pair:2+3"]["lay"] is None and "original" in cat._rows["pair:2+3"]["item"].text(1))
lcb.setCurrentIndex(lcb.findData(6))
check("layout: trocar para 5.1 vira @5.1 e o formato padrão passa a E-AC-3", cat.specs() == ["group:2@5.1"] and cat._rows["group:2"]["fmt"].currentData() == "eac3")
cat.tree.setCurrentItem(cat._rows["group:2"]["item"])
seen_lay = []; cat.on_current(lambda o: seen_lay.append((o["layout"], len(o["highlight"][0]))) if o else None)
lcb.setCurrentIndex(lcb.findData(2)); check("layout: voltar ao original remove o @ e o diagrama acompanha", cat.specs() == ["group:2"] and seen_lay and seen_lay[-1] == (2, 2))
p0 = cat._rows["preset:0"]["lay"]
check("layout: preset 5.1 oferece 2.0 e 5.1", [p0.itemText(i) for i in range(p0.count())] == ["2.0", "5.1 · original"])
pair = cat._rows["pair:2+3"]["item"]; check("catálogo: par L/R selecionável", bool(pair.flags() & Qt.ItemFlag.ItemIsUserCheckable))
check("catálogo: isolamento por edição sinalizado", not cat._rows["group:0"]["item"].icon(0).isNull())
w.nav_to("cut"); cg = w.pages["cut"]; cg.input.setText(f1); cg.read_points(); done(w)
cg.start.setText("00:10:00"); cg.end.setText("00:12:00"); cg.add_cut(); cg.start.setText("00:20:00"); cg.end.setText("00:21:00"); cg.add_cut()
check("cortes: 2 cortes adicionados", len(cg.cuts) == 2)
cg.table.selectRow(0); cg.remove_cuts(); check("cortes: remoção do selecionado", len(cg.cuts) == 1)
cg.start.setText("00:30:00"); cg.end.setText("00:29:00"); cg.add_cut(); check("cortes: fim antes do início é recusado", len(cg.cuts) == 1 and cg.msg.text() != "")
w.nav_to("ts"); tp = w.pages["ts"]; tp.files.add_item("a", f1); tp.files.add_item("b", f2); tp.files.list.setCurrentRow(0)
tp.files.rm_btn.click(); check("lista: remoção de arquivo", tp.files.values() == [f2])
jobs = tp.build_jobs(); check("ts: comando monta --outputs single", "single" in jobs[0].args)
w.close()

# --- codificadores (tabela vinda do describe do dtv_tools)
sys.path.insert(0, ROOT); import dtv_tools as _dtv
G.MainWindow._describe_core = lambda self: _dtv.describe()
w = G.MainWindow(); w.show(); done(w)
w.nav_to("transcode"); tp = w.pages["transcode"]
check("encoders: 4 codificadores no seletor", [tp.encoder.itemData(i) for i in range(tp.encoder.count())] == ["x265", "nvenc", "av1", "av1-nvenc"])
tp.files.add_item("a", f1)
tp.encoder.setCurrentIndex(tp.encoder.findData("av1"))
check("encoders: AV1 -> qualidade 28, presets 0..13, faixa até 63", tp.crf.value() == 28 and tp.crf.maximum() == 63 and tp.preset.count() == 15)
a = tp.build_jobs(True)[0].args
check("encoders: comando leva --encoder av1 --crf 28", a[a.index("--encoder") + 1] == "av1" and a[a.index("--crf") + 1] == "28.0")
tp.encoder.setCurrentIndex(tp.encoder.findData("x265"))
check("encoders: volta ao x265 com CRF 18", tp.crf.value() == 18 and tp.crf_lb.text() == "Qualidade (CRF)")
w.on_tools = None
w.set_tools_status({"version": _dtv.__version__, "protocol": 2, "tools": [{"key": "ffmpeg", "ok": True, "name": "ffmpeg", "path": "ff", "source": "x", "version": "",
                     "caps": {"lcevc_filter": True, "encoders": {"x265": True, "nvenc": False, "av1": True, "av1-nvenc": False}}}]})
check("encoders: sem hevc_nvenc/av1_nvenc no ffmpeg -> itens desabilitados", not tp.encoder.model().item(1).isEnabled() and tp.encoder.model().item(2).isEnabled())
w.close()

# --- predefinições de transcodificação
import tempfile as _tf
_pp = os.path.join(_tf.mkdtemp(), "transcode_presets.json")
G.presets_path = lambda: Path(_pp)
QSettings().setValue("tc_preset_last", "")
w = G.MainWindow(); w.show(); done(w)
w.nav_to("transcode"); tp = w.pages["transcode"]
names = [tp.preset_cb.itemData(i) for i in range(tp.preset_cb.count()) if tp.preset_cb.itemData(i) is not None]
check("predefinições: as embutidas aparecem, depois a 'configuração atual'", names[0] == "" and "AV1 · arquivo (SVT-AV1)" in names and not tp.preset_del.isEnabled())
tp.preset_cb.setCurrentIndex(tp.preset_cb.findData("AV1 · arquivo (SVT-AV1)")); tp._preset_chosen()
check("predefinições: aplicar AV1 muda codificador, CRF e preset", tp.encoder.currentData() == "av1" and tp.crf.value() == 30 and tp.preset.currentData() == "6")
check("predefinições: embutida não pode ser excluída", not tp.preset_del.isEnabled() and tp.preset_state.text() == "")
tp.crf.setValue(33)
check("predefinições: mexer na tela marca como modificada", tp.preset_state.text() == "modificada")
tp.limit.setValue(45); tp.nolcevc.setChecked(True); tp.container.setCurrentIndex(tp.container.findData("mkv")); tp.size.setText("1920x1080")
n = tp.save_preset_as("  Meu   teste ")
check("predefinições: salvar com nome normalizado, selecionada e sem 'modificada'", n == "Meu teste" and tp.preset_cb.currentData() == "Meu teste" and tp.preset_state.text() == "" and tp.preset_del.isEnabled())
check("predefinições: gravada em disco", "Meu teste" in json.load(open(_pp, encoding="utf-8"))["presets"])
try:
    tp.save_preset_as("AV1 · arquivo (SVT-AV1)"); bad = False
except ValueError:
    bad = True
check("predefinições: nome de embutida é recusado", bad)
tp.preset_cb.setCurrentIndex(tp.preset_cb.findData("HEVC 10 bits · qualidade (x265)")); tp._preset_chosen()
check("predefinições: trocar de predefinição volta ao x265", tp.encoder.currentData() == "x265" and tp.crf.value() == 18)
tp.preset_cb.setCurrentIndex(tp.preset_cb.findData("Meu teste")); tp._preset_chosen()
tp.files.add_item("a", f1); a = tp.build_jobs(True)[0].args
tp.maxrate.setValue(15)
a = tp.build_jobs(True)[0].args
check("limite de bitrate vai para o comando e some quando em 'Sem limite'", a[a.index("--max-bitrate") + 1] == "15.0")
tp.maxrate.setValue(0); check("limite 0 = não passa --max-bitrate", "--max-bitrate" not in tp.build_jobs(True)[0].args)
tp.maxrate.setValue(0)
tp.preset_cb.setCurrentIndex(tp.preset_cb.findData("AV1 · arquivo (SVT-AV1)")); tp._preset_chosen()
check("predefinição AV1 traz o limite de referência de 15 Mbps", tp.maxrate.value() == 15 and "15" in tp.maxrate.toolTip())
tp.preset_cb.setCurrentIndex(tp.preset_cb.findData("Meu teste")); tp._preset_chosen()
check("predefinições: aplicar a salva restaura tudo e vai para o comando",
      tp.encoder.currentData() == "av1" and tp.crf.value() == 33 and tp.limit.value() == 45 and tp.nolcevc.isChecked() and tp.size.text() == "1920x1080"
      and a[a.index("--encoder") + 1] == "av1" and a[a.index("--limit") + 1] == "45" and "--no-lcevc" in a and a[a.index("--container") + 1] == "mkv")
w.close()
w = G.MainWindow(); w.show(); done(w); tp = w.pages["transcode"]
check("predefinições: a última usada volta ao abrir o programa", tp.preset_cb.currentData() == "Meu teste" and tp.encoder.currentData() == "av1" and tp.crf.value() == 33)
check("predefinições: excluir", tp.delete_preset("Meu teste") and tp.preset_cb.findData("Meu teste") < 0 and tp.preset_cb.currentData() == "" and not tp.delete_preset("Meu teste"))
# predefinição com um codificador que este computador não conhece: aplica o resto e avisa
tp.store.user["de outra versão"] = {"encoder": "vp9", "crf": 25.0, "limit": 12}; tp._fill_presets("de outra versão")
warn = tp.apply_settings(tp.store.get("de outra versão"))
check("predefinições: codificador desconhecido é avisado e o resto é aplicado", bool(warn) and "vp9" in warn[0] and tp.limit.value() == 12)
w.close()

# --- normalização de volume
w = G.MainWindow(); w.show(); done(w)
w.nav_to("transcode"); tp = w.pages["transcode"]; tp.files.add_item("a", f1)
check("normalizar: desligado por padrão, nada vai para o comando", tp.norm.args() == [] and "--normalize" not in tp.build_jobs(True)[0].args)
check("normalizar: desligado esconde o pico e a meta", tp.norm.peak.isHidden() and tp.norm.target.isHidden())
tp.norm.combo.setCurrentIndex(tp.norm.combo.findData("ebu")); tp.norm.peak.setValue(-2)
a = tp.build_jobs(True)[0].args
check("normalizar: EBU vira --normalize ebu --true-peak -2", a[a.index("--normalize") + 1] == "ebu" and a[a.index("--true-peak") + 1] == "-2" and not tp.norm.peak.isHidden())
tp.norm.combo.setCurrentIndex(tp.norm.combo.findData("custom")); tp.norm.target.setValue(-18.5)
a = tp.build_jobs(True)[0].args
check("normalizar: outro valor em LUFS", a[a.index("--normalize") + 1] == "-18.5" and not tp.norm.target.isHidden())
tp.audio_mode.setCurrentIndex(tp.audio_mode.findData("copy"))
check("normalizar: só vale no modo que renderiza os áudios", "--normalize" not in tp.build_jobs(True)[0].args)
tp.audio_mode.setCurrentIndex(tp.audio_mode.findData("render"))
n = tp.save_preset_as("com volume"); tp.norm.combo.setCurrentIndex(0); tp.preset_cb.setCurrentIndex(tp.preset_cb.findData("com volume")); tp._preset_chosen()
check("normalizar: faz parte das predefinições", tp.norm.value() == "-18.5" and tp.norm.peak.value() == -2)
tp.delete_preset("com volume")
w.nav_to("audio"); ap = w.pages["audio"]; ap.input.setText(f1); ap.scan(); done(w)
ap.catalog._rows["group:2"]["item"].setCheckState(0, Qt.CheckState.Checked)
ap.norm.combo.setCurrentIndex(ap.norm.combo.findData("streaming"))
ja = ap.build_jobs()[0].args
check("normalizar: a tela de áudio MPEG-H também passa --normalize", ja[ja.index("--normalize") + 1] == "streaming")
w.close()

# --- aviso nativo ao terminar
w = G.MainWindow(); w.show(); done(w)
sent = []
w.notifier.notify = lambda title, text, kind="info": sent.append((title, text, kind)) or True
w.settings.setValue("notify/enabled", True); w.settings.setValue("notify/only_background", False)
w.nav_to("frame"); fp = w.pages["frame"]; fp.input.setText(f1); fp.run_built(fp.build_frame); done(w)
check("aviso: tarefa concluída gera 'Concluído' com o nome da tarefa", len(sent) == 1 and sent[0][0] == "Concluído" and "Extrair quadro" in sent[0][1])
w.settings.setValue("notify/only_background", True); w.isActiveWindow = lambda: True
fp.run_built(fp.build_frame); done(w)
check("aviso: 'só em segundo plano' não avisa com a janela em foco", len(sent) == 1)
w.isActiveWindow = lambda: False; fp.run_built(fp.build_frame); done(w)
check("aviso: em segundo plano avisa", len(sent) == 2)
w.notify_act.setChecked(False); fp.run_built(fp.build_frame); done(w)
check("aviso: desligado no menu não avisa e a opção é lembrada", len(sent) == 2 and w.settings.value("notify/enabled", True, bool) is False)
w.notify_act.setChecked(True)
w._q_jobs = [{"title": "Transcodificar · a.mp4", "ok": False, "quiet": False, "err": "disco cheio"}]; w._notify_done(False, False)
check("aviso: erro leva o motivo", sent[-1][0] == "Erro" and "disco cheio" in sent[-1][1] and sent[-1][2] == "error")
w.nav_to("tools"); w.pages["tools"].check(); done(w)
check("aviso: verificação pedida pelo usuário avisa", len(sent) == 4 and sent[-1][1] == "Verificando ferramentas")
w.startup_check(); done(w)
check("aviso: verificação silenciosa do início não avisa", len(sent) == 4)
from dtv_gui_notify import Notifier as _N
check("aviso: sem bandeja/serviço (aqui, offscreen), notificar devolve False em vez de falhar", _N(w).notify("t", "x") is False)
w.close()
# --- verificador de atualização
import dtv_gui_update as U
check("atualização: intervalo (nunca, recente, vencido, relógio voltou)", U.due(0, 1000) and not U.due(900, 1000) and U.due(1000, 1000 + U.INTERVAL_S) and U.due(5000, 1000))
check("atualização: automática só avisa de versão nova não ignorada", U.worth_showing({"update_available": True, "latest": "2"}, "1", False)
      and not U.worth_showing({"update_available": True, "latest": "2"}, "2", False) and not U.worth_showing({"update_available": False}, "", False)
      and U.worth_showing({"update_available": False}, "", True) and U.worth_showing({"update_available": True, "latest": "2"}, "2", True))
R.program_and_prefix = lambda: (sys.executable, [FAKE])
os.environ["FAKE_UPDATE"] = "new"
w = G.MainWindow(); w.show(); done(w)
w.check_update(manual=True); until(lambda: not w.up_runner.busy); wait(100)
box = getattr(w, "_update_box", None)
check("atualização: manual com versão nova abre o aviso com a versão e o arquivo do sistema", box is not None and "9.9.9" in box.text() and "DTVTools-win.exe" in box.text())
btns = [b.text() for b in box.buttons()]; print("   botões:", btns)
check("atualização: botões Baixar / Ver novidades / Ignorar / Fechar", set(btns) == {"Baixar", "Ver novidades", "Ignorar esta versão", "Fechar"})
next(b for b in box.buttons() if b.text() == "Ignorar esta versão").click(); box.close()
check("atualização: 'Ignorar esta versão' é lembrado", w.settings.value("update/skip", "", str) == "9.9.9")
w._update_box = None; w.check_update(manual=False); until(lambda: not w.up_runner.busy); wait(100)
check("atualização: automática não repete a versão ignorada", w._update_box is None)
w.settings.setValue("update/skip", "")
w.check_update(manual=False); until(lambda: not w.up_runner.busy); wait(100)
check("atualização: automática avisa de versão nova", w._update_box is not None and w._update_box.isVisible())
w._update_box.close(); w._update_box = None
os.environ["FAKE_UPDATE"] = "current"
w.check_update(manual=False); until(lambda: not w.up_runner.busy); wait(100)
check("atualização: automática e já atualizado fica em silêncio", w._update_box is None)
w.check_update(manual=True); until(lambda: not w.up_runner.busy); wait(100)
check("atualização: manual e já atualizado diz que está em dia", w._update_box is not None and "mais recente" in w._update_box.text())
w._update_box.close(); w._update_box = None
os.environ["FAKE_UPDATE"] = "error"
shown = []
import PySide6.QtWidgets as _W
_orig = _W.QMessageBox.warning
G.QMessageBox.warning = staticmethod(lambda *a, **k: shown.append(a[2]))
w.check_update(manual=False); until(lambda: not w.up_runner.busy); wait(100)
check("atualização: erro de rede na verificação automática é silencioso", not shown and w._update_box is None)
w.check_update(manual=True); until(lambda: not w.up_runner.busy); wait(100)
check("atualização: erro de rede na verificação manual é mostrado", len(shown) == 1 and "GitHub" in shown[0])
G.QMessageBox.warning = _orig
w.settings.setValue("update/auto", True); w.settings.setValue("update/last", 0.0); os.environ["FAKE_UPDATE"] = "new"
w._update_box = None; w.startup_check(); until(lambda: not w.up_runner.busy); wait(100)
check("atualização: na abertura, com a opção ligada e prazo vencido, consulta e avisa", w._update_box is not None)
last = w.settings.value("update/last", 0.0, float)
w._update_box.close(); w._update_box = None; w.startup_check(); until(lambda: not w.up_runner.busy); wait(100)
check("atualização: dentro das 24 h não consulta de novo", w._update_box is None and w.settings.value("update/last", 0.0, float) == last)
w.settings.setValue("update/auto", False); w.settings.setValue("update/last", 0.0); w.startup_check(); wait(300)
check("atualização: opção desligada não consulta", w._update_box is None and not w.up_runner.busy)
w.close()
R.program_and_prefix = REAL
print("RESULTADO:", sum(ok), "/", len(ok))
sys.exit(0 if all(ok) else 1)
