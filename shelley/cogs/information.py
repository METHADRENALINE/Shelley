from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

SUPPORT_CONTACT = "methadrenaline@pm.me"
PRIVACY_URL = "https://github.com/METHADRENALINE/Shelley/blob/main/PRIVACY.md"
TERMS_URL = "https://github.com/METHADRENALINE/Shelley/blob/main/TERMS.md"


async def send_private_response(interaction: discord.Interaction, content: str) -> None:
    await interaction.response.send_message(content, ephemeral=True)


class InformationCog(commands.Cog):
    @app_commands.command(name="support", description="Show the support contact.")
    async def support(self, interaction: discord.Interaction) -> None:
        await send_private_response(interaction, SUPPORT_CONTACT)

    @app_commands.command(name="privacy", description="Show the privacy policy.")
    async def privacy(self, interaction: discord.Interaction) -> None:
        await send_private_response(interaction, PRIVACY_URL)

    @app_commands.command(name="terms", description="Show the terms of service.")
    async def terms(self, interaction: discord.Interaction) -> None:
        await send_private_response(interaction, TERMS_URL)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(InformationCog())
