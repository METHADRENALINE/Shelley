from __future__ import annotations

import asyncio
import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock

import discord
import pytest
from PIL import Image
from pydantic import ValidationError

from shelley.bot import configure_command_scopes
from shelley.cogs.points_state import PointsStore
from shelley.config import BotConfig, MeowConfig
from shelley.db import Database, apply_schema
from shelley.meow.admin_views import (
    AdminSession,
    AdminView,
    EraModal,
    RewardModal,
    SeasonModal,
)
from shelley.meow.art import _paint
from shelley.meow.cog import MeowCog
from shelley.meow.era_archive import archive_embed, render_archive
from shelley.meow.models import (
    MEDAL_COLOR,
    ArchivedMedal,
    Dashboard,
    Draft,
    EraBoard,
    EraSlot,
    MeowError,
    Reward,
    Season,
    SeasonDesign,
    Showcase,
    can_update_live,
    emoji_presentation,
    next_era_slot,
)
from shelley.meow.repository import SeasonRepository, StaleDraft
from shelley.meow.roles import SeasonRoles
from shelley.meow.service import MeowService
from shelley.meow.showcase import SeasonShowcase


def reward(name: str, points: int, emoji: str = "\U0001f69c") -> Reward:
    return Reward(
        name=name,
        emoji=emoji,
        description="\u041e\u043f\u0438\u0441\u0430\u043d\u0438\u0435",
        points=points,
    )


@pytest.fixture
def design():
    return SeasonDesign(
        name="\u041c\u0443\u0440\u043b\u044b\u043a\u0438",
        ranks=(
            reward(
                "\u041a\u043b\u0430\u043d \u0442\u0440\u0430\u043a\u0442\u043e\u0440\u0438\u0441\u0442\u043e\u0432",
                5,
            ),
        ),
        medal=reward("\u041c\u0435\u0434\u0430\u043b\u044c", 20, "\U0001f3c5"),
    )


def live_season(design, **changes):
    season = Season(
        id=1,
        guild_id=2,
        design=design,
        state="active",
        roles={reward.key: index for index, reward in enumerate(design.rewards, 10)},
        revision=1,
        draft_revision=1,
        role_revision=1,
        started_at=datetime.now(UTC),
        closed_at=None,
        winner_id=None,
        test_user_id=None,
        cleaned=False,
    )
    return replace(season, **changes)


def test_design_validates_thresholds_and_allows_only_an_emoji(design):
    with pytest.raises(ValidationError, match="medal must require more"):
        design.put_reward(reward("Too high", 25), medal=False)
    with pytest.raises(ValidationError, match="one standard emoji"):
        reward("Bad emoji", 10, "Example")
    with pytest.raises(ValidationError, match="thresholds must be different"):
        design.put_reward(reward("Duplicate", 5), medal=False)
    assert reward("Custom", 10, "<:example:100000000000000001>").emoji.startswith("<:")
    assert design.rank_at(4) is None
    assert design.rank_at(5) == design.ranks[0]
    assert BotConfig().meow.enabled is False


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("\u2708", "\u2708\ufe0f"),
        ("\u2694\ufe0e", "\u2694\ufe0f"),
        ("\u2764\ufe0f", "\u2764\ufe0f"),
        ("\U0001f3cd", "\U0001f3cd\ufe0f"),
        ("\U0001f69c", "\U0001f69c"),
        ("1\u20e3", "1\ufe0f\u20e3"),
        ("\U0001f1fa\U0001f1e6", "\U0001f1fa\U0001f1e6"),
        ("\U0001f3f3\u200d\U0001f308", "\U0001f3f3\ufe0f\u200d\U0001f308"),
    ],
)
def test_role_emoji_uses_emoji_presentation(value, expected):
    assert emoji_presentation(value) == expected
    assert emoji_presentation(expected) == expected


def test_server_emoji_uses_a_standard_symbol_for_the_role():
    card = reward("Custom", 10, "<:example:100000000000000001>")
    assert card.role_symbol(medal=False) == "\U0001f431"
    assert card.role_symbol(medal=True) == "\U0001f3c5"
    override = Reward.model_validate({**card.model_dump(), "role_emoji": "\u2708"})
    assert override.role_symbol(medal=False) == "\u2708\ufe0f"
    with pytest.raises(ValidationError, match="one standard emoji"):
        Reward.model_validate({**card.model_dump(), "role_emoji": card.emoji})


def test_live_edits_keep_existing_reward_roles(design):
    active = live_season(design)
    cosmetic = design.updated(name="New name", accent=0x123456)
    assert can_update_live(Draft(cosmetic, 4, active.id), active)
    assert not can_update_live(Draft(cosmetic, 4, None), active)
    assert can_update_live(Draft(design.updated(source="voice"), 4, active.id), active)


def test_command_is_guild_only_admin_only_and_not_copied_to_other_guilds():
    bot = discord.Client(intents=discord.Intents.default())
    tree = discord.app_commands.CommandTree(bot)
    tree.add_command(MeowCog.meow)
    primary = discord.Object(id=10)
    extra = discord.Object(id=20)
    configure_command_scopes(tree, primary, (extra,))
    assert tree.get_command("meow") is None
    assert tree.get_command("meow", guild=extra) is None
    cmd = tree.get_command("meow", guild=primary)
    assert cmd.guild_only
    assert cmd.default_permissions.administrator


