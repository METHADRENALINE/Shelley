from __future__ import annotations

import asyncio
import os
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

import discord
import pytest
from discord import app_commands
from discord.ext import commands

from shelley.bot import (
    GLOBAL_COMMAND_NAMES,
    TRADEMARK_COMMAND_NAMES,
    configure_command_scopes,
)
from shelley.config import BotConfig, TrademarkGuildConfig, TrademarksConfig
from shelley.db import Database, apply_schema, schema_files
from shelley.trademarks.cog import claim_rejection_copy, occupied_claim_description
from shelley.trademarks.identity import GuildMemberResolver
from shelley.trademarks.ids import TRADEMARK_ID_ALPHABET, is_trademark_id
from shelley.trademarks.indexing import (
    TrademarkIndexConflict,
    refresh_trademark_index,
)
from shelley.trademarks.models import (
    ClaimResult,
    PatentAvailability,
    Trademark,
    TrademarkRequest,
)
from shelley.trademarks.normalization import (
    InvalidTrademarkName,
    normalize_trademark_name,
    parse_message_trademark,
    resolve_custom_emoji_aliases,
)
from shelley.trademarks.presenters import (
    all_trademarks_embed,
    exchange_success_announcement,
    inventory_embed,
    main_embed,
    member_name,
    patent_failure_announcement,
    patent_rejection_announcement,
    patent_success_announcement,
    release_announcement,
    safe_display,
    set_page_footer,
    trademark_card_embed,
    trademark_count_text,
)
from shelley.trademarks.service import TrademarkInvalidRequest, TrademarkService
from shelley.trademarks.views import (
    SELF_SELECTION_MESSAGE,
    ExchangeConfirmView,
    MainView,
    MarkChoiceSelect,
    PatentModal,
    SearchModal,
    ShowcaseSelect,
    ShowcaseTrademarkButton,
    TrademarkSelect,
)

TRADEMARK_TABLES = (
    "shelley_trademark_requests",
    "shelley_trademark_showcase",
    "shelley_trademark_events",
    "shelley_trademark_patent_windows",
    "shelley_trademarks",
)


def guild_config(**changes: object) -> TrademarkGuildConfig:
    values: dict[str, object] = {
        "channel_id": 123456789012345678,
        "patent_cooldown_seconds": 10,
    }
    values.update(changes)
    return TrademarkGuildConfig.model_validate(values)


def sequential_id_factory() -> Callable[[], str]:
    position = 0

    def factory() -> str:
        nonlocal position
        value = position
        position += 1
        characters = ["A"] * 25
        for index in range(24, -1, -1):
            characters[index] = TRADEMARK_ID_ALPHABET[value % len(TRADEMARK_ID_ALPHABET)]
            value //= len(TRADEMARK_ID_ALPHABET)
        return "-".join("".join(characters[index : index + 5]) for index in range(0, 25, 5))

    return factory


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self.value

    def advance(self, **values: float) -> None:
        self.value += timedelta(**values)


def test_trademark_config_is_isolated_by_guild() -> None:
    first = guild_config(channel_id=111111111111111111, max_name_characters=20)
    second = guild_config(channel_id=222222222222222222, max_name_characters=80)
    assert first.patent_limit == 30
    assert second.patent_limit == 30
    config = TrademarksConfig(
        enabled=True,
        guilds={
            333333333333333333: first,
            444444444444444444: second,
        },
    )

    assert config.for_guild(333333333333333333) == first
    assert config.for_guild(444444444444444444) == second
    assert config.for_guild(555555555555555555) is None
    assert first.automatic_patents is True
    assert guild_config(automatic_patents=False).automatic_patents is False
    assert TrademarksConfig(enabled=False, guilds=config.guilds).for_guild(333333333333333333) is None

    with pytest.raises(ValueError):
        guild_config(channel_id=0)
    with pytest.raises(ValueError):
        guild_config(showcase_limit=6)
    with pytest.raises(ValueError):
        guild_config(inventory_page_size=26)


def test_trademark_normalization_handles_case_spacing_scripts_and_emoji() -> None:
    config = guild_config(max_name_characters=20, max_spaces=2)
    first = normalize_trademark_name("  Красный   кот  ", config)
    second = normalize_trademark_name("красный КОТ", config)
    mixed_alphabet = normalize_trademark_name("кoт", config)
    emoji = normalize_trademark_name("<:First:123456789012345678>", config)
    renamed_emoji = normalize_trademark_name("<:Second:123456789012345678>", config)
    animated_emoji = normalize_trademark_name("<a:x:123456789012345679>", config)
    native_emoji = normalize_trademark_name(
        "\u2764\ufe0f \U0001f468\u200d\U0001f469\u200d\U0001f467\u200d\U0001f466",
        config,
    )
    tag_flag = normalize_trademark_name(
        "\U0001f3f4\U000e0067\U000e0062\U000e0065\U000e006e\U000e0067\U000e007f",
        config,
    )

    assert first.display == "Красный кот"
    assert first.key == second.key
    assert mixed_alphabet.key == normalize_trademark_name("кот", config).key
    assert emoji.key == renamed_emoji.key
    assert emoji.visible_characters == 1
    assert animated_emoji.visible_characters == 1
    assert native_emoji.visible_characters == 3
    assert tag_flag.visible_characters == 1
    assert parse_message_trademark("Красный кот™", config).key == first.key
    assert "<:First:123456789012345678>" in safe_display(emoji.display)
    assert safe_display(animated_emoji.display) == "<a:x:123456789012345679>"
    assert "@\u200beveryone" in safe_display("@everyone")
    for value in ("™", "Кот™", "Кот™™"):
        with pytest.raises(InvalidTrademarkName):
            normalize_trademark_name(value, config)
    for content in ("Кот", "Кот™ текст", "Кот™\n"):
        with pytest.raises(InvalidTrademarkName):
            parse_message_trademark(content, config)
    with pytest.raises(InvalidTrademarkName):
        normalize_trademark_name("один два три четыре", config)


