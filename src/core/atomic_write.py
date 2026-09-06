from __future__ import annotations

import json
import time
import uuid
from pathlib import Path


def atomic_write_json(
    path: Path,
    value: object,
    *,
    ensure_ascii: bool = False,
    indent: int | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    committed = False
    try:
        temporary.write_text(
            json.dumps(value, ensure_ascii=ensure_ascii, indent=indent),
            encoding="utf-8",
        )
        for attempt in range(3):
            try:
                temporary.replace(path)
                committed = True
                return
            except PermissionError:
                if attempt == 2:
                    raise
                time.sleep(0.075)
    finally:
        if not committed:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
