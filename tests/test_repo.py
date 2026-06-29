from __future__ import annotations

import json
import re
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

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
    ".sql",
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


class Cursor:
    def __init__(self, row=None, rows=None, rowcount=0):
        self.row = row
        self.rows = rows if rows is not None else ([] if row is None else [row])
        self.rowcount = rowcount

    def fetchone(self):
        return self.row

    def fetchall(self):
        return self.rows


class MemoryConnection:
    def __init__(self, db):
        self.db = db

    def execute(self, sql, params=()):
        normalized = " ".join(sql.lower().split())
        if normalized.startswith("insert into shelley_points_users"):
            guild_id, user_id = int(params[0]), int(params[1])
            record = self.db.users.setdefault(
                (guild_id, user_id),
                {
                    "user_id": user_id,
                    "text_points": 0,
                    "voice_points": 0,
                    "last_text_award_at": 0.0,
                    "last_voice_award_at": 0.0,
                    "last_name": None,
                    "last_display_name": None,
                },
            )
            if len(params) >= 4:
                record["last_name"] = params[2] or record["last_name"]
                record["last_display_name"] = params[3] or record["last_display_name"]
            return Cursor(rowcount=1)
        if "select text_points, last_text_award_at" in normalized:
            return Cursor(self.db.users[(int(params[0]), int(params[1]))])
        if "select voice_points, last_voice_award_at" in normalized:
            return Cursor(self.db.users[(int(params[0]), int(params[1]))])
        if normalized.startswith("update shelley_points_users") and "returning text_points" in normalized:
            amount, name, display_name, guild_id, user_id = params
            record = self.db.users[(int(guild_id), int(user_id))]
            if "greatest" in normalized:
                record["text_points"] = max(0, record["text_points"] - int(amount))
            else:
                record["text_points"] += int(amount)
            record["last_name"] = name or record["last_name"]
            record["last_display_name"] = display_name or record["last_display_name"]
            return Cursor({"text_points": record["text_points"]}, rowcount=1)
        if normalized.startswith("update shelley_points_users") and "returning voice_points" in normalized:
            amount, name, display_name, guild_id, user_id = params
            record = self.db.users[(int(guild_id), int(user_id))]
            if "greatest" in normalized:
                record["voice_points"] = max(0, record["voice_points"] - int(amount))
            else:
                record["voice_points"] += int(amount)
            record["last_name"] = name or record["last_name"]
            record["last_display_name"] = display_name or record["last_display_name"]
            return Cursor({"voice_points": record["voice_points"]}, rowcount=1)
        if normalized.startswith("update shelley_points_users") and "set text_points = %s" in normalized:
            total, now, channel_id, message_id, name, display_name, guild_id, user_id = params
            record = self.db.users[(int(guild_id), int(user_id))]
            record.update(
                {
                    "text_points": int(total),
                    "last_text_award_at": float(now),
                    "last_text_channel_id": channel_id,
                    "last_text_message_id": message_id,
                    "last_name": name or record["last_name"],
                    "last_display_name": display_name or record["last_display_name"],
                }
            )
            return Cursor(rowcount=1)
        if normalized.startswith("update shelley_points_users") and "set voice_points = %s" in normalized:
            total, now, name, display_name, guild_id, user_id = params
            record = self.db.users[(int(guild_id), int(user_id))]
            record.update(
                {
                    "voice_points": int(total),
                    "last_voice_award_at": float(now),
                    "last_name": name or record["last_name"],
                    "last_display_name": display_name or record["last_display_name"],
                }
            )
            return Cursor(rowcount=1)
        if normalized.startswith("update shelley_points_users") and "where guild_id = %s" in normalized:
            guild_id = int(params[0])
            changed = 0
            for (record_guild_id, _user_id), record in self.db.users.items():
                if record_guild_id != guild_id:
                    continue
                if "text_points = 0" in normalized:
                    record["text_points"] = 0
                    record["last_text_award_at"] = 0.0
                if "voice_points = 0" in normalized:
                    record["voice_points"] = 0
                    record["last_voice_award_at"] = 0.0
                changed += 1
            return Cursor(rowcount=changed)
        if normalized.startswith("select user_id, text_points"):
            guild_id, limit = int(params[0]), int(params[1])
            field = "voice_points" if "voice_points > 0" in normalized else "text_points"
            rows = [record for (record_guild_id, _), record in self.db.users.items() if record_guild_id == guild_id and record[field] > 0]
            rows.sort(key=lambda item: (-item[field], item["user_id"]))
            return Cursor(rows=rows[:limit])
        if normalized.startswith("select count(*) as users"):
            guild_id = int(params[0])
            rows = [record for (record_guild_id, _), record in self.db.users.items() if record_guild_id == guild_id]
            return Cursor(
                {
                    "users": len(rows),
                    "text_points": sum(row["text_points"] for row in rows),
                    "voice_points": sum(row["voice_points"] for row in rows),
                }
            )
        if normalized.startswith("insert into shelley_text_channel_cursors"):
            guild_id, channel_id, message_id = int(params[0]), int(params[1]), int(params[2])
            self.db.cursors[(guild_id, channel_id)] = max(self.db.cursors.get((guild_id, channel_id), 0), message_id)
            return Cursor(rowcount=1)
        if normalized.startswith("select message_id"):
            guild_id, channel_id = int(params[0]), int(params[1])
            value = self.db.cursors.get((guild_id, channel_id))
            return Cursor({"message_id": value}) if value else Cursor()
        raise AssertionError(normalized)


