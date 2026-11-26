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
QSettings().setValue("update/auto", False)       # os testes não consultam a internet (verificação de atualização desligada)

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
check("integração real: doctor devolve 5 ferramentas (ffplay opcional)", tools and len(tools.get("tools", [])) == 5)
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
check("script antigo: a página Ferramentas não fica em 'Verificando…'", all("Verificando" not in t and "1.3.2" in t and "unrecognized" in t for t in txt))
w.close()

# --- 1d. prévia do Corte MP4: quadros (ffmpeg real) e reprodução (ffplay trocado por um script)
import shutil, subprocess
if shutil.which("ffmpeg"):
    vid = os.path.join(tmp, "pv.mp4")
    enc = ["-c:v", "libx265", "-x265-params", "colorprim=bt2020:transfer=smpte2084:colormatrix=bt2020nc:keyint=25:open-gop=0:log-level=error", "-tag:v", "hvc1"] \
        if "libx265" in subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True).stdout else ["-c:v", "mpeg4", "-g", "25"]
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=640x360:r=25:d=6", "-vf", "format=yuv420p10le" if "x265" in " ".join(enc) else "format=yuv420p",
                    *enc, vid], check=True)
    stub = os.path.join(tmp, "ffplay_stub.sh"); argsf = os.path.join(tmp, "play_args.txt")
    open(stub, "w").write(f"#!/bin/sh\necho \"$@\" > '{argsf}'\nexit 0\n"); os.chmod(stub, 0o755)
    R.program_and_prefix = REAL
    w = G.MainWindow(); w.show(); wait(300); done(w)
    w.nav_to("cut"); cp = w.pages["cut"]
    cp.input.setText(vid); cp.read_points(); done(w); until(lambda: cp.work.isVisible() and cp.idr)
    cp.start.setText("00:00:01"); cp.end.setText("00:00:04"); cp.add_cut(); wait(100)
    cp.table.selectRow(0)
    check("prévia: ao selecionar o corte os quadros mostram o carregamento", all(i._loading for i in cp.pv_imgs) and not hasattr(cp, "pv_btn"))
    until(lambda: cp.pv_imgs[0]._img is not None and cp.pv_imgs[1]._img is not None, 30000)
    check("prévia: os dois quadros do corte aparecem", cp.pv_imgs[0]._img is not None and cp.pv_imgs[1]._img is not None)
    check("prévia: carregamento some quando os quadros chegam", not any(i._loading for i in cp.pv_imgs))
    check("prévia: legenda com o instante", "00:00:" in cp.pv_caps[0].text() and "00:00:" in cp.pv_caps[1].text())
    n_cache = len(cp._pv_cache)
    cp.table.clearSelection(); cp.table.selectRow(0); wait(700)
    check("prévia: repetir o mesmo corte usa o cache (não roda de novo)", len(cp._pv_cache) == n_cache and not w.pv_runner.busy)
    # reprodução: sem ffplay definido o erro é claro; com o programa informado ele recebe o trecho certo
    tp = w.pages["tools"]
    tp.rows["ffplay"]["edit"].setText(os.path.join(tmp, "nao_existe_ffplay"))
    cp.play(); until(lambda: not w.pl_runner.busy and not cp._playing)
    check("prévia: ffplay inexistente → mensagem na página", "ffplay" in cp.msg.text().lower())
    tp.rows["ffplay"]["edit"].setText(stub)
    cp.play(); until(lambda: not w.pl_runner.busy and not cp._playing); wait(200)
    got = open(argsf).read() if os.path.isfile(argsf) else ""
    print("   ffplay args:", got[:200])
    check("prévia: ffplay recebe -ss/-t do corte, sem áudio", "-an" in got and "-ss" in got and "-t " in got and "-autoexit" in got)
    check("prévia: botão volta a 'Reproduzir trecho'", cp.play_btn.text() == "Reproduzir trecho")
    w.close()
else:
    print("SKIP prévia do corte (sem ffmpeg)")

# --- 1e. download assistido de programas (servidor http local, zip pequeno; núcleo real)
import hashlib, http.server, threading, zipfile, functools
srvdir = os.path.join(tmp, "srv"); os.makedirs(srvdir, exist_ok=True)
zp = os.path.join(srvdir, "ff.zip")
with zipfile.ZipFile(zp, "w") as z:
    z.writestr("pkg/bin/ffmpeg" + (".exe" if os.name == "nt" else ""), "#!/bin/sh\n"); z.writestr("pkg/bin/ffplay" + (".exe" if os.name == "nt" else ""), "#!/bin/sh\n")