@pytest.mark.parametrize(
    ("first", "second"),
    (
        ("Neko", "N\u0435k\u043e"),
        ("Anime", "An\u0456me"),
        ("paypal", "p\u0430yp\u0430l"),
        ("scope", "\u0455\u0441\u043e\u0440\u0435"),
        ("modern", "rnodern"),
        ("B0T", "BOT"),
        ("A-B", "A\u2010B"),
        ("A-B", "A\u2011B"),
        ("A-B", "A\u2012B"),
        ("A-B", "A\u2013B"),
        ("A-B", "A\u2014B"),
        ("A-B", "A\u2015B"),
        ('"Cat"', "\u201cCat\u201d"),
        ('"Cat"', "\u201eCat\u201c"),
        ('"Cat"', "\u275dCat\u275e"),
        ("'Cat'", "\u2018Cat\u2019"),
        ("'Cat'", "\u201aCat\u2018"),
        ("'Cat'", "\u275bCat\u275c"),
    ),
)
def test_trademark_normalization_blocks_visual_substitutions(
    first: str,
    second: str,
) -> None:
    config = guild_config(max_name_characters=30, max_spaces=5)

    assert normalize_trademark_name(first, config).key == normalize_trademark_name(second, config).key


@pytest.mark.parametrize(
    ("first", "second"),
    (
        ("C++", "C"),
        ("A-B", "A B"),
        ("A-B", "AB"),
        ("A.B", "A,B"),
        ("Cat", '"Cat"'),
    ),
)
def test_trademark_normalization_preserves_meaningful_punctuation(
    first: str,
    second: str,
) -> None:
    config = guild_config(max_name_characters=30, max_spaces=5)

    assert normalize_trademark_name(first, config).key != normalize_trademark_name(second, config).key


def test_server_emoji_aliases_resolve_without_changing_existing_markup() -> None:
    animated = "<a:x:123456789012345679>"
    existing = "<:kept:123456789012345680>"
    value = resolve_custom_emoji_aliases(
        f"Кот :x: {existing} :missing:",
        {"x": animated, "invalid": "not-an-emoji"},
    )

    assert value == f"Кот {animated} {existing} :missing:"

    from shelley.trademarks.cog import resolve_interaction_custom_emojis

    interaction = SimpleNamespace(
        guild=SimpleNamespace(
            emojis=(
                discord.PartialEmoji(
                    name="x",
                    animated=True,
                    id=123456789012345679,
                ),
            )
        )
    )
    assert resolve_interaction_custom_emojis(cast(Any, interaction), ":x:") == animated


def test_automatic_patent_candidate_requires_a_complete_trademark_message() -> None:
    from shelley.trademarks.cog import automatic_patent_candidate

    assert automatic_patent_candidate("Кот™")
    assert automatic_patent_candidate("  Красный кот™  ")
    assert not automatic_patent_candidate("™")
    assert not automatic_patent_candidate("Кот")
    assert not automatic_patent_candidate("Кот™ и другой текст")
    assert not automatic_patent_candidate("Кот™™")
    assert not automatic_patent_candidate("Кот™\n")


def test_automatic_patent_claims_from_any_channel_and_publishes() -> None:
    from shelley.trademarks import cog as cog_module

    created_at = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)
    trademark = Trademark(
        id="AAAAA-AAAAA-AAAAA-AAAAA-AAAAA",
        guild_id=1,
        display_name="Кот <a:x:123456789012345679>",
        normalized_name="кот <emoji:123456789012345679>",
        owner_id=2,
        owner_name="owner",
        created_at=created_at,
        cycle_started_at=created_at,
        owner_since=created_at,
    )
    config = guild_config(channel_id=111111111111111111)
    cog = object.__new__(cog_module.TrademarkCog)
    cog.config = SimpleNamespace(trademarks=TrademarksConfig(enabled=True, guilds={1: config}))
    cog.service = SimpleNamespace(claim=object())
    claim_arguments: list[tuple[object, ...]] = []
    reactions: list[str] = []
    published: list[discord.Embed] = []

    async def call(_method: object, *args: object) -> ClaimResult:
        claim_arguments.append(args)
        return ClaimResult(status="claimed", trademark=trademark)

    async def reaction(_message: object, emoji: str) -> None:
        reactions.append(emoji)

    async def publish(_guild_id: int, _config: TrademarkGuildConfig, embed: discord.Embed) -> None:
        published.append(embed)

    cog.call = call
    cog.automatic_claim_reaction = reaction
    cog.publish = publish
    message = SimpleNamespace(
        guild=SimpleNamespace(
            id=1,
            emojis=(
                discord.PartialEmoji(
                    name="x",
                    animated=True,
                    id=123456789012345679,
                ),
            ),
        ),
        author=SimpleNamespace(id=2, name="owner", bot=False),
        webhook_id=None,
        content="Кот :x:™",
        channel=SimpleNamespace(id=999999999999999999),
        id=222222222222222222,
    )

    asyncio.run(cog.on_message(cast(Any, message)))

    assert claim_arguments == [
        (
            1,
            2,
            "owner",
            "Кот <a:x:123456789012345679>",
            config,
        )
    ]
    assert reactions == ["✅"]
    assert len(published) == 1
    assert published[0].title == "Новая трейд марка"