def test_editor_controls_stay_private_and_use_flat_workflow(design):
    async def run():
        interaction = SimpleNamespace(
            guild_id=2,
            user=SimpleNamespace(id=3, guild_permissions=discord.Permissions(administrator=True)),
            response=SimpleNamespace(is_done=lambda: False, send_message=AsyncMock()),
            is_expired=lambda: False,
        )
        service = SimpleNamespace(
            guild_id=2,
            config=BotConfig(meow=MeowConfig(enabled=True, channel_id=6, test_mode=True)),
            problems={},
        )
        session = AdminSession(service, interaction)
        draft = Draft(design, 1, None)
        view = AdminView(session, Dashboard(draft, None, None, None), None)
        components = view.to_components()[0]["components"]
        buttons = {child["label"]: child for row in components if row["type"] == 1 for child in row["components"] if child["type"] == 2}
        assert {
            "Settings",
            "Add rank",
            "Preview",
            "Post showcase",
            "Start season",
            "End season",
        } <= set(buttons)
        assert buttons["End season"]["disabled"]
        active = AdminView(session, Dashboard(draft, live_season(design), None, None), None)
        active_controls = [
            button
            for row in active.to_components()[0]["components"]
            if row["type"] == 1
            for button in row["components"]
            if button.get("label") == "End season"
        ]
        assert len(active_controls) == 1 and not active_controls[0]["disabled"]
        assert "Test mode" not in str(components)
        assert all(not item.get("content", "").startswith("01") for item in components)
        assert await view.interaction_check(interaction)
        interaction.user.guild_permissions = discord.Permissions.none()
        assert not await view.interaction_check(interaction)
        assert interaction.response.send_message.await_args.kwargs["ephemeral"]
        interaction.user.id = 99
        assert not await session.check(interaction)
        settings = SeasonModal(session, draft).to_components()
        assert [item["label"] for item in settings] == [
            "Season name",
            "Season emoji",
            "Introduction",
            "Point source",
            "Accent color",
        ]
        reward_fields = RewardModal(session, draft).to_components()
        assert [item["label"] for item in reward_fields][:4] == [
            "Name",
            "Emoji",
            "Points required",
            "Description",
        ]
        medal_fields = RewardModal(session, draft, design.medal, medal=True).to_components()
        assert [item["label"] for item in medal_fields] == [
            "Name",
            "Emoji",
            "Points required",
            "Description",
        ]
        active.stop()
        view.stop()

    asyncio.run(run())


def test_cards_render_and_showcase_fits_discord_component_limits(design):
    async def run():
        png = _paint(design.ranks[0], "text", design.accent, 1, None)
        voice = _paint(design.ranks[0], "voice", design.accent, 1, None)
        image = Image.open(BytesIO(png))
        assert image.size == (1280, 296)
        assert len(image.crop((60, 60, 236, 236)).getcolors(20000) or []) > 10
        assert png != voice
        ranks = tuple(reward(f"Rank {index}", index) for index in range(1, 10))
        full = design.updated(ranks=ranks)
        preview = SeasonShowcase(Mock(), Mock(), MeowConfig(channel_id=3))
        rendered = await preview.render(full, live_season(full, test_user_id=5))
        try:
            components = rendered.view.to_components()
            assert len(components) == 11
            assert rendered.view.total_children_count == 32
            assert len(rendered.files) == 10
            assert "\u0422\u0435\u0441\u0442" not in components[0]["components"][0]["content"]
            assert rendered.files[0].filename == "meow-1.png"
            assert components[-1]["accent_color"] == MEDAL_COLOR
            for card, file in zip(components[1:], rendered.files, strict=True):
                assert card["components"][0]["items"][0]["media"]["url"] == f"attachment://{file.filename}"
                assert card["components"][1]["type"] == 10
        finally:
            rendered.close()

    asyncio.run(run())


def test_showcase_header_uses_the_current_guild_emoji_names(design):
    async def run():
        emojis = {
            100000000000000001: discord.PartialEmoji(name="renamed_badge", id=100000000000000001),
            100000000000000002: discord.PartialEmoji(name="voice", id=100000000000000002),
        }
        guild = Mock()
        resolved = {}
        for emoji_id, emoji in emojis.items():
            icon = MagicMock(available=True)
            icon.__str__.return_value = str(emoji)
            resolved[emoji_id] = icon
        guild.get_emoji.side_effect = resolved.get
        show = SeasonShowcase(Mock(), Mock(), MeowConfig(channel_id=3, voice_points_emoji_id=100000000000000002))
        update = design.updated(emoji="<:old:100000000000000001>", source="voice")
        with pytest.raises(ValidationError, match="server emoji"):
            update.updated(emoji="\U0001f3c5")
        rendered = await show.render(update, live_season(update, state="finished"), guild=guild)
        try:
            header = rendered.view.to_components()[0]["components"][0]["content"]
            assert header.startswith("## <:renamed_badge:100000000000000001> ")
            assert "\u00b7  <:voice:100000000000000002> \u0413\u043e\u043b\u043e\u0441\u043e\u0432\u044b\u0435" in header
        finally:
            rendered.close()

    asyncio.run(run())


def test_archive_is_one_white_image_with_six_rows():
    rows = [ArchivedMedal(i, 7, "nickname", 100 + i, "\U0001f3c5", None) for i in range(1, 7)]
    image = Image.open(BytesIO(render_archive(rows, {})))
    assert image.size == (768, 648)
    assert image.getpixel((34, 139)) != image.getpixel((12, 139))
    assert image.getpixel((34, 139)) == image.getpixel((34, 579))
    embed = archive_embed()
    assert embed.color.value == 0xFFFFFF and embed.description is None and embed.title is None
    assert embed.image.url == "attachment://meow-era-i.png"


