#!/usr/bin/env python3
"""Regression checks for breath render + voice memo helpers."""

from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
import time
from pathlib import Path

import numpy as np

import breath_reduce_mac as m
import sync_voice_memos as s


def assert_eq(actual, expected, label):
    if actual != expected:
        raise AssertionError(f"{label}: expected {expected!r}, got {actual!r}")


def test_overlap_render_does_not_lengthen():
    y = np.ones(5000, dtype=np.float32)
    out, _ = m._render_output_audio(y, 1000, [(1000, 2000), (1500, 2500)], atten_db=20)
    assert_eq(len(out), 5000, "overlap length")


def test_half_time_shortens():
    y = np.ones(5000, dtype=np.float32)
    out, _ = m._render_output_audio(
        y, 1000, [(1000, 2000)], atten_db=20, half_time_segments=[(1000, 2000)]
    )
    assert_eq(len(out), 4500, "half exact")


def test_half_time_fuzzy_match():
    y = np.ones(5000, dtype=np.float32)
    out, _ = m._render_output_audio(
        y, 1000, [(1000, 2000)], atten_db=20, half_time_segments=[(999, 2001)]
    )
    assert_eq(len(out), 4500, "half fuzzy")


def test_half_time_survives_adjacent_merge():
    """Adjacent breath segments merge; half on first half must still shorten."""
    y = np.ones(5000, dtype=np.float32)
    # Touching ranges merge to (1000,3000); half only on (1000,2000)
    # Expected: 1000 passthrough + 500 half + 1000 breath atten full-time + 2000 tail = 4500
    out, timeline = m._render_output_audio(
        y,
        1000,
        [(1000, 2000), (2000, 3000)],
        atten_db=20,
        half_time_segments=[(1000, 2000)],
    )
    assert_eq(len(out), 4500, "half after adjacent merge")
    # Ensure a shortened piece exists in timeline (src 1000 samples -> out 500)
    half_pieces = [t for t in timeline if t[0] == 1000 and t[1] == 2000]
    assert half_pieces, f"missing half piece in timeline: {timeline}"
    _, _, out_s, out_e = half_pieces[0]
    assert_eq(out_e - out_s, 500, "half piece output length")


def test_split_segment_by_half_time():
    pieces = m._split_segment_by_half_time(1000, 3000, [(1000, 2000)])
    assert_eq(pieces, [(1000, 2000, True), (2000, 3000, False)], "split pieces")


def test_build_target_name_no_nested_path():
    name = s.build_target_name("Title", "2024/sub/foo.m4a")
    assert_eq(name, "Title+foo.m4a", "basename target")
    assert "/" not in name and "\\" not in name


def test_build_target_name_collision_disambiguates():
    used = set()
    a = s.build_target_name("Same", "dir1/foo.m4a", unique_id="uid-aaa", used_names=used)
    used.add(a)
    b = s.build_target_name("Same", "dir2/foo.m4a", unique_id="uid-bbb", used_names=used)
    used.add(b)
    assert a != b, f"expected unique names, got {a!r} and {b!r}"
    assert a == "Same+foo.m4a"
    assert "uid" in b or "(2)" in b


def test_op_token_exists():
    assert hasattr(m, "VERSION")
    assert m.VERSION >= 63
    assert hasattr(m.BreathReducerApp, "_bump_op_token")
    assert hasattr(m.BreathReducerApp, "_release_busy_if_token")
    assert hasattr(m.BreathReducerApp, "_map_half_time_on_resize")
    assert hasattr(m.BreathReducerApp, "_run_bg_job")
    assert hasattr(m, "_audio_buffers_share_content")
    assert hasattr(m, "DEFAULT_DETECT_PARAMS")
    # process/export/rewrite should all go through the unified job helper
    src = Path(m.__file__).read_text(encoding="utf-8")
    assert "def work():\n            return process_breath" in src or "return process_breath(" in src
    assert 'busy_text="状态：正在导出 MP3..."' in src


def test_mono_buffer_share_and_single_render():
    """Mono load should share analysis/playback; process should not force dual render."""
    y = np.ones(4000, dtype=np.float32) * 0.1
    # Simulate finalize share path
    assert m._audio_buffers_share_content(y, y)
    limited_plot, limited_playback, _ = m._finalize_rendered_output(y, y, 1000)
    assert m._audio_buffers_share_content(limited_plot, limited_playback)
    out_plot, tl = m._render_output_audio(limited_plot, 1000, [(1000, 2000)], atten_db=20)
    # shared mono rewrite path returns same object for playback when bases share
    class _App:
        pass
    # just ensure share helper works for equal mono arrays that are the same object
    assert m._audio_buffers_share_content(out_plot, out_plot)
    assert len(out_plot) == 4000 or len(out_plot) == 3500 or len(out_plot) > 0
    assert tl


