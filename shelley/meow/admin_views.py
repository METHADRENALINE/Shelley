from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from typing import Literal

import discord
from discord import ui
from pydantic import ValidationError

from ..security import require_administrator
from .models import (
    CUSTOM_EMOJI,
    FINAL_STAGE,
    MAX_BANNER_URL_LENGTH,
    MAX_MEDAL_EMOJI_LENGTH,
    MAX_POINTS,
    MAX_RANKS,
    MAX_REWARD_NAME_LENGTH,
    MAX_ROLE_NAME_LENGTH,
    MEDAL_COLOR,
    Dashboard,
    Draft,
    EraSlot,
    Medal,
    MeowError,
    PointSource,
    Reward,
    SeasonDesign,
    can_update_live,
    era_title,
    number,
    parse_color,
    validate_emoji,
)
from .service import MeowService
from .showcase import NO_MENTIONS, safe_display, season_icon

Action = Literal["publish", "update", "replace", "end", "cancel_setup", "empty", "copy", "delete", "post_draft"]
logger = logging.getLogger(__name__)
EMOJI_ALIAS = re.compile(r":([A-Za-z0-9_]{2,32}):")
EMOJI_OR_ALIAS = re.compile(rf"{CUSTOM_EMOJI.pattern}|{EMOJI_ALIAS.pattern}")


def validate_input(error: ValidationError) -> str:
    return error.errors(include_input=False, include_url=False, include_context=False)[0]["msg"].removeprefix("Value error, ")


def resolve_emoji_alias(guild: discord.Guild | None, value: str) -> str:
    value = value.strip()
    alias = EMOJI_ALIAS.fullmatch(value)
    if alias and guild is not None:
        emoji = discord.utils.get(guild.emojis, name=alias[1])
        if emoji is not None:
            value = str(emoji)
    return value


def resolve_emoji(guild: discord.Guild | None, value: str) -> str:
    return validate_emoji(resolve_emoji_alias(guild, value))


async def private_message(interaction: discord.Interaction, message: str) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True, allowed_mentions=NO_MENTIONS)
    else:
        await interaction.response.send_message(message, ephemeral=True, allowed_mentions=NO_MENTIONS)


async def interaction_error(interaction: discord.Interaction, error: Exception) -> None:
    logger.error("Meow dashboard action failed", exc_info=error)
    await private_message(
        interaction,
        "Shelley could not complete that action. Reopen /meow to check the saved state.",
    )


