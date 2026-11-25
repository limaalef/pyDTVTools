"""Substituto do dtv_tools para testes visuais: emite eventos do protocolo v2 (JSON Lines), sem depender de MP4Box/ffmpeg."""
import json, os, sys, time, threading
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import dtv_tools as T   # só aqui, para reaproveitar as tabelas reais (etapas, pesos, formatos); a GUI nunca importa o script

def ev(**kw): print(json.dumps(kw, ensure_ascii=False), flush=True)
def step(i, s, d=""): ev(type="step", id=i, status=s, detail=d)
CUR = {"stage": "", "start": 0.0, "w": 0.0}
def prog(p, d=""):
    fr = (p - CUR["start"]) / CUR["w"] if CUR["w"] else 0.0
    ev(type="progress", stage=CUR["stage"], frac=round(max(0.0, min(1.0, fr)), 4), pct=round(p, 1), detail=d)

stop = threading.Event()
def watch():
    for line in sys.stdin:
        t = line.strip()
        if t.startswith("{"):
            try: t = str(json.loads(t).get("cmd", ""))
            except ValueError: continue
        if t.upper() == "STOP" and not os.environ.get("FAKE_IGNORE_STOP"): break
    if not os.environ.get("FAKE_IGNORE_STOP"): stop.set()
threading.Thread(target=watch, daemon=True).start()

FEED = None


def run_steps(steps, weights, hold_at=None, delay=0.05, details=None):
    """percorre as etapas; se hold_at (0-100), fica parado nesse ponto até receber STOP."""
    done = 0.0
    ev(type="begin", command=CMD, steps=[{"id": k, "label": v, "weight": weights.get(k, 0)} for k, v in steps])
    for sid, _ in steps:
        w = weights.get(sid, 0)
        CUR.update(stage=sid, start=done, w=w)
        step(sid, "running")
        for k in range(1, 11):
            p = done + w * k / 10
            if hold_at is not None and p >= hold_at:
                prog(p, (details or {}).get(sid, "00:04 / 00:11 · 3,95 fps · 0,101x"))
                while not stop.is_set():
                    if FEED: FEED()
                    time.sleep(0.2)
                ev(type="error", kind="interrupted", message="Interrompido pelo usuário."); sys.exit(130)
            prog(p, (details or {}).get(sid, f"{k*10}%")); time.sleep(delay)
            if FEED: FEED()
        done += w
        step(sid, "done", (details or {}).get(sid + "_done", ""))
    return done

def outputs(*names, folder="/home/usuario/Vídeos"):
    return [{"kind": "x", "title": n, "path": f"{folder}/{n}", "size": 123456789 + i * 55555555} for i, n in enumerate(names)]

