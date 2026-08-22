import pytest
from fashionjobs.state import validate_state
from monitor.state import LoadedState


@pytest.mark.parametrize(
    "keys",
    [
        {" 800 "},
        {"0800"},
        {"abc"},
        {"0"},
        {"800", "abc"},
    ],
)
def test_any_noncanonical_job_id_makes_the_whole_state_corrupt(keys: set[str]) -> None:
    loaded = validate_state(LoadedState(keys=keys, corrupt=False))

    assert loaded == LoadedState(keys=None, corrupt=True)


def test_canonical_positive_decimal_ids_are_preserved_exactly() -> None:
    loaded = LoadedState(keys={"1", "800", "12000001"}, corrupt=False)

    assert validate_state(loaded) is loaded


def test_an_explicit_empty_baseline_remains_usable() -> None:
    loaded = LoadedState(keys=set(), corrupt=False)

    assert validate_state(loaded) is loaded


def test_a_missing_or_already_corrupt_baseline_remains_a_first_run() -> None:
    missing = LoadedState(keys=None, corrupt=False)
    corrupt = LoadedState(keys=None, corrupt=True)

    assert validate_state(missing) is missing
    assert validate_state(corrupt) is corrupt
