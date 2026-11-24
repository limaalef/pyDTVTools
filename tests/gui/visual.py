"""Teste visual: abre a GUI (offscreen), roda fluxos com o dtv_tools falso e salva capturas em claro e escuro."""
import os, sys, tempfile
os.environ["QT_QPA_PLATFORM"] = "offscreen"
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OUT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(tempfile.gettempdir(), "dtv_shots")
THEMES = sys.argv[2].split(",") if len(sys.argv) > 2 else ["light", "dark"]
os.makedirs(OUT, exist_ok=True)
sys.path.insert(0, os.path.join(ROOT, "gui"))
cfg = tempfile.mkdtemp()
os.environ["XDG_CONFIG_HOME"] = cfg; os.environ["HOME"] = cfg
from PySide6.QtCore import QEventLoop, QTimer, Qt, QSettings
from PySide6.QtGui import QImage, QPainter, QColor, QLinearGradient, QGuiApplication
import dtv_gui_runner as R
FAKE = os.path.join(ROOT, "tests", "gui", "fake_dtv.py")
R.program_and_prefix = lambda: (sys.executable, [FAKE])
import dtv_gui as G
QSettings.setPath(QSettings.Format.NativeFormat, QSettings.Scope.UserScope, cfg)
app = G.make_app(["x"])

def wait(ms):
    l = QEventLoop(); QTimer.singleShot(ms, l.quit); l.exec()

def wait_until(pred, timeout=20000, step=50):
    t = 0
    while not pred() and t < timeout:
        wait(step); t += step
    assert pred(), "timeout"

def idle(win):
    wait_until(lambda: not win.runner.busy and not win.bg_runner.busy, 30000)
    wait(150)

def sample_image(path):
    img = QImage(1280, 720, QImage.Format.Format_RGB32)
    p = QPainter(img); g = QLinearGradient(0, 0, 1280, 720); g.setColorAt(0, QColor("#0b3d2e")); g.setColorAt(1, QColor("#8bc34a")); p.fillRect(img.rect(), g)
    p.setPen(QColor("white")); p.drawEllipse(440, 160, 400, 400); p.drawLine(640, 0, 640, 720); p.end(); img.save(path)

tmp = tempfile.mkdtemp()
vid = os.path.join(tmp, "capture_20261122_004629.mp4"); open(vid, "wb").write(b"x")
vid2 = os.path.join(tmp, "jogo_2.mp4"); open(vid2, "wb").write(b"x")
tsf = os.path.join(tmp, "captura.ts"); open(tsf, "wb").write(b"x")
sdr = os.path.join(tmp, "sdr.png"); sample_image(sdr)
os.environ["FAKE_IMG"] = sdr

