import json
from pathlib import Path
from typing import Any

MAX_JSON_FILE_BYTES = 5 * 1024 * 1024


def read_json(path: Path, *, max_bytes: int = MAX_JSON_FILE_BYTES) -> Any:
    size = path.stat().st_size
    if size > max_bytes:
        raise ValueError(
            f"refusing to load {path.name}: size {size}B exceeds cap {max_bytes}B"
        )
    text = path.read_text(encoding="utf-8")
    return json.loads(text)


def write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

