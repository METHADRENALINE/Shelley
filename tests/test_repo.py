from __future__ import annotations

import json
import re
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
        if relative.parts[:1] == ("data",) and path.suffix == ".json":
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