class MemoryDb:
    def __init__(self):
        self.users = {}
        self.cursors = {}

    @contextmanager
    def connection(self):
        yield MemoryConnection(self)

    def fetchone(self, sql, params=()):
        with self.connection() as conn:
            return conn.execute(sql, params).fetchone()

    def fetchall(self, sql, params=()):
        with self.connection() as conn:
            return conn.execute(sql, params).fetchall()

    def execute(self, sql, params=()):
        with self.connection() as conn:
            return conn.execute(sql, params).rowcount

    def jsonb(self, value):
        return value


def test_config_validation_accepts_example_and_rejects_runtime_placeholders() -> None:
    from shelley.config import BotConfig, ConfigError

    cfg = BotConfig.model_validate(load_json("config.example.json"))
    assert cfg.database.resolved_url()
    assert cfg.database.pool_min_size == 1
    assert cfg.database.pool_max_size == 5
    assert cfg.points.text.interval_seconds == 120
    assert cfg.points.leaderboard.placeholder_text == ""
    assert cfg.remote_targets["bm"].key_path == "~/.ssh/shelley_bm"
    with pytest.raises(ConfigError):
        cfg.validate_runtime()


def test_config_validation_rejects_bad_ids_and_unknown_fields() -> None:
    from shelley.config import BotConfig, ConfigError, parse_config

    with pytest.raises(ValidationError):
        BotConfig.model_validate({"notify_channel_id": -1})
    with pytest.raises(ConfigError):
        parse_config({"unexpected": True}, "bad.json")


def test_permission_checks_use_discord_administrator_permission() -> None:
    from shelley.security import user_is_administrator

    allowed = SimpleNamespace(user=SimpleNamespace(guild_permissions=SimpleNamespace(administrator=True)))
    denied = SimpleNamespace(user=SimpleNamespace(guild_permissions=SimpleNamespace(administrator=False)))
    missing = SimpleNamespace(user=SimpleNamespace(guild_permissions=None))
    assert user_is_administrator(allowed)
    assert not user_is_administrator(denied)
    assert not user_is_administrator(missing)


def test_remote_command_builder_uses_safe_ssh_options() -> None:
    from shelley.config import BotConfig, RemoteTargetConfig
    from shelley.services.remote import build_ssh_command, parse_remote_result, remote_host_argument

    cfg = BotConfig.model_validate(load_json("config.example.json"))
    target = cfg.remote_targets["bm"]
    command = build_ssh_command(target, "/usr/local/bin/bm-safe-start")
    assert command[:2] == ["ssh", "-i"]
    assert "BatchMode=yes" in command
    assert "ConnectTimeout=10" in command
    assert "server-user@host-or-ip" in command
    assert command[-1] == "/usr/local/bin/bm-safe-start"

    profile_target = RemoteTargetConfig(host="ignored-host", user="ignored-user", ssh_profile="bm-profile")
    assert remote_host_argument(profile_target) == "bm-profile"

    result = parse_remote_result(7, b"ok\n", b"err\n")
    assert result.returncode == 7
    assert result.stdout == "ok"
    assert result.stderr == "err"


