from __future__ import annotations

import asyncio
import logging
import math
from datetime import UTC, datetime
from typing import Any, cast

import discord
from discord import app_commands
from discord.ext import commands

from ..config import TrademarkGuildConfig
from ..db import get_database
from .identity import GuildMemberResolver
from .models import ClaimResult, Trademark, TrademarkRequestListing
from .normalization import (
    CUSTOM_EMOJI_PATTERN,
    InvalidTrademarkName,
    parse_message_trademark,
    resolve_custom_emoji_aliases,
)
from .presenters import (
    all_trademarks_embed,
    exchange_success_announcement,
    history_embed,
    inventory_embed,
    main_embed,
    member_name,
    patent_failure_announcement,
    patent_rejection_announcement,
    patent_success_announcement,
    release_announcement,
    request_card_embed,
    requests_overview_embed,
    requests_page_embed,
    result_embed,
    safe_display,
    trademark_card_embed,
    trademark_count_text,
    trademark_name,
    trademark_names,
)
from .service import (
    TrademarkDuplicateRequest,
    TrademarkInvalidRequest,
    TrademarkNotFound,
    TrademarkOperationError,
    TrademarkPermissionDenied,
    TrademarkRequestLimitReached,
    TrademarkService,
    TrademarkShowcaseFull,
)
from .views import (
    AllTrademarksView,
    BackTarget,
    CardView,
    ConfirmationView,
    ContextPatentView,
    ExchangeConfirmView,
    GiftConfirmView,
    HistoryView,
    InventoryView,
    MainView,
    MarkChoiceView,
    RequestCardView,
    RequestListView,
    RequestsOverviewView,
    ShowcasePositionView,
    ShowcaseSettingsView,
)

logger = logging.getLogger(__name__)
TRADEMARK_CONTEXT_COMMAND_NAME = "Запатентовать трейд марку"


def automatic_patent_candidate(content: str) -> bool:
    raw = str(content)
    if "\n" in raw or "\r" in raw:
        return False
    stripped = raw.strip(" ")
    return stripped.endswith("™") and stripped.count("™") == 1 and bool(stripped[:-1])


def resolve_guild_custom_emojis(guild: discord.Guild | None, value: str) -> str:
    if guild is None:
        return str(value)
    aliases: dict[str, str] = {}
    for emoji in guild.emojis:
        name = emoji.name
        rendered = str(emoji)
        if name and CUSTOM_EMOJI_PATTERN.fullmatch(rendered):
            aliases[name] = rendered
    return resolve_custom_emoji_aliases(value, aliases)


def resolve_interaction_custom_emojis(interaction: discord.Interaction, value: str) -> str:
    return resolve_guild_custom_emojis(interaction.guild, value)


def claim_rejection_copy(
    result: ClaimResult,
    config: TrademarkGuildConfig,
    *,
    now: datetime | None = None,
) -> tuple[str, str]:
    if result.status == "inventory_full":
        return (
            "Инвентарь заполнен",
            (f"В инвентаре уже {trademark_count_text(config.inventory_limit)}. Освободи место, чтобы получить новую!"),
        )
    if result.status == "limit_reached":
        if result.next_available_at is None:
            description = "Попробуй ещё раз немного позже!"
        else:
            timestamp = int(result.next_available_at.timestamp())
            description = f"Новые патенты будут доступны <t:{timestamp}:F>!"
        return "Лимит патентов исчерпан", description
    if result.next_available_at is None:
        description = "Попробуй ещё раз немного позже!"
    else:
        current = now or datetime.now(UTC)
        remaining = max(
            1,
            math.ceil((result.next_available_at - current).total_seconds()),
        )
        description = f"Следующий патент будет доступен через {remaining_time_text(remaining)}!"
    return "Подожди немного", description


