"""Append-only, file-backed delivery seam for venue-neutral alpha events."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import config


OUTBOX_DIR = config.DEFAULT_DB_DIR / "alpha_outbox"


def dedupe_key(event: dict) -> str:
    """Return the stable identity for one strategy observation."""
    observed_at = event["observed_at"]
    if hasattr(observed_at, "isoformat"):
        observed_at = observed_at.isoformat()
    material = "|".join((event["strategy_id"], event["asset"], event["direction"], str(observed_at)))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def write_event(event: dict, outbox_dir: Path = OUTBOX_DIR) -> tuple[bool, Path]:
    """Atomically append an event, returning whether it was newly written.

    ``link`` creates the final path only when absent, so concurrent evaluators
    cannot replace a previously emitted event with the same dedupe identity.
    """
    key = dedupe_key(event)
    outbox_dir.mkdir(parents=True, exist_ok=True)
    destination = outbox_dir / f"{key}.json"
    payload = dict(event)
    payload["alpha_id"] = str(uuid5(NAMESPACE_URL, key))
    payload["dedupe_key"] = key
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str) + "\n"

    fd, temporary = tempfile.mkstemp(prefix=".alpha-", suffix=".tmp", dir=outbox_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, destination)
        except FileExistsError:
            return False, destination
        return True, destination
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
