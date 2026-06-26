import hashlib
import json
import os


def env_name() -> str:
    return (os.getenv("BOT_ENV") or "main").strip().lower()


def config_path() -> str:
    if os.getenv("BOT_CONFIG_PATH"):
        return str(os.getenv("BOT_CONFIG_PATH"))

    env = os.getenv("BOT_ENV")
    if env:
        return f"config.{env.strip().lower()}.json"

    return "config.json"


def load_json(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path: str, data: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def file_sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 64), b""):
            digest.update(chunk)
    return digest.hexdigest()