def test_automatic_era_advances_only_after_all_six_results():
    first_era = {EraSlot(era=1, stage=stage) for stage in range(1, 7)}
    assert next_era_slot(set()) == EraSlot(era=1, stage=1)
    assert next_era_slot(first_era) == EraSlot(era=2, stage=1)
    second_era = {EraSlot(era=2, stage=stage) for stage in range(1, 6)}
    assert next_era_slot(first_era | second_era).label == "II Era \u00b7 Final act"
    assert next_era_slot(first_era | second_era | {EraSlot(era=2, stage=6)}).era == 3


def test_partial_era_has_its_own_image_and_custom_medal_fallback():
    rows = [ArchivedMedal(4, 7, "winner", 104, "<:medal:123456789012345678>", None)]
    image = Image.open(BytesIO(render_archive(rows, {}, era=2)))
    assert image.size == (768, 208)
    assert archive_embed(2).image.url == "attachment://meow-era-2.png"


def test_era_modal_shows_automatic_and_all_stages(design):
    async def run():
        slot = EraSlot(era=2, stage=1)
        dashboard = Dashboard(Draft(design, 1, None), None, None, None, slot)
        modal = EraModal(Mock(), dashboard)
        components = modal.to_components()
        assert components[0]["description"] == f"Automatic: {slot.label}"
        options = components[1]["component"]["options"]
        assert [option["value"] for option in options] == ["auto", "1", "2", "3", "4", "5", "6"]
        assert options[0]["default"]
        assert options[-1]["label"] == "Final act"

    asyncio.run(run())


def test_era_rewards_restore_only_verified_role_ids(database, design):
    guild_id, winner_id = 79, 18
    repo, _season = start(database, design.updated(era_slot=EraSlot(era=2, stage=1)), guild_id)
    database.execute(
        """
        INSERT INTO shelley_meow_era_medals
            (guild_id, stage, winner_id, username, role_id, medal_emoji, season_emoji)
        VALUES (%s, 1, %s, 'nickname', 99, %s, %s)
        """,
        (guild_id, winner_id, "\U0001f3c5", "<:badge:100000000000000001>"),
    )
    assert repo.era_medals(guild_id)[0].username == "nickname"
    assert repo.earned_medal_roles(guild_id, winner_id) == {winner_id: {99}}
    assert repo.earned_medal_roles(guild_id + 1, winner_id) == {}
    before = datetime.now(UTC) - timedelta(days=1)
    assert repo.earned_medal_roles(guild_id, winner_id, joined_at=before) == {}
    repo.save_era_archive(guild_id, 22, 200)
    repo.save_showcase(guild_id, 22, 201, 1, 4, retired_message_id=199)
    repo.invalidate_showcase(guild_id, 200)
    assert repo.era_archive(guild_id).message_id is None
    assert repo.showcase(guild_id).revision == 4
    assert repo.showcase(guild_id).retired_message_id == 199
    repo.clear_retired_showcase(guild_id, 199)
    assert repo.showcase(guild_id).retired_message_id is None


def test_showcase_moves_below_archive_before_removing_the_original(design):
    async def run():
        guild = SimpleNamespace(id=2)
        bot = SimpleNamespace(user=SimpleNamespace(id=9))
        repo = Mock()
        repo.showcase.return_value = Showcase(3, 100, 1, 1)
        old = SimpleNamespace(id=100, author=bot.user, delete=AsyncMock())
        new = SimpleNamespace(id=102)
        channel = SimpleNamespace(
            id=3,
            guild=guild,
            fetch_message=AsyncMock(return_value=old),
            send=AsyncMock(return_value=new),
        )
        new.channel = channel
        show = SeasonShowcase(bot, repo, MeowConfig(channel_id=3))
        show.channel = AsyncMock(return_value=channel)
        show._sync_era_boards = AsyncMock(return_value=101)
        repo.save_showcase.side_effect = [RuntimeError("Unavailable"), None]
        season = live_season(design)
        with pytest.raises(RuntimeError, match="Unavailable"):
            await show.sync(guild, season)
        old.delete.assert_not_awaited()
        await show.sync(guild, season)
        channel.send.assert_awaited_once()
        repo.save_showcase.assert_called_with(2, 3, 102, 1, 1, retired_message_id=100)
        old.delete.assert_awaited_once()
        repo.clear_retired_showcase.assert_called_once_with(2, 100)

    asyncio.run(run())


