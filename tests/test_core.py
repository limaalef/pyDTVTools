import struct

import dtv_tools as d


def test_unwrap_33_bit_pts():
    es = type("E", (), {})()
    FULL = 1 << 33
    es.index = [(FULL - 200, FULL - 300, 0, 1), (FULL - 100, FULL - 200, 1, 1), (50, FULL - 100, 2, 1), (150, 0, 3, 1)]
    wraps = d.unwrap_index(es)
    pts = [x[0] for x in es.index]
    dts = [x[1] for x in es.index]
    assert wraps >= 1
    assert pts == sorted(pts) and dts == sorted(dts)
    assert pts[2] - pts[1] == 150


def test_timing_lists_handles_missing_dts_and_pts():
    idx = [(9000 * i, None, 0, 1) for i in (2, 0, 1, 5, 3, 4, 8, 6, 7, 11, 9, 10)]
    dts, pts = d.timing_lists(idx)
    assert all(b >= a for a, b in zip(dts, dts[1:]))          # decode clock must not go backwards
    assert all(p >= q for p, q in zip(pts, dts))              # a frame is never presented before it is decoded
    assert d.timing_lists([(0, 0, 0, 1)]) is None


def test_av_delay_ms():
    assert d.av_delay_ms(10.5, 10.0) == 500
    assert d.av_delay_ms(10.004, 10.0) == 0                   # below 10 ms: ignored
    assert d.av_delay_ms(None, 10.0) == 0
    assert d.av_delay_ms(9.9, 10.0) == -100


def test_lcevc_split_and_sei_roundtrip():
    nal = bytes([0x7F, 0xFF]) + b"\x01\x02\x03" * 5
    assert d.lcevc_split_nals(len(nal).to_bytes(4, "big") + nal) == [nal]
    assert d.lcevc_split_nals(b"\x00\x00\x00\x01" + nal) == [nal]
    assert d.lcevc_split_nals(b"\x00\x00\x01" + nal) == [nal]
    two = len(nal).to_bytes(4, "big") + nal + len(nal).to_bytes(4, "big") + nal
    assert d.lcevc_split_nals(two) == [nal, nal]
    sei = d.create_vvc_sei_t35_nalu(len(nal).to_bytes(4, "big") + nal)
    assert sei.startswith(b"\x00\x00\x00\x01\x00\xB9")
    assert b"\x00\x00\x00" not in sei[4:]                      # emulation prevention applied


def test_sei_goes_before_first_slice_not_after_aud():
    aud = b"\x00\x00\x00\x01\x00\x79\x00"                       # VVC AUD (nut 20 is not a slice here)
    slice_ = b"\x00\x00\x00\x01\x00\x41\xAA\xBB"                # nut 8 (IDR_N_LP)
    sei = b"\x00\x00\x00\x01\x00\xB9\x01"
    out = d.insert_sei_before_first_slice(aud + slice_, sei)
    assert out.index(sei) < out.index(slice_)


def test_cut_candidates():
    idr = [(0.0, 1, "IDR"), (2.0, 51, "IDR"), (4.0, 101, "IDR")]
    c = d.idr_candidates(idr, 2.9)
    assert (c["prev"][0], c["next"][0], c["nearest"][0]) == (2.0, 4.0, 2.0)
    c = d.idr_candidates(idr, 2.0)
    assert c["prev"][0] == 2.0 and c["next"][0] == 4.0
    c = d.idr_candidates(idr, 99)
    assert c["prev"][0] == 4.0 and c["nearest"][0] == 4.0


def test_parse_time_input():
    assert d.parse_time_input("01:02:03.5") == 3723.5
    assert d.parse_time_input("2:30") == 150
    assert d.parse_time_input("75,5") == 75.5
    import pytest
    with pytest.raises(d.Fail):
        d.parse_time_input("abc")


def test_fmt_helpers():
    assert d.fmt_clock(3661.5) == "01:01:01.500"
    assert d.human(1536) == "1.50 KB"


def test_mhas_config_parsing():
    # mpegh3daConfig: profile 0x12, 48 kHz, ref CICP 16, groups: 10-channel bed + 2 objects (taken from a real mhm1 stream)
    cfg = bytes.fromhex("12194404122020f0366069e550311060832044102208110044000081ff600d010300084000081b3200d0103591645900c400008190034040d00401000410000177f00d01030007c45ce072477e006d9c0db381b6720db381b67036ce06d9c0db381b6735130036ce6a26006d9cc0085a1000097084634bc200004638b0")
    m = d._parse_3da_config(cfg, 0)
    assert m["cicp"] == 16
    assert [(g["type"], g["signals"]) for g in m["groups"]] == [("channels", 10), ("object", 1), ("object", 1)]
    assert "10 canais" in d.describe_stream_config(m)


def test_cicp16_is_supported_layout():
    assert d.CICP_CH[16] == 10 and d.CICP_NAME[16] == "5.1.4" and d.FF_LAYOUT[16] == "5.1.4"
    assert len(d.LAYOUTS[16]) == 10


