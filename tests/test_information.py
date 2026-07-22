from __future__ import annotations

import asyncio

import discord
from discord import app_commands
from discord.ext import commands

from shelley.bot import GLOBAL_COMMAND_NAMES, configure_command_scopes
from shelley.cogs.information import (
    PRIVACY_URL,
    SUPPORT_CONTACT,
    TERMS_URL,
    InformationCog,
)


class Response:
    def __init__(self) -> None:
        self.messages: list[tuple[str, bool]] = []

    async def send_message(self, content: str, *, ephemeral: bool) -> None:
        self.messages.append((content, ephemeral))


class Interaction:
    def __init__(self) -> None:
        self.response = Response()


def test_information_commands_respond_privately_with_expected_values() -> None:
    async def scenario() -> None:
        cog = InformationCog()
        cases = (
            (InformationCog.support, SUPPORT_CONTACT),
            (InformationCog.privacy, PRIVACY_URL),
            (InformationCog.terms, TERMS_URL),
        )

        for command, expected in cases:
            interaction = Interaction()
            await command.callback(cog, interaction)
            assert interaction.response.messages == [(expected, True)]

    asyncio.run(scenario())


def test_only_information_commands_are_global() -> None:
    bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())

    async def callback(_interaction: discord.Interaction) -> None:
        return None

    for name in (*GLOBAL_COMMAND_NAMES, "notify", "reboot"):
        bot.tree.add_command(
            app_commands.Command(
                name=name,
                description=f"Test {name}",
                callback=callback,
            )
        )

    guild = discord.Object(id=123456789012345678)
    configure_command_scopes(bot.tree, guild)

    assert {command.name for command in bot.tree.get_commands()} == set(GLOBAL_COMMAND_NAMES)
    assert {command.name for command in bot.tree.get_commands(guild=guild)} == {
        "notify",
        "reboot",
    }