def test_era_board_retries_failed_save_and_edits_one_message():
    async def run():
        guild = SimpleNamespace(id=2)
        bot = SimpleNamespace(user=SimpleNamespace(id=9))
        repo = Mock()
        boards = {}
        repo.era_boards.side_effect = lambda _guild: boards.copy()
        repo.era_results.return_value = {2: [ArchivedMedal(1, 7, "winner", 101, "\U0001f3c5", None)]}
        message = SimpleNamespace(id=50, author=bot.user, edit=AsyncMock())
        channel = SimpleNamespace(
            id=3,
            guild=guild,
            send=AsyncMock(return_value=message),
            fetch_message=AsyncMock(return_value=message),
        )
        message.channel = channel
        message.edit.return_value = message
        show = SeasonShowcase(bot, repo, MeowConfig(channel_id=3))
        show.channel = AsyncMock(return_value=channel)
        repo.save_era_board.side_effect = RuntimeError("Unavailable")
        with pytest.raises(RuntimeError, match="Unavailable"):
            await show.sync(guild, None)

        def save_board(_guild, era, channel_id, message_id, signature):
            boards[era] = EraBoard(channel_id, message_id, signature)

        repo.save_era_board.side_effect = save_board
        await show.sync(guild, None)
        await show.sync(guild, None)
        channel.send.assert_awaited_once()
        message.edit.assert_not_awaited()
        repo.era_results.return_value[2].append(ArchivedMedal(2, 8, "other", 102, "\U0001f3c5", None))
        await show.sync(guild, None)
        assert boards[2].message_id == 50
        message.edit.assert_awaited_once()
        channel.send.assert_awaited_once()

    asyncio.run(run())


def _role(role_id):
    return SimpleNamespace(
        id=role_id,
        name=f"Role {role_id}",
        permissions=discord.Permissions.none(),
        is_assignable=lambda: True,
        delete=AsyncMock(),
    )


def test_discord_role_name_contains_only_the_emoji(design):
    async def run():
        repo = Mock()
        item = reward("Display name only", 5, "\u2708")
        selected = design.updated(ranks=(item,))
        season = live_season(selected, roles={}, test_user_id=20)
        role = _role(50)
        role.color = discord.Color.default()
        role.name = "\u2708\ufe0f"
        role.edit = AsyncMock(return_value=role)
        guild = SimpleNamespace(create_role=AsyncMock(return_value=role), me=None)
        await SeasonRoles(repo)._ensure_role(guild, season, item, {}, medal=False)
        repo.store_role.assert_called_once_with(season.id, item.key, role.id)
        assert guild.create_role.await_args.kwargs["name"] == "\u2708\ufe0f"
        assert guild.create_role.await_args.kwargs["permissions"].value == 0
        role.edit.assert_not_awaited()

    asyncio.run(run())


def test_medal_ignores_old_color_on_role_and_card(design):
    async def run():
        medal = Reward.model_validate({**design.medal.model_dump(), "color": 0xD62C46})
        season = live_season(design.updated(medal=medal))
        role = _role(season.roles[medal.key])
        role.name = medal.role_symbol(medal=True)
        role.color = discord.Color(medal.color)
        role.edit = AsyncMock(return_value=role)
        guild = SimpleNamespace(create_role=AsyncMock(), me=None)
        await SeasonRoles(Mock())._ensure_role(guild, season, medal, {role.id: role}, medal=True)
        assert role.edit.await_args.kwargs["color"] == 0xFF6565
        guild.create_role.assert_not_awaited()
        card = Image.open(BytesIO(_paint(medal, "text", design.accent, None, None)))
        assert card.getpixel((1, 1)) == (255, 101, 101, 255)

    asyncio.run(run())


def test_recovery_uses_bot_audit_entry_and_not_a_shared_emoji(design):
    async def run():
        roles = SeasonRoles(Mock())
        rank, medal = design.rewards
        season = live_season(design, roles={})
        expected, unrelated = (_role(item) for item in (50, 51))
        expected.name = unrelated.name = rank.role_symbol(medal=False)

        async def audit_logs(**kwargs):
            assert kwargs["action"] == discord.AuditLogAction.role_create
            assert kwargs["user"] is guild.me
            yield SimpleNamespace(reason="Manual role", target=unrelated)
            yield SimpleNamespace(reason=roles.creation_reason(season, rank), target=expected)
            yield SimpleNamespace(reason=roles.creation_reason(season, medal), target=None)

        guild = SimpleNamespace(
            me=SimpleNamespace(guild_permissions=discord.Permissions(view_audit_log=True)),
            audit_logs=audit_logs,
        )
        assert await roles._recover_roles(guild, season, season.design.rewards, {50: expected, 51: unrelated}) == {rank.key: expected}

    asyncio.run(run())


@pytest.mark.parametrize("state", ["finished", "cancelled"])
def test_cleanup_deletes_season_roles_and_preserves_earned_medals(design, state):
    async def run():
        repo = Mock()
        season = live_season(design, state=state, winner_id=30 if state == "finished" else None)
        rank, medal, unrelated = (_role(item) for item in (10, 11, 500))
        guild = SimpleNamespace(
            me=SimpleNamespace(guild_permissions=discord.Permissions(manage_roles=True)),
            fetch_roles=AsyncMock(return_value=[rank, medal, unrelated]),
        )
        await SeasonRoles(repo).cleanup(guild, season)
        rank.delete.assert_awaited_once()
        assert medal.delete.await_count == int(state == "cancelled")
        unrelated.delete.assert_not_awaited()
        repo.mark_cleaned.assert_called_once_with(season.id)

    asyncio.run(run())


