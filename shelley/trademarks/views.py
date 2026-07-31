from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

import discord

from .models import MAX_EXCHANGE_SIDE_MARKS, Trademark, TrademarkRequestListing
from .normalization import CUSTOM_EMOJI_PATTERN
from .presenters import result_embed, trademark_display_names

if TYPE_CHECKING:
    from .cog import TrademarkCog


logger = logging.getLogger(__name__)
SELF_SELECTION_MESSAGE = "Ну ты дед бом-бом я балдю... Нельзя выбрать самого себя!"


def component_label(value: str, limit: int = 80) -> str:
    plain = CUSTOM_EMOJI_PATTERN.sub(lambda match: f":{match.group('name')}:", str(value))
    plain = discord.utils.escape_mentions(plain).replace("\n", " ").strip()
    if len(plain) <= limit:
        return plain
    return plain[: max(1, limit - 1)] + "…"


def component_presentation(value: str, limit: int = 80, suffix: str = "") -> tuple[str, discord.PartialEmoji | None]:
    raw = str(value)
    match = CUSTOM_EMOJI_PATTERN.search(raw)
    emoji = None
    if match is not None:
        emoji = discord.PartialEmoji(
            name=match.group("name"),
            animated=bool(match.group("animated")),
            id=int(match.group("id")),
        )
        raw = raw[: match.start()] + raw[match.end() :]
    label = component_label(raw, max(1, limit - len(suffix)))
    label = " ".join(label.split())
    if not label and not suffix and match is not None:
        label = f":{match.group('name')}:"
    return f"{label}{suffix}", emoji


def trademark_component(trademark: Trademark, limit: int = 80) -> tuple[str, discord.PartialEmoji | None]:
    return component_presentation(trademark.display_name, limit, "™")


@dataclass(frozen=True, slots=True)
class BackTarget:
    screen: Literal["main", "inventory", "all", "requests", "request_list", "card"]
    owner_id: int | None = None
    owner_name: str | None = None
    page: int = 0
    direction: str | None = None
    trademark_id: str | None = None


class TrademarkView(discord.ui.View):
    def __init__(self, cog: TrademarkCog, user_id: int, guild_id: int) -> None:
        self.cog = cog
        self.user_id = int(user_id)
        self.guild_id = int(guild_id)
        self.guild_config = cog.config_for_guild(self.guild_id)
        super().__init__(timeout=self.guild_config.confirmation_timeout_seconds)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if int(interaction.user.id) == self.user_id:
            return True
        await interaction.response.send_message(
            "Это приватное окно принадлежит другому пользователю!",
            ephemeral=True,
        )
        return False

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: discord.ui.Item,
    ) -> None:
        logger.exception(
            "trademark interaction failed",
            exc_info=(type(error), error, error.__traceback__),
            extra={
                "guild_id": int(interaction.guild_id or 0),
                "user_id": int(interaction.user.id),
                "component": getattr(item, "custom_id", None),
            },
        )
        embed = result_embed(
            "Не удалось выполнить действие",
            "Shelley не смогла завершить действие. Попробуй ещё раз!",
            error=True,
        )
        try:
            await self.cog.replace(
                interaction,
                embed,
                MainView(self.cog, self.user_id, self.guild_id),
            )
        except discord.HTTPException:
            logger.exception("cannot show trademark error response")


