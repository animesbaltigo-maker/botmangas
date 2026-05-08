from __future__ import annotations

import json
import os
import time
from pathlib import Path
from threading import RLock

try:
    from config import DATA_DIR
except Exception:
    DATA_DIR = Path("data")

BLOCKLIST_PATH = Path(os.getenv("CONTROL_BLOCKLIST_FILE", str(Path(DATA_DIR) / "control_blocklist.json")))
_LOCK = RLock()


def _load() -> dict[str, dict[str, object]]:
    if not BLOCKLIST_PATH.exists():
        return {}
    try:
        raw = json.loads(BLOCKLIST_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return raw if isinstance(raw, dict) else {}


def _save(data: dict[str, dict[str, object]]) -> None:
    BLOCKLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = BLOCKLIST_PATH.with_suffix(BLOCKLIST_PATH.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, BLOCKLIST_PATH)


def block_user(user_id: int, *, username: str = "", reason: str = "", actor_id: int | None = None) -> None:
    key = str(int(user_id))
    with _LOCK:
        data = _load()
        current = data.get(key, {})
        data[key] = {
            "user_id": int(user_id),
            "username": str(username or current.get("username") or ""),
            "reason": str(reason or current.get("reason") or ""),
            "actor_id": int(actor_id) if actor_id else current.get("actor_id"),
            "blocked_at": int(current.get("blocked_at") or time.time()),
            "updated_at": int(time.time()),
        }
        _save(data)


def unblock_user(user_id: int) -> bool:
    key = str(int(user_id))
    with _LOCK:
        data = _load()
        existed = key in data
        data.pop(key, None)
        if existed:
            _save(data)
        return existed


def is_blocked(user_id: int | None) -> bool:
    if not user_id:
        return False
    with _LOCK:
        return str(int(user_id)) in _load()


def get_blocked_users() -> list[int]:
    with _LOCK:
        data = _load()
    result: list[int] = []
    for key in data:
        try:
            result.append(int(key))
        except Exception:
            continue
    return sorted(result)