def test_rank_upgrade_only_touches_roles_owned_by_this_season(design):
    async def run():
        design_with_two = design.put_reward(reward("Next", 10), medal=False)
        season = live_season(design_with_two)
        old_role, next_role, medal, unrelated = (_role(item) for item in (10, 11, 12, 99))
        repo = Mock()
        repo.pending_ranks.return_value = [SimpleNamespace(user_id=8, reward_key=design_with_two.ordered_ranks[1].key)]
        member = SimpleNamespace(
            bot=False,
            pending=False,
            roles=[old_role, unrelated],
            add_roles=AsyncMock(),
            remove_roles=AsyncMock(),
        )
        guild = SimpleNamespace(
            id=2,
            me=SimpleNamespace(guild_permissions=discord.Permissions(manage_roles=True)),
            roles=[old_role, next_role, medal, unrelated],
            fetch_member=AsyncMock(return_value=member),
        )
        await SeasonRoles(repo).apply_pending(guild, season)
        assert member.add_roles.await_args.args == (next_role,)
        assert member.remove_roles.await_args.args == (old_role,)
        repo.mark_rank_applied.assert_called_once_with(1, 8, 11)

    asyncio.run(run())


def test_demotion_removes_only_the_season_role(design):
    async def run():
        season = live_season(design)
        rank, medal, unrelated = (_role(item) for item in (10, 11, 99))
        repo = Mock()
        repo.pending_ranks.return_value = [SimpleNamespace(user_id=8, reward_key=None)]
        member = SimpleNamespace(
            bot=False,
            pending=False,
            roles=[rank, unrelated],
            add_roles=AsyncMock(),
            remove_roles=AsyncMock(),
        )
        guild = SimpleNamespace(
            id=2,
            me=SimpleNamespace(guild_permissions=discord.Permissions(manage_roles=True)),
            roles=[rank, medal, unrelated],
            fetch_member=AsyncMock(return_value=member),
        )
        await SeasonRoles(repo).apply_pending(guild, season)
        member.add_roles.assert_not_awaited()
        assert member.remove_roles.await_args.args == (rank,)
        repo.mark_rank_applied.assert_called_once_with(season.id, 8, None)

    asyncio.run(run())


def test_medal_selection_skips_blocked_bots_and_former_members(design):
    async def run():
        season = live_season(design)
        repo = Mock()
        repo.medal_candidates.return_value = [9, 10, 11, 12]
        blocked = _role(90)
        absent = discord.NotFound(SimpleNamespace(status=404, reason="Not found"), "Missing")
        members = {
            9: SimpleNamespace(bot=True),
            11: SimpleNamespace(bot=False, pending=False, roles=[blocked]),
            12: SimpleNamespace(bot=False, pending=False, roles=[]),
        }

        async def fetch_member(user_id):
            if user_id == 10:
                raise absent
            return members[user_id]

        guild = SimpleNamespace(
            id=2,
            me=SimpleNamespace(guild_permissions=discord.Permissions(manage_roles=True)),
            roles=[blocked],
            fetch_member=AsyncMock(side_effect=fetch_member),
        )
        service = MeowService(Mock(), BotConfig(meow=MeowConfig(blocked_role_id=90)), repo)
        await service._select_medal(guild, season)
        assert [call.args[0] for call in guild.fetch_member.await_args_list] == [9, 10, 11, 12]
        repo.claim_medal.assert_called_once_with(season, 12)
        assert set(service._ineligible_medal) == {9, 10, 11}

    asyncio.run(run())


def test_blocked_member_loses_only_the_active_rank(design):
    async def run():
        season = live_season(design)
        rank, medal, blocked, unrelated = (_role(item) for item in (10, 11, 90, 99))
        repo = Mock()
        repo.pending_ranks.return_value = [SimpleNamespace(user_id=8, reward_key=design.ranks[0].key)]
        member = SimpleNamespace(
            id=8,
            bot=False,
            pending=False,
            roles=[rank, medal, blocked, unrelated],
            add_roles=AsyncMock(),
            remove_roles=AsyncMock(),
        )
        guild = SimpleNamespace(
            id=2,
            me=SimpleNamespace(guild_permissions=discord.Permissions(manage_roles=True)),
            roles=[rank, medal, blocked, unrelated],
            fetch_member=AsyncMock(return_value=member),
        )
        await SeasonRoles(repo, MeowConfig(blocked_role_id=90)).apply_pending(guild, season)
        member.add_roles.assert_not_awaited()
        assert member.remove_roles.await_args.args == (rank,)
        repo.mark_rank_applied.assert_called_once_with(season.id, 8, None)

    asyncio.run(run())


def test_medal_award_rechecks_block_role_after_role_lookup(design):
    async def run():
        season = live_season(design, state="awarding", winner_id=8)
        rank, medal, blocker = (_role(item) for item in (10, 11, 90))
        before = SimpleNamespace(bot=False, pending=False, roles=[], add_roles=AsyncMock())
        after = SimpleNamespace(bot=False, pending=False, roles=[blocker], add_roles=AsyncMock())
        guild = SimpleNamespace(
            id=2,
            me=SimpleNamespace(guild_permissions=discord.Permissions(manage_roles=True)),
            fetch_roles=AsyncMock(return_value=[rank, medal, blocker]),
            fetch_member=AsyncMock(side_effect=[before, after]),
        )
        repo = Mock()
        roles = SeasonRoles(repo, MeowConfig(blocked_role_id=90))
        roles._ensure_role = AsyncMock(return_value=medal)
        await roles.award_medal(guild, season)
        before.add_roles.assert_not_awaited()
        after.add_roles.assert_not_awaited()
        repo.finish.assert_not_called()
        repo.reopen_medal_award.assert_called_once_with(season)

    asyncio.run(run())