zsha = hashlib.sha256(open(zp, "rb").read()).hexdigest()
hnd = functools.partial(http.server.SimpleHTTPRequestHandler, directory=srvdir); hnd.log_message = lambda *a, **k: None
httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), hnd); threading.Thread(target=httpd.serve_forever, daemon=True).start()
url = f"http://127.0.0.1:{httpd.server_address[1]}/ff.zip"
R.program_and_prefix = REAL
w = G.MainWindow(); w.show(); wait(300); done(w)
tp = w.pages["tools"]
dlg = G.FetchToolDialog("ffmpeg", {"title": "t", "notice": "aviso", "license": "GPL", "license_url": "https://x.org/l", "url": "https://x.org/a.zip", "sha256_url": "https://x.org/sums"})
check("download: botão desabilitado sem aceitar a licença", not dlg.go.isEnabled() and "licença" in dlg.msg.text())
dlg.accept_lic.setChecked(True)
check("download: endereço do catálogo + checksums publicados bastam", dlg.go.isEnabled())
dlg.url.setText("https://outro.org/b.zip")
check("download: endereço próprio exige SHA-256 ou marcação explícita", not dlg.go.isEnabled() and "SHA-256" in dlg.msg.text())
dlg.sha.setText("abc"); check("download: SHA-256 inválido é recusado", not dlg.go.isEnabled())
dlg.sha.setText("a" * 64); check("download: SHA-256 válido habilita", dlg.go.isEnabled() and dlg.values()["sha256"] == "a" * 64)
dlg.sha.setText(""); dlg.url.setText("http://exemplo.org/b.zip"); check("download: só https", not dlg.go.isEnabled())
tp._ask_fetch = lambda key: {"url": url, "sha256": zsha, "unverified": False}
tp.rows["ffmpeg"]["edit"].setText("")
tp.fetch("ffmpeg")
until(lambda: tp.rows["ffmpeg"]["edit"].text() != "", 30000); until(lambda: not w.runner.busy, 30000)
got = tp.rows["ffmpeg"]["edit"].text()
check("download: caminho do programa definido na tela", got.replace("\\", "/").endswith("tools/ffmpeg/pkg/bin/ffmpeg" + (".exe" if os.name == "nt" else "")) and os.path.isfile(got))
check("download: caminho salvo na configuração", json.loads(G.config_path().read_text(encoding="utf-8")).get("ffmpeg") == got)
wait(900); done(w)  # a verificação automática depois do download termina antes do próximo
tp._ask_fetch = lambda key: {"url": url, "sha256": "0" * 64, "unverified": False}
tp.rows["ffmpeg"]["edit"].setText("")
tp.fetch("ffmpeg"); until(lambda: not w.runner.busy, 30000); wait(200)
check("download: SHA-256 errado não define caminho e mostra o erro", tp.rows["ffmpeg"]["edit"].text() == "" and "SHA-256" in tp.msg.text())
tp.rows["ffmpeg"]["edit"].setText(""); tp.rows["ffplay"]["edit"].setText(""); tp.write_config()
threading.Thread(target=httpd.shutdown, daemon=True).start(); w.close()

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
_STUB_DESCRIBE = G.MainWindow._describe_core
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
# --- abertura: `describe` em cache e processos sem console
import dtv_gui_runner as _R
R.program_and_prefix = REAL
cache_f = G.config_path().with_name("describe_cache.json"); cache_f.unlink(missing_ok=True)
calls = []
G.MainWindow._describe_core = _STUB_DESCRIBE
w = G.MainWindow(); w.close()
check("abertura: describe grava o cache", cache_f.exists() and json.load(open(cache_f))["fp"] == _R.core_fingerprint())
c = json.load(open(cache_f)); c["describe"]["marcador"] = 1; json.dump(c, open(cache_f, "w"))
w = G.MainWindow(); w.close()
check("abertura: com o cache válido não inicia processo (usa o cache)", (w.core_info or {}).get("marcador") == 1)
c["fp"] = "outro"; json.dump(c, open(cache_f, "w"))
w = G.MainWindow(); w.close()
check("abertura: script diferente invalida o cache", "marcador" not in (w.core_info or {}))
open(cache_f, "w").write("{lixo")
w = G.MainWindow(); w.close()
check("abertura: cache corrompido é ignorado e refeito", bool(w.core_info) and json.load(open(cache_f))["fp"] == _R.core_fingerprint())
R.program_and_prefix = REAL
# --- captura DASH ao vivo
import dtv_gui_dash as DD
check("dash: descrição da trilha", DD.describe_track({"width": "3840", "height": "2160", "bandwidth": 14_200_000, "codecs": "vvc1", "lang": "", "role": "main"}) == "3840×2160 · 14.2 Mbps · vvc1 · Main")
check("dash: taxas legíveis", DD.fmt_bitrate(640_000) == "640 kbps" and DD.fmt_bitrate(2_500_000) == "2.5 Mbps" and DD.fmt_bitrate(0) == "—")
oth, alts = DD.summarize_extra([{"kind": "subtitle"}, {"kind": "alt_vvc_base", "width": "1920", "height": "1080", "bandwidth": 6_000_000}])
check("dash: legenda separada das qualidades ignoradas", len(oth) == 1 and "1920×1080" in alts and "6.0 Mbps" in alts)
check("dash: escala do gráfico", DD.SpeedGraph.nice_max(0) == 1_000_000 and DD.SpeedGraph.nice_max(17_000_000) == 20_000_000 and DD.SpeedGraph.nice_max(2_100_000) == 2_500_000)
os.environ["FAKE_HOLD"] = "49"
R.program_and_prefix = lambda: (sys.executable, [FAKE])
w = G.MainWindow(); w.show(); done(w)
w.runner.env_extra["FAKE_HOLD"] = "49"
w.nav_to("dash"); pg = w.pages["dash"]
check("dash: cartão ao vivo escondido antes de gravar", not pg.live.isVisible())
pg.url.setText("https://exemplo.tv/live/manifest.mpd")
pg.run_built(pg.build_jobs)
until(lambda: len(pg.live._rows) == 3 and pg.live.t_frames.value.text() not in ("—", "")); wait(300)
check("dash: cartão aparece com o selo AO VIVO", pg.live.isVisible() and pg.live.badge._text == "AO VIVO")
check("dash: trilhas gravadas + legendas listadas", set(pg.live._rows) == {"vvc_base", "lcevc_enh", "audio"} and len(pg.live._extra_rows) == 2)
check("dash: legendas marcadas como não gravadas", all("não é gravada" in r.stat.text() for r in pg.live._extra_rows))
check("dash: linha da trilha mostra estado, velocidade e segmentos", "Mbps" in pg.live._rows["vvc_base"].stat.text() and "seg" in pg.live._rows["vvc_base"].stat.text())
check("dash: totais ao vivo", "Mbps" in pg.live.t_rate.value.text() and pg.live.t_rec.value.text() != "—")
check("dash: gráfico recebe amostras", all(len(d) >= 1 for d in pg.live.graph.series.values()) and len(pg.live.graph.series) == 3)
check("dash: formulário travado, botão Parar ativo", not pg.stream_card.isEnabled() and pg.live.stop_btn.isEnabled() and pg.live.stop_btn.text() == "Parar e empacotar")
check("dash: painel com botão 'Forçar parada' e indicador simples", w.panel.stop_btn.text() == "Forçar parada" and w.panel.stop_btn.isEnabled()
      and w.panel.state_lb.text() == "Gravando ao vivo")
