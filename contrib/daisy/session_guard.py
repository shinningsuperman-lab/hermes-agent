#!/usr/bin/env python3
"""Rotate overgrown Daisy LINE sessions before they slow the bot down.

This guard intentionally changes only the active SessionStore pointer in
sessions.json. Existing transcript files stay on disk for /resume/search, while
the next LINE message starts from a fresh short-context session.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any


PROFILE_DIR = Path(
    os.environ.get("DAISY_PROFILE_DIR", str(Path.home() / ".hermes/profiles/daisy"))
)
SESSIONS_DIR = PROFILE_DIR / "sessions"
SESSIONS_FILE = SESSIONS_DIR / "sessions.json"
LOG_FILE = PROFILE_DIR / "logs" / "session-guard.log"

MAX_PROMPT_TOKENS = int(os.environ.get("DAISY_SESSION_GUARD_MAX_PROMPT_TOKENS", "60000"))
MAX_TRANSCRIPT_BYTES = int(
    os.environ.get("DAISY_SESSION_GUARD_MAX_TRANSCRIPT_BYTES", "2000000")
)
MAX_JSONL_BYTES = int(os.environ.get("DAISY_SESSION_GUARD_MAX_JSONL_BYTES", "2000000"))
MIN_AGE_SECONDS = int(os.environ.get("DAISY_SESSION_GUARD_MIN_AGE_SECONDS", "180"))


def _now() -> datetime:
    return datetime.now()


def _iso_now() -> str:
    return _now().replace(microsecond=0).isoformat()


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        return parsed.astimezone().replace(tzinfo=None)
    return parsed


def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except FileNotFoundError:
        return 0


def _log(message: str) -> None:
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(f"{_iso_now()} {message}\n")
    except OSError:
        return


def _session_paths(session_id: str) -> tuple[Path, Path]:
    return SESSIONS_DIR / f"session_{session_id}.json", SESSIONS_DIR / f"{session_id}.jsonl"


def _should_rotate(entry: dict[str, Any]) -> list[str]:
    if (entry.get("platform") or entry.get("origin", {}).get("platform")) != "line":
        return []
    if entry.get("suspended") or entry.get("resume_pending"):
        return []

    updated_at = _parse_iso(entry.get("updated_at"))
    if updated_at is not None:
        age = (_now() - updated_at).total_seconds()
        if age < MIN_AGE_SECONDS:
            return []

    reasons: list[str] = []
    prompt_tokens = _safe_int(entry.get("last_prompt_tokens"))
    if prompt_tokens >= MAX_PROMPT_TOKENS:
        reasons.append(f"prompt_tokens={prompt_tokens}")

    session_id = str(entry.get("session_id") or "")
    if session_id:
        transcript_path, jsonl_path = _session_paths(session_id)
        transcript_bytes = _file_size(transcript_path)
        jsonl_bytes = _file_size(jsonl_path)
        if transcript_bytes >= MAX_TRANSCRIPT_BYTES:
            reasons.append(f"transcript_bytes={transcript_bytes}")
        if jsonl_bytes >= MAX_JSONL_BYTES:
            reasons.append(f"jsonl_bytes={jsonl_bytes}")

    return reasons


def _fresh_entry(entry: dict[str, Any]) -> dict[str, Any]:
    now = _iso_now()
    fresh = dict(entry)
    fresh.update(
        {
            "session_id": f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}",
            "created_at": now,
            "updated_at": now,
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "total_tokens": 0,
            "last_prompt_tokens": 0,
            "estimated_cost_usd": 0.0,
            "cost_status": "unknown",
            "expiry_finalized": False,
            "suspended": False,
            "resume_pending": False,
            "resume_reason": None,
            "last_resume_marked_at": None,
            "is_fresh_reset": True,
            "was_auto_reset": False,
            "auto_reset_reason": None,
            "reset_had_activity": False,
        }
    )
    return fresh


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + f".tmp-{os.getpid()}")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def run(dry_run: bool = False) -> int:
    if not SESSIONS_FILE.exists():
        _log("skip: sessions.json missing")
        return 0

    before_mtime = SESSIONS_FILE.stat().st_mtime_ns
    with SESSIONS_FILE.open(encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        _log("skip: sessions.json root is not an object")
        return 1

    rotated: list[tuple[str, str, list[str]]] = []
    new_data = dict(data)
    for session_key, raw_entry in data.items():
        if not isinstance(raw_entry, dict):
            continue
        reasons = _should_rotate(raw_entry)
        if not reasons:
            continue
        old_id = str(raw_entry.get("session_id") or "")
        new_data[session_key] = _fresh_entry(raw_entry)
        rotated.append((session_key, old_id, reasons))

    if not rotated:
        _log("ok: no sessions over threshold")
        return 0

    if dry_run:
        for session_key, old_id, reasons in rotated:
            print(f"would rotate {session_key} old_session={old_id} reasons={','.join(reasons)}")
        _log(f"dry-run: would rotate {len(rotated)} session(s)")
        return 0

    after_mtime = SESSIONS_FILE.stat().st_mtime_ns
    if after_mtime != before_mtime:
        _log("skip: sessions.json changed during scan; retry next interval")
        return 2

    _atomic_write_json(SESSIONS_FILE, new_data)
    for session_key, old_id, reasons in rotated:
        _log(
            "rotated "
            f"session_key={session_key} old_session={old_id} reasons={','.join(reasons)}"
        )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        return run(dry_run=args.dry_run)
    except Exception as exc:
        _log(f"error: {type(exc).__name__}: {exc}")
        print(f"session_guard error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