def scan_audio():
    L = T.catalog_public()
    F = {k: {"label": v["label"], "br": v["br"], "max": v["max"]} for k, v in T.FORMATS.items()}
    presets = [{"id": 0, "name": "Padrão"}, {"id": 1, "name": "Campo"}, {"id": 2, "name": "Torcida Palmeiras"}, {"id": 3, "name": "Torcida Flamengo"}]
    groups = [{"id": 0, "name": "Futebol na Globo"}, {"id": 1, "name": "Ambiente"}, {"id": 2, "name": "CAMPO L"}, {"id": 3, "name": "CAMPO R"}, {"id": 4, "name": "TORCIDA MANDANTE"}]
    f51 = [k for k in F][:3]
    opts = []
    for p in presets:
        opts.append({"id": f"preset:{p['id']}", "group": "presets", "title": p["name"], "desc": f"Preset {p['id']}", "layout": 6, "out_ch": 6,
                     "composition": "10 canais + %d objeto(s)" % (1 + p["id"] % 2), "fmt": "eac3", "formats": list(F), "isolation": "preset", "highlight": [[0, 1, 2, 3, 4, 5]]})
    for g in groups:
        iso = g["id"] not in (0, 4)
        opts.append({"id": f"group:{g['id']}", "group": "groups", "title": g["name"], "desc": f"Grupo {g['id']}", "layout": 6 if g["id"] < 2 else 2,
                     "out_ch": 6 if g["id"] < 2 else 2, "composition": "10 canais" if g["id"] < 2 else "1 objeto em 2.0", "fmt": "eac3" if g["id"] < 2 else "aac",
                     "formats": list(F), "isolation": "preset" if iso else "edit", "highlight": [[0, 1]]})
    opts.append({"id": "pair:2+3", "group": "groups", "title": "CAMPO (L+R)", "desc": "Par L/R", "layout": 2, "out_ch": 2, "composition": "2 objetos", "fmt": "aac",
                 "formats": list(F), "isolation": "mute", "elements": [2, 3], "highlight": [[0, 1]]})
    for o in opts:                                     # como o dtv_tools: layout original da trilha e os que podem ser escolhidos
        o["layout_orig"] = o["layout"]
        o["layout_choices"] = [2] if o["id"].startswith("pair:") else sorted({2, 6, o["layout"]})
    tl = [{"start_s": 0, "end_s": 600, "has_scene": True, "presets": {"0": "Padrão", "1": "Campo", "2": "Torcida Palmeiras", "3": "Torcida Flamengo"}, "groups": {"0": "", "1": "", "2": "", "3": "", "4": ""}},
          {"start_s": 600, "end_s": 780, "has_scene": True, "presets": {"0": "Padrão", "1": "Campo"}, "groups": {"0": "", "1": "", "2": "", "3": ""}},
          {"start_s": 780, "end_s": 1500, "has_scene": True, "presets": {"0": "Padrão", "1": "Campo", "2": "Torcida Palmeiras", "3": "Torcida Flamengo"}, "groups": {"0": "", "1": "", "2": "", "3": "", "4": ""}}]
    return {"duration": 1500.0, "timeline": tl, "presets": presets, "groups": groups, "options": opts, "layouts": L, "formats": F, "cache": "/tmp/x",
            "warnings": ["Grupo 4 (TORCIDA MANDANTE): o preset 2 não permite silenciar G0; a saída tende a vir com esses elementos junto.",
                         "A configuração de áudio (AudioSceneConfig) muda ao longo do vídeo. Confira no gráfico abaixo a disponibilidade de cada preset e grupo."]}

CMD = ""

