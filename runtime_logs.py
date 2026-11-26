"""Small bounded JSONL writer for plugin diagnostics."""

from __future__ import annotations

import json
from pathlib import Path
import threading


_LOCK = threading.Lock()


def append_json_line(path: str | Path, record: dict, *, max_bytes: int, backup_count: int) -> None:
    """Append one complete JSON line and retain a bounded set of rotated files."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
    encoded = line.encode("utf-8")
    if len(encoded) > max_bytes:
        raise ValueError("runtime log entry exceeds max_bytes")
    if not isinstance(backup_count, int) or backup_count < 0:
        raise ValueError("backup_count must be non-negative")
    with _LOCK:
        if target.exists() and target.stat().st_size + len(encoded) > max_bytes:
            for index in range(backup_count, 0, -1):
                source = target if index == 1 else target.with_name(f"{target.name}.{index - 1}")
                destination = target.with_name(f"{target.name}.{index}")
                if source.exists():
                    if destination.exists():
                        destination.unlink()
                    source.replace(destination)
        with target.open("a", encoding="utf-8") as output:
            output.write(line)
