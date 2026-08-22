from __future__ import annotations

import math
from collections.abc import Collection, Iterable
from datetime import datetime

import discord

from .models import (
    PatentAvailability,
    Trademark,
    TrademarkEvent,
    TrademarkRequest,
    TrademarkRequestListing,
)
from .normalization import CUSTOM_EMOJI_PATTERN

COLOR = 0x57F287
ERROR_COLOR = 0xED4245
NEUTRAL_COLOR = 0x5865F2


def safe_display(value: str, limit: int | None = None) -> str:
    parts: list[str] = []
    position = 0
    for match in CUSTOM_EMOJI_PATTERN.finditer(str(value)):
        segment = str(value)[position : match.start()]
        parts.append(discord.utils.escape_markdown(discord.utils.escape_mentions(segment)))
        parts.append(match.group(0))
        position = match.end()
    tail = str(value)[position:]
    parts.append(discord.utils.escape_markdown(discord.utils.escape_mentions(tail)))
    rendered = "".join(parts)
    if limit is None or len(rendered) <= limit:
        return rendered
    bounded: list[str] = []
    used = 0
    for part in parts:
        if used + len(part) <= limit:
            bounded.append(part)
            used += len(part)
            continue
        remaining = max(0, limit - used - 1)
        if remaining and CUSTOM_EMOJI_PATTERN.fullmatch(part) is None:
            bounded.append(part[:remaining].rstrip("\\"))
        bounded.append("…")
        break
    return "".join(bounded)


def trademark_name(trademark: Trademark, limit: int | None = None) -> str:
    name_limit = None if limit is None else max(1, limit - 1)
    return f"{safe_display(trademark.display_name, name_limit)}™"


def trademark_names(
    trademarks: Iterable[Trademark],
    *,
    separator: str = "\n",
    limit: int = 1024,
) -> str:
    return trademark_display_names(
        (trademark.display_name for trademark in trademarks),
        separator=separator,
        limit=limit,
    )


def trademark_display_names(
    names: Iterable[str],
    *,
    separator: str = "\n",
    limit: int = 1024,
) -> str:
    rendered: list[str] = []
    used = 0
    for name in names:
        remaining = limit - used - (len(separator) if rendered else 0)
        if remaining <= 1:
            break
        value = f"{safe_display(name, remaining - 1)}™"
        rendered.append(value)
        used += len(value) + (len(separator) if len(rendered) > 1 else 0)
    return separator.join(rendered)


def discord_time(value: datetime | None, style: str = "F") -> str:
    if value is None:
        return "Неизвестно"
    return f"<t:{int(value.timestamp())}:{style}>"


def member_name(
    user_id: int | None,
    snapshot: str | None,
    present_ids: Collection[int],
) -> str:
    if user_id is None:
        return "Нет владельца"
    if int(user_id) in present_ids:
        return f"<@{int(user_id)}>"
    if snapshot:
        return f"@{discord.utils.escape_markdown(snapshot)}"
    return "Неизвестный пользователь"


def page_count(total: int, page_size: int) -> int:
    return max(1, math.ceil(int(total) / int(page_size)))


def trademark_count_text(count: int) -> str:
    value = int(count)
    remainder = abs(value) % 100
    if 11 <= remainder <= 14:
        word = "трейд марок"
    else:
        remainder = abs(value) % 10
        if remainder == 1:
            word = "трейд марка"
        elif 2 <= remainder <= 4:
            word = "трейд марки"
        else:
            word = "трейд марок"
    return f"{value} {word}"


def set_page_footer(embed: discord.Embed, total: int, page: int, page_size: int) -> None:
    pages = page_count(total, page_size)
    if pages > 1:
        embed.set_footer(text=f"Страница {page + 1} из {pages}")


def main_embed(availability: PatentAvailability, incoming_requests: int) -> discord.Embed:
    embed = discord.Embed(title="Трейд марки", color=COLOR)
    embed.add_field(
        name="Твоих трейд марок",
        value=f"{availability.owned} из {availability.inventory_limit}",
        inline=False,
    )
    embed.add_field(
        name="Патенты на сутки",
        value=f"{availability.available} из {availability.patent_limit}",
        inline=False,
    )
    if availability.next_window_at is not None:
        embed.add_field(
            name="Новые патенты",
            value=discord_time(availability.next_window_at),
            inline=False,
        )
    embed.add_field(
        name="Входящих запросов",
        value=str(incoming_requests),
        inline=False,
    )
    return embed


