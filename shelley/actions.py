import logging

import discord

from .config import BotConfig, RemoteTargetConfig
from .security import require_administrator
from .services.recovery_log import append_recovery_control_log
from .services.remote import RemoteCommandResult, remote_target_cfg, run_ssh_command
from .settings import get_config
from .state import mark_starting_status

logger = logging.getLogger(__name__)


async def defer_remote_interaction(interaction: discord.Interaction, notify: bool) -> None:
    if notify:
        await interaction.response.defer(ephemeral=True, thinking=True)
    else:
        await interaction.response.defer(thinking=False)


async def write_audit_event(
    interaction: discord.Interaction,
    config: BotConfig,
    target: str,
    command_key: str,
    action_name: str,
    status: str,
    recovery_button_id: str | None,
    recovery_button_label: str | None,
    returncode: int | None = None,
    error: str | None = None,
) -> None:
    if not recovery_button_id:
        return

    user = interaction.user
    entry = {
        "button_id": recovery_button_id,
        "button_label": recovery_button_label or recovery_button_id,
        "target": target,
        "action": action_name,
        "command_key": command_key,
        "status": status,
        "returncode": returncode,
        "error": error,
        "user": {
            "id": int(user.id),
            "name": str(user),
            "display_name": str(getattr(user, "display_name", str(user))),
        },
    }

    try:
        await append_recovery_control_log(
            config.recovery_log_path,
            entry,
            int(config.recovery_log_retention_days),
            int(getattr(interaction.guild, "id", 0) or config.runtime_guild_id()),
        )
    except Exception:
        logger.exception("failed to record recovery control use")


def remote_command(target_cfg: RemoteTargetConfig, command_key: str) -> str | None:
    value = getattr(target_cfg, command_key, None)
    return str(value) if value else None


async def handle_unknown_target(
    interaction: discord.Interaction,
    config: BotConfig,
    target: str,
    command_key: str,
    action_name: str,
    notify: bool,
    recovery_button_id: str | None,
    recovery_button_label: str | None,
) -> None:
    await write_audit_event(
        interaction,
        config,
        target,
        command_key,
        action_name,
        "unknown_target",
        recovery_button_id,
        recovery_button_label,
        error=f"Unknown target: {target}",
    )
    if notify:
        await interaction.followup.send(f"Unknown target: `{target}`.", ephemeral=True)
    else:
        logger.warning("unknown remote target", extra={"target": target})


async def handle_missing_remote_command(
    interaction: discord.Interaction,
    config: BotConfig,
    target: str,
    command_key: str,
    action_name: str,
    notify: bool,
    recovery_button_id: str | None,
    recovery_button_label: str | None,
) -> None:
    await write_audit_event(
        interaction,
        config,
        target,
        command_key,
        action_name,
        "not_configured",
        recovery_button_id,
        recovery_button_label,
        error=f"{target} does not have {action_name} configured",
    )
    if notify:
        await interaction.followup.send(
            f"`{target}` does not have `{action_name}` configured.",
            ephemeral=True,
        )
    else:
        logger.warning("remote action is not configured", extra={"target": target, "action": action_name})


def mark_remote_starting_if_needed(
    interaction: discord.Interaction,
    config: BotConfig,
    target_cfg: RemoteTargetConfig,
    command_key: str,
) -> None:
    if command_key != "start_command":
        return
    ttl_seconds = int(target_cfg.starting_ttl_seconds)
    guild_id = getattr(interaction.guild, "id", 0) or config.runtime_guild_id()
    mark_starting_status(guild_id, target_cfg.status_placeholder, ttl_seconds)


async def handle_remote_success(
    interaction: discord.Interaction,
    config: BotConfig,
    target: str,
    target_cfg: RemoteTargetConfig,
    command_key: str,
    action_name: str,
    result: RemoteCommandResult,
    notify: bool,
    recovery_button_id: str | None,
    recovery_button_label: str | None,
) -> None:
    await write_audit_event(
        interaction,
        config,
        target,
        command_key,
        action_name,
        "ok",
        recovery_button_id,
        recovery_button_label,
        returncode=result.returncode,
    )
    mark_remote_starting_if_needed(interaction, config, target_cfg, command_key)
    if notify:
        details = f"\n```{result.stdout[:1500]}```" if result.stdout else ""
        await interaction.followup.send(f"`{action_name}` sent for `{target}`.{details}", ephemeral=True)


async def handle_remote_failure(
    interaction: discord.Interaction,
    config: BotConfig,
    target: str,
    command_key: str,
    action_name: str,
    result: RemoteCommandResult,
    notify: bool,
    recovery_button_id: str | None,
    recovery_button_label: str | None,
) -> None:
    error = result.stderr or result.stdout or f"ssh exited with code {result.returncode}"
    await write_audit_event(
        interaction,
        config,
        target,
        command_key,
        action_name,
        "failed",
        recovery_button_id,
        recovery_button_label,
        returncode=result.returncode,
        error=error[:1500],
    )
    if notify:
        await interaction.followup.send(
            f"`{action_name}` failed for `{target}`:\n```{error[:1500]}```",
            ephemeral=True,
        )
    else:
        logger.warning("remote action failed", extra={"target": target, "action": action_name, "returncode": result.returncode})


async def run_remote_action(
    interaction: discord.Interaction,
    target: str,
    command_key: str,
    action_name: str,
    notify: bool = True,
    require_admin: bool = True,
    recovery_button_id: str | None = None,
    recovery_button_label: str | None = None,
) -> None:
    config = get_config()
    if require_admin and not await require_administrator(interaction):
        return

    await defer_remote_interaction(interaction, notify)
    normalized_target = target.strip().lower()
    target_cfg = remote_target_cfg(config, normalized_target)
    if target_cfg is None:
        await handle_unknown_target(
            interaction, config, normalized_target, command_key, action_name, notify, recovery_button_id, recovery_button_label
        )
        return

    command = remote_command(target_cfg, command_key)
    if not command:
        await handle_missing_remote_command(
            interaction, config, normalized_target, command_key, action_name, notify, recovery_button_id, recovery_button_label
        )
        return

    result = await run_ssh_command(target_cfg, command)
    if result.returncode == 0:
        await handle_remote_success(
            interaction,
            config,
            normalized_target,
            target_cfg,
            command_key,
            action_name,
            result,
            notify,
            recovery_button_id,
            recovery_button_label,
        )
        return

    await handle_remote_failure(
        interaction, config, normalized_target, command_key, action_name, result, notify, recovery_button_id, recovery_button_label
    )