def main():
    a = sys.argv[1:]
    global CMD
    cmd, hold = a[0], os.environ.get("FAKE_HOLD")
    CMD = cmd
    ev(type="hello", protocol=2, version=T.__version__, command=cmd, pid=os.getpid(), features=list(T.FEATURES))
    hold = float(hold) if hold else None
    if cmd == "doctor":
        run_steps([("check", "Check the external programs")], {"check": 100}, delay=0.02)
        tools = [{"key": "mp4box", "name": "MP4Box", "ok": True, "path": "C:/Users/x/Tools/MP4Box.exe", "source": "tools/", "version": "GPAC 2.4.0", "caps": {"vvc": True}},
                 {"key": "ffmpeg", "name": "ffmpeg", "ok": True, "path": "/usr/bin/ffmpeg", "source": "PATH", "version": "ffmpeg 7.1", "caps": {"lcevc_filter": False}},
                 {"key": "mpegh_ui", "name": "mpeghUiManager", "ok": False, "path": "", "source": "", "version": "", "caps": {}},
                 {"key": "mpegh_decoder", "name": "mpeghDecoder", "ok": False, "path": "", "source": "", "version": "", "caps": {}}]
        ev(type="result", out="", outputs=[], notes=[], rows=[], tools=tools, modules={"requests": True, "rich": False, "numpy": True, "PIL": True}, version=T.__version__, protocol=2, features=list(T.FEATURES)); return 0
    if cmd == "check-update":
        mode = os.environ.get("FAKE_UPDATE", "new")
        if mode == "error":
            ev(type="error", kind="fail", message="Não foi possível consultar o GitHub (sem internet?)"); return 2
        new = mode == "new"
        info = {"repo": "x/y", "current": T.__version__, "latest": "9.9.9" if new else T.__version__, "update_available": new,
                "url": "https://example.invalid/rel", "published": "", "prerelease": False, "notes": "Novidades da versão",
                "assets": [], "suggested": {"name": "DTVTools-win.exe", "size": 52428800, "url": "https://example.invalid/DTVTools-win.exe"} if new else None}
        ev(type="result", out="", outputs=[], notes=[], rows=[], update=info); return 0
    if cmd == "mpegh-scan":
        run_steps(T.MP_SCAN_STEPS, {"prepare": 65, "scene": 30, "catalog": 5}, delay=0.02)
        s = scan_audio(); ev(type="result", out="", outputs=[], scan=s, notes=s["warnings"]); return 0
    if cmd == "mp4-cut" and "--list-idr" in a:
        ev(type="begin", command=CMD, steps=[{"id": "read", "label": "Read the keyframe table", "weight": 8}])
        CUR.update(stage="read", start=0.0, w=8)
        step("read", "running"); time.sleep(0.2)
        idr = [{"time": round(i * 2.0 + (0.04 if i % 7 == 0 else 0), 3), "frame": i * 120, "kind": "IDR"} for i in range(0, 750)]
        step("read", "done", "750 pontos"); prog(100)
        ev(type="result", out="", outputs=[], notes=[], scan={"duration": 1500.0, "frames": 90000, "timescale": 60000, "idr": idr}); return 0
    if cmd == "ts-tv3" and "--dry-run" in a:
        for m in ("PID 0x0100  stream_type 0x33 (VVC)", "PID 0x0101  stream_type 0x36 (LCEVC)", "PID 0x0110  stream_type 0x2D (MPEG-H)"): ev(type="log", level="info", msg=m)
        ev(type="result", out="", outputs=[], notes=[], dry=True); return 0
    if cmd == "ts-tv3":
        run_steps(T.TS_STEPS, T.TS_WEIGHTS, hold_at=hold, delay=0.01)
        ev(type="result", out="x", outputs=outputs("capture_single_track_4k.mp4", "capture_dual_track_4k.mp4"), notes=["LCEVC pareado por índice em 12 quadros."],
           rows=[["Programa", "1"], ["Quadros VVC", "89.940"], ["Frame rate", "59.94 fps"]],
           tracks=[{"pid": 256, "kind": "VVC", "frames": 89940, "cc_errors": 0}, {"pid": 257, "kind": "LCEVC", "frames": 89938, "cc_errors": 3}, {"pid": 272, "kind": "MPEGH", "frames": 70290, "cc_errors": 0}]); return 0
    if cmd in ("transcode",):
        steps = list(T.TC_STEPS_VIDEO) + list(T.TC_STEPS_AUDIO) + [("encode", "Codificar o vídeo HEVC HDR10 e montar o arquivo"), ("report", "Resumo")]
        det = {"a_check": "p0 (00:11, 6ch), p1 (00:11, 6ch)", "a_encode": "2 arquivo(s) de áudio gerado(s)"}
        run_steps(steps, T.TC_WEIGHTS_AUDIO, hold_at=hold, delay=0.01, details=det)
        ev(type="result", out="x", outputs=outputs("capture_20261122_cut_hevc_hdr10.mp4"), notes=[], rows=[["Codec", "HEVC Main10 · CRF 18"], ["Faixas de áudio", "3"]]); return 0
    if cmd == "mp4-cut":
        run_steps(T.CUT_STEPS, T.CUT_WEIGHTS, hold_at=hold, delay=0.01)
        ev(type="result", out="x", outputs=outputs("capture_cut_00-10-00_00-12-30.mp4"), notes=[], rows=[["Cortes", "1"]], sync=["Vídeo 150.000 s · áudio 150.021 s", "Diferença A/V: +21 ms (ok)"]); return 0
    if cmd == "mpegh-audio":
        run_steps(T.MP_STEPS, {"prepare": 15, "decode": 50, "check": 5, "encode": 20, "mux": 10, "report": 0}, hold_at=hold, delay=0.01)
        ev(type="result", out="x", outputs=outputs("a_p0.ec3", "a_p2.ec3", "a_g2.m4a"), notes=[], rows=[["Arquivos", "3"]]); return 0
    if cmd == "dash-capture":
        import random
        n = [0]
        def feed():
            if n[0] == 0:
                ev(type="tracks", dynamic=os.environ.get("FAKE_STATIC") != "1", limit=0, drm=False, captured=[
                    {"kind": "vvc_base", "id": "v1", "width": "3840", "height": "2160", "bandwidth": 14_200_000, "codecs": "vvc1.1.L153.CL1.O0", "lang": "", "role": "main"},
                    {"kind": "lcevc_enh", "id": "e1", "width": "3840", "height": "2160", "bandwidth": 3_400_000, "codecs": "lvc1", "lang": "", "role": ""},
                    {"kind": "audio", "id": "a1", "bandwidth": 640_000, "codecs": "mhm1.0x0C", "lang": "por", "channels": "17", "role": "main", "width": "", "height": ""}],
                   extra=[{"kind": "subtitle", "id": "s1", "codecs": "stpp", "mime": "application/mp4", "lang": "por", "bandwidth": 0, "width": "", "height": "", "role": "subtitle", "label": "Português"},
                          {"kind": "subtitle", "id": "s2", "codecs": "stpp", "mime": "application/mp4", "lang": "eng", "bandwidth": 0, "width": "", "height": "", "role": "", "label": ""},
                          {"kind": "alt_vvc_base", "id": "v0", "codecs": "vvc1", "mime": "video/mp4", "lang": "", "bandwidth": 6_000_000, "width": "1920", "height": "1080", "role": ""}])
            n[0] += 1
            i = n[0]
            ev(type="live", phase="capture", elapsed=i * 0.2, recorded=i * 0.4, limit=0, frames=i * 24, paired=i * 24, errors=i // 40,
               tracks=[{"kind": "vvc_base", "state": "Downloading", "done": i, "seen": i + 1, "bytes": i * 900_000, "speed": 1_700_000 + random.randint(-300_000, 300_000), "dur": i * 0.4},
                       {"kind": "lcevc_enh", "state": "Waiting", "done": i, "seen": i, "bytes": i * 210_000, "speed": 420_000 + random.randint(-80_000, 80_000), "dur": i * 0.4},
                       {"kind": "audio", "state": "Downloading", "done": i, "seen": i + 1, "bytes": i * 40_000, "speed": 80_000, "dur": i * 0.4}])
        global FEED
        FEED = feed
        run_steps(T.DASH_STEPS, T.DASH_WEIGHTS, hold_at=hold, delay=0.01)
        ev(type="result", out="x", outputs=outputs("capture_20261122_145001.mp4"), notes=[], rows=[]); return 0
    if cmd == "frame":
        run_steps(T.FRAME_STEPS, T.FRAME_WEIGHTS, hold_at=hold, delay=0.01)
        ev(type="result", out="x", outputs=[{"kind": "frame-hdr", "title": "HDR", "path": os.environ.get("FAKE_IMG_HDR", "/tmp/hdr.png"), "size": 20_000_000},
                                           {"kind": "frame-sdr", "title": "SDR", "path": os.environ.get("FAKE_IMG", "/tmp/sdr.png"), "size": 4_000_000}], notes=[], rows=[]); return 0
    ev(type="error", kind="fail", message=f"comando de teste desconhecido: {cmd}"); return 2

sys.exit(main())
