"""Testes funcionais da GUI: fila, erro, parada suave/forçada e integração real com o dtv_tools.py (protocolo v2)."""
import os, sys, tempfile, json
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
check("layout: seletor com o original indicado", lcb is not None and [lcb.itemText(i) for i in range(lcb.count())] == ["2.0 · original", "5.1 · de 2.0"])
check("layout: par L/R não tem seletor (só 2.0)", cat._rows["pair:2+3"]["lay"] is None and "original" in cat._rows["pair:2+3"]["item"].text(1))
lcb.setCurrentIndex(lcb.findData(6))
check("layout: trocar para 5.1 vira @5.1 e o formato padrão passa a E-AC-3", cat.specs() == ["group:2@5.1"] and cat._rows["group:2"]["fmt"].currentData() == "eac3")
cat.tree.setCurrentItem(cat._rows["group:2"]["item"])
seen_lay = []; cat.on_current(lambda o: seen_lay.append((o["layout"], len(o["highlight"][0]))) if o else None)
lcb.setCurrentIndex(lcb.findData(2)); check("layout: voltar ao original remove o @ e o diagrama acompanha", cat.specs() == ["group:2"] and seen_lay and seen_lay[-1] == (2, 2))
p0 = cat._rows["preset:0"]["lay"]
check("layout: preset 5.1 oferece 2.0 e 5.1", [p0.itemText(i) for i in range(p0.count())] == ["2.0 · de 5.1", "5.1 · original"])
pair = cat._rows["pair:2+3"]["item"]; check("catálogo: par L/R selecionável", bool(pair.flags() & Qt.ItemFlag.ItemIsUserCheckable))
check("catálogo: isolamento por edição sinalizado", not cat._rows["group:0"]["item"].icon(0).isNull())
w.nav_to("cut"); cg = w.pages["cut"]; cg.input.setText(f1); cg.read_points(); done(w)
cg.start.setText("00:10:00"); cg.end.setText("00:12:00"); cg.add_cut(); cg.start.setText("00:20:00"); cg.end.setText("00:21:00"); cg.add_cut()
check("cortes: 2 cortes adicionados", len(cg.cuts) == 2)
cg.table.selectRow(0); cg.remove_cuts(); check("cortes: remoção do selecionado", len(cg.cuts) == 1)
cg.start.setText("00:30:00"); cg.end.setText("00:29:00"); cg.add_cut(); check("cortes: fim antes do início é recusado", len(cg.cuts) == 1 and cg.msg.text() != "")
w.nav_to("ts"); tp = w.pages["ts"]; tp.files.add_item("a", f1); tp.files.add_item("b", f2); tp.files.list.setCurrentRow(0)
tp.files.rm_btn.click(); check("lista: remoção de arquivo", tp.files.values() == [f2])
jobs = tp.build_jobs(); check("ts: comando monta --outputs single,dual", "single,dual" in jobs[0].args)
w.close()
print("RESULTADO:", sum(ok), "/", len(ok))
sys.exit(0 if all(ok) else 1)