def test_automatic_patent_rejects_invalid_name_publicly() -> None:
    from shelley.trademarks import cog as cog_module

    config = guild_config(channel_id=111111111111111111)
    cog = object.__new__(cog_module.TrademarkCog)
    cog.config = SimpleNamespace(trademarks=TrademarksConfig(enabled=True, guilds={1: config}))
    cog.service = SimpleNamespace(claim=object())
    reactions: list[str] = []
    published: list[discord.Embed] = []

    async def call(_method: object, *_args: object) -> ClaimResult:
        raise AssertionError("Invalid names must not reach the service")

    async def reaction(_message: object, emoji: str) -> None:
        reactions.append(emoji)

    async def publish(_guild_id: int, _config: TrademarkGuildConfig, embed: discord.Embed) -> None:
        published.append(embed)

    cog.call = call
    cog.automatic_claim_reaction = reaction
    cog.publish = publish
    message = SimpleNamespace(
        guild=SimpleNamespace(id=1, emojis=()),
        author=SimpleNamespace(id=2, name="owner", bot=False),
        webhook_id=None,
        content="Кот\u2800™",
        channel=SimpleNamespace(id=999999999999999999),
        id=222222222222222222,
    )

    asyncio.run(cog.on_message(cast(Any, message)))

    assert reactions == ["❌"]
    assert len(published) == 1
    assert published[0].title == "Патент не оформлен"
    assert "<@2>" in str(published[0].description)
    assert "невидимый символ" in str(published[0].description)


@pytest.mark.parametrize(
    "invisible",
    (
        "\t",
        "\u00a0",
        "\u00ad",
        "\u034f",
        "\u061c",
        "\u115f",
        "\u1160",
        "\u180e",
        "\u2007",
        "\u200b",
        "\u202e",
        "\u202f",
        "\u2060",
        "\u2061",
        "\u2065",
        "\u2800",
        "\u3164",
        "\ufeff",
        "\uffa0",
        "\ue000",
        "\U000e0020",
        "\U0001f3f4\U000e0061\U000e0062\U000e0063\U000e007f",
    ),
)
def test_trademark_normalization_rejects_invisible_characters(
    invisible: str,
) -> None:
    config = guild_config(max_name_characters=20, max_spaces=2)

    with pytest.raises(InvalidTrademarkName):
        normalize_trademark_name(f"Кот{invisible}Дом", config)
    with pytest.raises(InvalidTrademarkName):
        normalize_trademark_name(invisible, config)


def test_trademark_normalization_rejects_marks_without_visible_base() -> None:
    config = guild_config(max_name_characters=20, max_spaces=2)

    with pytest.raises(InvalidTrademarkName):
        normalize_trademark_name("\u0301", config)
    with pytest.raises(InvalidTrademarkName):
        normalize_trademark_name("Кот \u0301 Дом", config)


def test_trademark_ids_have_the_permanent_public_format() -> None:
    factory = sequential_id_factory()
    ids = {factory() for _ in range(100)}

    assert len(ids) == 100
    assert all(is_trademark_id(value) for value in ids)
    assert all(len(value) == 29 for value in ids)


def test_trademark_copy_uses_correct_declension_and_punctuation() -> None:
    expected = {
        0: "0 трейд марок",
        1: "1 трейд марка",
        2: "2 трейд марки",
        4: "4 трейд марки",
        5: "5 трейд марок",
        11: "11 трейд марок",
        21: "21 трейд марка",
        22: "22 трейд марки",
        25: "25 трейд марок",
    }

    assert {count: trademark_count_text(count) for count in expected} == expected
    assert SELF_SELECTION_MESSAGE == "Ну ты дед бом-бом я балдю... Нельзя выбрать самого себя!"


def test_single_page_embeds_do_not_show_pagination() -> None:
    single_page = discord.Embed()
    set_page_footer(single_page, 10, 0, 10)
    assert single_page.footer.text is None

    multiple_pages = discord.Embed()
    set_page_footer(multiple_pages, 11, 0, 10)
    assert multiple_pages.footer.text == "Страница 1 из 2"

    inventory = inventory_embed("owner", [], [], 2, 0, 10, own=True)
    count_field = next(field for field in inventory.fields if field.name == "Все трейд марки")
    assert count_field.value == "2 трейд марки"
    assert inventory.footer.text is None


def test_other_inventory_does_not_repeat_owner_field() -> None:
    inventory = inventory_embed(
        "gastron001",
        [],
        [],
        1,
        0,
        25,
        own=False,
    )

    assert inventory.title == "Инвентарь @gastron001"
    assert all(field.name != "Владелец" for field in inventory.fields)


def test_main_menu_primary_actions_share_first_row() -> None:
    cog = SimpleNamespace(config_for_guild=lambda _guild_id: guild_config())
    view = MainView(cast(Any, cog), 1, 1)
    rows = {item.label: item.row for item in view.children if isinstance(item, discord.ui.Button)}

    assert rows["Запатентовать"] == 0
    assert rows["Мой инвентарь"] == 0
    assert rows["Запросы"] == 0


