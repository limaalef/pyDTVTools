import json
import os

import pytest

import dtv_tools as d


def test_option_wins_and_must_exist(fake_tool, tmp_path):
    p = fake_tool("MP4Box")
    path, src = d.find_tool("mp4box", str(p))
    assert src == "option" and path == str(p.resolve())
    with pytest.raises(d.Fail, match="--mp4box"):
        d.find_tool("mp4box", str(tmp_path / "nope"))


def test_search_order_config_then_path_then_tools_dir(fake_tool, tmp_path, monkeypatch):
    cfg_tool = fake_tool("mympeg", tmp_path / "cfg")
    path_tool = fake_tool("ffmpeg", tmp_path / "pathbin")
    tools_dir = tmp_path / "tools"
    fake_tool("ffmpeg", tools_dir / "sub")
    cfg = tmp_path / "dtv_tools.json"
    monkeypatch.setattr(d, "CONFIG_PATH", cfg)
    monkeypatch.setattr(d, "TOOLS_DIR", tools_dir)
    monkeypatch.setenv("PATH", str(path_tool.parent))
    monkeypatch.setenv("DTV_FFMPEG", "/should/be/ignored")      # environment variables are NOT part of the search

    assert d.find_tool("ffmpeg")[1] == "PATH"
    d._TOOL_CACHE.clear()
    cfg.write_text(json.dumps({"ffmpeg": str(cfg_tool)}))
    d._CFG_CACHE.clear()
    assert d.find_tool("ffmpeg")[1] == "dtv_tools.json"
    d._TOOL_CACHE.clear()
    d._CFG_CACHE.clear()
    cfg.unlink()
    monkeypatch.setenv("PATH", os.devnull)
    path, src = d.find_tool("ffmpeg")
    assert src.startswith("tools") and "sub" in src


def test_input_folder_is_never_searched(fake_tool, tmp_path, monkeypatch):
    fake_tool("MP4Box", tmp_path / "videos")                     # a binary sitting next to the user's file
    monkeypatch.setattr(d, "TOOLS_DIR", tmp_path / "empty")
    monkeypatch.setattr(d, "CONFIG_PATH", tmp_path / "none.json")
    monkeypatch.setenv("PATH", os.devnull)
    monkeypatch.chdir(tmp_path / "videos")
    with pytest.raises(d.Fail, match="not found"):
        d.find_tool("mp4box")


def test_bad_config_entry_is_reported_not_fatal(tmp_path, monkeypatch):
    cfg = tmp_path / "dtv_tools.json"
    cfg.write_text(json.dumps({"mp4box": str(tmp_path / "gone")}))
    monkeypatch.setattr(d, "CONFIG_PATH", cfg)
    monkeypatch.setattr(d, "TOOLS_DIR", tmp_path / "empty")
    monkeypatch.setenv("PATH", os.devnull)
    with pytest.raises(d.Fail):
        d.find_tool("mp4box")
    assert any("does not exist" in n for n in d.NOTES)


def test_optional_tool(monkeypatch, tmp_path):
    monkeypatch.setattr(d, "TOOLS_DIR", tmp_path / "empty")
    monkeypatch.setattr(d, "CONFIG_PATH", tmp_path / "none.json")
    monkeypatch.setenv("PATH", os.devnull)
    assert d.optional_tool("ffmpeg") is None
    with pytest.raises(d.Fail):
        d.optional_tool("ffmpeg", "/does/not/exist")
