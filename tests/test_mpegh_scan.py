import json
import os
import stat
import sys

import pytest

from conftest import need_mp4box, run_cli

FAKE_UI = '''#!{py}
import sys
a = sys.argv
xml = a[a.index("-xmlSceneState") + 1]
of = a[a.index("-of") + 1]
for i in range(40000):                       # a chatty tool: far more than any pipe buffer
    print("[ui] processing access unit %d ................................................" % i)
def scene(frame):
    return """[%d]
<AudioSceneConfig uuid="11111111-2222-3333-4444-555555555555" version="11.1" configChanged="true">
 <preset id="0" isDefault="true"><customKind><description isPreferred="true">Padrao</description></customKind>
  <nonInteractiveAudioElement id="0"><kind code="1"/><customKind><description>Ambiente</description></customKind></nonInteractiveAudioElement>
  <nonInteractiveAudioElement id="5"><kind code="2"/><customKind><description>Vozes</description></customKind></nonInteractiveAudioElement>
 </preset>
 <preset id="1"><customKind><description>Campo</description></customKind>
  <nonInteractiveAudioElement id="0"><kind code="1"/><customKind><description>Ambiente</description></customKind></nonInteractiveAudioElement>
 </preset>
</AudioSceneConfig>
""" % frame
open(xml, "w", encoding="utf-8").write(scene(0) + scene(250))
open(of, "wb").write(b"x")
'''


@need_mp4box
@pytest.mark.skipif(os.name == "nt", reason="shebang script")
def test_mpegh_scan_does_not_hang_on_a_chatty_mpeghuimanager(sample_mp4, tmp_path):
    ui = tmp_path / "mpeghUiManager"
    ui.write_text(FAKE_UI.format(py=sys.executable))
    ui.chmod(ui.stat().st_mode | stat.S_IEXEC)
    # relative --cache-dir on purpose (MP4Box runs inside the cache folder)
    rc, text, ev = run_cli("mpegh-scan", sample_mp4, "--mpegh-ui", ui, "--cache-dir", "cache_rel", "--progress-json", cwd=tmp_path)
    assert rc == 0, text
    res = [e for e in ev if e["type"] == "result"][0]
    assert [p["id"] for p in res["scan"]["presets"]] == [0, 1]
    assert {g["id"] for g in res["scan"]["groups"]} == {0, 5}
