"""The command line and the @@DTV events are a contract with the web interface: these tests fail if it changes."""
import dtv_tools as d
from conftest import need_mp4box, run_cli


def _flags(parser, cmd):
    sub = next(a for a in parser._actions if a.dest == "cmd").choices[cmd]
    return {s for a in sub._actions for s in a.option_strings}


def test_original_commands_and_flags_are_still_there():
    p = d.build_parser()
    cmds = next(a for a in p._actions if a.dest == "cmd").choices
    assert {"ts-tv3", "mpegh-scan", "mpegh-audio"} <= set(cmds)
    common = {"--mp4box", "--ffmpeg", "--ffprobe", "--tmpdir", "--keep-temp", "--progress-json", "-v", "--verbose", "--no-color", "--ascii"}
    assert common <= _flags(p, "ts-tv3")
    assert {"--outputs", "--program", "--vvc-pid", "--lcevc-pid", "--mpegh-pid", "--fps", "--lcevc-size", "--outdir", "--name",
            "--overwrite", "--dry-run"} <= _flags(p, "ts-tv3")
    assert {"--mpegh-ui", "--mpegh-decoder", "--native-layout", "--cache-dir"} | common <= _flags(p, "mpegh-scan")
    assert {"--option", "--mpegh-ui", "--mpegh-decoder", "--native-layout", "--jobs", "--mux", "--audio-delay", "--drc-effect",
            "--target-loudness", "--sleep-mode", "--uim-version", "--outdir", "--cache-dir", "--overwrite"} | common <= _flags(p, "mpegh-audio")


def test_new_commands_keep_the_old_names_as_aliases():
    p = d.build_parser()
    assert {"--sem-ajuste-av", "--no-av-fix", "--snap", "--end-snap", "--list-idr", "-s", "-e", "-o"} <= _flags(p, "mp4-cut")
    assert {"--mp4box-binary-path", "--ffmpeg-binary-path", "--tmp-dir", "--save-dir", "--save-name", "--thread-count",
            "--live-record-limit", "--live-take-count", "--live-wait-time", "--cleanup-after", "--del-after-done", "--log-level"} <= _flags(p, "dash-capture")
    assert d.build_parser().parse_args(["mp4-cut", "x.mp4", "--sem-ajuste-av"]).no_av_fix is True
    assert d.build_parser().parse_args(["dash-capture", "http://x", "--live-wait-time", "0.4"]).live_wait_time == 0.4


def test_fps_is_a_float():
    assert d.build_parser().parse_args(["ts-tv3", "a.ts", "--fps", "59.94"]).fps == 59.94


def test_doctor_events():
    rc, text, ev = run_cli("doctor", "--progress-json")
    kinds = [e["type"] for e in ev]
    assert "step" in kinds and kinds[-1] == "result"
    res = ev[-1]
    assert {t["key"] for t in res["tools"]} == {"mp4box", "ffmpeg", "mpegh_ui", "mpegh_decoder"}
    assert set(res) >= {"out", "outputs", "notes", "rows", "tools", "modules", "version"}


def test_error_event_and_exit_codes(tmp_path):
    rc, text, ev = run_cli("ts-tv3", tmp_path / "missing.ts", "--progress-json")
    err = next(e for e in ev if e["type"] == "error")
    assert rc == 2 and "message" in err


def test_progress_json_has_no_stray_output_on_stdout(tmp_path):
    """Everything on stdout in --progress-json mode is either an @@DTV line or the human text the UI already receives."""
    rc, text, ev = run_cli("doctor", "--progress-json")
    assert not any(ln.startswith("(") or ln.startswith("['") for ln in text.splitlines())     # the old debug print is gone