def test_member_identity_uses_current_guild_membership_instead_of_voice_cache() -> None:
    class Guild:
        id = 1

        def __init__(self) -> None:
            self.cached_ids = {10}
            self.present_ids = {20}
            self.fetches: list[int] = []

        def get_member(self, user_id: int) -> object | None:
            return object() if user_id in self.cached_ids else None

        async def fetch_member(self, user_id: int) -> object:
            self.fetches.append(user_id)
            if user_id in self.present_ids:
                return object()
            response = SimpleNamespace(status=404, reason="Not Found")
            raise discord.NotFound(
                cast(Any, response),
                {"code": 10007, "message": "Unknown Member"},
            )

    async def scenario() -> None:
        guild = Guild()
        resolver = GuildMemberResolver()
        present_ids = await resolver.present_ids(
            cast(discord.Guild, guild),
            (10, 20, 30, 40),
            known_present=(40,),
        )

        assert present_ids == frozenset({10, 20, 40})
        assert guild.fetches == [20, 30]
        assert member_name(10, "voice-member", present_ids) == "<@10>"
        assert member_name(20, "server-member", present_ids) == "<@20>"
        assert member_name(30, "former-member", present_ids) == "@former-member"
        assert member_name(40, "interaction-user", present_ids) == "<@40>"

    asyncio.run(scenario())


def test_member_resolver_deduplicates_simultaneous_discord_requests() -> None:
    class Guild:
        id = 1

        def __init__(self) -> None:
            self.fetches = 0

        def get_member(self, _user_id: int) -> None:
            return None

        async def fetch_member(self, _user_id: int) -> object:
            self.fetches += 1
            await asyncio.sleep(0.01)
            return object()

    async def scenario() -> None:
        guild = Guild()
        resolver = GuildMemberResolver()
        first, second = await asyncio.gather(
            resolver.present_ids(cast(discord.Guild, guild), (20,)),
            resolver.present_ids(cast(discord.Guild, guild), (20,)),
        )

        assert first == frozenset({20})
        assert second == frozenset({20})
        assert guild.fetches == 1

    asyncio.run(scenario())


def test_trademark_interface_hides_technical_ids_and_identifies_people() -> None:
    created_at = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)
    offered = Trademark(
        id="AAAAA-AAAAA-AAAAA-AAAAA-AAAAA",
        guild_id=1,
        display_name="Кот",
        normalized_name="кот",
        owner_id=2,
        owner_name="owner",
        created_at=created_at,
        cycle_started_at=created_at,
        owner_since=created_at,
    )
    requested = Trademark(
        id="AAAAA-AAAAA-AAAAA-AAAAA-AAAB",
        guild_id=1,
        display_name="Пёс",
        normalized_name="пёс",
        owner_id=1,
        owner_name="sender",
        created_at=created_at,
        cycle_started_at=created_at,
        owner_since=created_at,
    )
    request = TrademarkRequest(
        id="request",
        guild_id=1,
        request_type="exchange",
        sender_id=1,
        sender_name="sender",
        recipient_id=2,
        recipient_name="recipient",
        offered_trademark_ids=(offered.id,),
        requested_trademark_ids=(requested.id,),
        source_channel_id=3,
        status="accepted",
        created_at=created_at,
        expires_at=created_at + timedelta(hours=24),
        resolved_at=created_at,
        resolved_by_id=2,
        resolved_by_name="recipient",
    )

    success = patent_success_announcement(offered, 1, {1})
    failure = patent_failure_announcement(offered, 3, "actor", {2, 3})
    own_failure = patent_failure_announcement(offered, 2, "owner", {2})
    rejection = patent_rejection_announcement(
        "Кот™",
        3,
        "actor",
        "Достигнут лимит",
        {3},
    )
    released = release_announcement(offered, 2, "owner", {2})
    exchange = exchange_success_announcement(
        request,
        (offered,),
        (requested,),
        {1, 2},
    )
    listing = all_trademarks_embed([offered, requested], 2, 0, 10, {2})
    card = trademark_card_embed(offered, {2})
    trademark_select = TrademarkSelect(cast(Any, None), [offered, requested])
    mark_choice = MarkChoiceSelect(cast(Any, None), [offered, requested])
    showcase_select = ShowcaseSelect(cast(Any, None), [offered, requested])
    showcase_button = ShowcaseTrademarkButton(cast(Any, None), offered, 0)
    server_emoji = Trademark(
        id="AAAAA-AAAAA-AAAAA-AAAAA-AAAC",
        guild_id=1,
        display_name="<a:x:123456789012345679>",
        normalized_name="<emoji:123456789012345679>",
        owner_id=2,
        owner_name="owner",
        created_at=created_at,
        cycle_started_at=created_at,
        owner_since=created_at,
    )
    server_emoji_button = ShowcaseTrademarkButton(cast(Any, None), server_emoji, 0)
    server_emoji_selects = (
        TrademarkSelect(cast(Any, None), [server_emoji]),
        MarkChoiceSelect(cast(Any, None), [server_emoji]),
        ShowcaseSelect(cast(Any, None), [server_emoji]),
    )

    success_text = str(success.to_dict())
    failure_text = str(failure.to_dict())
    own_failure_text = str(own_failure.to_dict())
    rejection_text = str(rejection.to_dict())
    released_text = str(released.to_dict())
    exchange_text = str(exchange.to_dict())
    rendered = (
        success_text,
        failure_text,
        own_failure_text,
        rejection_text,
        released_text,
        exchange_text,
        str(listing.to_dict()),
        str(card.to_dict()),
    )
    assert all(value in success_text for value in ("Кот™", "<@1>"))
    assert all(value in failure_text for value in ("Неудачная попытка патента", "Кот™", "<@3>", "<@2>"))
    assert own_failure.description == ("<@2> такой глупец, что решил подать патент на свою же трейд марку Кот™!")
    assert all(value in rejection_text for value in ("Патент не оформлен", "Кот™", "<@3>", "Достигнут лимит!"))
    assert "**Причина**\nДостигнут лимит!" in str(rejection.description)
    assert "Причина:" not in str(rejection.description)
    assert all(
        value in exchange_text
        for value in (
            "Обмен завершён",
            "<@1>",
            "<@2>",
            "Кот™",
            "Пёс™",
        )
    )
    assert all(value in released_text for value in ("Свободная трейд марка", "<@2>", "Кот™"))
    assert "<@2>" in str(listing.description)
    assert "@sender" in str(listing.description)
    assert "<@1>" not in str(listing.description)
    assert all(offered.id not in value for value in rendered)
    assert all(requested.id not in value for value in rendered)
    assert all("(`@" not in value for value in rendered)
    assert success.fields == []
    assert failure.fields == []
    assert released.fields == []
    assert len(exchange.fields) == 1
    assert exchange.fields[0].name == "Результат"
    assert all(value not in exchange_text for value in ("Первый результат", "Второй результат"))
    assert all(
        option.label.endswith("™")
        for option in (
            *trademark_select.options,
            *mark_choice.options,
            *showcase_select.options,
        )
    )
    assert showcase_button.label == "Кот™"
    assert server_emoji_button.label == "™"
    assert str(server_emoji_button.emoji) == "<a:x:123456789012345679>"
    assert all(select.options[0].label == "™" for select in server_emoji_selects)
    assert all(str(select.options[0].emoji) == "<a:x:123456789012345679>" for select in server_emoji_selects)
    assert all(
        offered.id not in str(option.description) and requested.id not in str(option.description)
        for option in (*trademark_select.options, *mark_choice.options)
    )