for theme in THEMES:
    from PySide6.QtGui import QPalette
    app.setStyle(G.FluentStyle("Fusion"))
    if theme == "dark":
        pal = QPalette(); R = QPalette.ColorRole
        for role, col in ((R.Window, "#202124"), (R.WindowText, "#e8eaed"), (R.Base, "#171819"), (R.AlternateBase, "#1d1e20"), (R.Text, "#e8eaed"),
                          (R.Button, "#2b2d31"), (R.ButtonText, "#e8eaed"), (R.Highlight, "#2b7de0"), (R.HighlightedText, "#ffffff"), (R.ToolTipBase, "#2b2d31"),
                          (R.ToolTipText, "#e8eaed"), (R.PlaceholderText, "#7d838c"), (R.Link, "#4cc2ff"), (R.BrightText, "#ffffff")):
            pal.setColor(role, QColor(col))
        for role in (R.Text, R.WindowText, R.ButtonText):
            pal.setColor(QPalette.ColorGroup.Disabled, role, QColor("#6f747b"))
        app.setPalette(pal)
    else:
        app.setPalette(app.style().standardPalette())
    win = G.MainWindow(); win.resize(1360, 860); win.show(); wait(300)
    def snap(name, w=None):
        wait(250); (w or win).grab().save(os.path.join(OUT, f"{theme}_{name}.png"))
    idle(win)
    if theme == THEMES[0]:
        print("window lightness:", win.palette().window().color().lightness())
    # --- ferramentas
    win.nav_to("tools"); win.pages["tools"].check(); idle(win); wait(300); snap("01_tools")
    # --- áudio
    win.nav_to("audio"); pg = win.pages["audio"]; pg.input.setText(vid); pg.scan(); idle(win)
    cat = pg.catalog
    for k in ("group:2", "group:3"): cat._rows[k]["item"].setCheckState(0, Qt.CheckState.Checked)
    cat.tree.setCurrentItem(cat._rows["preset:2"]["item"]); wait(300)
    snap("02_audio")
    win.resize(1360, 1560); wait(300); snap("03_audio_full"); win.resize(1360, 860); wait(200)
    pg.gen_btn.click(); idle(win); wait(300); snap("04_audio_done")
    # --- transcodificar em execução e interrompido
    win.nav_to("transcode"); pg = win.pages["transcode"]; pg.files.add_item("capture_20261122_004629.mp4   —   " + tmp, vid); pg.files.add_item("jogo_2.mp4   —   " + tmp, vid2)
    pg.scan(); idle(win)
    pg.norm.combo.setCurrentIndex(pg.norm.combo.findData("ebu"))
    pg.preset_cb.setCurrentIndex(pg.preset_cb.findData("AV1 · arquivo (SVT-AV1)")); pg._preset_chosen(); pg.crf.setValue(31); snap("04b_transcode_presets")
    pg.preset_cb.setCurrentIndex(0); pg._preset_chosen(); pg.norm.combo.setCurrentIndex(0)
    os.environ["FAKE_HOLD"] = "49"; win.runner.env_extra["FAKE_HOLD"] = "49"
    pg.run_built(lambda: pg.build_jobs(False)); wait_until(lambda: win.panel._pct >= 45); wait(400)
    win.resize(1360, 1700); wait(300); snap("05_transcode_running_full"); win.resize(1360, 860); wait(200); snap("06_transcode_running")
    win.panel.stop_btn.click(); idle(win); wait(200); snap("07_transcode_stopped")
    del os.environ["FAKE_HOLD"]; win.runner.env_extra.pop("FAKE_HOLD")
    # --- cortar
    win.nav_to("cut"); pg = win.pages["cut"]; pg.input.setText(vid); pg.read_points(); idle(win)
    pg.timeline.rangeDragged.emit(600.0, 750.0); pg.add_cut(); pg.start.setText("00:13:00"); pg.end.setText("00:14:10"); pg.add_cut(); wait(300)
    win.resize(1360, 1300); wait(300); snap("08_cut_full"); win.resize(1360, 860)
    pg.cut_btn.click(); idle(win); win.panel.tabs.setCurrentIndex(1); wait(300); snap("09_cut_done")
    # --- TS
    win.nav_to("ts"); pg = win.pages["ts"]; pg.files.add_item("captura.ts   —   " + tmp, tsf); pg.inspect(); idle(win)
    pg.run_built(pg.build_jobs); idle(win); win.panel.tabs.setCurrentIndex(1); wait(300); snap("10_ts_done")
    # --- quadro
    win.nav_to("frame"); pg = win.pages["frame"]; pg.input.setText(vid); pg.run_built(pg.build_frame); idle(win); wait(300)
    win.resize(1360, 1100); wait(300); snap("11_frame"); win.resize(1360, 860)
    # --- dash
    win.nav_to("dash"); pg = win.pages["dash"]; pg.url.setText("https://exemplo.tv/live/manifest.mpd"); wait(200); snap("12_dash")
    # registro
    win.panel.tabs.setCurrentIndex(2); wait(200); snap("13_log")
    win.runner.stop(force=True); win.close()
print("ok", OUT)