class MainView(TrademarkView):
    @discord.ui.button(label="Запатентовать", style=discord.ButtonStyle.success, row=0)
    async def patent(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await interaction.response.send_modal(PatentModal(self.cog, self.user_id))

    @discord.ui.button(label="Мой инвентарь", style=discord.ButtonStyle.primary, row=0)
    async def inventory(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self.cog.show_inventory(
            interaction,
            int(interaction.user.id),
            interaction.user.name,
            0,
        )

    @discord.ui.button(label="Инвентарь пользователя", style=discord.ButtonStyle.secondary, row=1)
    async def other_inventory(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self.cog.replace(
            interaction,
            discord.Embed(
                title="Инвентарь пользователя",
                description="Выбери пользователя",
                color=0x5865F2,
            ),
            UserPickerView(self.cog, self.user_id, self.guild_id, "inventory"),
        )

    @discord.ui.button(label="Все трейд марки", style=discord.ButtonStyle.secondary, row=1)
    async def all_trademarks(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self.cog.show_all(interaction, 0)

    @discord.ui.button(label="Запросы", style=discord.ButtonStyle.secondary, row=0)
    async def requests(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self.cog.show_requests(interaction)


class PatentModal(discord.ui.Modal):
    def __init__(self, cog: TrademarkCog, user_id: int) -> None:
        self.cog = cog
        self.user_id = int(user_id)
        super().__init__(title="Запатентовать трейд марку")
        self.name_input: discord.ui.TextInput = discord.ui.TextInput(
            label="Трейд марка",
            placeholder="Пример: Семя Кирилла",
            required=True,
            max_length=4000,
        )
        self.add_item(self.name_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if int(interaction.user.id) != self.user_id:
            await interaction.response.send_message(
                "Это приватное окно принадлежит другому пользователю!",
                ephemeral=True,
            )
            return
        await self.cog.handle_claim(interaction, str(self.name_input.value))


class SearchModal(discord.ui.Modal):
    def __init__(self, cog: TrademarkCog, user_id: int) -> None:
        self.cog = cog
        self.user_id = int(user_id)
        super().__init__(title="Найти трейд марку")
        self.query_input: discord.ui.TextInput = discord.ui.TextInput(
            label="Название",
            required=True,
            max_length=4000,
        )
        self.add_item(self.query_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if int(interaction.user.id) != self.user_id:
            await interaction.response.send_message(
                "Это приватное окно принадлежит другому пользователю!",
                ephemeral=True,
            )
            return
        await self.cog.show_search(interaction, str(self.query_input.value))


class UserPicker(discord.ui.UserSelect):
    def __init__(self, view: UserPickerView) -> None:
        self.parent_view = view
        super().__init__(
            placeholder="Выбери пользователя",
            min_values=1,
            max_values=1,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        user = self.values[0]
        if user.bot:
            await interaction.response.send_message(
                "Боты не участвуют в системе трейд марок!",
                ephemeral=True,
            )
            return
        if int(user.id) == self.parent_view.user_id and self.parent_view.action != "inventory":
            await interaction.response.send_message(
                SELF_SELECTION_MESSAGE,
                ephemeral=True,
            )
            return
        await self.parent_view.selected(interaction, user)


class UserPickerView(TrademarkView):
    def __init__(
        self,
        cog: TrademarkCog,
        user_id: int,
        guild_id: int,
        action: Literal["inventory", "gift", "exchange"],
        trademark_id: str | None = None,
    ) -> None:
        self.action = action
        self.trademark_id = trademark_id
        super().__init__(cog, user_id, guild_id)
        self.add_item(UserPicker(self))

    async def selected(self, interaction: discord.Interaction, user: discord.User | discord.Member) -> None:
        if self.action == "inventory":
            await self.cog.show_inventory(interaction, int(user.id), user.name, 0)
            return
        if self.action == "gift":
            await self.cog.show_gift_confirmation(
                interaction,
                str(self.trademark_id),
                int(user.id),
                user.name,
            )
            return
        await self.cog.show_exchange_target_marks(
            interaction,
            (str(self.trademark_id),),
            (),
            int(user.id),
            user.name,
            0,
        )

    @discord.ui.button(label="Назад", style=discord.ButtonStyle.secondary, row=1)
    async def back(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if self.trademark_id:
            await self.cog.show_card(
                interaction,
                self.trademark_id,
                BackTarget("inventory", owner_id=self.user_id, owner_name=interaction.user.name),
            )
        else:
            await self.cog.show_main(interaction)


class TrademarkSelect(discord.ui.Select):
    def __init__(
        self,
        view: TrademarkListView,
        trademarks: list[Trademark],
        placeholder: str = "Выбери трейд марку",
    ) -> None:
        self.parent_view = view
        options = []
        for item in trademarks:
            label, emoji = trademark_component(item, 100)
            status = "Свободна" if item.owner_id is None else f"@{item.owner_name}" if item.owner_name else "Занята"
            options.append(
                discord.SelectOption(
                    label=label,
                    value=item.id,
                    description=component_label(status, 100),
                    emoji=emoji,
                )
            )
        super().__init__(
            placeholder=placeholder,
            options=options,
            min_values=1,
            max_values=1,
            row=1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.parent_view.selected_trademark(interaction, self.values[0])


class ShowcaseTrademarkButton(discord.ui.Button):
    def __init__(
        self,
        parent_view: InventoryView,
        trademark: Trademark,
        index: int,
    ) -> None:
        self.parent_view = parent_view
        self.trademark_id = trademark.id
        label, emoji = trademark_component(trademark)
        super().__init__(
            label=label,
            emoji=emoji,
            style=discord.ButtonStyle.secondary,
            row=0,
            custom_id=f"tm:showcase:{index}:{trademark.id}",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.parent_view.selected_trademark(interaction, self.trademark_id)


class TrademarkListView(TrademarkView):
    async def selected_trademark(self, interaction: discord.Interaction, trademark_id: str) -> None:
        raise NotImplementedError


class InventoryView(TrademarkListView):
    def __init__(
        self,
        cog: TrademarkCog,
        user_id: int,
        guild_id: int,
        owner_id: int,
        owner_name: str,
        page: int,
        items: list[Trademark],
        showcase: list[Trademark],
        total: int,
    ) -> None:
        self.owner_id = int(owner_id)
        self.owner_name = owner_name
        self.page = int(page)
        self.total = int(total)
        self.own = self.owner_id == int(user_id)
        super().__init__(cog, user_id, guild_id)
        for index, item in enumerate(showcase[:5]):
            self.add_item(ShowcaseTrademarkButton(self, item, index))
        if items:
            self.add_item(TrademarkSelect(self, items))
        if not self.own:
            self.remove_item(self.configure_showcase)
            self.remove_item(self.patent)
        elif total:
            self.remove_item(self.patent)
        else:
            self.remove_item(self.configure_showcase)
        self.previous.disabled = self.page <= 0
        self.next.disabled = (self.page + 1) * self.guild_config.inventory_page_size >= self.total

    async def selected_trademark(self, interaction: discord.Interaction, trademark_id: str) -> None:
        await self.cog.show_card(
            interaction,
            trademark_id,
            BackTarget(
                "inventory",
                owner_id=self.owner_id,
                owner_name=self.owner_name,
                page=self.page,
            ),
        )

    @discord.ui.button(label="Настроить витрину", style=discord.ButtonStyle.primary, row=2)
    async def configure_showcase(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self.cog.show_showcase_settings(interaction)

    @discord.ui.button(label="Запатентовать", style=discord.ButtonStyle.success, row=2)
    async def patent(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await interaction.response.send_modal(PatentModal(self.cog, self.user_id))

    @discord.ui.button(label="Предыдущая", style=discord.ButtonStyle.secondary, row=3)
    async def previous(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self.cog.show_inventory(interaction, self.owner_id, self.owner_name, self.page - 1)

    @discord.ui.button(label="Следующая", style=discord.ButtonStyle.success, row=3)
    async def next(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self.cog.show_inventory(interaction, self.owner_id, self.owner_name, self.page + 1)

    @discord.ui.button(label="В меню", style=discord.ButtonStyle.secondary, row=4)
    async def menu(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self.cog.show_main(interaction)


class AllTrademarksView(TrademarkListView):
    def __init__(
        self,
        cog: TrademarkCog,
        user_id: int,
        guild_id: int,
        page: int,
        items: list[Trademark],
        total: int,
        *,
        search: str | None = None,
    ) -> None:
        self.page = int(page)
        self.total = int(total)
        self.search = search
        super().__init__(cog, user_id, guild_id)
        if items:
            self.add_item(TrademarkSelect(self, items))
        if search is not None:
            self.remove_item(self.previous)
            self.remove_item(self.next)
        else:
            self.previous.disabled = self.page <= 0
            self.next.disabled = (self.page + 1) * self.guild_config.all_trademarks_page_size >= self.total

    async def selected_trademark(self, interaction: discord.Interaction, trademark_id: str) -> None:
        await self.cog.show_card(
            interaction,
            trademark_id,
            BackTarget("all", page=self.page),
        )

    @discord.ui.button(label="Поиск", style=discord.ButtonStyle.primary, row=2)
    async def search_button(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await interaction.response.send_modal(SearchModal(self.cog, self.user_id))

    @discord.ui.button(label="Предыдущая", style=discord.ButtonStyle.secondary, row=3)
    async def previous(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self.cog.show_all(interaction, self.page - 1)

    @discord.ui.button(label="Следующая", style=discord.ButtonStyle.success, row=3)
    async def next(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self.cog.show_all(interaction, self.page + 1)

    @discord.ui.button(label="В меню", style=discord.ButtonStyle.secondary, row=4)
    async def menu(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self.cog.show_main(interaction)


class CardView(TrademarkView):
    def __init__(
        self,
        cog: TrademarkCog,
        user_id: int,
        guild_id: int,
        trademark: Trademark,
        back: BackTarget,
        *,
        administrator: bool,
    ) -> None:
        self.trademark = trademark
        self.back_target = back
        super().__init__(cog, user_id, guild_id)
        own = trademark.owner_id == user_id
        free = trademark.owner_id is None
        if not own:
            self.remove_item(self.exchange)
            self.remove_item(self.gift)
            self.remove_item(self.release)
        if own or free:
            self.remove_item(self.propose_exchange)
        if not free:
            self.remove_item(self.claim)
        if not administrator or own or free:
            self.remove_item(self.admin_release)

    @discord.ui.button(label="Обменять", style=discord.ButtonStyle.primary, row=0)
    async def exchange(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self.cog.replace(
            interaction,
            discord.Embed(
                title="Обмен трейд марки",
                description="Выбери пользователя",
                color=0x5865F2,
            ),
            UserPickerView(
                self.cog,
                self.user_id,
                self.guild_id,
                "exchange",
                self.trademark.id,
            ),
        )

    @discord.ui.button(label="Подарить", style=discord.ButtonStyle.success, row=0)
    async def gift(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self.cog.replace(
            interaction,
            discord.Embed(
                title="Подарок",
                description="Выбери получателя",
                color=0x5865F2,
            ),
            UserPickerView(
                self.cog,
                self.user_id,
                self.guild_id,
                "gift",
                self.trademark.id,
            ),
        )

    @discord.ui.button(label="Предложить обмен", style=discord.ButtonStyle.primary, row=0)
    async def propose_exchange(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if self.trademark.owner_id is None:
            await self.cog.show_card(interaction, self.trademark.id, self.back_target)
            return
        await self.cog.show_exchange_offer_marks(
            interaction,
            (),
            (self.trademark.id,),
            int(self.trademark.owner_id),
            str(self.trademark.owner_name),
            0,
        )

    @discord.ui.button(label="Запатентовать", style=discord.ButtonStyle.success, row=0)
    async def claim(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self.cog.show_claim_confirmation(interaction, self.trademark)

    @discord.ui.button(label="Снять патент", style=discord.ButtonStyle.danger, row=1)
    async def release(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self.cog.show_release_confirmation(interaction, self.trademark, administrator=False)

    @discord.ui.button(label="Снять чужой патент", style=discord.ButtonStyle.danger, row=1)
    async def admin_release(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self.cog.show_release_confirmation(interaction, self.trademark, administrator=True)

    @discord.ui.button(label="История", style=discord.ButtonStyle.secondary, row=2)
    async def history(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self.cog.show_history(interaction, self.trademark.id, 0)

    @discord.ui.button(label="Назад", style=discord.ButtonStyle.secondary, row=3)
    async def back(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self.cog.show_back(interaction, self.back_target)


class ConfirmationView(TrademarkView):
    def __init__(
        self,
        cog: TrademarkCog,
        user_id: int,
        guild_id: int,
        trademark: Trademark,
        action: Literal["claim", "release", "admin_release"],
    ) -> None:
        self.trademark = trademark
        self.action = action
        super().__init__(cog, user_id, guild_id)
        self.confirm.label = "Запатентовать" if action == "claim" else "Снять патент"

    @discord.ui.button(label="Подтвердить", style=discord.ButtonStyle.danger, row=0)
    async def confirm(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if self.action == "claim":
            await self.cog.handle_claim(interaction, self.trademark.display_name)
            return
        await self.cog.handle_release(
            interaction,
            self.trademark.id,
            administrator=self.action == "admin_release",
        )

    @discord.ui.button(label="Отмена", style=discord.ButtonStyle.secondary, row=0)
    async def cancel(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self.cog.show_card(
            interaction,
            self.trademark.id,
            BackTarget("all"),
        )


class ContextPatentView(TrademarkView):
    def __init__(
        self,
        cog: TrademarkCog,
        user_id: int,
        guild_id: int,
        message_id: int,
        display_name: str,
        source_content: str,
    ) -> None:
        self.message_id = int(message_id)
        self.display_name = display_name
        self.source_content = source_content
        self.message: discord.Message | None = None
        super().__init__(cog, user_id, guild_id)

    @discord.ui.button(label="Да", style=discord.ButtonStyle.success, row=0)
    async def confirm(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self.cog.handle_context_claim(
            interaction,
            self.message_id,
            self.display_name,
            self.source_content,
        )

    @discord.ui.button(label="Нет", style=discord.ButtonStyle.danger, row=0)
    async def cancel(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        active = self.cog.finish_confirmation(int(interaction.guild_id or 0), self.message_id, self.user_id)
        if not active:
            await self.cog.replace(
                interaction,
                result_embed(
                    "Подтверждение устарело",
                    "Эту попытку патента больше нельзя продолжить!",
                    error=True,
                ),
                MainView(self.cog, self.user_id, self.guild_id),
            )
            return
        await self.cog.replace(
            interaction,
            result_embed("Патент отменён", "Трейд марка не была запатентована!"),
            MainView(self.cog, self.user_id, self.guild_id),
        )

    async def on_timeout(self) -> None:
        self.cog.finish_confirmation(self.guild_id, self.message_id, self.user_id)
        if self.message is not None:
            try:
                await self.message.edit(
                    embed=result_embed(
                        "Подтверждение устарело",
                        "Эту попытку патента больше нельзя продолжить!",
                        error=True,
                    ),
                    view=None,
                )
            except discord.HTTPException:
                logger.exception("cannot expire trademark confirmation")


class MarkChoiceSelect(discord.ui.Select):
    def __init__(
        self,
        view: MarkChoiceView,
        items: list[Trademark],
    ) -> None:
        self.parent_view = view
        options = []
        for item in items:
            label, emoji = trademark_component(item, 100)
            options.append(
                discord.SelectOption(
                    label=label,
                    value=item.id,
                    emoji=emoji,
                )
            )
        super().__init__(
            placeholder="Выбери трейд марку",
            min_values=1,
            max_values=1,
            options=options,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.parent_view.selected(interaction, self.values[0])


class MarkChoiceView(TrademarkView):
    def __init__(
        self,
        cog: TrademarkCog,
        user_id: int,
        guild_id: int,
        mode: Literal["exchange_target", "exchange_offer", "showcase_add"],
        items: list[Trademark],
        page: int,
        total: int,
        *,
        offered_ids: tuple[str, ...] = (),
        requested_ids: tuple[str, ...] = (),
        counterpart_id: int | None = None,
        counterpart_name: str | None = None,
    ) -> None:
        self.mode = mode
        self.page = int(page)
        self.total = int(total)
        self.offered_ids = offered_ids
        self.requested_ids = requested_ids
        self.counterpart_id = counterpart_id
        self.counterpart_name = counterpart_name
        super().__init__(cog, user_id, guild_id)
        if items:
            self.add_item(MarkChoiceSelect(self, items))
        page_size = self.guild_config.inventory_page_size
        self.previous.disabled = page <= 0
        self.next.disabled = (page + 1) * page_size >= total

    async def selected(self, interaction: discord.Interaction, trademark_id: str) -> None:
        if self.mode == "exchange_target":
            if not self.offered_ids or self.counterpart_id is None:
                raise RuntimeError("Exchange target selection is incomplete")
            await self.cog.show_exchange_confirmation(
                interaction,
                self.offered_ids,
                (*self.requested_ids, trademark_id),
                self.counterpart_id,
                str(self.counterpart_name),
            )
        elif self.mode == "exchange_offer":
            if not self.requested_ids or self.counterpart_id is None:
                raise RuntimeError("Exchange offer selection is incomplete")
            await self.cog.show_exchange_confirmation(
                interaction,
                (*self.offered_ids, trademark_id),
                self.requested_ids,
                self.counterpart_id,
                str(self.counterpart_name),
            )
        else:
            await self.cog.handle_showcase_add(interaction, trademark_id)

    async def move_page(self, interaction: discord.Interaction, page: int) -> None:
        if self.mode == "exchange_target":
            if not self.offered_ids or self.counterpart_id is None:
                raise RuntimeError("Exchange target pagination is incomplete")
            await self.cog.show_exchange_target_marks(
                interaction,
                self.offered_ids,
                self.requested_ids,
                self.counterpart_id,
                str(self.counterpart_name),
                page,
            )
        elif self.mode == "exchange_offer":
            if not self.requested_ids or self.counterpart_id is None:
                raise RuntimeError("Exchange offer pagination is incomplete")
            await self.cog.show_exchange_offer_marks(
                interaction,
                self.offered_ids,
                self.requested_ids,
                self.counterpart_id,
                str(self.counterpart_name),
                page,
            )
        else:
            await self.cog.show_showcase_add(interaction, page)

    @discord.ui.button(label="Предыдущая", style=discord.ButtonStyle.secondary, row=2)
    async def previous(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self.move_page(interaction, self.page - 1)

    @discord.ui.button(label="Следующая", style=discord.ButtonStyle.success, row=2)
    async def next(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self.move_page(interaction, self.page + 1)

    @discord.ui.button(label="Назад", style=discord.ButtonStyle.secondary, row=3)
    async def back(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if self.mode == "showcase_add":
            await self.cog.show_showcase_settings(interaction)
        elif self.offered_ids and self.requested_ids:
            if self.counterpart_id is None:
                raise RuntimeError("Exchange selection is incomplete")
            await self.cog.show_exchange_confirmation(
                interaction,
                self.offered_ids,
                self.requested_ids,
                self.counterpart_id,
                str(self.counterpart_name),
            )
        else:
            await self.cog.show_card(
                interaction,
                str(self.offered_ids[0] if self.offered_ids else self.requested_ids[0]),
                BackTarget("inventory", owner_id=self.user_id, owner_name=interaction.user.name),
            )


class ExchangeConfirmView(TrademarkView):
    def __init__(
        self,
        cog: TrademarkCog,
        user_id: int,
        guild_id: int,
        offered: tuple[Trademark, ...],
        requested: tuple[Trademark, ...],
        recipient_id: int,
        recipient_name: str,
    ) -> None:
        self.offered = offered
        self.requested = requested
        self.recipient_id = int(recipient_id)
        self.recipient_name = recipient_name
        super().__init__(cog, user_id, guild_id)
        self.add_requested.label = component_label(
            f"Добавить ещё @{recipient_name}",
            80,
        )
        if len(offered) >= MAX_EXCHANGE_SIDE_MARKS:
            self.remove_item(self.add_offered)
        if len(requested) >= MAX_EXCHANGE_SIDE_MARKS:
            self.remove_item(self.add_requested)

    @discord.ui.button(label="Добавить ещё свою", style=discord.ButtonStyle.primary, row=0)
    async def add_offered(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self.cog.show_exchange_offer_marks(
            interaction,
            tuple(item.id for item in self.offered),
            tuple(item.id for item in self.requested),
            self.recipient_id,
            self.recipient_name,
            0,
        )

    @discord.ui.button(label="Добавить ещё чужую", style=discord.ButtonStyle.primary, row=0)
    async def add_requested(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self.cog.show_exchange_target_marks(
            interaction,
            tuple(item.id for item in self.offered),
            tuple(item.id for item in self.requested),
            self.recipient_id,
            self.recipient_name,
            0,
        )

    @discord.ui.button(label="Отправить запрос", style=discord.ButtonStyle.success, row=1)
    async def send(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self.cog.handle_exchange_create(
            interaction,
            tuple(item.id for item in self.offered),
            tuple(item.id for item in self.requested),
            self.recipient_id,
            self.recipient_name,
        )

    @discord.ui.button(label="Отмена", style=discord.ButtonStyle.danger, row=1)
    async def cancel(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self.cog.show_card(
            interaction,
            self.offered[0].id,
            BackTarget("inventory", owner_id=self.user_id, owner_name=interaction.user.name),
        )


class GiftConfirmView(TrademarkView):
    def __init__(
        self,
        cog: TrademarkCog,
        user_id: int,
        guild_id: int,
        trademark: Trademark,
        recipient_id: int,
        recipient_name: str,
    ) -> None:
        self.trademark = trademark
        self.recipient_id = int(recipient_id)
        self.recipient_name = recipient_name
        super().__init__(cog, user_id, guild_id)

    @discord.ui.button(label="Отправить предложение", style=discord.ButtonStyle.success, row=0)
    async def send(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self.cog.handle_gift_create(
            interaction,
            self.trademark.id,
            self.recipient_id,
            self.recipient_name,
        )

    @discord.ui.button(label="Отмена", style=discord.ButtonStyle.danger, row=0)
    async def cancel(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self.cog.show_card(
            interaction,
            self.trademark.id,
            BackTarget("inventory", owner_id=self.user_id, owner_name=interaction.user.name),
        )


class RequestsOverviewView(TrademarkView):
    @discord.ui.button(label="Входящие", style=discord.ButtonStyle.primary, row=0)
    async def incoming(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self.cog.show_request_list(interaction, "incoming", 0)

    @discord.ui.button(label="Отправленные", style=discord.ButtonStyle.secondary, row=0)
    async def outgoing(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self.cog.show_request_list(interaction, "outgoing", 0)

    @discord.ui.button(label="Назад", style=discord.ButtonStyle.secondary, row=1)
    async def back(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self.cog.show_main(interaction)


class RequestSelect(discord.ui.Select):
    def __init__(
        self,
        view: RequestListView,
        listings: list[TrademarkRequestListing],
    ) -> None:
        self.parent_view = view
        options = []
        for listing in listings:
            request = listing.request
            label = (
                f"{trademark_display_names(listing.offered_names, separator=', ', limit=45)} "
                f"↔ "
                f"{trademark_display_names(listing.requested_names, separator=', ', limit=45)}"
                if request.request_type == "exchange"
                else f"Подарок {trademark_display_names(listing.offered_names, separator=', ', limit=80)}"
            )
            counterpart = request.sender_name if view.direction == "incoming" else request.recipient_name
            component_text, emoji = component_presentation(label, 100)
            options.append(
                discord.SelectOption(
                    label=component_text,
                    value=request.id,
                    description=component_label(f"@{counterpart}", 100),
                    emoji=emoji,
                )
            )
        super().__init__(
            placeholder="Выбери запрос",
            min_values=1,
            max_values=1,
            options=options,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.parent_view.cog.show_request_card(
            interaction,
            self.values[0],
            self.parent_view.direction,
            self.parent_view.page,
        )


class RequestListView(TrademarkView):
    def __init__(
        self,
        cog: TrademarkCog,
        user_id: int,
        guild_id: int,
        direction: str,
        page: int,
        listings: list[TrademarkRequestListing],
        total: int,
    ) -> None:
        self.direction = direction
        self.page = int(page)
        self.total = int(total)
        super().__init__(cog, user_id, guild_id)
        if listings:
            self.add_item(RequestSelect(self, listings))
        self.previous.disabled = page <= 0
        self.next.disabled = (page + 1) * self.guild_config.requests_page_size >= total

    @discord.ui.button(label="Предыдущая", style=discord.ButtonStyle.secondary, row=1)
    async def previous(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self.cog.show_request_list(interaction, self.direction, self.page - 1)

    @discord.ui.button(label="Следующая", style=discord.ButtonStyle.success, row=1)
    async def next(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self.cog.show_request_list(interaction, self.direction, self.page + 1)

    @discord.ui.button(label="Назад", style=discord.ButtonStyle.secondary, row=2)
    async def back(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self.cog.show_requests(interaction)


class RequestCardView(TrademarkView):
    def __init__(
        self,
        cog: TrademarkCog,
        user_id: int,
        guild_id: int,
        listing: TrademarkRequestListing,
        direction: str,
        page: int,
    ) -> None:
        self.listing = listing
        self.direction = direction
        self.page = int(page)
        super().__init__(cog, user_id, guild_id)
        if direction == "incoming":
            self.remove_item(self.cancel_request)
        else:
            self.remove_item(self.accept)
            self.remove_item(self.decline)

    @discord.ui.button(label="Принять", style=discord.ButtonStyle.success, row=0)
    async def accept(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self.cog.handle_request_accept(interaction, self.listing.request.id)

    @discord.ui.button(label="Отклонить", style=discord.ButtonStyle.danger, row=0)
    async def decline(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self.cog.handle_request_decline(interaction, self.listing.request.id)

    @discord.ui.button(label="Отменить запрос", style=discord.ButtonStyle.danger, row=0)
    async def cancel_request(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self.cog.handle_request_cancel(interaction, self.listing.request.id)

    @discord.ui.button(label="Назад", style=discord.ButtonStyle.secondary, row=1)
    async def back(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self.cog.show_request_list(interaction, self.direction, self.page)


class HistoryView(TrademarkView):
    def __init__(
        self,
        cog: TrademarkCog,
        user_id: int,
        guild_id: int,
        trademark_id: str,
        page: int,
        total: int,
    ) -> None:
        self.trademark_id = trademark_id
        self.page = int(page)
        self.total = int(total)
        super().__init__(cog, user_id, guild_id)
        self.previous.disabled = page <= 0
        self.next.disabled = (page + 1) * self.guild_config.history_page_size >= total

    @discord.ui.button(label="Предыдущая", style=discord.ButtonStyle.secondary, row=0)
    async def previous(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self.cog.show_history(interaction, self.trademark_id, self.page - 1)

    @discord.ui.button(label="Следующая", style=discord.ButtonStyle.success, row=0)
    async def next(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self.cog.show_history(interaction, self.trademark_id, self.page + 1)

    @discord.ui.button(label="К трейд марке", style=discord.ButtonStyle.secondary, row=1)
    async def back(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self.cog.show_card(interaction, self.trademark_id, BackTarget("all"))


class ShowcaseSelect(discord.ui.Select):
    def __init__(
        self,
        view: ShowcaseSettingsView,
        items: list[Trademark],
    ) -> None:
        self.parent_view = view
        options = []
        for index, item in enumerate(items, start=1):
            label, emoji = trademark_component(item, 100)
            options.append(
                discord.SelectOption(
                    label=label,
                    value=item.id,
                    description=f"Место {index}",
                    emoji=emoji,
                )
            )
        super().__init__(
            placeholder="Выбери закреплённую трейд марку",
            options=options,
            min_values=1,
            max_values=1,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        self.parent_view.selected_id = self.values[0]
        await interaction.response.defer(ephemeral=True, thinking=False)


class ShowcaseSettingsView(TrademarkView):
    def __init__(
        self,
        cog: TrademarkCog,
        user_id: int,
        guild_id: int,
        items: list[Trademark],
    ) -> None:
        self.items = items
        self.selected_id: str | None = None
        super().__init__(cog, user_id, guild_id)
        if items:
            self.add_item(ShowcaseSelect(self, items))
        else:
            self.remove_item(self.remove)
            self.remove_item(self.move)
        if len(items) >= self.guild_config.showcase_limit:
            self.remove_item(self.add)

    @discord.ui.button(label="Добавить", style=discord.ButtonStyle.success, row=1)
    async def add(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self.cog.show_showcase_add(interaction, 0)

    @discord.ui.button(label="Убрать", style=discord.ButtonStyle.danger, row=1)
    async def remove(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if self.selected_id is None:
            await interaction.response.send_message(
                "Сначала выбери трейд марку!",
                ephemeral=True,
            )
            return
        await self.cog.handle_showcase_remove(interaction, self.selected_id)

    @discord.ui.button(label="Изменить порядок", style=discord.ButtonStyle.primary, row=1)
    async def move(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if self.selected_id is None:
            await interaction.response.send_message(
                "Сначала выбери трейд марку!",
                ephemeral=True,
            )
            return
        await self.cog.show_showcase_positions(interaction, self.selected_id, len(self.items))

    @discord.ui.button(label="Готово", style=discord.ButtonStyle.secondary, row=2)
    async def done(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self.cog.show_inventory(interaction, self.user_id, interaction.user.name, 0)


class PositionSelect(discord.ui.Select):
    def __init__(
        self,
        view: ShowcasePositionView,
        count: int,
    ) -> None:
        self.parent_view = view
        super().__init__(
            placeholder="Выбери новое место",
            options=[discord.SelectOption(label=str(index), value=str(index)) for index in range(1, count + 1)],
            min_values=1,
            max_values=1,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.parent_view.cog.handle_showcase_move(
            interaction,
            self.parent_view.trademark_id,
            int(self.values[0]),
        )


class ShowcasePositionView(TrademarkView):
    def __init__(
        self,
        cog: TrademarkCog,
        user_id: int,
        guild_id: int,
        trademark_id: str,
        count: int,
    ) -> None:
        self.trademark_id = trademark_id
        super().__init__(cog, user_id, guild_id)
        self.add_item(PositionSelect(self, count))

    @discord.ui.button(label="Назад", style=discord.ButtonStyle.secondary, row=1)
    async def back(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self.cog.show_showcase_settings(interaction)
