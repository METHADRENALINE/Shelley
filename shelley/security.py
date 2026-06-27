import discord


def user_is_administrator(interaction: discord.Interaction) -> bool:
    permissions = getattr(interaction.user, "guild_permissions", None)
    return bool(permissions and permissions.administrator)


async def require_administrator(interaction: discord.Interaction) -> bool:
    if user_is_administrator(interaction):
        return True

    message = "This command requires the Discord Administrator permission."
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)
    return False
