import hashlib
import json
import os
from functools import lru_cache

from .config import BotConfig, ConfigError, parse_config


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
    with open(path, encoding="utf-8") as f:
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


def load_config(path: str | None = None, *, validate_runtime: bool = False) -> BotConfig:
    config_file = path or config_path()
    data = load_json(config_file)
    if not isinstance(data, dict):
        raise ConfigError(f"Invalid config file {config_file}: top-level JSON value must be an object")
    config = parse_config(data, config_file)
    if validate_runtime:
        config.validate_runtime()
    return config


@lru_cache(maxsize=1)
def get_config() -> BotConfig:
    return load_config(validate_runtime=True)


def reset_config_cache() -> None:
    get_config.cache_clear()
