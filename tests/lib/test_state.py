import os
from pathlib import Path

import pytest
from monitor.state import load_state, save_state


def test_missing_file_reads_as_first_run_and_is_not_corrupt(tmp_path: Path) -> None:
    loaded = load_state(str(tmp_path / "missing.json"))
    assert loaded.keys is None
    assert loaded.corrupt is False


def test_round_trips_a_key_set(tmp_path: Path) -> None:
    path = str(tmp_path / "rt.json")
    save_state(path, {"2026-12-01 10:30", "2026-12-02 09:00"})
    loaded = load_state(path)
    assert loaded.keys == {"2026-12-01 10:30", "2026-12-02 09:00"}
    assert loaded.corrupt is False


def test_an_empty_array_is_a_baseline_not_a_first_run(tmp_path: Path) -> None:
    # This is what makes `echo '[]' > state.json` the supported way to ask for the
    # current backlog: it must load as an empty set, never as None.
    path = tmp_path / "empty.json"
    path.write_text("[]", encoding="utf-8")
    assert load_state(str(path)).keys == set()


def test_an_unparseable_file_reads_as_first_run_AND_reports_corrupt(tmp_path: Path) -> None:
    # Missing and corrupt both re-baseline, and they mean opposite things. The flag
    # is what lets the runner say so — the lost keys are unrecoverable, so saying so
    # is the only useful response.
    path = tmp_path / "corrupt.json"
    path.write_text("not json{{{", encoding="utf-8")
    loaded = load_state(str(path))
    assert loaded.keys is None
    assert loaded.corrupt is True


def test_a_json_object_is_not_a_baseline(tmp_path: Path) -> None:
    # Valid JSON of the wrong shape is corruption too: iterating a dict would
    # silently produce a baseline of its keys.
    path = tmp_path / "object.json"
    path.write_text('{"a": 1}', encoding="utf-8")
    loaded = load_state(str(path))
    assert loaded.keys is None
    assert loaded.corrupt is True


def test_the_new_baseline_is_staged_in_a_sibling_before_it_replaces_the_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Freeze the rename to observe the instant before it. This is stronger than
    # asserting which functions were called: it proves the target still holds the
    # OLD baseline while the new one is on disk, which is exactly the property that
    # makes a kill mid-write survivable.
    path = tmp_path / "atomic.json"
    path.write_text('["old"]', encoding="utf-8")
    monkeypatch.setattr("monitor.state.os.replace", lambda src, dst: None)

    save_state(str(path), {"new"})

    assert path.read_text(encoding="utf-8") == '["old"]'
    assert (tmp_path / "atomic.json.tmp").read_text(encoding="utf-8") == '["new"]'


def test_a_completed_save_leaves_no_temp_file_behind(tmp_path: Path) -> None:
    path = tmp_path / "clean.json"
    save_state(str(path), {"a"})
    assert path.read_text(encoding="utf-8") == '["a"]'
    assert not (tmp_path / "clean.json.tmp").exists()


def test_it_creates_the_parent_directory(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "deeper" / "state.json"
    save_state(str(path), {"a"})
    assert load_state(str(path)).keys == {"a"}


def test_the_written_file_is_a_sorted_json_array(tmp_path: Path) -> None:
    path = tmp_path / "sorted.json"
    save_state(str(path), {"c", "a", "b"})
    assert path.read_text(encoding="utf-8") == '["a", "b", "c"]'
    assert os.path.exists(path)