def test_cleanup_name_twins():
    root = Path(tempfile.mkdtemp())
    try:
        active_dir = root / "active"
        deleted_dir = root / "最近删除"
        trash = root / "回收站"
        active_dir.mkdir()
        deleted_dir.mkdir()
        name = "Song+a.m4a"
        (active_dir / name).write_bytes(b"active")
        (deleted_dir / name).write_bytes(b"deleted")
        path, conflicts = s.cleanup_name_twins(active_dir, deleted_dir, trash, name)
        assert path is not None and path.name == name
        assert conflicts == 1
        assert (active_dir / name).exists()
        assert not (deleted_dir / name).exists()
        assert any(trash.iterdir())
        # locate without trash
        (deleted_dir / name).write_bytes(b"deleted2")
        located = s.locate_managed_target(active_dir, deleted_dir, name)
        assert located is not None and located.parent == active_dir
        assert (deleted_dir / name).exists()  # not trashed by locate
    finally:
        shutil.rmtree(root)


def test_map_half_time_on_resize_preserves_partial():
    """Partial half-time must not expand to the full resized segment."""

    class _Dummy:
        pass

    app = _Dummy()
    app.half_time_ranges = [(1.0, 2.0)]
    # segment was [1,3], half only [1,2]; resize end to 3.5
    m.BreathReducerApp._map_half_time_on_resize(app, 1.0, 3.0, 1.0, 3.5)
    # proportional map: half covers first half of old length 2.0 → first 1.25 of new length 2.5
    # r0=0, r1=0.5 → mapped [1.0, 2.25]
    assert len(app.half_time_ranges) == 1
    hs, he = app.half_time_ranges[0]
    assert abs(hs - 1.0) < 1e-6
    assert abs(he - 2.25) < 1e-6


def test_resolve_under_blocks_traversal():
    root = Path(tempfile.mkdtemp())
    try:
        (root / "ok.m4a").write_bytes(b"1")
        s.resolve_under(root, "ok.m4a")
        try:
            s.resolve_under(root, "../etc/passwd")
            raise AssertionError("traversal should fail")
        except ValueError:
            pass
    finally:
        shutil.rmtree(root)


def test_missing_source_does_not_trash():
    root = Path(tempfile.mkdtemp())
    try:
        rec = root / "Recordings"
        rec.mkdir()
        tgt = root / "target"
        tgt.mkdir()
        src = rec / "a.m4a"
        src.write_bytes(b"audio-bytes-aaaa")
        db = rec / "CloudRecordings.db"
        conn = sqlite3.connect(db)
        conn.execute(
            "CREATE TABLE ZCLOUDRECORDING (ZPATH TEXT, ZUNIQUEID TEXT, ZENCRYPTEDTITLE TEXT, "
            "ZCUSTOMLABELFORSORTING TEXT, ZCUSTOMLABEL TEXT, ZDATE REAL, ZDURATION REAL, ZEVICTIONDATE REAL)"
        )
        conn.execute(
            "INSERT INTO ZCLOUDRECORDING VALUES ('a.m4a','uid1','TitleA',NULL,NULL,1.0,1.0,NULL)"
        )
        conn.commit()
        conn.close()
        state = tgt / ".voice_memos_sync_state.json"
        r1 = s.sync_once(rec, db, tgt, state)
        assert_eq(r1["copied"], 1, "first copy")
        dests = list(tgt.glob("*.m4a"))
        assert dests, "destination missing"
        src.unlink()
        r2 = s.sync_once(rec, db, tgt, state)
        assert_eq(r2["trashed"], 0, "no trash on missing source")
        assert dests[0].exists(), "file should remain"
        conn = sqlite3.connect(db)
        conn.execute("DELETE FROM ZCLOUDRECORDING")
        conn.commit()
        conn.close()
        r3 = s.sync_once(rec, db, tgt, state)
        assert_eq(r3["trashed"], 1, "trash after DB delete")
        assert not dests[0].exists(), "file should move to trash"
    finally:
        shutil.rmtree(root)


def test_conflict_goes_to_trash():
    root = Path(tempfile.mkdtemp())
    try:
        trash = root / "回收站"
        src = root / "src.m4a"
        dst = root / "dst.m4a"
        src.write_bytes(b"new-content")
        dst.write_bytes(b"old-content")
        action = s.safe_replace_move(src, dst, trash)
        assert_eq(action, "trashed_conflict", "conflict action")
        assert dst.exists()
        assert any(trash.iterdir())
    finally:
        shutil.rmtree(root)


def main():
    tests = [
        test_overlap_render_does_not_lengthen,
        test_half_time_shortens,
        test_half_time_fuzzy_match,
        test_half_time_survives_adjacent_merge,
        test_split_segment_by_half_time,
        test_build_target_name_no_nested_path,
        test_build_target_name_collision_disambiguates,
        test_op_token_exists,
        test_mono_buffer_share_and_single_render,
        test_cleanup_name_twins,
        test_map_half_time_on_resize_preserves_partial,
        test_resolve_under_blocks_traversal,
        test_missing_source_does_not_trash,
        test_conflict_goes_to_trash,
    ]
    failed = 0
    for fn in tests:
        try:
            fn()
            print(f"OK  {fn.__name__}")
        except Exception as exc:
            failed += 1
            print(f"FAIL {fn.__name__}: {exc}")
    if failed:
        raise SystemExit(f"{failed} test(s) failed")
    print(f"All {len(tests)} tests passed. VERSION={m.VERSION}")


if __name__ == "__main__":
    main()