class AdminSession:
    def __init__(self, service: MeowService, interaction: discord.Interaction) -> None:
        self.service = service
        self.owner_id = interaction.user.id
        self.origin = interaction
        self.guild_id = service.guild_id
        self.view: AdminView | None = None
        self.lock = asyncio.Lock()

    async def check(self, interaction: discord.Interaction) -> bool:
        if interaction.guild_id != self.guild_id or interaction.user.id != self.owner_id or not self.service.config.meow.enabled:
            await private_message(interaction, "Open your own dashboard with /meow.")
            return False
        if self.origin.is_expired():
            await private_message(interaction, "This dashboard expired. Reopen /meow to continue.")
            return False
        return await require_administrator(interaction)

    async def fetch(self) -> Dashboard:
        return await asyncio.to_thread(self.service.repository.dashboard, self.guild_id, include_era=True)

    async def refresh(self, note: str | None = None) -> None:
        dashboard = await self.fetch()
        view = AdminView(self, dashboard, note)
        if self.view is not None:
            self.view.stop()
        self.view = view
        await self.origin.edit_original_response(view=view, allowed_mentions=NO_MENTIONS)

    async def open(self) -> None:
        await self.origin.response.defer(ephemeral=True, thinking=True)
        await self.refresh()

    async def save(self, interaction: discord.Interaction, draft: Draft, design: SeasonDesign) -> None:
        await interaction.response.defer()
        async with self.lock:
            try:
                await asyncio.to_thread(
                    self.service.repository.save_draft,
                    self.guild_id,
                    draft.revision,
                    design,
                    draft.based_on,
                )
            except MeowError as exc:
                await self.refresh(str(exc))
                return
            await self.refresh("Draft saved.")

    async def preview(self, interaction: discord.Interaction, dashboard: Dashboard) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            design = dashboard.draft.design.updated(era_slot=dashboard.draft_slot)
            rendered = await self.service.showcase.render(design, guild=interaction.guild)
        except MeowError as exc:
            await interaction.edit_original_response(content=str(exc), allowed_mentions=NO_MENTIONS)
            return
        try:
            await interaction.edit_original_response(view=rendered.view, attachments=rendered.files, allowed_mentions=NO_MENTIONS)
        finally:
            rendered.close()

    async def save_era_emoji(self, interaction: discord.Interaction, era: int, emoji: str | None) -> None:
        await interaction.response.defer()
        async with self.lock:
            await asyncio.to_thread(self.service.repository.set_era_emoji, self.guild_id, era, emoji)
            self.service.wake(retry=True)
            status = "saved" if emoji else "removed"
            await self.refresh(f"{era_title(era)} emoji {status}.")

    async def confirm(
        self,
        interaction: discord.Interaction,
        action: Action,
        dashboard: Dashboard,
        reward: Reward | None = None,
    ) -> None:
        live = dashboard.live
        draft = dashboard.draft
        target = dashboard.preparing if action == "cancel_setup" else live
        prompts = {
            "publish": (
                "Start season?",
                "Existing point balances determine ranks. The season can end immediately if someone already qualifies for the medal.",
            ),
            "update": (
                "Publish changes?",
                "Updates the active season without resetting points. It can end immediately if someone qualifies under the new rules.",
            ),
            "replace": (
                "Replace the live season?",
                "The current season will end without a medal. Its temporary roles will be deleted. Points and earlier medals are kept. "
                "The new season uses existing point balances. It can end immediately if someone already qualifies for the medal.",
            ),
            "end": (
                "End season?",
                "Stops the season immediately, without awarding a medal. Its temporary roles will be deleted. "
                "Ordinary points and medals already earned are kept.",
            ),
            "cancel_setup": (
                "Cancel setup?",
                "Deletes roles created for the pending season. The current season stays active.",
            ),
            "empty": (
                "Start a blank draft?",
                "Replaces the saved draft. It does not change the live season.",
            ),
            "copy": (
                "Replace the draft with this season?",
                "Copies the season back into the draft. The live season will not change.",
            ),
            "delete": (
                "Remove this reward from the draft?",
                f"**{safe_display(reward.name)}**" if reward is not None else "The draft only will change.",
            ),
            "post_draft": (
                "Post showcase?",
                "Posts the draft without starting a season or creating roles.",
            ),
        }
        title, message = prompts[action]
        if action in ("publish", "update", "replace") and dashboard.draft_slot:
            message += f"\n\n**{dashboard.draft_slot.label}**"
        if self.service.config.meow.test_mode and action in ("publish", "replace"):
            message += "\n\nOnly your point balance is eligible."
        if action in ("end", "cancel_setup") and target is not None:
            message += f"\n\n**{safe_display(target.design.name)}**"
        confirmation = ConfirmationView(self, action, dashboard, reward)
        await interaction.response.send_message(
            embed=discord.Embed(title=title, description=message, color=draft.design.accent),
            view=confirmation,
            ephemeral=True,
            allowed_mentions=NO_MENTIONS,
        )

    async def execute(
        self,
        interaction: discord.Interaction,
        action: Action,
        dashboard: Dashboard,
        reward: Reward | None,
    ) -> None:
        await interaction.response.defer()
        async with self.lock:
            try:
                result = await self._execute(interaction, action, dashboard, reward)
            except MeowError as exc:
                result = str(exc)
            await interaction.edit_original_response(content=result, embed=None, view=None, allowed_mentions=NO_MENTIONS)
            await self.refresh()

    async def _execute(
        self,
        interaction: discord.Interaction,
        action: Action,
        dashboard: Dashboard,
        reward: Reward | None,
    ) -> str:
        draft, live = dashboard.draft, dashboard.live
        repository = self.service.repository
        if action == "post_draft":
            if interaction.guild is None:
                raise MeowError("Open /meow on the configured server.")
            current = await self.fetch()
            if current.draft.revision != draft.revision or current.preparing or current.live or current.latest:
                raise MeowError("The draft or season changed. Refresh the dashboard.")
            draft.design.ready()
            design = draft.design.updated(era_slot=current.draft_slot)
            message = await self.service.showcase.post_draft(interaction.guild, design)
            return f"Showcase posted: {message.jump_url}"
        if action in ("publish", "replace", "update"):
            if interaction.guild is None:
                raise MeowError("Open /meow on the configured server.")
            await self.service.preflight(interaction.guild, draft)
            await asyncio.to_thread(
                repository.publish_draft,
                self.guild_id,
                draft.revision,
                live.id if live else None,
                update=action == "update",
                test_user_id=self.owner_id if self.service.config.meow.test_mode else None,
                expected_slot=dashboard.draft_slot,
            )
            self.service.wake(retry=True)
            return "Update queued." if action == "update" else "Season queued. Refresh to check its status."
        if action in ("end", "cancel_setup"):
            target = dashboard.preparing if action == "cancel_setup" else live
            if target is None:
                raise MeowError("No matching season. Refresh the dashboard.")
            operation = repository.end_season if action == "end" else repository.cancel_setup
            await asyncio.to_thread(operation, self.guild_id, target.id)
            self.service.wake(retry=True)
            return (
                "Season ended. Temporary roles will be deleted."
                if action == "end"
                else "Setup cancelled. Its temporary roles will be deleted."
            )
        if action == "copy":
            shown = dashboard.shown
            if shown is None:
                raise MeowError("There is no season to copy.")
            design, based_on = shown.design, shown.id
            if shown.state not in ("active", "awarding"):
                design = design.updated(era_slot=None)
        elif action == "empty":
            design, based_on = SeasonDesign(), None
        else:
            if reward is None:
                raise MeowError("Select a reward first.")
            design = (
                draft.design.updated(medal=None)
                if draft.design.medal and reward.key == draft.design.medal.key
                else draft.design.remove_rank(reward.key)
            )
            based_on = draft.based_on
        await asyncio.to_thread(repository.save_draft, self.guild_id, draft.revision, design, based_on)
        return "Draft saved."