def test_medal_restore_cannot_grant_admin_channel_access_or_protected_roles():
    async def run():
        medal, admin, channel_access, blocked, managed = (_role(i) for i in (21, 22, 23, 24, 25))
        admin.permissions = discord.Permissions(administrator=True)
        managed.is_assignable = lambda: False
        channel = SimpleNamespace(
            overwrites_for=lambda role: (
                discord.PermissionOverwrite(view_channel=True) if role.id == channel_access.id else discord.PermissionOverwrite()
            )
        )
        guild = SimpleNamespace(
            id=2,
            me=SimpleNamespace(guild_permissions=discord.Permissions(manage_roles=True)),
            channels=[channel],
            fetch_roles=AsyncMock(return_value=[medal, admin, channel_access, blocked, managed]),
        )
        for role in (medal, admin, channel_access, blocked, managed):
            role.guild = guild
        member = SimpleNamespace(guild=guild, bot=False, pending=False, roles=[], add_roles=AsyncMock())
        await SeasonRoles(Mock(), MeowConfig(blocked_role_id=24)).restore_medals(member, [21, 22, 23, 24, 25, 100])
        member.add_roles.assert_awaited_once()
        assert member.add_roles.await_args.args == (medal,)

    asyncio.run(run())


def test_rejoined_member_is_rechecked_by_join_date_and_stored_rewards(design):
    async def run():
        joined_at = datetime.now(UTC)
        other_guild = SimpleNamespace(id=3)
        repo = Mock()
        repo.earned_medal_roles.return_value = {8: {11}}
        member = SimpleNamespace(
            id=8,
            guild=SimpleNamespace(id=2),
            bot=False,
            pending=False,
            joined_at=joined_at,
            roles=[],
        )
        guild = SimpleNamespace(id=2, fetch_member=AsyncMock(return_value=member))
        service = MeowService(Mock(), BotConfig(dev_guild_id=2), repo)
        service._restore_on_ready = False
        service.roles.restore_medals = AsyncMock()
        cog = SimpleNamespace(service=service)
        await MeowCog.on_member_join(cog, replace_member(member, guild=other_guild))
        assert not service._members_to_refresh
        await MeowCog.on_member_join(cog, member)
        await service._refresh_members(guild, live_season(design))
        repo.refresh_member_rank.assert_called_once_with(2, 8, rejoined=True)
        repo.earned_medal_roles.assert_called_once_with(2, 8, joined_at=joined_at)
        service.roles.restore_medals.assert_awaited_once_with(member, {11})

    asyncio.run(run())


def replace_member(member, **changes):
    return SimpleNamespace(**{**vars(member), **changes})


@pytest.mark.parametrize("tie_on_first_edit", [False, True])
def test_rank_is_inserted_between_adjacent_roles_without_moving_the_anchors(design, tie_on_first_edit):
    async def run():
        guild = SimpleNamespace(id=2, channels=[])

        def role(role_id, position):
            return discord.Role(
                guild=guild,
                state=Mock(),
                data={
                    "id": str(role_id),
                    "name": str(role_id),
                    "position": position,
                    "permissions": "0",
                },
            )

        rank, lower, upper, top = (role(role_id, pos) for role_id, pos in ((100, 1), (14, 14), (15, 15), (19, 19)))
        guild.me = SimpleNamespace(id=8, top_role=top, guild_permissions=discord.Permissions(manage_roles=True))
        guild.owner_id = 100
        season = live_season(design, roles={design.ranks[0].key: rank.id})
        moved = [role(rank.id, 14), role(14, 13), upper, top]
        results = [[role(rank.id, 14), lower, upper, top], moved] if tie_on_first_edit else [moved]
        guild.edit_role_positions = AsyncMock(side_effect=results)
        manager = SeasonRoles(Mock(), MeowConfig(blocked_role_id=15, ranks_below_role_id=15, ranks_above_role_id=14))
        await manager.place_ranks(guild, season, {role.id: role for role in (rank, lower, upper, top)})
        assert guild.edit_role_positions.await_args_list[0].args == ({rank: lower.position},)
        if tie_on_first_edit:
            assert guild.edit_role_positions.await_args.args == ({rank: upper.position},)
        await manager.place_ranks(guild, season, {role.id: role for role in moved})
        assert guild.edit_role_positions.await_count == len(results)

    asyncio.run(run())


def test_startup_checks_missed_joins_without_overwriting_unrelated_roles(design):
    async def run():
        repo = Mock()
        repo.earned_medal_roles.return_value = {8: {11}, 10: {21}}
        repo.assigned_rank_ids.return_value = {9: 10, 10: 10, 11: 10}
        joined_at = datetime.now(UTC)
        other_role, rank, medal = (_role(i) for i in (42, 10, 21))
        members = {
            8: SimpleNamespace(joined_at=joined_at, roles=[other_role]),
            9: SimpleNamespace(joined_at=joined_at, roles=[other_role]),
            10: SimpleNamespace(joined_at=joined_at, roles=[rank, medal]),
        }
        guild = SimpleNamespace(id=2, chunked=True, get_member=members.get)
        service = MeowService(Mock(), BotConfig(), repo)
        await service._queue_missed_joins(guild, live_season(design))
        assert service._members_to_refresh == {8: joined_at, 9: joined_at}

    asyncio.run(run())