def test_cooldown_rejection_uses_static_remaining_time() -> None:
    now = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)
    result = ClaimResult(
        status="cooldown",
        next_available_at=now + timedelta(hours=1, minutes=2, seconds=3),
    )

    title, description = claim_rejection_copy(result, guild_config(), now=now)

    assert title == "Подожди немного"
    assert description == ("Следующий патент будет доступен через 1 час 2 минуты 3 секунды!")
    assert "<t:" not in description


def test_exchange_confirmation_allows_five_marks_per_side() -> None:
    created_at = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)

    def marks(owner_id: int, prefix: str, count: int) -> tuple[Trademark, ...]:
        return tuple(
            Trademark(
                id=f"AAAAA-AAAAA-AAAAA-AAAAA-{index:05d}",
                guild_id=1,
                display_name=f"{prefix} {index}",
                normalized_name=f"{prefix.casefold()} {index}",
                owner_id=owner_id,
                owner_name=f"user{owner_id}",
                created_at=created_at,
                cycle_started_at=created_at,
                owner_since=created_at,
            )
            for index in range(count)
        )

    cog = SimpleNamespace(config_for_guild=lambda _guild_id: guild_config())
    partial = ExchangeConfirmView(
        cast(Any, cog),
        1,
        1,
        marks(1, "Own", 3),
        marks(2, "Other", 1),
        2,
        "other",
    )
    full = ExchangeConfirmView(
        cast(Any, cog),
        1,
        1,
        marks(1, "Own", 5),
        marks(2, "Other", 5),
        2,
        "other",
    )
    partial_labels = {item.label for item in partial.children if isinstance(item, discord.ui.Button)}
    full_labels = {item.label for item in full.children if isinstance(item, discord.ui.Button)}

    assert "Добавить ещё свою" in partial_labels
    assert "Добавить ещё @other" in partial_labels
    assert "Добавить ещё свою" not in full_labels
    assert all(not str(label).startswith("Добавить ещё @") for label in full_labels)
    assert {"Отправить запрос", "Отмена"} <= full_labels


def test_occupied_claim_private_copy_distinguishes_the_owner() -> None:
    created_at = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)
    trademark = Trademark(
        id="AAAAA-AAAAA-AAAAA-AAAAA-AAAAA",
        guild_id=1,
        display_name="Кот",
        normalized_name="кот",
        owner_id=2,
        owner_name="owner",
        created_at=created_at,
        cycle_started_at=created_at,
        owner_since=created_at,
    )

    assert occupied_claim_description(trademark, 2) == ("Кот™ уже запатентована на твое имя!")
    assert occupied_claim_description(trademark, 3) == "Кот™ уже занята!"


def test_exchange_service_rejects_more_than_five_marks_per_side() -> None:
    service = TrademarkService(cast(Database, SimpleNamespace()))

    with pytest.raises(TrademarkInvalidRequest, match="от 1 до 5"):
        service.create_exchange(
            1,
            1,
            "sender",
            2,
            "recipient",
            ("1", "2", "3", "4", "5", "6"),
            ("7",),
            3,
            guild_config(),
        )


def test_trademark_interface_copy_matches_current_product_language() -> None:
    availability = PatentAvailability(
        owned=1,
        inventory_limit=20,
        used=2,
        patent_limit=3,
        next_window_at=None,
        cooldown_until=None,
    )
    embed = main_embed(availability, 0)
    patent_modal = PatentModal(cast(Any, None), 1)
    search_modal = SearchModal(cast(Any, None), 1)

    assert any(field.name == "Патенты на сутки" for field in embed.fields)
    assert all(field.name != "Доступно патентов" for field in embed.fields)
    assert patent_modal.name_input.placeholder == "Пример: Семя Кирилла"
    assert search_modal.query_input.to_component_dict()["label"] == "Название"


