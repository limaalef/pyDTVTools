"""Predefinições de transcodificação: armazenamento, validação, importação/exportação (sem Qt)."""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "gui"))
import dtv_gui_presets as P  # noqa: E402

import dtv_tools as d  # noqa: E402


def test_builtin_presets_are_valid_and_match_the_encoder_table():
    for name, s in P.BUILTIN.items():
        assert P.clean(s) == s, name                                  # nothing is dropped by validation
        e = d.ENCODERS[s["encoder"]]
        assert e["crf_range"][0] <= s["crf"] <= e["crf_range"][1], name
        assert s.get("preset", "") in ("", *e["presets"]), name


def test_clean_drops_unknown_keys_and_wrong_types():
    s = P.clean({"encoder": "x265", "crf": "18", "limit": 30.0, "container": "avi", "no_lcevc": 1, "keep_orig": True, "zzz": 1, "native_layout": 6})
    assert s == {"encoder": "x265", "limit": 30, "keep_orig": True, "native_layout": 6}
    assert P.clean("x") == {} and P.clean(None) == {}


def test_names():
    assert P.check_name("  Meu   preset ") == "Meu preset"
    for bad in ("", "   ", "x" * 61, next(iter(P.BUILTIN))):
        with pytest.raises(ValueError):
            P.check_name(bad)


def test_save_load_delete(tmp_path):
    st = P.PresetStore(tmp_path / "p.json")
    assert st.user_names() == [] and st.error == ""
    st.put("b preset", {"encoder": "av1", "crf": 30})
    st.put("A preset", {"encoder": "x265", "crf": 20.5, "junk": 1})
    assert st.user_names() == ["A preset", "b preset"]                # sorted ignoring case
    again = P.PresetStore(tmp_path / "p.json")
    assert again.get("A preset") == {"encoder": "x265", "crf": 20.5}
    assert again.delete("A preset") and not again.delete("A preset") and not again.delete(next(iter(P.BUILTIN)))
    assert P.PresetStore(tmp_path / "p.json").user_names() == ["b preset"]
    assert not list(tmp_path.glob("*.tmp"))                           # the write is atomic: no leftovers


def test_get_returns_a_copy():
    st = P.PresetStore(Path("/nonexistent/x.json"))
    g = st.get(next(iter(P.BUILTIN)))
    g["crf"] = 99
    assert st.get(next(iter(P.BUILTIN)))["crf"] != 99


def test_corrupt_file_is_kept_aside_and_the_list_restarts(tmp_path):
    f = tmp_path / "p.json"
    f.write_text("{ not json")
    st = P.PresetStore(f)
    assert st.user_names() == [] and "ilegível" in st.error and (tmp_path / "p.json.bad").is_file() and not f.exists()
    st.put("novo", {"crf": 20})
    assert P.PresetStore(f).user_names() == ["novo"]


def test_user_preset_cannot_shadow_a_builtin_even_from_a_hand_edited_file(tmp_path):
    f = tmp_path / "p.json"
    f.write_text(json.dumps({"presets": {next(iter(P.BUILTIN)): {"crf": 1}, "ok": {"crf": 2}, "vazio": {"zzz": 1}}}))
    assert P.PresetStore(f).user_names() == ["ok"]


def test_export_import_roundtrip_renames_duplicates(tmp_path):
    a = P.PresetStore(tmp_path / "a.json")
    a.put("um", {"encoder": "nvenc", "crf": 22})
    a.put("dois", {"encoder": "av1", "crf": 30})
    assert a.export_to(tmp_path / "out.json") == 2
    b = P.PresetStore(tmp_path / "b.json")
    b.put("um", {"crf": 10})
    added = b.import_from(tmp_path / "out.json")
    assert sorted(added) == ["dois", "um (2)"] and b.get("um") == {"crf": 10} and b.get("um (2)")["encoder"] == "nvenc"
    assert P.PresetStore(tmp_path / "b.json").user_names() == ["dois", "um", "um (2)"]


def test_import_errors(tmp_path):
    st = P.PresetStore(tmp_path / "p.json")
    (tmp_path / "bad.json").write_text("nope")
    (tmp_path / "empty.json").write_text(json.dumps({"presets": {}}))
    (tmp_path / "junk.json").write_text(json.dumps({"presets": {"x": {"zzz": 1}}}))
    for f in ("bad.json", "empty.json", "junk.json", "missing.json"):
        with pytest.raises(ValueError):
            st.import_from(tmp_path / f)
    assert st.user_names() == []