def test_database_unavailable_has_clear_error(monkeypatch) -> None:
    from shelley.db import Database, DatabaseUnavailable

    class BrokenPool:
        def __init__(self, *_args, **_kwargs):
            pass

        def open(self, *_args, **_kwargs):
            raise OSError("database is down")

    monkeypatch.setattr("shelley.db._driver", lambda: (object(), object(), BrokenPool))
    db = Database("postgresql:///missing", connect_timeout=1, pool_min_size=1, pool_max_size=1)
    with pytest.raises(DatabaseUnavailable, match="PostgreSQL is unavailable"):
        db.fetchone("SELECT 1")


def test_status_templates_match_configured_components() -> None:
    cfg = load_json("config.example.json")
    servers = {server["placeholder"]: server for server in cfg["servers"]}
    for message in cfg["status_messages"]:
        if message.get("type") == "separator":
            continue
        template = load_json(str(message["template_path"]))
        embeds = template.get("embeds", [])
        assert embeds
        if message.get("renderer") == "smp_cluster":
            server = servers[message["status_placeholder"]]
            assert len(embeds) == 2 + len(server.get("components", []))
            assert "[версия]" in str(embeds[1].get("description", ""))
        if message.get("renderer") == "bm":
            assert len(embeds) >= 2
            assert any("[версия]" in str(embed.get("description", "")) for embed in embeds)


def test_minecraft_status_version_mapping_handles_velocity_and_modloader_names() -> None:
    from shelley.services.minecraft import status_version_label

    assert status_version_label("Java Edition", "Velocity 1.7.2-26.1.2") == "Java Edition 26.1.2"
    assert status_version_label("NeoForge", "1.21.1") == "NeoForge 1.21.1"
    assert status_version_label("Bedrock", "1.21.93") == "Bedrock 1.21.93"


def test_points_store_award_cooldown_add_remove_reset_and_top() -> None:
    from shelley.cogs.points_state import PointsStore

    store = PointsStore(MemoryDb())
    award = store.award(
        10, 1, "text", amount=15, now=1000, cooldown=120, name="user", display_name="User", text_channel_id=50, text_message_id=60
    )
    assert award and award.total == 15
    assert store.award(10, 1, "text", amount=15, now=1050, cooldown=120) is None
    assert store.award(10, 1, "voice", amount=20, now=1000, cooldown=120, name="user", display_name="User").total == 20
    assert store.add_points(10, 1, "text", 5) == 20
    assert store.remove_points(10, 1, "text", 200) == 0
    store.add_points(10, 2, "voice", 30, "second", "Second")
    assert [row.user_id for row in store.top(10, "voice_points", 10)] == [2, 1]
    assert store.reset_points(10, "all") == 2
    assert store.counts(10) == {"users": 2, "text_points": 0, "voice_points": 0}


def test_text_points_award_uses_immediate_reward_then_cooldown(monkeypatch) -> None:
    from shelley.cogs.text_points import TextPointsService
    from shelley.config import BotConfig

    cfg_data = load_json("config.example.json")
    cfg_data["points"]["text"]["channel_ids"] = [123]
    config = BotConfig.model_validate(cfg_data)
    store = PointsStoreForText()
    service = TextPointsService(store, lambda: setattr(store, "dirty", True))
    author = FakeAuthor(1, "User")
    message = SimpleNamespace(
        guild=SimpleNamespace(id=10),
        author=author,
        id=100,
    )
    monkeypatch.setattr("shelley.cogs.text_points.message_counting_channel_id", lambda _message: 123)
    monkeypatch.setattr("shelley.cogs.text_points.random_points_amount", lambda _config: 10)
    assert async_await(service.award_for_message(message, config, source="test")) is True
    assert async_await(service.award_for_message(message, config, source="test")) is False
    assert store.dirty is True


class FakeAuthor:
    def __init__(self, user_id: int, display_name: str):
        self.id = user_id
        self.display_name = display_name
        self.bot = False

    def __str__(self):
        return self.display_name


class PointsStoreForText:
    def __init__(self):
        self.last_award_at = 0
        self.total = 0
        self.dirty = False

    def award(self, guild_id, user_id, kind, amount, now, cooldown, **_kwargs):
        if self.last_award_at and now - self.last_award_at < cooldown:
            return None
        self.last_award_at = now
        self.total += amount
        return SimpleNamespace(amount=amount, total=self.total)


def async_await(coro):
    import asyncio

    return asyncio.run(coro)


