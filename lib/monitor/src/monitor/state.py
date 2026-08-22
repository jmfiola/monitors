"""The seen-key baseline: one JSON array of item keys per app.

Sync rather than async on purpose. It is one ~700-byte write per tick against a
10-second interval, so the blocking cost is noise, and keeping it sync means the
baseline has no event-loop dependency and its tests need no async fixture. The
TypeScript version is async only because node's fs API is.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class LoadedState:
    """The baseline as found on disk.

    `keys is None` means "no usable baseline, treat this as a first run". `corrupt`
    separates the two ways that happens, because they mean opposite things: missing
    is a normal first boot, while a file that exists but cannot be read as a key array
    means the baseline is *gone* and this boot will re-baseline silently — the failure
    that looks exactly like everything being fine. Nothing can recover the lost keys,
    so the only useful response is for the runner to say so.
    """

    keys: set[str] | None
    corrupt: bool


def load_state(path: str) -> LoadedState:
    """Load the previous key set, distinguishing missing from unusable.

    An empty array loads as an empty *set*, not as None — that is what makes
    `echo '[]' > state.json` the supported way to ask for the current backlog.
    """
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return LoadedState(keys=None, corrupt=False)
    except OSError:
        return LoadedState(keys=None, corrupt=True)
    try:
        parsed = json.loads(raw)
    except ValueError:
        return LoadedState(keys=None, corrupt=True)
    if not isinstance(parsed, list):
        # Valid JSON of the wrong shape is corruption too: iterating a dict would
        # silently produce a baseline of its keys.
        return LoadedState(keys=None, corrupt=True)
    if any(not isinstance(key, str) for key in parsed):
        # `save_state` only writes strings. Coercing arbitrary JSON values here
        # invents healthy-looking keys that no monitor could have persisted.
        return LoadedState(keys=None, corrupt=True)
    return LoadedState(keys=set(parsed), corrupt=False)


def save_state(path: str, keys: set[str]) -> None:
    """Persist the key set as a JSON array, creating the parent dir if needed.

    Atomic: writes a sibling temp file then renames it over `path`, so a kill
    mid-write cannot corrupt or truncate the baseline. `os.replace`, not
    `os.rename`: replace overwrites an existing destination atomically on every
    platform. A stale `${path}.tmp` from an interrupted rename is harmlessly
    overwritten on the next call (single-writer process), so no cleanup is needed.

    Sorted, unlike the TypeScript version's insertion order: the file is one a
    human diffs when a monitor misbehaves, and a stable ordering makes that diff
    mean something. The content is loaded back into a set, so order is not
    behaviour.
    """
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = f"{path}.tmp"
    Path(tmp).write_text(json.dumps(sorted(keys)), encoding="utf-8")
    os.replace(tmp, path)