@pytest.fixture
def database():
    dsn = os.getenv("SHELLEY_MEOW_TEST_DSN")
    if not dsn:
        pytest.skip("SHELLEY_MEOW_TEST_DSN is not set")
    db = Database(dsn)
    with db.connection() as conn:
        if not conn.info.dbname.startswith("shelley_meow_") or not conn.info.dbname.endswith("_test"):
            raise RuntimeError("Meow integration tests require a dedicated shelley_meow database.")
    apply_schema(db)
    yield db
    db.close()


def start(database, design, guild_id, *, test_user_id=None):
    repo = SeasonRepository(database)
    empty = repo.dashboard(guild_id)
    repo.save_draft(guild_id, empty.draft.revision, design, None)
    draft = repo.dashboard(guild_id).draft
    season = repo.publish_draft(guild_id, draft.revision, None, update=False, test_user_id=test_user_id)
    for index, item in enumerate(design.rewards, 100):
        repo.store_role(season.id, item.key, index)
    assert repo.activate(guild_id, season.id)
    return repo, repo.dashboard(guild_id).live


def test_existing_balances_claim_one_medal_without_spending_points(database, design):
    guild_id = 70
    store = PointsStore(database)
    store.add_points(guild_id, 20, "text", 500)
    store.add_points(guild_id, 21, "text", 400)
    store.add_points(guild_id, 22, "voice", 700)
    repo, season = start(database, design, guild_id)
    assert repo.medal_candidates(season, []) == [20, 21]
    assert repo.medal_candidates(season, [20]) == [21]
    store.remove_points(guild_id, 21, "text", 400)
    assert not repo.claim_medal(season, 21)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(repo.claim_medal, season, 20) for _ in range(2)]
        assert sum(future.result(timeout=10) for future in futures) == 1
    frozen = repo.dashboard(guild_id).live
    assert frozen.state == "awarding"
    assert frozen.winner_id == 20
    assert repo.pending_ranks(season.id) == []
    repo.finish(season.id, "winner")
    assert repo.dashboard(guild_id).latest.state == "finished"
    assert store.get_user(guild_id, 20).text_points == 500
    assert store.get_user(guild_id, 22).voice_points == 700


def test_ranks_follow_existing_balances_and_points_resets(database, design):
    guild_id = 71
    design = design.put_reward(reward("Higher", 10), medal=False)
    store = PointsStore(database)
    store.add_points(guild_id, 10, "text", 6)
    store.add_points(guild_id, 11, "text", 500)
    store.add_points(guild_id, 10, "voice", 500)
    repo, season = start(database, design, guild_id, test_user_id=10)
    assert repo.medal_candidates(season, []) == []
    assert not repo.claim_medal(season, 11)
    database.execute(
        "INSERT INTO shelley_meow_progress (season_id, user_id, points) VALUES (%s, %s, %s)",
        (season.id, 10, 5000),
    )
    rank, next_rank = design.ordered_ranks
    repo.sync_rank_targets(season)
    assert [(item.user_id, item.reward_key) for item in repo.pending_ranks(season.id)] == [(10, rank.key)]
    repo.mark_rank_applied(season.id, 10, season.roles[rank.key])
    assert repo.pending_ranks(season.id) == []
    store.award(guild_id, 10, "text", 5, time.time(), 0)
    repo.sync_rank_targets(season)
    assert [item.reward_key for item in repo.pending_ranks(season.id)] == [next_rank.key]
    repo.mark_rank_applied(season.id, 10, season.roles[next_rank.key])
    store.reset_points(guild_id, "text")
    repo.sync_rank_targets(season)
    assert [(item.user_id, item.reward_key) for item in repo.pending_ranks(season.id)] == [(10, None)]
    repo.mark_rank_applied(season.id, 10, None)
    assert repo.pending_ranks(season.id) == []
    assert store.get_user(guild_id, 10).voice_points == 500


def test_replacing_season_keeps_live_until_ready_and_uses_the_selected_source(database, design):
    guild_id = 73
    repo, active = start(database, design, guild_id)
    store = PointsStore(database)
    assert store.award(guild_id, 9, "text", 12, time.time(), 0).total == 12
    repo.sync_rank_targets(active)
    current = repo.dashboard(guild_id).draft
    updated = current.design.updated(introduction="Updated")
    repo.save_draft(guild_id, current.revision, updated, current.based_on)
    cosmetic = repo.dashboard(guild_id).draft
    assert repo.publish_draft(guild_id, cosmetic.revision, active.id, update=True).id == active.id
    new_design = cosmetic.design.updated(source="voice")
    repo.save_draft(guild_id, cosmetic.revision, new_design, cosmetic.based_on)
    with pytest.raises(StaleDraft):
        repo.save_draft(guild_id, cosmetic.revision, new_design, cosmetic.based_on)
    current = repo.dashboard(guild_id).draft
    candidate = repo.publish_draft(guild_id, current.revision, active.id, update=False)
    assert repo.dashboard(guild_id).live.id == active.id
    assert not repo.activate(guild_id, candidate.id)
    for index, item in enumerate(new_design.rewards, 200):
        repo.store_role(candidate.id, item.key, index)
    assert repo.activate(guild_id, candidate.id)
    assert repo.dashboard(guild_id).live.id == candidate.id
    assert [season.id for season in repo.pending_cleanup(guild_id)] == [active.id]
    assert store.get_user(guild_id, 9).text_points == 12
    assert repo.pending_ranks(candidate.id) == []
    assert store.award(guild_id, 9, "voice", 6, time.time(), 0).total == 6
    repo.sync_rank_targets(repo.dashboard(guild_id).live)
    assert [change.user_id for change in repo.pending_ranks(candidate.id)] == [9]