def test_releasing_trademark_publishes_public_announcement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from shelley.trademarks import cog as cog_module

    created_at = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)
    trademark = Trademark(
        id="AAAAA-AAAAA-AAAAA-AAAAA-AAAAA",
        guild_id=1,
        display_name="Кот",
        normalized_name="кот",
        owner_id=None,
        owner_name=None,
        created_at=created_at,
        cycle_started_at=created_at,
        owner_since=None,
    )
    config = guild_config()
    published: list[discord.Embed] = []
    cog = object.__new__(cog_module.TrademarkCog)
    cog.service = SimpleNamespace(release=object())
    cog.config_for_guild = lambda _guild_id: config

    async def call(*_args: object, **_kwargs: object) -> Trademark:
        return trademark

    async def defer(_interaction: object) -> None:
        return None

    async def publish(_guild_id: int, _config: TrademarkGuildConfig, embed: discord.Embed) -> None:
        published.append(embed)

    async def replace(_interaction: object, _embed: discord.Embed, _view: object) -> None:
        return None

    cog.call = call
    cog.defer = defer
    cog.publish = publish
    cog.replace = replace
    monkeypatch.setattr(
        cog_module,
        "CardView",
        lambda *_args, **_kwargs: None,
    )
    interaction = SimpleNamespace(
        guild_id=1,
        user=SimpleNamespace(id=2, name="owner"),
    )

    asyncio.run(
        cog.handle_release(
            cast(Any, interaction),
            trademark.id,
            administrator=False,
        )
    )

    assert len(published) == 1
    assert published[0].title == "Свободная трейд марка"
    assert published[0].description == "<@2> решил освободить Кот™!"


def test_only_trademark_commands_are_copied_to_extra_guilds() -> None:
    bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())

    async def slash_callback(_interaction: discord.Interaction) -> None:
        return None

    async def message_callback(_interaction: discord.Interaction, _message: discord.Message) -> None:
        return None

    for name in (*GLOBAL_COMMAND_NAMES, "notify", "tm"):
        bot.tree.add_command(
            app_commands.Command(
                name=name,
                description=f"Test {name}",
                callback=slash_callback,
            )
        )
    bot.tree.add_command(
        app_commands.ContextMenu(
            name="Запатентовать трейд марку",
            callback=message_callback,
        )
    )

    primary = discord.Object(id=111111111111111111)
    extra = discord.Object(id=222222222222222222)
    configure_command_scopes(bot.tree, primary, (primary, extra))

    assert {command.name for command in bot.tree.get_commands()} == set(GLOBAL_COMMAND_NAMES)
    assert {command.name for command in bot.tree.get_commands(guild=primary)} == {
        "notify",
        *TRADEMARK_COMMAND_NAMES,
    }
    assert {command.name for command in bot.tree.get_commands(guild=extra)} == set(TRADEMARK_COMMAND_NAMES)


def test_trademark_cog_registers_both_entry_points() -> None:
    from shelley.trademarks.cog import TrademarkCog

    async def scenario() -> None:
        guild_id = 333333333333333333
        config = BotConfig(
            trademarks=TrademarksConfig(
                enabled=True,
                guilds={guild_id: guild_config()},
            )
        )
        bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
        bot.config = config
        await bot.add_cog(TrademarkCog(bot))

        guild = discord.Object(id=guild_id)
        configure_command_scopes(bot.tree, guild, (guild,))
        commands_by_name = {command.name: command for command in bot.tree.get_commands(guild=guild)}

        assert set(commands_by_name) == set(TRADEMARK_COMMAND_NAMES)
        assert isinstance(commands_by_name["tm"], app_commands.Command)
        assert commands_by_name["tm"].description == "Open the trademark system."
        assert isinstance(
            commands_by_name["Запатентовать трейд марку"],
            app_commands.ContextMenu,
        )

        await bot.remove_cog("TrademarkCog")
        await bot.close()

    asyncio.run(scenario())


@pytest.fixture(scope="session")
def trademark_database() -> Iterator[Database]:
    url = os.getenv("SHELLEY_TEST_DATABASE_URL", "").strip()
    if not url:
        pytest.skip("SHELLEY_TEST_DATABASE_URL is not configured")
    database = Database(url)
    with database.connection() as connection:
        database_name = str(connection.info.dbname or "")
    if not database_name.endswith("_test"):
        raise RuntimeError("Trademark integration tests require a *_test database")
    apply_schema(database)
    yield database


@pytest.fixture
def clean_trademark_database(trademark_database: Database) -> Iterator[Database]:
    with trademark_database.connection() as connection:
        connection.execute(f"TRUNCATE {', '.join(TRADEMARK_TABLES)} RESTART IDENTITY CASCADE")
    yield trademark_database
    with trademark_database.connection() as connection:
        connection.execute(f"TRUNCATE {', '.join(TRADEMARK_TABLES)} RESTART IDENTITY CASCADE")


def service_for(database: Database, clock: Clock) -> TrademarkService:
    return TrademarkService(
        database,
        now=clock.now,
        trademark_id_factory=sequential_id_factory(),
    )