def remaining_time_text(total_seconds: int) -> str:
    seconds = max(1, int(total_seconds))
    values = (
        (seconds // 3600, ("час", "часа", "часов")),
        ((seconds % 3600) // 60, ("минуту", "минуты", "минут")),
        (seconds % 60, ("секунду", "секунды", "секунд")),
    )
    parts = [f"{value} {russian_plural(value, *forms)}" for value, forms in values if value]
    return " ".join(parts)


def russian_plural(value: int, one: str, few: str, many: str) -> str:
    remainder = abs(int(value)) % 100
    if 11 <= remainder <= 14:
        return many
    remainder %= 10
    if remainder == 1:
        return one
    if 2 <= remainder <= 4:
        return few
    return many


def occupied_claim_description(trademark: Trademark, actor_id: int) -> str:
    if trademark.owner_id == int(actor_id):
        return f"{trademark_name(trademark)} уже запатентована на твое имя!"
    return f"{trademark_name(trademark)} уже занята!"


class TrademarkCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.config = cast(Any, bot).config
        self.service = TrademarkService(get_database(self.config))
        self.member_resolver = GuildMemberResolver()
        self.confirmations: set[tuple[int, int, int]] = set()
        self.patent_message_command = app_commands.ContextMenu(
            name=TRADEMARK_CONTEXT_COMMAND_NAME,
            callback=self.patent_message,
        )

    async def cog_load(self) -> None:
        if not self.config.trademarks.enabled:
            return
        for guild_id in self.config.trademarks.guilds:
            self.bot.tree.add_command(
                self.patent_message_command,
                guild=discord.Object(id=int(guild_id)),
            )

    async def cog_unload(self) -> None:
        for guild_id in self.config.trademarks.guilds:
            self.bot.tree.remove_command(
                TRADEMARK_CONTEXT_COMMAND_NAME,
                guild=discord.Object(id=int(guild_id)),
                type=discord.AppCommandType.message,
            )

    def config_for_guild(self, guild_id: int) -> TrademarkGuildConfig:
        config = self.config.trademarks.for_guild(int(guild_id))
        if config is None:
            raise TrademarkPermissionDenied("Система трейд марок не настроена для этого сервера")
        return config

    def command_context(self, interaction: discord.Interaction) -> tuple[int, TrademarkGuildConfig] | None:
        if interaction.guild_id is None:
            return None
        try:
            config = self.config_for_guild(int(interaction.guild_id))
        except TrademarkPermissionDenied:
            return None
        if int(interaction.channel_id or 0) != config.channel_id:
            return None
        return int(interaction.guild_id), config

    async def reject_context(self, interaction: discord.Interaction) -> None:
        if interaction.guild_id is None:
            text = "Система трейд марок работает только внутри Discord-сервера!"
        else:
            config = self.config.trademarks.for_guild(int(interaction.guild_id))
            if config is None:
                text = "Система трейд марок не настроена для этого сервера!"
            else:
                text = f"Используй систему трейд марок в <#{config.channel_id}>!"
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)

    async def call(self, method: Any, *args: Any, **kwargs: Any) -> Any:
        return await asyncio.to_thread(method, *args, **kwargs)

    async def defer(self, interaction: discord.Interaction) -> None:
        if interaction.response.is_done():
            return
        await interaction.response.defer(ephemeral=True, thinking=False)

    async def replace(
        self,
        interaction: discord.Interaction,
        embed: discord.Embed,
        view: discord.ui.View | None,
    ) -> None:
        if interaction.response.is_done():
            await interaction.edit_original_response(
                content=None,
                embed=embed,
                view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        if interaction.message is not None:
            await interaction.response.edit_message(
                content=None,
                embed=embed,
                view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        if view is None:
            await interaction.response.send_message(
                embed=embed,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        else:
            await interaction.response.send_message(
                embed=embed,
                view=view,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )

    async def configured_channel(self, guild_id: int, config: TrademarkGuildConfig) -> discord.abc.Messageable:
        channel = self.bot.get_channel(config.channel_id)
        if channel is None:
            channel = await self.bot.fetch_channel(config.channel_id)
        channel_guild = getattr(channel, "guild", None)
        if channel_guild is None or int(channel_guild.id) != int(guild_id):
            raise RuntimeError("Configured trademark channel belongs to another server")
        if not isinstance(channel, discord.abc.Messageable):
            raise TypeError("Configured trademark channel cannot receive messages")
        return channel

    async def publish(self, guild_id: int, config: TrademarkGuildConfig, embed: discord.Embed) -> None:
        try:
            channel = await self.configured_channel(guild_id, config)
            await channel.send(
                embed=embed,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            logger.exception(
                "cannot publish trademark event",
                extra={"guild_id": guild_id, "channel_id": config.channel_id},
            )

    async def automatic_claim_reaction(self, message: discord.Message, emoji: str) -> None:
        try:
            await message.add_reaction(emoji)
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            logger.exception(
                "cannot add automatic patent reaction",
                extra={
                    "guild_id": int(message.guild.id) if message.guild else 0,
                    "channel_id": int(message.channel.id),
                    "message_id": int(message.id),
                },
            )

    async def reject_automatic_claim(
        self,
        message: discord.Message,
        guild_id: int,
        config: TrademarkGuildConfig,
        attempted_name: str,
        reason: str,
    ) -> None:
        await self.automatic_claim_reaction(message, "❌")
        await self.publish(
            guild_id,
            config,
            patent_rejection_announcement(
                attempted_name,
                int(message.author.id),
                message.author.name,
                reason,
                {int(message.author.id)},
            ),
        )

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.guild is None or message.author.bot or message.webhook_id is not None or not automatic_patent_candidate(message.content):
            return
        guild_id = int(message.guild.id)
        config = self.config.trademarks.for_guild(guild_id)
        if config is None or not config.automatic_patents:
            return
        resolved_content = resolve_guild_custom_emojis(
            message.guild,
            message.content,
        )
        attempted_name = resolved_content.strip(" ")
        try:
            normalized = parse_message_trademark(resolved_content, config)
            result = await self.call(
                self.service.claim,
                guild_id,
                int(message.author.id),
                message.author.name,
                normalized.display,
                config,
            )
        except InvalidTrademarkName as error:
            await self.reject_automatic_claim(
                message,
                guild_id,
                config,
                attempted_name,
                str(error),
            )
            return
        except Exception:
            logger.exception(
                "automatic patent failed",
                extra={
                    "guild_id": guild_id,
                    "channel_id": int(message.channel.id),
                    "message_id": int(message.id),
                    "user_id": int(message.author.id),
                },
            )
            await self.reject_automatic_claim(
                message,
                guild_id,
                config,
                attempted_name,
                "Shelley не смогла завершить оформление из-за внутренней ошибки",
            )
            return
        trademark = result.trademark
        if result.status == "claimed" and trademark is not None:
            await self.automatic_claim_reaction(message, "✅")
            await self.publish(
                guild_id,
                config,
                patent_success_announcement(
                    trademark,
                    int(message.author.id),
                    {int(message.author.id)},
                ),
            )
            return
        if result.status == "occupied" and trademark is not None:
            await self.automatic_claim_reaction(message, "❌")
            present_ids = await self.member_resolver.present_ids(
                message.guild,
                (message.author.id, trademark.owner_id),
                known_present=(int(message.author.id),),
            )
            await self.publish(
                guild_id,
                config,
                patent_failure_announcement(
                    trademark,
                    int(message.author.id),
                    message.author.name,
                    present_ids,
                ),
            )
            return
        _, description = claim_rejection_copy(result, config)
        await self.reject_automatic_claim(
            message,
            guild_id,
            config,
            attempted_name,
            description,
        )

    async def show_main(self, interaction: discord.Interaction) -> None:
        guild_id = int(interaction.guild_id or 0)
        config = self.config_for_guild(guild_id)
        await self.defer(interaction)
        availability, counts = await asyncio.gather(
            self.call(
                self.service.availability,
                guild_id,
                int(interaction.user.id),
                config,
            ),
            self.call(
                self.service.request_counts,
                guild_id,
                int(interaction.user.id),
            ),
        )
        await self.replace(
            interaction,
            main_embed(availability, counts[0]),
            MainView(self, int(interaction.user.id), guild_id),
        )

    async def show_inventory(
        self,
        interaction: discord.Interaction,
        owner_id: int,
        owner_name: str,
        page: int,
    ) -> None:
        guild_id = int(interaction.guild_id or 0)
        config = self.config_for_guild(guild_id)
        page = max(0, int(page))
        await self.defer(interaction)
        (items, total), showcase = await asyncio.gather(
            self.call(
                self.service.owned_page,
                guild_id,
                owner_id,
                config.inventory_page_size,
                page * config.inventory_page_size,
            ),
            self.call(self.service.showcase, guild_id, owner_id),
        )
        if total and page * config.inventory_page_size >= total:
            page = max(0, (total - 1) // config.inventory_page_size)
            items, total = await self.call(
                self.service.owned_page,
                guild_id,
                owner_id,
                config.inventory_page_size,
                page * config.inventory_page_size,
            )
        own = int(interaction.user.id) == int(owner_id)
        await self.replace(
            interaction,
            inventory_embed(
                owner_name,
                items,
                showcase,
                total,
                page,
                config.inventory_page_size,
                own=own,
            ),
            InventoryView(
                self,
                int(interaction.user.id),
                guild_id,
                owner_id,
                owner_name,
                page,
                items,
                showcase,
                total,
            ),
        )

    async def show_all(self, interaction: discord.Interaction, page: int) -> None:
        guild_id = int(interaction.guild_id or 0)
        config = self.config_for_guild(guild_id)
        page = max(0, int(page))
        await self.defer(interaction)
        items, total = await self.call(
            self.service.all_page,
            guild_id,
            config.all_trademarks_page_size,
            page * config.all_trademarks_page_size,
        )
        if total and page * config.all_trademarks_page_size >= total:
            page = max(0, (total - 1) // config.all_trademarks_page_size)
            items, total = await self.call(
                self.service.all_page,
                guild_id,
                config.all_trademarks_page_size,
                page * config.all_trademarks_page_size,
            )
        present_ids = await self.member_resolver.present_ids(
            interaction.guild,
            (item.owner_id for item in items),
            known_present=(int(interaction.user.id),),
        )
        await self.replace(
            interaction,
            all_trademarks_embed(
                items,
                total,
                page,
                config.all_trademarks_page_size,
                present_ids,
            ),
            AllTrademarksView(
                self,
                int(interaction.user.id),
                guild_id,
                page,
                items,
                total,
            ),
        )

    async def show_search(self, interaction: discord.Interaction, query: str) -> None:
        guild_id = int(interaction.guild_id or 0)
        config = self.config_for_guild(guild_id)
        await self.defer(interaction)
        try:
            items = await self.call(self.service.search, guild_id, query, config)
        except InvalidTrademarkName as error:
            await self.replace(
                interaction,
                result_embed("Не удалось найти", str(error), error=True),
                AllTrademarksView(
                    self,
                    int(interaction.user.id),
                    guild_id,
                    0,
                    [],
                    0,
                    search=str(query),
                ),
            )
            return
        present_ids = await self.member_resolver.present_ids(
            interaction.guild,
            (item.owner_id for item in items),
            known_present=(int(interaction.user.id),),
        )
        await self.replace(
            interaction,
            all_trademarks_embed(
                items,
                len(items),
                0,
                max(1, len(items)),
                present_ids,
                search=str(query),
            ),
            AllTrademarksView(
                self,
                int(interaction.user.id),
                guild_id,
                0,
                items,
                len(items),
                search=str(query),
            ),
        )

    async def show_card(
        self,
        interaction: discord.Interaction,
        trademark_id: str,
        back: BackTarget,
    ) -> None:
        guild_id = int(interaction.guild_id or 0)
        await self.defer(interaction)
        trademark = await self.call(self.service.get, guild_id, trademark_id)
        if trademark is None:
            await self.replace(
                interaction,
                result_embed(
                    "Данные изменились",
                    "Трейд марка больше не существует!",
                    error=True,
                ),
                MainView(self, int(interaction.user.id), guild_id),
            )
            return
        member = interaction.user
        administrator = isinstance(member, discord.Member) and member.guild_permissions.administrator
        present_ids = await self.member_resolver.present_ids(
            interaction.guild,
            (trademark.owner_id,),
            known_present=(int(interaction.user.id),),
        )
        await self.replace(
            interaction,
            trademark_card_embed(trademark, present_ids),
            CardView(
                self,
                int(interaction.user.id),
                guild_id,
                trademark,
                back,
                administrator=administrator,
            ),
        )

    async def show_back(self, interaction: discord.Interaction, target: BackTarget) -> None:
        if target.screen == "inventory":
            if target.owner_id is None or target.owner_name is None:
                await self.show_main(interaction)
                return
            await self.show_inventory(
                interaction,
                target.owner_id,
                target.owner_name,
                target.page,
            )
        elif target.screen == "all":
            await self.show_all(interaction, target.page)
        elif target.screen == "requests":
            await self.show_requests(interaction)
        elif target.screen == "request_list":
            await self.show_request_list(interaction, str(target.direction), target.page)
        elif target.screen == "card":
            await self.show_card(interaction, str(target.trademark_id), BackTarget("all"))
        else:
            await self.show_main(interaction)

    async def handle_claim(self, interaction: discord.Interaction, raw_name: str) -> None:
        guild_id = int(interaction.guild_id or 0)
        config = self.config_for_guild(guild_id)
        raw_name = resolve_interaction_custom_emojis(interaction, raw_name)
        await self.defer(interaction)
        try:
            result = await self.call(
                self.service.claim,
                guild_id,
                int(interaction.user.id),
                interaction.user.name,
                raw_name,
                config,
            )
        except InvalidTrademarkName as error:
            await self.replace(
                interaction,
                result_embed(
                    "Не удалось запатентовать",
                    str(error),
                    error=True,
                ),
                MainView(self, int(interaction.user.id), guild_id),
            )
            return
        trademark = result.trademark
        if result.status == "claimed" and trademark is not None:
            await self.publish(
                guild_id,
                config,
                patent_success_announcement(
                    trademark,
                    int(interaction.user.id),
                    {int(interaction.user.id)},
                ),
            )
            await self.replace(
                interaction,
                result_embed(
                    "Патент оформлен",
                    f"{trademark_name(trademark)} теперь принадлежит тебе!",
                ),
                CardView(
                    self,
                    int(interaction.user.id),
                    guild_id,
                    trademark,
                    BackTarget(
                        "inventory",
                        owner_id=int(interaction.user.id),
                        owner_name=interaction.user.name,
                    ),
                    administrator=False,
                ),
            )
            return
        if result.status == "occupied" and trademark is not None:
            present_ids = await self.member_resolver.present_ids(
                interaction.guild,
                (interaction.user.id, trademark.owner_id),
                known_present=(int(interaction.user.id),),
            )
            await self.publish(
                guild_id,
                config,
                patent_failure_announcement(
                    trademark,
                    int(interaction.user.id),
                    interaction.user.name,
                    present_ids,
                ),
            )
            member = interaction.user
            administrator = isinstance(member, discord.Member) and member.guild_permissions.administrator
            await self.replace(
                interaction,
                result_embed(
                    "Трейд марка уже запатентована",
                    occupied_claim_description(
                        trademark,
                        int(interaction.user.id),
                    ),
                    error=True,
                ),
                CardView(
                    self,
                    int(interaction.user.id),
                    guild_id,
                    trademark,
                    BackTarget("all"),
                    administrator=administrator,
                ),
            )
            return
        title, description = claim_rejection_copy(result, config)
        await self.replace(
            interaction,
            result_embed(title, description, error=True),
            MainView(self, int(interaction.user.id), guild_id),
        )

    async def handle_context_claim(
        self,
        interaction: discord.Interaction,
        message_id: int,
        display_name: str,
        source_content: str,
    ) -> None:
        guild_id = int(interaction.guild_id or 0)
        active = self.finish_confirmation(
            guild_id,
            message_id,
            int(interaction.user.id),
        )
        if not active:
            await self.replace(
                interaction,
                result_embed(
                    "Подтверждение устарело",
                    "Эту попытку патента больше нельзя продолжить!",
                    error=True,
                ),
                MainView(self, int(interaction.user.id), guild_id),
            )
            return
        await self.defer(interaction)
        channel = interaction.channel
        try:
            if channel is None or not hasattr(channel, "fetch_message"):
                raise TypeError("Interaction channel cannot fetch messages")
            message = await cast(Any, channel).fetch_message(int(message_id))
        except discord.NotFound:
            await self.replace(
                interaction,
                result_embed(
                    "Подтверждение устарело",
                    "Исходное сообщение было удалено!",
                    error=True,
                ),
                MainView(self, int(interaction.user.id), guild_id),
            )
            return
        except (discord.Forbidden, discord.HTTPException, TypeError):
            logger.exception(
                "cannot verify trademark source message",
                extra={
                    "guild_id": guild_id,
                    "channel_id": int(interaction.channel_id or 0),
                    "message_id": int(message_id),
                },
            )
            await self.replace(
                interaction,
                result_embed(
                    "Не удалось проверить сообщение",
                    "Попробуй открыть подтверждение ещё раз!",
                    error=True,
                ),
                MainView(self, int(interaction.user.id), guild_id),
            )
            return
        if int(message.author.id) != int(interaction.user.id) or message.content != source_content:
            await self.replace(
                interaction,
                result_embed(
                    "Подтверждение устарело",
                    "Исходное сообщение изменилось!",
                    error=True,
                ),
                MainView(self, int(interaction.user.id), guild_id),
            )
            return
        await self.handle_claim(interaction, display_name)

    async def show_claim_confirmation(self, interaction: discord.Interaction, trademark: Trademark) -> None:
        guild_id = int(interaction.guild_id or 0)
        await self.replace(
            interaction,
            discord.Embed(
                title="Патент трейд марки",
                description=f"Запатентовать {trademark_name(trademark)}?",
                color=0x5865F2,
            ),
            ConfirmationView(
                self,
                int(interaction.user.id),
                guild_id,
                trademark,
                "claim",
            ),
        )

    async def show_release_confirmation(
        self,
        interaction: discord.Interaction,
        trademark: Trademark,
        *,
        administrator: bool,
    ) -> None:
        guild_id = int(interaction.guild_id or 0)
        description = (
            f"Снять чужой патент с {trademark_name(trademark)}?"
            if administrator
            else (f"После снятия патента {trademark_name(trademark)} сможет запатентовать другой пользователь. История сохранится!")
        )
        await self.replace(
            interaction,
            discord.Embed(
                title=f"Снять патент с {trademark_name(trademark)}",
                description=description,
                color=0xED4245,
            ),
            ConfirmationView(
                self,
                int(interaction.user.id),
                guild_id,
                trademark,
                "admin_release" if administrator else "release",
            ),
        )

    async def handle_release(
        self,
        interaction: discord.Interaction,
        trademark_id: str,
        *,
        administrator: bool,
    ) -> None:
        guild_id = int(interaction.guild_id or 0)
        config = self.config_for_guild(guild_id)
        if administrator:
            member = interaction.user
            if not (isinstance(member, discord.Member) and member.guild_permissions.administrator):
                await self.replace(
                    interaction,
                    result_embed(
                        "Действие недоступно",
                        "Для снятия чужого патента нужны права администратора!",
                        error=True,
                    ),
                    MainView(self, int(interaction.user.id), guild_id),
                )
                return
        await self.defer(interaction)
        try:
            trademark = await self.call(
                self.service.release,
                guild_id,
                trademark_id,
                int(interaction.user.id),
                interaction.user.name,
                administrator=administrator,
            )
        except TrademarkOperationError as error:
            await self.replace(
                interaction,
                result_embed("Данные изменились", str(error), error=True),
                MainView(self, int(interaction.user.id), guild_id),
            )
            return
        await self.publish(
            guild_id,
            config,
            release_announcement(
                trademark,
                int(interaction.user.id),
                interaction.user.name,
                {int(interaction.user.id)},
            ),
        )
        await self.replace(
            interaction,
            result_embed(
                "Патент снят",
                f"{trademark_name(trademark)} больше никому не принадлежит!",
            ),
            CardView(
                self,
                int(interaction.user.id),
                guild_id,
                trademark,
                BackTarget("all"),
                administrator=administrator,
            ),
        )

    async def show_exchange_target_marks(
        self,
        interaction: discord.Interaction,
        offered_ids: tuple[str, ...],
        requested_ids: tuple[str, ...],
        recipient_id: int,
        recipient_name: str,
        page: int,
    ) -> None:
        guild_id = int(interaction.guild_id or 0)
        config = self.config_for_guild(guild_id)
        await self.defer(interaction)
        items, total = await self.call(
            self.service.owned_page_excluding,
            guild_id,
            recipient_id,
            requested_ids,
            config.inventory_page_size,
            page * config.inventory_page_size,
        )
        present_ids = await self.member_resolver.present_ids(
            interaction.guild,
            (recipient_id,),
            known_present=(int(interaction.user.id),),
        )
        recipient = member_name(recipient_id, recipient_name, present_ids)
        if not items:
            description = "У пользователя нет подходящих трейд марок!"
        elif requested_ids:
            description = f"Выбери ещё одну трейд марку пользователя {recipient}!"
        else:
            description = f"Выбери трейд марку пользователя {recipient}!"
        embed = discord.Embed(
            title="Обмен трейд марки",
            description=description,
            color=0x5865F2,
        )
        await self.replace(
            interaction,
            embed,
            MarkChoiceView(
                self,
                int(interaction.user.id),
                guild_id,
                "exchange_target",
                items,
                page,
                total,
                offered_ids=offered_ids,
                requested_ids=requested_ids,
                counterpart_id=recipient_id,
                counterpart_name=recipient_name,
            ),
        )

    async def show_exchange_offer_marks(
        self,
        interaction: discord.Interaction,
        offered_ids: tuple[str, ...],
        requested_ids: tuple[str, ...],
        recipient_id: int,
        recipient_name: str,
        page: int,
    ) -> None:
        guild_id = int(interaction.guild_id or 0)
        config = self.config_for_guild(guild_id)
        await self.defer(interaction)
        items, total = await self.call(
            self.service.owned_page_excluding,
            guild_id,
            int(interaction.user.id),
            offered_ids,
            config.inventory_page_size,
            page * config.inventory_page_size,
        )
        if not items:
            description = "У тебя нет подходящих трейд марок!"
        elif offered_ids:
            description = "Выбери ещё одну свою трейд марку!"
        else:
            description = "Выбери свою трейд марку!"
        embed = discord.Embed(
            title="Предложение обмена",
            description=description,
            color=0x5865F2,
        )
        await self.replace(
            interaction,
            embed,
            MarkChoiceView(
                self,
                int(interaction.user.id),
                guild_id,
                "exchange_offer",
                items,
                page,
                total,
                offered_ids=offered_ids,
                requested_ids=requested_ids,
                counterpart_id=recipient_id,
                counterpart_name=recipient_name,
            ),
        )

    async def show_exchange_confirmation(
        self,
        interaction: discord.Interaction,
        offered_ids: tuple[str, ...],
        requested_ids: tuple[str, ...],
        recipient_id: int,
        recipient_name: str,
    ) -> None:
        guild_id = int(interaction.guild_id or 0)
        await self.defer(interaction)
        offered, requested = await asyncio.gather(
            self.call(self.service.get_many, guild_id, offered_ids),
            self.call(self.service.get_many, guild_id, requested_ids),
        )
        if (
            len(offered) != len(offered_ids)
            or len(requested) != len(requested_ids)
            or any(item.owner_id != int(interaction.user.id) for item in offered)
            or any(item.owner_id != recipient_id for item in requested)
        ):
            await self.replace(
                interaction,
                result_embed(
                    "Данные изменились",
                    "Одна из трейд марок больше недоступна!",
                    error=True,
                ),
                MainView(self, int(interaction.user.id), guild_id),
            )
            return
        present_ids = await self.member_resolver.present_ids(
            interaction.guild,
            (recipient_id,),
            known_present=(int(interaction.user.id),),
        )
        embed = discord.Embed(title="Подтверждение обмена", color=0x5865F2)
        embed.add_field(
            name="Ты отдаёшь",
            value=trademark_names(offered),
            inline=False,
        )
        embed.add_field(
            name="Ты получаешь",
            value=trademark_names(requested),
            inline=False,
        )
        embed.add_field(
            name="Получатель запроса",
            value=member_name(recipient_id, recipient_name, present_ids),
            inline=False,
        )
        await self.replace(
            interaction,
            embed,
            ExchangeConfirmView(
                self,
                int(interaction.user.id),
                guild_id,
                offered,
                requested,
                recipient_id,
                recipient_name,
            ),
        )

    async def handle_exchange_create(
        self,
        interaction: discord.Interaction,
        offered_ids: tuple[str, ...],
        requested_ids: tuple[str, ...],
        recipient_id: int,
        recipient_name: str,
    ) -> None:
        guild_id = int(interaction.guild_id or 0)
        config = self.config_for_guild(guild_id)
        await self.defer(interaction)
        try:
            await self.call(
                self.service.create_exchange,
                guild_id,
                int(interaction.user.id),
                interaction.user.name,
                recipient_id,
                recipient_name,
                offered_ids,
                requested_ids,
                config.channel_id,
                config,
            )
        except (
            TrademarkInvalidRequest,
            TrademarkRequestLimitReached,
            TrademarkDuplicateRequest,
        ) as error:
            await self.replace(
                interaction,
                result_embed("Обмен недоступен", str(error), error=True),
                MainView(self, int(interaction.user.id), guild_id),
            )
            return
        present_ids = await self.member_resolver.present_ids(
            interaction.guild,
            (recipient_id,),
            known_present=(int(interaction.user.id),),
        )
        await self.replace(
            interaction,
            result_embed(
                "Запрос отправлен",
                f"{member_name(recipient_id, recipient_name, present_ids)} увидит предложение в разделе запросов!",
            ),
            RequestsOverviewView(self, int(interaction.user.id), guild_id),
        )

    async def show_gift_confirmation(
        self,
        interaction: discord.Interaction,
        trademark_id: str,
        recipient_id: int,
        recipient_name: str,
    ) -> None:
        guild_id = int(interaction.guild_id or 0)
        await self.defer(interaction)
        trademark = await self.call(self.service.get, guild_id, trademark_id)
        if trademark is None:
            await self.replace(
                interaction,
                result_embed(
                    "Данные изменились",
                    "Трейд марка больше недоступна!",
                    error=True,
                ),
                MainView(self, int(interaction.user.id), guild_id),
            )
            return
        present_ids = await self.member_resolver.present_ids(
            interaction.guild,
            (recipient_id,),
            known_present=(int(interaction.user.id),),
        )
        embed = discord.Embed(title="Подтверждение подарка", color=0x5865F2)
        embed.add_field(name="Трейд марка", value=trademark_name(trademark), inline=False)
        embed.add_field(
            name="Получатель",
            value=member_name(recipient_id, recipient_name, present_ids),
            inline=False,
        )
        await self.replace(
            interaction,
            embed,
            GiftConfirmView(
                self,
                int(interaction.user.id),
                guild_id,
                trademark,
                recipient_id,
                recipient_name,
            ),
        )

    async def handle_gift_create(
        self,
        interaction: discord.Interaction,
        trademark_id: str,
        recipient_id: int,
        recipient_name: str,
    ) -> None:
        guild_id = int(interaction.guild_id or 0)
        config = self.config_for_guild(guild_id)
        await self.defer(interaction)
        try:
            await self.call(
                self.service.create_gift,
                guild_id,
                int(interaction.user.id),
                interaction.user.name,
                recipient_id,
                recipient_name,
                trademark_id,
                config.channel_id,
                config,
            )
        except (
            TrademarkInvalidRequest,
            TrademarkRequestLimitReached,
            TrademarkDuplicateRequest,
        ) as error:
            await self.replace(
                interaction,
                result_embed("Подарок недоступен", str(error), error=True),
                MainView(self, int(interaction.user.id), guild_id),
            )
            return
        present_ids = await self.member_resolver.present_ids(
            interaction.guild,
            (recipient_id,),
            known_present=(int(interaction.user.id),),
        )
        await self.replace(
            interaction,
            result_embed(
                "Предложение подарка отправлено",
                f"{member_name(recipient_id, recipient_name, present_ids)} увидит предложение в разделе запросов!",
            ),
            RequestsOverviewView(self, int(interaction.user.id), guild_id),
        )

    async def show_requests(self, interaction: discord.Interaction) -> None:
        guild_id = int(interaction.guild_id or 0)
        await self.defer(interaction)
        incoming, outgoing = await self.call(self.service.request_counts, guild_id, int(interaction.user.id))
        await self.replace(
            interaction,
            requests_overview_embed(incoming, outgoing),
            RequestsOverviewView(self, int(interaction.user.id), guild_id),
        )

    async def show_request_list(self, interaction: discord.Interaction, direction: str, page: int) -> None:
        guild_id = int(interaction.guild_id or 0)
        config = self.config_for_guild(guild_id)
        page = max(0, int(page))
        await self.defer(interaction)
        listings, total = await self.call(
            self.service.request_page,
            guild_id,
            int(interaction.user.id),
            direction,
            config.requests_page_size,
            page * config.requests_page_size,
        )
        present_ids = await self.member_resolver.present_ids(
            interaction.guild,
            (listing.request.sender_id if direction == "incoming" else listing.request.recipient_id for listing in listings),
            known_present=(int(interaction.user.id),),
        )
        await self.replace(
            interaction,
            requests_page_embed(
                listings,
                total,
                page,
                config.requests_page_size,
                direction,
                present_ids,
            ),
            RequestListView(
                self,
                int(interaction.user.id),
                guild_id,
                direction,
                page,
                listings,
                total,
            ),
        )

    async def show_request_card(
        self,
        interaction: discord.Interaction,
        request_id: str,
        direction: str,
        page: int,
    ) -> None:
        guild_id = int(interaction.guild_id or 0)
        await self.defer(interaction)
        try:
            request, offered, requested = await self.call(self.service.request, guild_id, request_id)
        except TrademarkNotFound as error:
            await self.replace(
                interaction,
                result_embed("Запрос недоступен", str(error), error=True),
                RequestsOverviewView(self, int(interaction.user.id), guild_id),
            )
            return
        listing = TrademarkRequestListing(
            request=request,
            offered_names=tuple(item.display_name for item in offered),
            requested_names=tuple(item.display_name for item in requested),
        )
        present_ids = await self.member_resolver.present_ids(
            interaction.guild,
            (request.sender_id, request.recipient_id),
            known_present=(int(interaction.user.id),),
        )
        await self.replace(
            interaction,
            request_card_embed(listing, direction, present_ids),
            RequestCardView(
                self,
                int(interaction.user.id),
                guild_id,
                listing,
                direction,
                page,
            ),
        )

    async def handle_request_accept(self, interaction: discord.Interaction, request_id: str) -> None:
        guild_id = int(interaction.guild_id or 0)
        config = self.config_for_guild(guild_id)
        await self.defer(interaction)
        result = await self.call(
            self.service.accept_request,
            guild_id,
            request_id,
            int(interaction.user.id),
            interaction.user.name,
            config,
        )
        if result.status == "completed" and result.offered:
            if result.request and result.request.request_type == "exchange":
                requested = result.requested
                present_ids = await self.member_resolver.present_ids(
                    interaction.guild,
                    (
                        result.request.sender_id,
                        result.request.recipient_id,
                    ),
                    known_present=(int(interaction.user.id),),
                )
                await self.publish(
                    guild_id,
                    config,
                    exchange_success_announcement(
                        result.request,
                        result.offered,
                        requested,
                        present_ids,
                    ),
                )
                title = "Обмен завершён"
                description = (
                    f"Ты получил "
                    f"{trademark_names(result.offered, separator=', ')}!\n"
                    f"{member_name(result.request.sender_id, result.request.sender_name, present_ids)} "
                    f"получил {trademark_names(requested, separator=', ')}!"
                )
            else:
                title = "Подарок принят"
                description = f"Ты получил {trademark_name(result.offered[0])}!"
            await self.replace(
                interaction,
                result_embed(title, description),
                RequestsOverviewView(self, int(interaction.user.id), guild_id),
            )
            return
        messages = {
            "expired": ("Запрос недействителен", "Срок действия запроса истёк!"),
            "invalid": (
                "Запрос недействителен",
                "Одна из трейд марок больше не принадлежит участнику!",
            ),
            "already_processed": (
                "Запрос уже обработан",
                "Это предложение больше не ожидает ответа!",
            ),
            "inventory_full": (
                "Инвентарь заполнен",
                "У одного из участников не хватает места для завершения запроса!",
            ),
        }
        title, description = messages[result.status]
        await self.replace(
            interaction,
            result_embed(title, description, error=True),
            RequestsOverviewView(self, int(interaction.user.id), guild_id),
        )

    async def handle_request_decline(self, interaction: discord.Interaction, request_id: str) -> None:
        await self._handle_request_close(interaction, request_id, "decline_request", "Запрос отклонён")

    async def handle_request_cancel(self, interaction: discord.Interaction, request_id: str) -> None:
        await self._handle_request_close(interaction, request_id, "cancel_request", "Запрос отменён")

    async def _handle_request_close(
        self,
        interaction: discord.Interaction,
        request_id: str,
        method_name: str,
        title: str,
    ) -> None:
        guild_id = int(interaction.guild_id or 0)
        await self.defer(interaction)
        try:
            await self.call(
                getattr(self.service, method_name),
                guild_id,
                request_id,
                int(interaction.user.id),
                interaction.user.name,
            )
        except TrademarkOperationError as error:
            await self.replace(
                interaction,
                result_embed("Запрос уже обработан", str(error), error=True),
                RequestsOverviewView(self, int(interaction.user.id), guild_id),
            )
            return
        await self.replace(
            interaction,
            result_embed(title, "Предложение закрыто!"),
            RequestsOverviewView(self, int(interaction.user.id), guild_id),
        )

    async def show_history(self, interaction: discord.Interaction, trademark_id: str, page: int) -> None:
        guild_id = int(interaction.guild_id or 0)
        config = self.config_for_guild(guild_id)
        page = max(0, int(page))
        await self.defer(interaction)
        trademark, history_data = await asyncio.gather(
            self.call(self.service.get, guild_id, trademark_id),
            self.call(
                self.service.history_page,
                guild_id,
                trademark_id,
                config.history_page_size,
                page * config.history_page_size,
            ),
        )
        if trademark is None:
            await self.replace(
                interaction,
                result_embed("Данные изменились", "Трейд марка не найдена!", error=True),
                MainView(self, int(interaction.user.id), guild_id),
            )
            return
        events, total = history_data
        present_ids = await self.member_resolver.present_ids(
            interaction.guild,
            (
                user_id
                for event in events
                for user_id in (
                    event.actor_id,
                    event.from_user_id,
                    event.to_user_id,
                )
            ),
            known_present=(int(interaction.user.id),),
        )
        await self.replace(
            interaction,
            history_embed(
                trademark,
                events,
                total,
                page,
                config.history_page_size,
                present_ids,
            ),
            HistoryView(
                self,
                int(interaction.user.id),
                guild_id,
                trademark_id,
                page,
                total,
            ),
        )

    async def show_showcase_settings(self, interaction: discord.Interaction) -> None:
        guild_id = int(interaction.guild_id or 0)
        config = self.config_for_guild(guild_id)
        await self.defer(interaction)
        items = await self.call(self.service.showcase, guild_id, int(interaction.user.id))
        description = (
            "\n".join(f"{index}. {trademark_name(item)}" for index, item in enumerate(items, start=1)) if items else "Витрина пока пуста"
        )
        embed = discord.Embed(
            title="Настройка витрины",
            description=description,
            color=0x5865F2,
        )
        embed.add_field(
            name="Закреплено",
            value=f"{len(items)} из {config.showcase_limit}",
            inline=False,
        )
        await self.replace(
            interaction,
            embed,
            ShowcaseSettingsView(self, int(interaction.user.id), guild_id, items),
        )

    async def show_showcase_add(self, interaction: discord.Interaction, page: int) -> None:
        guild_id = int(interaction.guild_id or 0)
        config = self.config_for_guild(guild_id)
        await self.defer(interaction)
        (items, total), showcase = await asyncio.gather(
            self.call(
                self.service.owned_page,
                guild_id,
                int(interaction.user.id),
                config.inventory_page_size,
                page * config.inventory_page_size,
            ),
            self.call(self.service.showcase, guild_id, int(interaction.user.id)),
        )
        showcase_ids = {item.id for item in showcase}
        available = [item for item in items if item.id not in showcase_ids]
        await self.replace(
            interaction,
            discord.Embed(
                title="Добавить на витрину",
                description=("Выбери одну трейд марку!" if available else "На этой странице нет доступных трейд марок!"),
                color=0x5865F2,
            ),
            MarkChoiceView(
                self,
                int(interaction.user.id),
                guild_id,
                "showcase_add",
                available,
                page,
                total,
            ),
        )

    async def handle_showcase_add(self, interaction: discord.Interaction, trademark_id: str) -> None:
        guild_id = int(interaction.guild_id or 0)
        config = self.config_for_guild(guild_id)
        await self.defer(interaction)
        try:
            await self.call(
                self.service.add_to_showcase,
                guild_id,
                int(interaction.user.id),
                trademark_id,
                config,
            )
        except (TrademarkOperationError, TrademarkShowcaseFull) as error:
            await self.replace(
                interaction,
                result_embed("Не удалось изменить витрину", str(error), error=True),
                MainView(self, int(interaction.user.id), guild_id),
            )
            return
        await self.show_showcase_settings(interaction)

    async def handle_showcase_remove(self, interaction: discord.Interaction, trademark_id: str) -> None:
        guild_id = int(interaction.guild_id or 0)
        await self.defer(interaction)
        await self.call(
            self.service.remove_from_showcase,
            guild_id,
            int(interaction.user.id),
            trademark_id,
        )
        await self.show_showcase_settings(interaction)

    async def show_showcase_positions(self, interaction: discord.Interaction, trademark_id: str, count: int) -> None:
        guild_id = int(interaction.guild_id or 0)
        await self.replace(
            interaction,
            discord.Embed(
                title="Изменить порядок",
                description="Выбери новое место!",
                color=0x5865F2,
            ),
            ShowcasePositionView(
                self,
                int(interaction.user.id),
                guild_id,
                trademark_id,
                count,
            ),
        )

    async def handle_showcase_move(
        self,
        interaction: discord.Interaction,
        trademark_id: str,
        position: int,
    ) -> None:
        guild_id = int(interaction.guild_id or 0)
        await self.defer(interaction)
        await self.call(
            self.service.move_in_showcase,
            guild_id,
            int(interaction.user.id),
            trademark_id,
            position,
        )
        await self.show_showcase_settings(interaction)

    def finish_confirmation(self, guild_id: int, message_id: int, user_id: int) -> bool:
        key = (int(guild_id), int(message_id), int(user_id))
        if key not in self.confirmations:
            return False
        self.confirmations.remove(key)
        return True

    @app_commands.command(name="tm", description="Open the trademark system.")
    @app_commands.guild_only()
    async def tm(self, interaction: discord.Interaction) -> None:
        if self.command_context(interaction) is None:
            await self.reject_context(interaction)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.show_main(interaction)

    async def patent_message(self, interaction: discord.Interaction, message: discord.Message) -> None:
        context = self.command_context(interaction)
        if context is None:
            await self.reject_context(interaction)
            return
        guild_id, config = context
        if int(message.channel.id) != config.channel_id:
            await self.reject_context(interaction)
            return
        if int(message.author.id) != int(interaction.user.id):
            await interaction.response.send_message(
                "Можно запатентовать только собственное сообщение!",
                ephemeral=True,
            )
            return
        try:
            normalized = parse_message_trademark(message.content, config)
        except InvalidTrademarkName as error:
            await interaction.response.send_message(
                embed=result_embed(
                    "Не удалось распознать трейд марку",
                    str(error),
                    error=True,
                ),
                ephemeral=True,
            )
            return
        key = (guild_id, int(message.id), int(interaction.user.id))
        if key in self.confirmations:
            await interaction.response.send_message(
                "Для этого сообщения уже открыто подтверждение!",
                ephemeral=True,
            )
            return
        self.confirmations.add(key)
        view = ContextPatentView(
            self,
            int(interaction.user.id),
            guild_id,
            int(message.id),
            normalized.display,
            message.content,
        )
        await interaction.response.send_message(
            embed=discord.Embed(
                title="Патент трейд марки",
                description=(f"{interaction.user.mention}, ты пытаешься запатентовать {safe_display(normalized.display)}™ на своё имя?"),
                color=0x5865F2,
            ),
            view=view,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        view.message = await interaction.original_response()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(TrademarkCog(bot))