def inventory_embed(
    owner_name: str,
    items: list[Trademark],
    showcase: list[Trademark],
    total: int,
    page: int,
    page_size: int,
    *,
    own: bool,
) -> discord.Embed:
    title = "Мой инвентарь" if own else f"Инвентарь @{owner_name}"
    embed = discord.Embed(title=title, color=NEUTRAL_COLOR)
    if showcase:
        embed.add_field(
            name="Витрина",
            value="\n".join(trademark_name(item) for item in showcase),
            inline=False,
        )
    elif own and total:
        embed.add_field(name="Витрина", value="Витрина пока пуста", inline=False)
    if total:
        embed.add_field(
            name="Все трейд марки",
            value=trademark_count_text(total),
            inline=False,
        )
        set_page_footer(embed, total, page, page_size)
    else:
        label = "У тебя пока нет трейд марок" if own else "У пользователя пока нет трейд марок"
        embed.description = label
    return embed


def all_trademarks_embed(
    items: list[Trademark],
    total: int,
    page: int,
    page_size: int,
    present_ids: Collection[int],
    *,
    search: str | None = None,
) -> discord.Embed:
    title = "Поиск трейд марок" if search is not None else "Все трейд марки"
    embed = discord.Embed(title=title, color=NEUTRAL_COLOR)
    if not items:
        embed.description = "Трейд марки не найдены"
        return embed
    lines = []
    for item in items:
        owner = member_name(item.owner_id, item.owner_name, present_ids)
        lines.append(f"**{trademark_name(item, 72)}**\n{owner}")
    embed.description = "\n\n".join(lines)
    if search is None:
        set_page_footer(embed, total, page, page_size)
    return embed


def trademark_card_embed(
    trademark: Trademark,
    present_ids: Collection[int],
) -> discord.Embed:
    embed = discord.Embed(title=trademark_name(trademark), color=COLOR)
    embed.add_field(
        name="Владелец",
        value=member_name(trademark.owner_id, trademark.owner_name, present_ids),
        inline=False,
    )
    embed.add_field(
        name="Дата первого патента",
        value=discord_time(trademark.created_at),
        inline=False,
    )
    return embed


def requests_overview_embed(incoming: int, outgoing: int) -> discord.Embed:
    embed = discord.Embed(title="Запросы", color=NEUTRAL_COLOR)
    embed.add_field(name="Входящие", value=str(incoming), inline=False)
    embed.add_field(name="Отправленные", value=str(outgoing), inline=False)
    return embed


def requests_page_embed(
    listings: list[TrademarkRequestListing],
    total: int,
    page: int,
    page_size: int,
    direction: str,
    present_ids: Collection[int],
) -> discord.Embed:
    title = "Входящие запросы" if direction == "incoming" else "Отправленные запросы"
    embed = discord.Embed(title=title, color=NEUTRAL_COLOR)
    if not listings:
        embed.description = "Новых запросов нет"
        return embed
    lines = []
    for listing in listings:
        request = listing.request
        if request.request_type == "exchange":
            description = (
                f"{trademark_display_names(listing.offered_names, separator=', ', limit=330)} "
                f"↔ "
                f"{trademark_display_names(listing.requested_names, separator=', ', limit=330)}"
            )
        else:
            description = f"Подарок {trademark_display_names(listing.offered_names, separator=', ', limit=650)}"
        counterpart_id, counterpart_name = (
            (request.sender_id, request.sender_name) if direction == "incoming" else (request.recipient_id, request.recipient_name)
        )
        lines.append(
            f"**{description}**\n"
            f"{member_name(counterpart_id, counterpart_name, present_ids)}\n"
            f"Истекает {discord_time(request.expires_at, 'R')}"
        )
    embed.description = "\n\n".join(lines)
    set_page_footer(embed, total, page, page_size)
    return embed


def request_card_embed(
    listing: TrademarkRequestListing,
    direction: str,
    present_ids: Collection[int],
) -> discord.Embed:
    request = listing.request
    if request.request_type == "exchange":
        embed = discord.Embed(title="Предложение обмена", color=NEUTRAL_COLOR)
        embed.add_field(
            name="Отправитель",
            value=member_name(
                request.sender_id,
                request.sender_name,
                present_ids,
            ),
            inline=False,
        )
        embed.add_field(
            name="Отдаёт",
            value=trademark_display_names(listing.offered_names),
            inline=False,
        )
        embed.add_field(
            name="Получает",
            value=trademark_display_names(listing.requested_names),
            inline=False,
        )
    else:
        embed = discord.Embed(title="Подарок", color=NEUTRAL_COLOR)
        embed.description = (
            f"{member_name(request.sender_id, request.sender_name, present_ids)} хочет "
            f"подарить {trademark_display_names(listing.offered_names)}"
        )
    if direction == "outgoing":
        embed.add_field(
            name="Получатель",
            value=member_name(
                request.recipient_id,
                request.recipient_name,
                present_ids,
            ),
            inline=False,
        )
    embed.add_field(
        name="Срок действия",
        value=discord_time(request.expires_at),
        inline=False,
    )
    return embed


