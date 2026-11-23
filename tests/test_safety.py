import pytest

import dtv_tools as d


def test_safe_rmtree_refuses_foreign_folder(tmp_path):
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    (downloads / "important.txt").write_text("x")
    assert d.safe_rmtree(downloads) is False
    assert (downloads / "important.txt").exists()
    assert any("not deleted" in n.lower() or "not created" in n.lower() for n in d.NOTES)


def test_safe_rmtree_deletes_own_folder(tmp_path):
    t = d.make_tmp_dir(tmp_path / "x_tmp")
    (t / "a.bin").write_bytes(b"1")
    assert d.safe_rmtree(t) is True and not t.exists()


def test_tmp_session_reuse_and_discard(tmp_path):
    src = tmp_path / "a.ts"
    src.write_bytes(b"1234")
    tmp = tmp_path / "work"
    sig = {"src": d.src_sig(src), "fps": None}
    assert d.tmp_session(tmp, sig) is False
    (tmp / "keep.bin").write_bytes(b"x")
    assert d.tmp_session(tmp, sig) is True and (tmp / "keep.bin").exists()
    assert d.tmp_session(tmp, {**sig, "fps": 30.0}) is False        # parameters changed
    assert not (tmp / "keep.bin").exists()
    (tmp / "keep.bin").write_bytes(b"x")
    src.write_bytes(b"12345678")                                    # source changed (size)
    assert d.tmp_session(tmp, {"src": d.src_sig(src), "fps": 30.0}) is False
    assert not (tmp / "keep.bin").exists()


def test_tmp_session_never_wipes_unmarked_user_folder(tmp_path):
    folder = tmp_path / "Downloads"
    folder.mkdir()
    (folder / "photo.jpg").write_bytes(b"x")
    with pytest.raises(d.Fail):
        d.tmp_session(folder, {"a": 1})
    assert (folder / "photo.jpg").exists()


def test_mp4box_in_hides_hash_and_colon(tmp_path):
    f = tmp_path / "a#b:c.mp4"
    f.write_bytes(b"data")
    alias = d.mp4box_in(f)
    assert "#" not in alias and ":" not in alias.replace("C:\\", "")
    assert open(alias, "rb").read() == b"data"
    plain = tmp_path / "plain.mp4"
    plain.write_bytes(b"1")
    assert d.mp4box_in(plain) == str(plain)


def test_free_space_check(tmp_path):
    with pytest.raises(d.Fail, match="Not enough free space"):
        d.check_free_space(tmp_path, 1 << 60, "test")
    d.check_free_space(tmp_path, 10, "test")


def test_children_are_killed():
    import sys
    p = d.sp_popen([sys.executable, "-c", "import time; time.sleep(60)"])
    d.kill_children()
    assert p.poll() is not None