def test_mae_groups_and_element_channels():
    mae = bytes.fromhex("8180c13000780280066004502e50000198011601004404080341090001dca4180b6d90621000782003706f72033706f72053706f7200001302000cadcce0884eae6406204195b99c1d05d591a5bc80c4c41032b73383a0bab234b790189928013810032b73384283934b731b4b830b604195b99c470e05d591a5bc811195cd8dc9a70e9f0e8dbc03000203f006f00b")
    scene = d._parse_mae_groups(mae)
    assert {g: len(e["signals"]) for g, e in scene.items()} == {0: 10, 1: 1, 2: 1}
    cfg = {"groups": [{"type": "channels", "signals": 10}, {"type": "object", "signals": 1}, {"type": "object", "signals": 1}]}
    els = d._element_channels(cfg, scene)
    assert els[0]["channels"] == 10 and els[1]["objects"] == 1
    stream = {"elements": els}
    assert d.option_layout(stream, 16, False, [0])[0] == 16
    assert d.option_layout(stream, 16, False, [1, 2])[0] == 2          # objects only -> 2.0
    assert d.option_layout(stream, 6, True, [0])[0] == 6               # --native-layout wins


def _els(*ids, mute=()):
    return [{"id": i, "name": f"G{i}", "mute_ok": i in mute} for i in ids]


def test_lr_pairs_by_name():
    g = [{"id": 1, "name": "CAMPO L"}, {"id": 2, "name": "CAMPO R"}, {"id": 3, "name": "TORCIDA MANDANTE"},
         {"id": 4, "name": "Crowd-Left"}, {"id": 5, "name": "Crowd-Right"}, {"id": 6, "name": "VOZES"}, {"id": 7, "name": "AMB L"}]
    assert d.find_lr_pairs(g) == [(1, 2, "CAMPO"), (4, 5, "Crowd")]


def test_isolation_needs_mutable_others():
    pres = [{"id": 0, "elements": _els(0, 5)}, {"id": 1, "elements": _els(0, 1, 2, mute=(0,))}]
    assert d.isolation(pres, [5])[0] is False
    assert d.isolation(pres, [1, 2])[0] is True
    assert d.isolation([{"id": 1, "elements": _els(1, 2)}], [1, 2])[0] is True


def test_action_script_uses_mute_not_gain(tmp_path):
    uuid = "8E020000-0000-0000-0000-00002B184337"
    states = [{"frame": 0, "uuid": uuid, "version": "11.1", "presets": [
        {"id": 0, "elements": _els(0, 5)}, {"id": 1, "elements": _els(0, 1, 2, mute=(0,))}]}]
    out = tmp_path / "s.xml"
    assert d.action_script_for_target(out, states, "m", [1, 2], 1, "11.0", "relative")
    txt = out.read_text()
    assert 'actionType="30"' in txt and 'paramInt="1"' in txt
    assert 'actionType="40"' in txt and 'paramBool="true"' in txt and 'actionType="32"' not in txt


def test_compat_51_caps_cicp16():
    els = {0: {"channels": 10, "objects": 0, "position": False}, 1: {"channels": 0, "objects": 1, "position": True}}
    info = {"unified_presets": [{"id": 0, "name": "P", "elements": _els(0, 1)}], "unified_groups": [
        {"id": 0, "name": "Bed", "preset_ids": [0], "carrier_preset": 0}], "main": {"presets": []}}
    st = {"elements": els}
    assert d.build_catalog(info, 16, st, False)["options"][0]["layout"] == 16
    cat = d.build_catalog(info, 16, st, False, compat=True)
    assert cat["native"] == 6 and all(o["layout"] in (2, 6) for o in cat["options"])


MAE_HEX = ("8180c13000780280066004502e50000198011601004404080341090001dca4180b6d90621000782003706f72033706f72053706f7200001302000cadcce0884eae6406204195b99c1d05d591a5bc80c4c41032b73383a0bab234b790189928013810032b73384283934b731b4b830b604195b99c470e05d591a5bc811195cd8dc9a70e9f0e8dbc03000203f006f00b")


def test_asi_layout_and_edit():
    payload = bytearray.fromhex(MAE_HEX)
    lay = d._asi_layout(payload)
    assert [g["id"] for g in lay["groups"]] == [0, 1, 2]
    p1 = next(p for p in lay["presets"] if p["id"] == 1)
    assert [c["ref"] for c in p1["conds"]] == [0, 1, 2]
    d._set_bits(payload, p1["conds"][2]["pos"], 7, 127)             # redirect the condition for group 2
    d._set_bits(payload, lay["groups"][2]["default_pos"], 1, 0)
    lay2 = d._asi_layout(payload)
    assert [c["ref"] for c in next(p for p in lay2["presets"] if p["id"] == 1)["conds"]] == [0, 1, 127]
    assert len(lay2["groups"]) == 3 and len(payload) == len(bytes.fromhex(MAE_HEX))


def test_plan_isolation_methods():
    pres = [{"id": 0, "elements": _els(0, 5)}, {"id": 1, "elements": _els(0, 1, 2, mute=(0,))}, {"id": 2, "elements": _els(7)}]
    assert d.plan_isolation(pres, [7])["method"] == "preset"
    assert d.plan_isolation(pres, [1, 2]) == {"method": "mute", "preset": 1, "others": [0]}
    assert d.plan_isolation(pres, [5]) == {"method": "edit", "preset": 0, "others": [0]}
    assert d.plan_isolation(pres, [99]) is None


def test_option_spec_accepts_pairs():
    assert d.parse_opt_spec("pair:1+2=aac:128k") == ("pair:1+2", "aac", "128k", None)