class ActionButton(ui.Button["AdminView"]):
    def __init__(
        self,
        label: str,
        action: Callable[[discord.Interaction], Awaitable[None]],
        style: discord.ButtonStyle,
        *,
        disabled: bool,
    ) -> None:
        super().__init__(label=label, style=style, disabled=disabled)
        self.action = action

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.action(interaction)


class RewardPicker(ui.Select["AdminView"]):
    def __init__(
        self,
        placeholder: str,
        options: list[discord.SelectOption],
        action: Callable[[discord.Interaction, str], Awaitable[None]],
    ) -> None:
        super().__init__(placeholder=placeholder, options=options)
        self.action = action

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.action(interaction, self.values[0])


class AdminView(ui.LayoutView):
    def __init__(self, session: AdminSession, dashboard: Dashboard, note: str | None) -> None:
        super().__init__(timeout=780)
        self.session = session
        self.snapshot = dashboard
        self.draft = dashboard.draft
        design = self.draft.design
        shown, live, pending = dashboard.shown, dashboard.live, dashboard.preparing
        status = "No live season"
        if shown is not None:
            state = "Ended" if shown.state == "cancelled" else shown.state.title()
            status = f"{state} \u00b7 {safe_display(shown.design.name)}"
            if shown.design.era_slot is not None:
                status += f" \u00b7 {shown.design.era_slot.label}"
            if shown.winner_id is not None:
                status += f" \u00b7 <@{shown.winner_id}>"
        if pending is not None:
            status += f"\nPreparing \u00b7 {safe_display(pending.design.name)}"
        header = f"## Meow\n-# {status}\n\nShowcase  <#{session.service.config.meow.channel_id}>"
        problems = session.service.problems
        if problems:
            header += "\n\n" + "\n".join(f"**{label}**  {value}" for label, value in problems.items())
        if note:
            header += f"\n\n{safe_display(note)}"
        surface: ui.Container[AdminView] = ui.Container(ui.TextDisplay(header), accent_color=design.accent)
        title = f"### {safe_display(design.name) if design.name else 'Untitled draft'}"
        rows = [
            f"{index}  {rank.emoji}  **{safe_display(rank.name)}**  \u00b7  {number(rank.points)}"
            for index, rank in enumerate(design.ordered_ranks, 1)
        ]
        if design.medal is not None:
            medal = design.medal
            rows.append(f"Medal  {medal.emoji}  **{safe_display(medal.name)}**  \u00b7  {number(medal.points)}")
        try:
            design.ready()
            missing = ""
        except MeowError as exc:
            missing = f"\n-# {exc}"
        slot = dashboard.draft_slot
        placement = f" \u00b7 {slot.label}" if slot else ""
        if slot and design.era_slot is None:
            placement += " (automatic)"
        surface.add_item(ui.Separator())
        surface.add_item(
            ui.TextDisplay(f"{title}\n-# {design.source.title()} points{placement}{missing}\n\n" + ("\n".join(rows) or "No rewards yet."))
        )
        surface.add_item(
            ui.ActionRow(
                self.button("Settings", self.settings, discord.ButtonStyle.secondary),
                self.button("Accent color", self.accent),
                self.button("Stage", self.era),
                self.button("Add rank", self.new_rank, disabled=len(design.ranks) >= MAX_RANKS),
                self.button("Set medal" if design.medal is None else "Edit medal", self.medal),
            )
        )
        if design.rewards:
            picker = RewardPicker(
                placeholder="Edit a rank or medal",
                options=[
                    discord.SelectOption(
                        label=reward.name,
                        value=reward.key,
                        description=f"{number(reward.points)} {design.source} points",
                    )
                    for reward in design.rewards
                ],
                action=self.edit_reward,
            )
            surface.add_item(ui.ActionRow(picker))
        if design.rewards:
            removing = RewardPicker(
                placeholder="Remove a reward",
                options=[discord.SelectOption(label=reward.name, value=reward.key) for reward in design.rewards],
                action=self.delete_reward,
            )
            surface.add_item(ui.ActionRow(removing))
        update = can_update_live(self.draft, live)
        self.publication: Action = "update" if update else "replace" if live is not None else "publish"
        unchanged = live is not None and design.updated(era_slot=slot) == live.design and self.draft.based_on == live.id
        surface.add_item(ui.Separator())
        surface.add_item(
            ui.ActionRow(
                self.button(
                    "Preview",
                    self.preview,
                    disabled=not (design.rewards or design.name or design.introduction or design.banner_url),
                ),
                self.button(
                    {
                        "update": "Publish changes",
                        "replace": "Replace season",
                        "publish": "Start season",
                    }[self.publication],
                    self.publish,
                    discord.ButtonStyle.success,
                    disabled=bool(missing or unchanged or pending or (live and live.state == "awarding")),
                ),
                self.button("Refresh", self.refresh),
                self.button("Era emoji", self.era_emoji),
            )
        )
        manage = [self.button("Blank draft", self.blank)]
        if not (shown or pending):
            manage.append(self.button("Post showcase", self.post_draft, disabled=bool(missing)))
        if shown is not None:
            manage.append(self.button("Copy season", self.copy))
        if pending is not None:
            manage.append(self.button("Cancel setup", self.cancel_setup, discord.ButtonStyle.danger))
        manage.append(
            self.button(
                "End season",
                self.end_season,
                discord.ButtonStyle.danger,
                disabled=live is None or live.state != "active",
            )
        )
        surface.add_item(ui.ActionRow(*manage))
        self.add_item(surface)

    @staticmethod
    def button(
        label: str,
        callback: Callable[[discord.Interaction], Awaitable[None]],
        style: discord.ButtonStyle = discord.ButtonStyle.secondary,
        *,
        disabled: bool = False,
    ) -> ActionButton:
        return ActionButton(label, callback, style, disabled=disabled)

    async def interaction_check(self, interaction: discord.Interaction, /) -> bool:
        return await self.session.check(interaction)

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: ui.Item, /) -> None:
        await interaction_error(interaction, error)

    async def settings(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(SeasonModal(self.session, self.draft))

    async def accent(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(AccentModal(self.session, self.draft))

    async def era(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(EraModal(self.session, self.snapshot))

    async def era_emoji(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(EraEmojiModal(self.session, self.snapshot))

    async def new_rank(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(RewardModal(self.session, self.draft))

    async def medal(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(RewardModal(self.session, self.draft, self.draft.design.medal, medal=True))

    async def edit_reward(self, interaction: discord.Interaction, key: str) -> None:
        reward = next(reward for reward in self.draft.design.rewards if reward.key == key)
        await interaction.response.send_modal(RewardModal(self.session, self.draft, reward, medal=reward == self.draft.design.medal))

    async def delete_reward(self, interaction: discord.Interaction, key: str) -> None:
        reward = next(reward for reward in self.draft.design.rewards if reward.key == key)
        await self.session.confirm(interaction, "delete", self.snapshot, reward)

    async def preview(self, interaction: discord.Interaction) -> None:
        await self.session.preview(interaction, self.snapshot)

    async def publish(self, interaction: discord.Interaction) -> None:
        await self.session.confirm(interaction, self.publication, self.snapshot)

    async def refresh(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        async with self.session.lock:
            self.session.service.wake(retry=True)
            await self.session.refresh()

    async def blank(self, interaction: discord.Interaction) -> None:
        await self.session.confirm(interaction, "empty", self.snapshot)

    async def copy(self, interaction: discord.Interaction) -> None:
        await self.session.confirm(interaction, "copy", self.snapshot)

    async def cancel_setup(self, interaction: discord.Interaction) -> None:
        await self.session.confirm(interaction, "cancel_setup", self.snapshot)

    async def end_season(self, interaction: discord.Interaction) -> None:
        await self.session.confirm(interaction, "end", self.snapshot)

    async def post_draft(self, interaction: discord.Interaction) -> None:
        await self.session.confirm(interaction, "post_draft", self.snapshot)


class ConfirmationView(ui.View):
    def __init__(self, session: AdminSession, action: Action, dashboard: Dashboard, reward: Reward | None) -> None:
        super().__init__(timeout=120)
        self.session = session
        self.action = action
        self.dashboard = dashboard
        self.reward = reward

    async def interaction_check(self, interaction: discord.Interaction, /) -> bool:
        return await self.session.check(interaction)

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: ui.Item, /) -> None:
        await interaction_error(interaction, error)

    @ui.button(label="Confirm", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, _button: ui.Button) -> None:
        if not self.is_finished():
            self.stop()
            await self.session.execute(interaction, self.action, self.dashboard, self.reward)

    @ui.button(label="Back", style=discord.ButtonStyle.secondary)
    async def dismiss(self, interaction: discord.Interaction, _button: ui.Button) -> None:
        self.stop()
        await interaction.response.edit_message(content="No changes made.", embed=None, view=None)


class EditorModal(ui.Modal):
    def __init__(self, title: str, session: AdminSession, draft: Draft) -> None:
        super().__init__(title=title, timeout=300)
        self.session = session
        self.draft = draft

    async def interaction_check(self, interaction: discord.Interaction, /) -> bool:
        return await self.session.check(interaction)

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: ui.Item | None = None, /) -> None:
        await interaction_error(interaction, error)

    def field(
        self,
        label: str,
        default: str,
        max_length: int,
        *,
        paragraph: bool = False,
        required: bool = True,
        placeholder: str | None = None,
        description: str | None = None,
    ) -> ui.TextInput[EditorModal]:
        item: ui.TextInput[EditorModal] = ui.TextInput(
            default=default or None,
            max_length=max_length,
            style=discord.TextStyle.paragraph if paragraph else discord.TextStyle.short,
            required=required,
            placeholder=placeholder,
        )
        self.add_item(ui.Label(text=label, component=item, description=description))
        return item


class EraModal(EditorModal):
    def __init__(self, session: AdminSession, dashboard: Dashboard) -> None:
        super().__init__("Stage", session, dashboard.draft)
        slot = self.draft.design.era_slot
        suggested = dashboard.automatic_slot
        self.era = self.field(
            "Era number",
            str(slot.era) if slot else "",
            4,
            required=False,
            description=f"Automatic: {suggested.label}" if suggested else None,
        )
        self.stage: ui.Select[EraModal] = ui.Select(
            options=[
                discord.SelectOption(label="Automatic", value="auto", default=slot is None),
                *[
                    discord.SelectOption(
                        label="Final act" if stage == FINAL_STAGE else f"Season {stage}",
                        value=str(stage),
                        default=slot is not None and slot.stage == stage,
                    )
                    for stage in range(1, FINAL_STAGE + 1)
                ],
            ],
        )
        self.add_item(ui.Label(text="Stage", component=self.stage))

    async def on_submit(self, interaction: discord.Interaction, /) -> None:
        raw = self.era.value.strip()
        stage = self.stage.values[0]
        try:
            if stage == "auto":
                if raw:
                    raise MeowError("Leave the era number blank for automatic placement.")
                slot = None
            else:
                if not re.fullmatch(r"[0-9]{1,4}", raw) or not 1 <= int(raw) <= 3999:
                    raise MeowError("Enter an era number from 1 to 3999.")
                slot = EraSlot(era=int(raw), stage=int(stage))
        except MeowError as exc:
            await private_message(interaction, str(exc))
            return
        await self.session.save(interaction, self.draft, self.draft.design.updated(era_slot=slot))


class EraEmojiModal(EditorModal):
    def __init__(self, session: AdminSession, dashboard: Dashboard) -> None:
        super().__init__("Era emoji", session, dashboard.draft)
        slot = dashboard.draft_slot
        self.era = self.field("Era number", str(slot.era) if slot else "", 4)
        self.emoji = self.field(
            "Era emoji",
            "",
            64,
            required=False,
            description="One emoji. Leave blank to remove it from this era.",
        )

    async def on_submit(self, interaction: discord.Interaction, /) -> None:
        raw = self.era.value.strip()
        try:
            if not re.fullmatch(r"[0-9]{1,4}", raw) or not 1 <= int(raw) <= 3999:
                raise MeowError("Enter an era number from 1 to 3999.")
            emoji = resolve_emoji(interaction.guild, self.emoji.value) if self.emoji.value.strip() else None
            if emoji and CUSTOM_EMOJI.fullmatch(emoji):
                emoji = season_icon(interaction.guild, emoji)
                if emoji is None:
                    raise MeowError("Choose an available emoji from this server.")
        except MeowError as exc:
            await private_message(interaction, str(exc))
            return
        await self.session.save_era_emoji(interaction, int(raw), emoji)


class SeasonModal(EditorModal):
    def __init__(self, session: AdminSession, draft: Draft) -> None:
        super().__init__("Season settings", session, draft)
        design = draft.design
        self.name = self.field("Season name", design.name, 80)
        self.emoji = self.field("Season emoji", design.emoji or "", 64, required=False)
        self.intro = self.field("Introduction", design.introduction, 800, paragraph=True, required=False)
        self.banner = self.field(
            "Season banner",
            design.banner_url or "",
            MAX_BANNER_URL_LENGTH,
            required=False,
            description="Direct HTTPS image URL",
        )
        self.source: ui.RadioGroup[SeasonModal] = ui.RadioGroup(
            options=[
                discord.RadioGroupOption(label="Text points", value="text", default=design.source == "text"),
                discord.RadioGroupOption(label="Voice points", value="voice", default=design.source == "voice"),
            ]
        )
        self.add_item(ui.Label(text="Point source", component=self.source))

    async def on_submit(self, interaction: discord.Interaction, /) -> None:
        try:
            old = self.draft.design
            name = self.name.value.strip()
            if not name or len(name.splitlines()) != 1:
                raise MeowError("Use a single line season name.")
            source: PointSource = "voice" if self.source.value == "voice" else "text"
            emoji = resolve_emoji(interaction.guild, self.emoji.value) if self.emoji.value.strip() else None
            if emoji is not None:
                emoji = season_icon(interaction.guild, emoji)
                if not emoji:
                    raise MeowError("Choose an available emoji from this server.")
            design = old.updated(
                name=name,
                emoji=emoji,
                introduction=self.intro.value,
                banner_url=self.banner.value,
                source=source,
            )
        except ValueError as exc:
            await private_message(interaction, validate_input(exc) if isinstance(exc, ValidationError) else str(exc))
            return
        await self.session.save(interaction, self.draft, design)


class AccentModal(EditorModal):
    def __init__(self, session: AdminSession, draft: Draft) -> None:
        super().__init__("Accent color", session, draft)
        self.color = self.field("Accent color", f"#{draft.design.accent:06X}", 7, required=False)

    async def on_submit(self, interaction: discord.Interaction, /) -> None:
        try:
            color = parse_color(self.color.value, self.draft.design.accent)
        except MeowError as exc:
            await private_message(interaction, str(exc))
            return
        await self.session.save(interaction, self.draft, self.draft.design.updated(accent=color))


class RewardModal(EditorModal):
    def __init__(
        self,
        session: AdminSession,
        draft: Draft,
        reward: Reward | None = None,
        *,
        medal: bool = False,
    ) -> None:
        super().__init__("Season medal" if medal else "Season rank", session, draft)
        self.reward = reward
        self.medal = medal
        self.name = self.field("Name", reward.name if reward else "", MAX_REWARD_NAME_LENGTH)
        emojis = "\n".join(part for part in (reward.emoji, reward.role_emoji) if part) if reward else ""
        self.emoji = self.field(
            "Emoji",
            emojis,
            MAX_MEDAL_EMOJI_LENGTH + 1 + MAX_ROLE_NAME_LENGTH if medal else 129,
            paragraph=True,
            description=(
                "Card emojis on the first line. Optional standard role emojis on the second."
                if medal
                else "Card emoji on the first line. Optional standard role emoji on the second."
            ),
        )
        self.points = self.field("Points required", str(reward.points) if reward else "", 16)
        self.description = self.field("Description", reward.description if reward else "", 1000, paragraph=True)
        self.color = (
            None
            if medal
            else self.field(
                "Role color (optional)",
                f"#{reward.color:06X}" if reward and reward.color else "",
                7,
                required=False,
                placeholder="#408871",
            )
        )

    async def on_submit(self, interaction: discord.Interaction, /) -> None:
        try:
            points = self.points.value.strip().replace(" ", "")
            if not points.isascii() or not points.isdecimal() or not 1 <= int(points) <= MAX_POINTS:
                raise MeowError(f"Enter between 1 and {number(MAX_POINTS)} points.")
            emojis = self.emoji.value.strip().splitlines()
            if not 1 <= len(emojis) <= 2:
                raise MeowError("Use the first line for the card and the optional second line for the role.")
            if self.medal:
                card_emojis = EMOJI_OR_ALIAS.sub(lambda match: resolve_emoji_alias(interaction.guild, match[0]), emojis[0])
            else:
                card_emojis = resolve_emoji(interaction.guild, emojis[0])
            previous_key = {"key": self.reward.key} if self.reward else {}
            reward_type = Medal if self.medal else Reward
            reward = reward_type(
                name=self.name.value,
                emoji=card_emojis,
                role_emoji=emojis[1].strip() if len(emojis) > 1 else None,
                points=int(points),
                description=self.description.value,
                color=MEDAL_COLOR if self.color is None else parse_color(self.color.value, 0),
                **previous_key,
            )
            design = self.draft.design.put_reward(reward, medal=self.medal)
        except ValueError as exc:
            await private_message(interaction, validate_input(exc) if isinstance(exc, ValidationError) else str(exc))
            return
        await self.session.save(interaction, self.draft, design)