def test_exchange_schema_preserves_existing_single_mark_requests(
    trademark_database: Database,
) -> None:
    schemas = dict(schema_files())
    schema_name = "shelley_exchange_schema_test"
    first = "AAAAA-AAAAA-AAAAA-AAAAA-AAAAA"
    second = "AAAAA-AAAAA-AAAAA-AAAAA-AAAAB"
    with trademark_database.connection() as connection:
        connection.execute(f"DROP SCHEMA IF EXISTS {schema_name} CASCADE")
        connection.execute(f"CREATE SCHEMA {schema_name}")
        try:
            connection.execute(f"SET search_path TO {schema_name}")
            connection.execute(schemas["003_trademarks"])
            connection.execute(
                """
                INSERT INTO shelley_trademarks (
                    id, guild_id, display_name, normalized_name, owner_id,
                    owner_name, cycle_started_at, owner_since
                )
                VALUES
                    (%s, 1, 'First', 'first', 1, 'first', now(), now()),
                    (%s, 1, 'Second', 'second', 2, 'second', now(), now())
                """,
                (first, second),
            )
            connection.execute(
                """
                INSERT INTO shelley_trademark_requests (
                    id, guild_id, request_type, sender_id, sender_name,
                    recipient_id, recipient_name, offered_trademark_id,
                    requested_trademark_id, source_channel_id, expires_at
                )
                VALUES (
                    '00000000-0000-0000-0000-000000000001',
                    1, 'exchange', 1, 'sender', 2, 'recipient',
                    %s, %s, 1, now() + interval '1 day'
                )
                """,
                (first, second),
            )
            connection.execute(schemas["004_trademark_exchange_sets"])
            row = connection.execute(
                """
                SELECT offered_trademark_ids, requested_trademark_ids
                FROM shelley_trademark_requests
                """
            ).fetchone()
            assert tuple(row["offered_trademark_ids"]) == (first,)
            assert tuple(row["requested_trademark_ids"]) == (second,)
        finally:
            connection.execute("SET search_path TO public")
            connection.execute(f"DROP SCHEMA IF EXISTS {schema_name} CASCADE")


def claim(
    service: TrademarkService,
    config: TrademarkGuildConfig,
    guild_id: int,
    user_id: int,
    name: str,
) -> str:
    result = service.claim(guild_id, user_id, f"user{user_id}", name, config)
    assert result.status == "claimed"
    assert result.trademark is not None
    return result.trademark.id


def test_trademark_index_refresh_is_atomic_and_detects_collisions(
    clean_trademark_database: Database,
) -> None:
    first_id = "AAAAA-AAAAA-AAAAA-AAAAA-AAAAA"
    second_id = "AAAAA-AAAAA-AAAAA-AAAAA-AAAAB"
    with clean_trademark_database.connection() as connection:
        connection.execute(
            """
            INSERT INTO shelley_trademarks (
                id, guild_id, display_name, normalized_name, owner_id,
                owner_name, cycle_started_at, owner_since
            )
            VALUES
                (%s, 1, 'Neko', 'neko', 1, 'first', now(), now()),
                (%s, 1, U&'N\\0435k\\043E', U&'n\\0435k\\043E',
                 2, 'second', now(), now())
            """,
            (first_id, second_id),
        )

    with pytest.raises(TrademarkIndexConflict, match="Confusable trademark names"):
        refresh_trademark_index(clean_trademark_database)

    with clean_trademark_database.connection() as connection:
        rows = connection.execute(
            """
            SELECT normalized_name, search_name
            FROM shelley_trademarks
            ORDER BY id
            """
        ).fetchall()
        assert [row["normalized_name"] for row in rows] == [
            "neko",
            "n\u0435k\u043e",
        ]
        assert all(row["search_name"] is None for row in rows)
        connection.execute(
            "DELETE FROM shelley_trademarks WHERE id = %s",
            (second_id,),
        )

    assert refresh_trademark_index(clean_trademark_database) == 1
    assert refresh_trademark_index(clean_trademark_database) == 0
    with clean_trademark_database.connection() as connection:
        row = connection.execute(
            """
            SELECT normalized_name, search_name
            FROM shelley_trademarks
            WHERE id = %s
            """,
            (first_id,),
        ).fetchone()
        assert str(row["normalized_name"]).startswith("uts39-17.0.0-v1:")
        assert row["search_name"] == "neko"


def test_claim_and_search_use_visual_confusable_identity(
    clean_trademark_database: Database,
) -> None:
    clock = Clock()
    config = guild_config()
    service = service_for(clean_trademark_database, clock)
    guild_id = 700000000000000000

    claimed = service.claim(guild_id, 1, "first", "Neko", config)
    assert claimed.status == "claimed"
    assert claimed.trademark is not None
    occupied = service.claim(guild_id, 2, "second", "N\u0435k\u043e", config)
    results = service.search(guild_id, "N\u0435k", config)
    id_results = service.search(guild_id, claimed.trademark.id, config)
    missing_id_results = service.search(
        guild_id,
        "ZZZZZ-ZZZZZ-ZZZZZ-ZZZZZ-ZZZZZ",
        config,
    )

    assert occupied.status == "occupied"
    assert occupied.trademark is not None
    assert occupied.trademark.id == claimed.trademark.id
    assert [item.id for item in results] == [claimed.trademark.id]
    assert [item.id for item in id_results] == [claimed.trademark.id]
    assert missing_id_results == []


def test_claim_release_and_reclaim_preserve_identity_and_history(
    clean_trademark_database: Database,
) -> None:
    clock = Clock()
    config = guild_config()
    service = service_for(clean_trademark_database, clock)
    guild_id = 700000000000000001

    first = service.claim(guild_id, 1, "first", "  Кот  ", config)
    occupied = service.claim(guild_id, 2, "second", "кОТ", config)

    assert first.status == "claimed"
    assert first.trademark is not None
    assert occupied.status == "occupied"
    assert occupied.trademark is not None
    assert occupied.trademark.id == first.trademark.id

    released = service.release(
        guild_id,
        first.trademark.id,
        3,
        "administrator",
        administrator=True,
    )
    assert released.owner_id is None

    clock.advance(seconds=11)
    reclaimed = service.claim(guild_id, 2, "second", "КОТ", config)
    assert reclaimed.status == "claimed"
    assert reclaimed.trademark is not None
    assert reclaimed.trademark.id == first.trademark.id
    assert reclaimed.trademark.created_at == first.trademark.created_at
    assert reclaimed.trademark.display_name == "КОТ"

    events, total = service.history_page(guild_id, first.trademark.id, 10, 0)
    assert total == 3
    assert [event.event_type for event in reversed(events)] == [
        "patent",
        "admin_release",
        "patent",
    ]
    assert events[1].actor_id == 3
    assert events[1].from_user_id == 1