def test_forced_end_stops_scoring_and_does_not_start_a_queued_replacement(database, design):
    guild_id = 74
    repo, season = start(database, design, guild_id)
    store = PointsStore(database)
    assert store.award(guild_id, 9, "text", 6, time.time(), 0).total == 6
    repo.sync_rank_targets(season)
    draft = repo.dashboard(guild_id).draft
    candidate = repo.publish_draft(guild_id, draft.revision, season.id, update=False)
    with pytest.raises(MeowError, match="no longer pending"):
        repo.cancel_setup(guild_id, season.id)
    with pytest.raises(MeowError, match="no longer active"):
        repo.end_season(guild_id, candidate.id)
    repo.end_season(guild_id, season.id)
    dashboard = repo.dashboard(guild_id)
    assert dashboard.live is dashboard.preparing is None
    assert dashboard.latest.state == "cancelled"
    assert dashboard.latest.winner_id is None
    assert {s.id for s in repo.pending_cleanup(guild_id)} == {season.id, candidate.id}
    assert store.award(guild_id, 9, "text", 25, time.time(), 0).total == 31
    assert not repo.claim_medal(season, 9)
    repo.sync_rank_targets(season)
    assert repo.pending_ranks(season.id) == []


def test_forced_end_does_not_override_a_winner(database, design):
    guild_id = 75
    repo, season = start(database, design, guild_id)
    store = PointsStore(database)
    assert store.award(guild_id, 9, "text", 20, time.time(), 0).total == 20
    assert repo.claim_medal(season, 9)
    with pytest.raises(MeowError, match="no longer active"):
        repo.end_season(guild_id, season.id)
    assert repo.dashboard(guild_id).live.winner_id == 9


def test_active_rank_is_requeued_for_member_and_medals_are_guild_scoped(database, design):
    guild_id, user_id = 76, 9
    repo, season = start(database, design, guild_id)
    store = PointsStore(database)
    store.add_points(guild_id, user_id, "text", 6)
    repo.sync_rank_targets(season)
    rank_id = season.roles[design.ranks[0].key]
    repo.mark_rank_applied(season.id, user_id, rank_id)
    assert repo.assigned_rank_ids(season.id) == {user_id: rank_id}
    assert repo.pending_ranks(season.id) == []
    repo.refresh_member_rank(guild_id + 1, user_id, rejoined=True)
    assert repo.pending_ranks(season.id) == []
    repo.refresh_member_rank(guild_id, user_id, rejoined=True)
    assert [item.user_id for item in repo.pending_ranks(season.id)] == [user_id]
    store.add_points(guild_id, user_id, "text", 20)
    assert repo.claim_medal(season, user_id)
    assert repo.earned_medal_roles(guild_id, user_id) == {}
    repo.finish(season.id, "winner")
    finish = repo.dashboard(guild_id).latest.closed_at
    assert repo.earned_medal_roles(guild_id, user_id + 1) == {}
    assert repo.earned_medal_roles(guild_id + 1, user_id) == {}
    assert repo.earned_medal_roles(guild_id, user_id, joined_at=finish - timedelta(seconds=1)) == {}
    medal_id = season.roles[design.medal.key]
    assert repo.earned_medal_roles(guild_id, user_id, joined_at=finish + timedelta(seconds=1)) == {user_id: {medal_id}}
    assert store.get_user(guild_id, user_id).text_points == 26


def test_live_era_edit_routes_medal_to_the_new_stage_without_changing_roles_or_balances(database, design):
    guild_id, user_id = 80, 20
    repo, active = start(database, design, guild_id)
    store = PointsStore(database)
    store.add_points(guild_id, user_id, "text", 100)
    store.add_points(guild_id, user_id, "voice", 30)
    draft = repo.dashboard(guild_id).draft
    slot = EraSlot(era=2, stage=4)
    update = draft.design.updated(source="voice", era_slot=slot)
    assert update.medal is not None
    update = update.put_reward(update.medal.model_copy(update={"points": 25}), medal=True)
    repo.save_draft(guild_id, draft.revision, update, active.id)
    draft = repo.dashboard(guild_id).draft
    revised = repo.publish_draft(guild_id, draft.revision, active.id, update=True, expected_slot=slot)
    assert revised.id == active.id and revised.roles == active.roles
    assert not repo.claim_medal(active, user_id)
    assert repo.claim_medal(revised, user_id)
    assert repo.era_results(guild_id) == {}
    repo.finish(active.id, "winner")
    results = repo.era_results(guild_id)
    assert list(results) == [2]
    assert [(row.stage, row.winner_id, row.username) for row in results[2]] == [(4, user_id, "winner")]
    assert repo.era_results(guild_id + 1) == {}
    assert repo.suggest_era_slot(guild_id) == EraSlot(era=2, stage=1)
    points = store.get_user(guild_id, user_id)
    assert (points.text_points, points.voice_points) == (100, 30)
    with pytest.raises(MeowError, match="already has a winner"):
        repo.publish_draft(guild_id, draft.revision, None, update=False)


def test_manual_end_does_not_consume_stage(database, design):
    guild_id = 82
    repo, active = start(database, design, guild_id)
    repo.end_season(guild_id, active.id)
    assert repo.era_results(guild_id) == {}
    assert repo.suggest_era_slot(guild_id) == active.design.era_slot == EraSlot(era=1, stage=1)