def history_embed(
    trademark: Trademark,
    events: list[TrademarkEvent],
    total: int,
    page: int,
    page_size: int,
    present_ids: Collection[int],
) -> discord.Embed:
    embed = discord.Embed(title=f"История {trademark_name(trademark)}", color=NEUTRAL_COLOR)
    if not events:
        embed.description = "История пока пуста"
        return embed
    blocks = []
    for event in events:
        actor = member_name(event.actor_id, event.actor_name, present_ids)
        source = member_name(event.from_user_id, event.from_user_name, present_ids)
        destination = member_name(
            event.to_user_id,
            event.to_user_name,
            present_ids,
        )
        if event.event_type == "patent":
            body = f"**Патент**\nЗапатентована пользователем {destination}"
        elif event.event_type == "release":
            body = f"**Патент снят**\nПатент снят пользователем {actor}"
        elif event.event_type == "admin_release":
            body = f"**Патент снят администратором**\nАдминистратор {actor}\nПредыдущий владелец {source}"
        elif event.event_type == "gift":
            body = f"**Подарок**\nПерешла от {source} к {destination}"
        else:
            related = safe_display(event.related_trademark_name or "")
            body = f"**Обмен**\nПерешла от {source} к {destination}\nВстречная трейд марка {related}™"
        blocks.append(f"{discord_time(event.created_at)}\n{body}")
    embed.description = "\n\n".join(blocks)
    set_page_footer(embed, total, page, page_size)
    return embed


def patent_success_announcement(
    trademark: Trademark,
    actor_id: int,
    present_ids: Collection[int],
) -> discord.Embed:
    actor = member_name(actor_id, trademark.owner_name, present_ids)
    return discord.Embed(
        title="Новая трейд марка",
        description=(f"Патент на {trademark_name(trademark)} успешно оформлен пользователем {actor}!"),
        color=COLOR,
    )


def patent_failure_announcement(
    trademark: Trademark,
    actor_id: int,
    actor_name: str,
    present_ids: Collection[int],
) -> discord.Embed:
    if trademark.owner_id == actor_id:
        reason = "Эта трейд марка уже запатентована на его же имя"
    else:
        owner = member_name(trademark.owner_id, trademark.owner_name, present_ids)
        reason = f"Эта трейд марка уже принадлежит {owner}"
    return _patent_failure_embed(
        trademark_name(trademark),
        actor_id,
        actor_name,
        reason,
        present_ids,
    )


def patent_rejection_announcement(
    attempted_name: str,
    actor_id: int,
    actor_name: str,
    reason: str,
    present_ids: Collection[int],
) -> discord.Embed:
    return _patent_failure_embed(
        attempted_name,
        actor_id,
        actor_name,
        reason,
        present_ids,
    )


def _patent_failure_embed(
    attempted_name: str,
    actor_id: int,
    actor_name: str,
    reason: str,
    present_ids: Collection[int],
) -> discord.Embed:
    actor = member_name(actor_id, actor_name, present_ids)
    attempted = safe_display(attempted_name, 500)
    explanation = str(reason).strip().rstrip(".!")
    return discord.Embed(
        title="Неудачная попытка патента",
        description=(f"{actor} попытался запатентовать {attempted}, но не судьба.\n\n**Причина**\n{explanation}!"),
        color=ERROR_COLOR,
    )


def release_announcement(
    trademark: Trademark,
    actor_id: int,
    actor_name: str,
    present_ids: Collection[int],
) -> discord.Embed:
    actor = member_name(actor_id, actor_name, present_ids)
    return discord.Embed(
        title="Свободная трейд марка",
        description=f"{actor} решил освободить {trademark_name(trademark)}!",
        color=COLOR,
    )


def exchange_success_announcement(
    request: TrademarkRequest,
    offered: Iterable[Trademark],
    requested: Iterable[Trademark],
    present_ids: Collection[int],
) -> discord.Embed:
    sender = member_name(request.sender_id, request.sender_name, present_ids)
    recipient = member_name(
        request.recipient_id,
        request.recipient_name,
        present_ids,
    )
    embed = discord.Embed(
        title="Обмен завершён",
        description=(f"{sender} и {recipient} успешно обменялись трейд марками!"),
        color=COLOR,
    )
    embed.add_field(
        name="Результат",
        value=(
            f"{recipient} получает "
            f"{trademark_names(offered, separator=', ', limit=450)}\n"
            f"{sender} получает "
            f"{trademark_names(requested, separator=', ', limit=450)}"
        ),
        inline=False,
    )
    return embed


def result_embed(title: str, description: str, *, error: bool = False) -> discord.Embed:
    return discord.Embed(
        title=title,
        description=description,
        color=ERROR_COLOR if error else COLOR,
    )