def test_voice_points_eligibility_blocks_bots_deaf_mute_and_alone() -> None:
    from shelley.cogs.voice_points import VoicePointsService, voice_member_can_listen, voice_member_is_points_eligible
    from shelley.config import BotConfig

    assert voice_member_is_points_eligible(
        SimpleNamespace(bot=False, voice=SimpleNamespace(deaf=False, self_deaf=False, mute=False, self_mute=False))
    )
    assert not voice_member_is_points_eligible(SimpleNamespace(bot=True, voice=SimpleNamespace(deaf=False)))
    assert not voice_member_is_points_eligible(SimpleNamespace(bot=False, voice=SimpleNamespace(deaf=True)))
    assert not voice_member_is_points_eligible(SimpleNamespace(bot=False, voice=SimpleNamespace(self_mute=True)))
    assert voice_member_can_listen(SimpleNamespace(bot=False, voice=SimpleNamespace(deaf=False, self_deaf=False, self_mute=True)))

    member1 = SimpleNamespace(id=1, bot=False, voice=SimpleNamespace(deaf=False, self_deaf=False, mute=False, self_mute=False))
    member2 = SimpleNamespace(id=2, bot=False, voice=SimpleNamespace(deaf=False, self_deaf=True, mute=False, self_mute=False))
    channel = SimpleNamespace(id=123, members=[member1, member2])
    bot = SimpleNamespace(get_channel=lambda channel_id: channel if channel_id == 123 else None)
    cfg_data = load_json("config.example.json")
    cfg_data["points"]["voice"]["channel_ids"] = [123]
    config = BotConfig.model_validate(cfg_data)
    service = VoicePointsService(bot, SimpleNamespace(), lambda: None)
    assert service.eligible_members(config) == {}


def test_voice_points_muted_listener_counts_but_does_not_receive_points() -> None:
    from shelley.cogs.voice_points import VoicePointsService
    from shelley.config import BotConfig

    speaker = SimpleNamespace(id=1, bot=False, voice=SimpleNamespace(deaf=False, self_deaf=False, mute=False, self_mute=False))
    muted_listener = SimpleNamespace(id=2, bot=False, voice=SimpleNamespace(deaf=False, self_deaf=False, mute=False, self_mute=True))
    channel = SimpleNamespace(id=123, members=[speaker, muted_listener])
    bot = SimpleNamespace(get_channel=lambda channel_id: channel if channel_id == 123 else None)
    cfg_data = load_json("config.example.json")
    cfg_data["points"]["voice"]["channel_ids"] = [123]
    config = BotConfig.model_validate(cfg_data)
    service = VoicePointsService(bot, SimpleNamespace(), lambda: None)
    assert service.eligible_members(config) == {1: speaker}


def test_leaderboard_renderer_uses_assets_and_generates_png() -> None:
    from shelley.cogs.leaderboard_renderer import compact_points_text, render_points_leaderboard_png

    png = render_points_leaderboard_png([(1, "rudator", 38468), (2, "wechirok", 16189)], icon_kind="text", accent_color=0x5865F2)
    empty_png = render_points_leaderboard_png([], icon_kind="text", accent_color=0x5865F2, placeholder_text="")
    wide_png = render_points_leaderboard_png([(1, "Wechirok", 9999999999999999)], icon_kind="voice", accent_color=0x5865F2)
    assert png.startswith(b"\x89PNG")
    assert empty_png.startswith(b"\x89PNG")
    assert wide_png.startswith(b"\x89PNG")
    assert len(png) > 5000
    assert compact_points_text(999999) == "999999"
    assert compact_points_text(1000000) == "1M"
    assert (ROOT / "assets/text-point.png").is_file()
    assert (ROOT / "assets/voice-points.png").is_file()


