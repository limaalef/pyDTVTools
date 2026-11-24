"""Layout (2.0 / 5.1) escolhido para cada trilha de áudio MPEG-H; o layout original da trilha é sempre conhecido."""
import pytest

import dtv_tools as d

pytestmark = pytest.mark.filterwarnings("ignore")


def _els(*ids):
    return [{"id": i, "name": f"G{i}", "mute_ok": True} for i in ids]


@pytest.fixture
def cat():
    info = {"unified_presets": [{"id": 1, "name": "Padrão", "elements": _els(0, 1)}],
            "unified_groups": [{"id": 0, "name": "Bed", "preset_ids": [1], "carrier_preset": 1},
                               {"id": 1, "name": "Voz L", "preset_ids": [1], "carrier_preset": 1},
                               {"id": 2, "name": "Voz R", "preset_ids": [1], "carrier_preset": 1}],
            "main": {"presets": []}}
    stream = {"elements": {0: {"channels": 6, "objects": 0, "position": False}, 1: {"channels": 2, "objects": 0, "position": False},
                           2: {"channels": 0, "objects": 0, "position": False}}}
    return d.build_catalog(info, 6, stream, False)


def test_every_option_knows_its_original_and_the_choices(cat):
    for o in cat["options"]:
        assert o["layout_orig"] == o["layout"] and o["layout"] in o["layout_choices"]
    p = next(o for o in cat["options"] if o["id"] == "preset:1")
    assert p["layout_choices"] == [2, 6]
    pair = [o for o in cat["options"] if o["id"].startswith("pair:")]
    assert pair and pair[0]["layout_choices"] == [2]                   # a L/R pair is stereo by construction


def test_native_5_1_4_is_kept_among_the_choices():
    info = {"unified_presets": [{"id": 1, "name": "P", "elements": _els(0)}],
            "unified_groups": [{"id": 0, "name": "G", "preset_ids": [1], "carrier_preset": 1}], "main": {"presets": []}}
    o = d.build_catalog(info, 16, None, False)["options"][0]
    assert o["layout"] == 16 and o["layout_choices"] == [2, 6, 16]


def test_spec_parsing_with_layout():
    assert d.parse_opt_spec("group:3@2.0") == ("group:3", None, None, 2)
    assert d.parse_opt_spec("preset:1=eac3:448k@5.1") == ("preset:1", "eac3", "448k", 6)
    assert d.parse_opt_spec("preset:1=aac@2") == ("preset:1", "aac", None, 2)
    assert d.parse_opt_spec("preset:1") == ("preset:1", None, None, None)
    with pytest.raises(d.Fail):
        d.parse_opt_spec("preset:1@7.1")


def test_choosing_another_layout_changes_channels_format_and_name(cat):
    c = d.pick_options(cat, ["preset:1@2.0"])[0]
    assert c["layout"] == 2 and c["out_ch"] == 2 and c["fmt"] == "aac" and c["inputs"] == [["p1", 2]]
    assert c["stem"].endswith("_2.0") and "5.1" not in c["stem"] and c["layout_orig"] == 6
    assert c["highlight"] == [[0, 1]]
    # the catalog itself is not modified
    assert next(o for o in cat["options"] if o["id"] == "preset:1")["layout"] == 6


def test_explicit_format_wins_over_the_new_default(cat):
    c = d.pick_options(cat, ["preset:1=flac@2.0"])[0]
    assert c["fmt"] == "flac" and c["layout"] == 2
    with pytest.raises(d.Fail):
        d.pick_options(cat, ["preset:1=nope@2.0"])


def test_layout_not_offered_is_refused(cat):
    pair = next(o["id"] for o in cat["options"] if o["id"].startswith("pair:"))
    with pytest.raises(d.Fail):
        d.pick_options(cat, [pair + "@5.1"])


def test_same_option_in_two_layouts_has_distinct_files(cat):
    a, b = d.pick_options(cat, ["preset:1@2.0", "preset:1@5.1"])
    assert a["stem"] != b["stem"] and a["inputs"] != b["inputs"]


def test_track_title_says_which_one_is_the_original(cat):
    a, b = d.pick_options(cat, ["preset:1@2.0", "preset:1"])
    assert d.track_label(a).endswith("(2.0 de 5.1)")
    assert d.track_label(b).endswith("(5.1)")


def test_cli_help_mentions_the_layout_suffix():
    p = d.build_parser()
    sub = p._subparsers._group_actions[0].choices["transcode"]
    assert any("@LAYOUT" in (x.help or "") for x in sub._actions if x.dest == "option")
