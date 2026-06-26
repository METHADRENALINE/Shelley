from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "venv",
}
TEXT_SUFFIXES = {
    "",
    ".cfg",
    ".example",
    ".gitignore",
    ".json",
    ".md",
    ".py",
    ".toml",
    ".txt",
    ".yml",
    ".yaml",
}


def load_json(relative_path: str) -> dict:
    return json.loads((ROOT / relative_path).read_text(encoding="utf-8"))


def iter_repo_text_files():
    for path in ROOT.rglob("*"):
        relative = path.relative_to(ROOT)
        if any(part in SKIP_DIRS for part in relative.parts):
            continue
        if not path.is_file() or path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        yield relative, path.read_text(encoding="utf-8", errors="ignore")


def test_status_templates_match_configured_components() -> None:
    cfg = load_json("config.json")
    servers = {server["placeholder"]: server for server in cfg["servers"]}

    for message in cfg["status_messages"]:
        if message.get("type") == "separator":
            continue

        template = load_json(str(message["template_path"]))
        embeds = template.get("embeds", [])
        assert embeds, f"{message['template_path']} must define at least one embed"

        if message.get("renderer") == "smp_cluster":
            server = servers[message["status_placeholder"]]
            components = server.get("components", [])
            assert len(embeds) == 2 + len(components)
            assert "[\u0432\u0435\u0440\u0441\u0438\u044f]" in str(embeds[1].get("description", ""))

        if message.get("renderer") == "bm":
            assert len(embeds) >= 2
            assert any(
                "[\u0432\u0435\u0440\u0441\u0438\u044f]" in str(embed.get("description", ""))
                for embed in embeds
            )


def test_config_uses_safe_placeholders_for_servers() -> None:
    cfg = load_json("config.json")

    assert cfg.get("client_id") == 0
    assert cfg.get("token") == "replace-with-your-discord-bot-token"
    assert cfg.get("recovery_log_path") == "data/recovery-controls.jsonl"
    assert cfg.get("recovery_log_retention_days") == 365

    for server in cfg["servers"]:
        assert server.get("kind") == "minecraft"
        assert server.get("address") == "ip:port"
        for component in server.get("components", []):
            assert component.get("address") == "ip:port"

    for target in cfg["remote_targets"].values():
        assert target.get("host") == "host-or-ip"
        assert target.get("user") == "server-user"


def test_runtime_private_files_are_not_committed() -> None:
    forbidden_paths: list[str] = []

    for path in ROOT.rglob("*"):
        relative = path.relative_to(ROOT)
        if any(part in SKIP_DIRS for part in relative.parts) or not path.is_file():
            continue

        relative_posix = relative.as_posix()
        if path.name.startswith(".env"):
            forbidden_paths.append(relative_posix)
        if path.name.startswith("config.") and path.suffix == ".json" and relative_posix != "config.json":
            forbidden_paths.append(relative_posix)
        if relative.parts[:1] == ("data",) and path.suffix in (".json", ".jsonl"):
            forbidden_paths.append(relative_posix)

    assert forbidden_paths == []


def test_repo_has_no_sensitive_or_private_patterns() -> None:
    archive_exts = ("zip", "rar", "7z", "jar", "mr" + "pack")
    forbidden_patterns = {
        "private network ip": re.compile(
            r"\b(?:"
            r"10\.\d{1,3}\.\d{1,3}\.\d{1,3}|"
            r"172\.(?:1[6-9]|2\d|3[0-1])\.\d{1,3}\.\d{1,3}|"
            r"192\.168\.\d{1,3}\.\d{1,3}"
            r")\b"
        ),
        "private key block": re.compile("BEGIN " + r"(?:OPENSSH|RSA|DSA|EC) " + "PRIVATE " + "KEY"),
        "ssh key": re.compile(r"\bssh" + r"-rsa\b"),
        "dropbox archive": re.compile(
            r"dropbox\.com/.*\.(?:" + "|".join(archive_exts) + r")\b",
            re.IGNORECASE,
        ),
    }

    matches: list[str] = []
    for relative, text in iter_repo_text_files():
        for label, pattern in forbidden_patterns.items():
            if pattern.search(text):
                matches.append(f"{relative.as_posix()}: {label}")

    assert matches == []


def test_recovery_log_prunes_entries_older_than_retention(tmp_path) -> None:
    from shelley.services.recovery_log import append_recovery_control_log

    log_path = tmp_path / "recovery-controls.jsonl"
    old_entry = {
        "created_at": "2024-01-01T00:00:00+00:00",
        "created_at_unix": int(time.time()) - 366 * 24 * 60 * 60,
        "button_id": "old_button",
    }
    log_path.write_text(json.dumps(old_entry, ensure_ascii=False) + "\n", encoding="utf-8")

    asyncio.run(
        append_recovery_control_log(
            str(log_path),
            {
                "button_id": "bm_status_start",
                "button_label": "Старт",
                "target": "bm",
                "action": "Старт",
                "command_key": "start_command",
                "status": "ok",
                "returncode": 0,
                "error": None,
                "user": {
                    "id": 1,
                    "name": "tester",
                    "display_name": "Tester",
                },
            },
            retention_days=365,
        )
    )

    entries = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    assert [entry["button_id"] for entry in entries] == ["bm_status_start"]
    assert "created_at" in entries[0]
    assert entries[0]["user"]["id"] == 1
