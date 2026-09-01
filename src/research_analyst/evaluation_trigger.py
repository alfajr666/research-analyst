"""Durable 5m evaluation trigger spool shared by gateway and evaluator."""

from __future__ import annotations

import json
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import config


TRIGGER_DIR = Path(os.getenv("EVALUATION_TRIGGER_DIR", str(config.DEFAULT_DB_DIR / "evaluation_triggers")))


def cutoff_key(cutoff_at: datetime, interval: str = "5m") -> str:
    """Return a filesystem-safe canonical key for one completed cutoff."""
    if isinstance(cutoff_at, str):
        cutoff_at = datetime.fromisoformat(cutoff_at.replace("Z", "+00:00"))
    if cutoff_at.tzinfo is None:
        cutoff_at = cutoff_at.replace(tzinfo=timezone.utc)
    cutoff_at = cutoff_at.astimezone(timezone.utc).replace(second=0, microsecond=0)
    minutes = {"1m": 1, "5m": 5}.get(interval, 5)
    cutoff_at = cutoff_at.replace(minute=cutoff_at.minute - cutoff_at.minute % minutes)
    return cutoff_at.strftime(f"{interval}-%Y-%m-%dT%H-%M-00Z")


def publish(cutoff_at: datetime, trigger_dir: Path | None = None,
            *, interval: str = "5m") -> tuple[bool, Path]:
    """Atomically publish one completed base cutoff; duplicate publication is harmless."""
    if interval not in {"1m", "5m"}:
        raise ValueError(f"unsupported evaluation trigger interval: {interval}")
    if isinstance(cutoff_at, str):
        cutoff_at = datetime.fromisoformat(cutoff_at.replace("Z", "+00:00"))
    if cutoff_at.tzinfo is None:
        cutoff_at = cutoff_at.replace(tzinfo=timezone.utc)
    cutoff_at = cutoff_at.astimezone(timezone.utc).replace(second=0, microsecond=0)
    minutes = 1 if interval == "1m" else 5
    cutoff_at = cutoff_at.replace(minute=cutoff_at.minute - cutoff_at.minute % minutes)
    directory = Path(trigger_dir or TRIGGER_DIR)
    directory.mkdir(parents=True, exist_ok=True)
    key = cutoff_key(cutoff_at, interval)
    destination = directory / f"{key}.json"
    if destination.exists() or destination.with_suffix(".claimed").exists() or destination.with_suffix(".processed").exists():
        return False, destination
    payload = {"interval": interval, "cutoff_at": cutoff_at.astimezone(timezone.utc).isoformat()}
    fd, temporary = tempfile.mkstemp(prefix=".trigger-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
            handle.write("\n")
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


def pending(trigger_dir: Path | None = None) -> list[Path]:
    """Return pending trigger files in cutoff order."""
    directory = Path(trigger_dir or TRIGGER_DIR)
    if not directory.exists():
        return []
    recover_claimed(directory)
    paths = [*directory.glob("1m-*.json"), *directory.glob("5m-*.json")]
    def order(path: Path) -> tuple[datetime, str]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            value = datetime.fromisoformat(payload["cutoff_at"].replace("Z", "+00:00"))
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            value = datetime.max.replace(tzinfo=timezone.utc)
        return value, path.name
    return sorted(paths, key=order)


def claim(path: Path) -> Path:
    """Claim a trigger without changing its payload or identity."""
    claimed = path.with_suffix(".claimed")
    path.rename(claimed)
    return claimed


def recover_claimed(trigger_dir: Path | None = None) -> int:
    """Return expired claims to pending so restarts cannot strand work."""
    directory = Path(trigger_dir or TRIGGER_DIR)
    lease = getattr(config, "EVALUATION_LEASE_SECONDS", 600)
    recovered = 0
    paths = [*directory.glob("1m-*.claimed"), *directory.glob("5m-*.claimed")] if directory.exists() else []
    for path in paths:
        if time.time() - path.stat().st_mtime >= lease:
            path.rename(path.with_suffix(".json"))
            recovered += 1
    return recovered


def retry(path: Path, error: str, trigger_dir: Path | None = None) -> Path:
    """Increment retry metadata, or quarantine a trigger after the retry limit."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    attempts = int(payload.get("attempts", 0)) + 1
    payload["attempts"] = attempts
    payload["last_error"] = str(error)[:500]
    destination = path.with_suffix(".json")
    if attempts > getattr(config, "EVALUATION_MAX_RETRIES", 5):
        destination = path.with_suffix(".failed")
    destination.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    path.unlink()
    return destination