def test_patent_cooldown_limit_and_guild_isolation(
    clean_trademark_database: Database,
) -> None:
    clock = Clock()
    config = guild_config(patent_limit=2, patent_window_hours=24)
    service = service_for(clean_trademark_database, clock)
    first_guild = 700000000000000002
    second_guild = 700000000000000003

    first_id = claim(service, config, first_guild, 1, "One")
    cooldown = service.claim(first_guild, 1, "user1", "Two", config)
    assert cooldown.status == "cooldown"

    clock.advance(seconds=11)
    claim(service, config, first_guild, 1, "Two")
    clock.advance(seconds=11)
    limited = service.claim(first_guild, 1, "user1", "Three", config)
    assert limited.status == "limit_reached"
    assert limited.next_available_at == datetime(2026, 7, 31, 12, 0, tzinfo=UTC)

    second_id = claim(service, config, second_guild, 1, "One")
    assert first_id != second_id

    clock.advance(hours=24)
    assert service.claim(first_guild, 1, "user1", "Three", config).status == "claimed"


def test_showcase_order_survives_move_and_compaction(
    clean_trademark_database: Database,
) -> None:
    clock = Clock()
    config = guild_config()
    service = service_for(clean_trademark_database, clock)
    guild_id = 700000000000000004
    identifiers = []

    for name in ("One", "Two", "Three"):
        identifiers.append(claim(service, config, guild_id, 1, name))
        clock.advance(seconds=11)

    for trademark_id in identifiers:
        service.add_to_showcase(guild_id, 1, trademark_id, config)
    service.move_in_showcase(guild_id, 1, identifiers[2], 1)
    assert [item.id for item in service.showcase(guild_id, 1)] == [
        identifiers[2],
        identifiers[0],
        identifiers[1],
    ]

    service.remove_from_showcase(guild_id, 1, identifiers[0])
    assert [item.id for item in service.showcase(guild_id, 1)] == [
        identifiers[2],
        identifiers[1],
    ]


def test_exchange_is_atomic_and_invalidates_competing_requests(
    clean_trademark_database: Database,
) -> None:
    clock = Clock()
    config = guild_config()
    service = service_for(clean_trademark_database, clock)
    guild_id = 700000000000000005
    offered_ids = [claim(service, config, guild_id, 1, "Offered 1")]
    clock.advance(seconds=11)
    offered_ids.append(claim(service, config, guild_id, 1, "Offered 2"))
    clock.advance(seconds=11)
    offered_ids.append(claim(service, config, guild_id, 1, "Offered 3"))
    requested_id = claim(service, config, guild_id, 2, "Requested")
    clock.advance(seconds=11)
    competing_id = claim(service, config, guild_id, 2, "Competing")

    accepted_request = service.create_exchange(
        guild_id,
        1,
        "sender",
        2,
        "recipient",
        offered_ids,
        (requested_id,),
        config.channel_id,
        config,
    )
    competing_request = service.create_exchange(
        guild_id,
        1,
        "sender",
        2,
        "recipient",
        (offered_ids[0],),
        (competing_id,),
        config.channel_id,
        config,
    )

    result = service.accept_request(
        guild_id,
        accepted_request.id,
        2,
        "recipient",
        config,
    )
    assert result.status == "completed"
    assert len(result.offered) == 3
    assert len(result.requested) == 1
    assert all(service.get(guild_id, item_id).owner_id == 2 for item_id in offered_ids)
    assert service.get(guild_id, requested_id).owner_id == 1
    assert service.request(guild_id, competing_request.id)[0].status == "invalidated"
    assert [event.event_type for event in service.history_page(guild_id, offered_ids[0], 10, 0)[0]] == [
        "exchange",
        "patent",
    ]
    assert [event.event_type for event in service.history_page(guild_id, requested_id, 10, 0)[0]] == [
        "exchange",
        "patent",
    ]


def test_gift_inventory_limit_and_expiry_are_persisted(
    clean_trademark_database: Database,
) -> None:
    clock = Clock()
    config = guild_config(inventory_limit=1, gift_expiry_hours=1)
    service = service_for(clean_trademark_database, clock)
    guild_id = 700000000000000006
    gift_id = claim(service, config, guild_id, 1, "Gift")
    claim(service, config, guild_id, 2, "Occupied slot")

    blocked_request = service.create_gift(
        guild_id,
        1,
        "sender",
        2,
        "recipient",
        gift_id,
        config.channel_id,
        config,
    )
    blocked = service.accept_request(
        guild_id,
        blocked_request.id,
        2,
        "recipient",
        config,
    )
    assert blocked.status == "inventory_full"
    assert service.request(guild_id, blocked_request.id)[0].status == "pending"

    service.cancel_request(guild_id, blocked_request.id, 1, "sender")
    expiring_request = service.create_gift(
        guild_id,
        1,
        "sender",
        3,
        "recipient",
        gift_id,
        config.channel_id,
        config,
    )
    clock.advance(hours=2)
    with pytest.raises(TrademarkInvalidRequest, match="истёк"):
        service.decline_request(guild_id, expiring_request.id, 3, "recipient")
    assert service.request(guild_id, expiring_request.id)[0].status == "expired"