def test_leaderboard_uses_display_name_top_ten_and_has_no_button_logic() -> None:
    from shelley.cogs.leaderboard_sync import LEADERBOARD_LIMIT, LeaderboardSync
    from shelley.cogs.points_state import PointsRow

    row = PointsRow(
        user_id=1,
        text_points=10,
        voice_points=0,
        last_text_award_at=0,
        last_voice_award_at=0,
        last_name="stored_username",
        last_display_name="Stored Display",
    )
    member = SimpleNamespace(id=1, name="actual_username", display_name="Server Nick")
    guild = SimpleNamespace(get_member=lambda user_id: member if user_id == 1 else None)
    bot = SimpleNamespace(
        get_user=lambda user_id: SimpleNamespace(name="cached_username", global_name="Global Display") if user_id == 1 else None
    )
    sync = LeaderboardSync(bot, SimpleNamespace())
    assert async_await(sync.resolve_board_name(guild, row)) == "Server Nick"
    assert async_await(sync.resolve_board_name(None, row)) == "Global Display"
    assert not hasattr(sync, "build_fallback_embed")
    assert LEADERBOARD_LIMIT == 10

    rows = [
        PointsRow(
            user_id=user_id,
            text_points=1000 - user_id,
            voice_points=500 - user_id,
            last_text_award_at=0,
            last_voice_award_at=0,
            last_name=f"user{user_id}",
            last_display_name=f"Stored {user_id}",
        )
        for user_id in range(1, 26)
    ]
    store = SimpleNamespace(top=lambda _guild_id, _field, limit: rows[:limit])
    bot = SimpleNamespace(get_user=lambda _user_id: None)
    guild = SimpleNamespace(
        id=10,
        get_member=lambda user_id: SimpleNamespace(name=f"user{user_id}", display_name=f"Display {user_id}"),
    )
    sync = LeaderboardSync(bot, store)
    top_rows = async_await(sync.rows(guild, "text_points"))
    assert top_rows[0] == (1, "Display 1", 999)
    assert top_rows[-1] == (10, "Display 10", 990)
    assert len(top_rows) == 10

    source = (ROOT / "shelley/cogs/leaderboard_sync.py").read_text(encoding="utf-8")
    assert "ephemeral=True" not in source
    assert "discord.ui" not in source
    assert "Button" not in source
    assert "View" not in source
    assert "rows_page" not in source
    assert "page_key" not in source


def test_json_state_import_counts_points_and_recovery_without_ids(tmp_path) -> None:
    from shelley.scripts.import_json_state import import_points, import_recovery

    db = MemoryDb()
    points = {
        "text_channel_cursors": {"456": 789},
        "users": {
            "1": {"text_points": 10, "voice_points": 20, "last_name": "a"},
            "2": {"text_points": 5, "voice_points": 0, "last_name": "b"},
        },
    }
    summary = import_points(points, 10, db)
    assert summary == {"users": 2, "text_points": 15, "voice_points": 20, "text_cursors": 1}

    class RecoveryDb(MemoryDb):
        @contextmanager
        def connection(self):
            yield RecoveryConnection(self)

    class RecoveryConnection(MemoryConnection):
        def execute(self, sql, params=()):
            if "insert into shelley_recovery_controls" in " ".join(sql.lower().split()):
                return Cursor({"id": 1}, rowcount=1)
            return super().execute(sql, params)

    path = tmp_path / "recovery.jsonl"
    path.write_text(json.dumps({"created_at_unix": 1700000000, "button_id": "x", "user": {"id": 1}}), encoding="utf-8")
    assert import_recovery(path, 10, RecoveryDb()) == {"recovery_entries": 1}


def test_public_files_do_not_contain_private_data_or_old_logic() -> None:
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
        "dropbox archive": re.compile(r"dropbox\.com/.*\.(?:" + "|".join(archive_exts) + r")\b", re.IGNORECASE),
        "old command": re.compile(r"\b" + "say" + r"_channel_id\b|/" + "say" + r"\b|" + "allowed" + r"_user", re.IGNORECASE),
        "known password": re.compile("122" + "566|774" + "78049"),
    }
    matches: list[str] = []
    for relative, text in iter_repo_text_files():
        if relative.as_posix() == ".env.example":
            text = text.replace("DISCORD_TOKEN=", "")
        for label, pattern in forbidden_patterns.items():
            if pattern.search(text):
                matches.append(f"{relative.as_posix()}: {label}")
    assert matches == []


def test_runtime_private_files_are_not_committed() -> None:
    forbidden_paths: list[str] = []
    for path in ROOT.rglob("*"):
        relative = path.relative_to(ROOT)
        if any(part in SKIP_DIRS for part in relative.parts) or not path.is_file():
            continue
        relative_posix = relative.as_posix()
        if path.name.startswith(".env") and path.name != ".env.example":
            forbidden_paths.append(relative_posix)
        if path.name == "config.json":
            forbidden_paths.append(relative_posix)
        if path.name.startswith("config.") and path.suffix == ".json" and relative_posix != "config.example.json":
            forbidden_paths.append(relative_posix)
        if relative.parts[:1] == ("data",) and path.suffix in (".json", ".jsonl"):
            forbidden_paths.append(relative_posix)
    assert forbidden_paths == []
