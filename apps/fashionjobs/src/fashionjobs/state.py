from monitor.state import LoadedState


def _is_safe_canonical_job_id(key: str) -> bool:
    try:
        value = int(key)
    except ValueError:
        return False
    return value > 0 and str(value) == key


def validate_state(loaded: LoadedState) -> LoadedState:
    """Fail closed when persisted FashionJobs keys are not canonical job IDs."""
    if loaded.keys is None or all(_is_safe_canonical_job_id(key) for key in loaded.keys):
        return loaded
    return LoadedState(keys=None, corrupt=True)
