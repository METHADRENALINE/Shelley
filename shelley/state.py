import os
import time
from typing import Optional

from .settings import load_json, save_json

def load_state(path: str) -> dict:
    if not os.path.exists(path):
        return {}

    try:
        data = load_json(path)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}

def get_status_message_id(state_path: str, key: str, use_legacy_fallback: bool = False) -> Optional[int]:
    state = load_state(state_path)
    ids = state.get("status_message_ids", {})
    if isinstance(ids, dict):
        try:
            message_id = int(ids.get(key, 0))
            if message_id:
                return message_id
        except Exception:
            pass

    if use_legacy_fallback:
        try:
            message_id = int(state.get("message_id", 0))
            if message_id:
                return message_id
        except Exception:
            pass

    return None

def set_status_message_id(state_path: str, message_id: int, key: Optional[str] = None) -> None:
    state = load_state(state_path)

    if key is None:
        state["message_id"] = int(message_id)
    else:
        ids = state.get("status_message_ids", {})
        if not isinstance(ids, dict):
            ids = {}
        ids[str(key)] = int(message_id)
        state["status_message_ids"] = ids

    save_json(state_path, state)

def migrate_status_message_ids_for_embed_layout(state_path: str) -> None:
    state = load_state(state_path)
    ids = state.get("status_message_ids", {})
    if not isinstance(ids, dict):
        return

    old_bm_id = ids.get("bm")
    old_bm_info_id = ids.get("bm_info")
    if not ids.get("smp_interval") and old_bm_id and old_bm_info_id:
        ids["smp_interval"] = int(old_bm_id)
        ids["bm"] = int(old_bm_info_id)
        ids.pop("bm_info", None)
        state["status_message_ids"] = ids
        save_json(state_path, state)

def get_welcome_message_id(state_path: str) -> Optional[int]:
    state = load_state(state_path)
    try:
        message_id = int(state.get("welcome_message_id", 0))
        if message_id:
            return message_id
    except Exception:
        pass

    return None

def set_welcome_message_id(state_path: str, message_id: int) -> None:
    state = load_state(state_path)
    state["welcome_message_id"] = int(message_id)
    save_json(state_path, state)

def get_welcome_message_file_hash(state_path: str) -> Optional[str]:
    state = load_state(state_path)
    value = state.get("welcome_message_file_hash")
    return str(value) if value else None

def set_welcome_message_file_hash(state_path: str, file_hash: str) -> None:
    state = load_state(state_path)
    state["welcome_message_file_hash"] = str(file_hash)
    save_json(state_path, state)

def mark_starting_status(state_path: str, placeholder: Optional[str], ttl_seconds: int) -> None:
    if not placeholder:
        return

    state = load_state(state_path)
    starting = state.get("starting_statuses", {})
    if not isinstance(starting, dict):
        starting = {}

    starting[str(placeholder)] = time.time() + max(1, int(ttl_seconds))
    state["starting_statuses"] = starting
    save_json(state_path, state)

def clear_starting_status(state_path: str, placeholder: str) -> None:
    state = load_state(state_path)
    starting = state.get("starting_statuses", {})
    if not isinstance(starting, dict) or placeholder not in starting:
        return

    starting.pop(placeholder, None)
    state["starting_statuses"] = starting
    save_json(state_path, state)

def load_active_starting_statuses(state_path: str) -> set[str]:
    state = load_state(state_path)
    raw = state.get("starting_statuses", {})
    if not isinstance(raw, dict):
        return set()

    now = time.time()
    active: dict[str, float] = {}
    changed = False

    for placeholder, expires_at in raw.items():
        try:
            expires = float(expires_at)
        except (TypeError, ValueError):
            changed = True
            continue

        if expires > now:
            active[str(placeholder)] = expires
        else:
            changed = True

    if changed:
        state["starting_statuses"] = active
        save_json(state_path, state)

    return set(active)
