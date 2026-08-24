"""Dumb, dependency-free disk cache with a TTL.

The PRD's cost note asks for caching of the Directions/Distance Matrix work so
repeated testing on the same location pair doesn't re-bill. Keys are hashes of
the full request, so a changed departure hour or budget misses cleanly.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

from .config import get_settings


def _key_path(namespace: str, key: dict[str, Any]) -> Path:
    blob = json.dumps(key, sort_keys=True, default=str).encode()
    digest = hashlib.sha256(blob).hexdigest()[:32]
    root = Path(get_settings().cache_dir) / namespace
    root.mkdir(parents=True, exist_ok=True)
    return root / f"{digest}.json"


def get(namespace: str, key: dict[str, Any]) -> Optional[Any]:
    path = _key_path(namespace, key)
    try:
        with path.open() as fh:
            entry = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    if time.time() - entry.get("stored_at", 0) > get_settings().cache_ttl_seconds:
        return None
    return entry.get("value")


def put(namespace: str, key: dict[str, Any], value: Any) -> None:
    path = _key_path(namespace, key)
    payload = {"stored_at": time.time(), "value": value}
    # Write via a temp file in the same directory so a crashed write never
    # leaves a half-written entry that later reads as valid JSON.
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(payload, fh)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