pg.live.stop_btn.click(); until(lambda: not w.runner.busy); wait(300)
check("dash: parar encerra e o cartão mostra INTERROMPIDA sem botão", pg.live.badge._text == "INTERROMPIDA" and not pg.live.stop_btn.isVisible())
check("dash: formulário liberado depois de parar", pg.stream_card.isEnabled())
w.runner.env_extra["FAKE_HOLD"] = ""; os.environ.pop("FAKE_HOLD", None)
pg.run_built(pg.build_jobs)
until(lambda: not w.runner.busy, 60000); wait(300)
check("dash: execução completa termina como CONCLUÍDA", pg.live.badge._text == "CONCLUÍDA")
# 'Forçar parada' do painel mata na hora (sem esperar o empacotamento)
w.runner.env_extra["FAKE_HOLD"] = "49"
pg.run_built(pg.build_jobs); until(lambda: len(pg.live._rows) == 3); wait(300)
w.panel.stop_btn.click(); until(lambda: not w.runner.busy); wait(300)
check("dash: 'Forçar parada' do painel termina o processo e fecha o cartão", pg.live.badge._text == "INTERROMPIDA")
w.runner.env_extra.pop("FAKE_HOLD", None); os.environ.pop("FAKE_HOLD", None)
w.close()
R.program_and_prefix = REAL
print("RESULTADO:", sum(ok), "/", len(ok))
sys.exit(0 if all(ok) else 1)
