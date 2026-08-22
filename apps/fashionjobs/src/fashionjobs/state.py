import re

from monitor.state import LoadedState

_CANONICAL_JOB_ID = re.compile(r"[1-9]\d*", re.ASCII)


def validate_state(loaded: LoadedState) -> LoadedState:
    """Fail closed when persisted FashionJobs keys are not canonical job IDs."""
    if loaded.keys is None or all(_CANONICAL_JOB_ID.fullmatch(key) for key in loaded.keys):
        return loaded
    return LoadedState(keys=None, corrupt=True)
