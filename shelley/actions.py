import discord
import logging

from .security import require_administrator
from .settings import get_config
from .state import mark_starting_status
from .services.recovery_log import append_recovery_control_log
from .services.remote import remote_target_cfg, run_ssh_command

logger = logging.getLogger(__name__)


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
    cfg = get_config()
    if require_admin and not await require_administrator(interaction):
        return

    if notify:
        await interaction.response.defer(ephemeral=True, thinking=True)
    else:
        await interaction.response.defer(thinking=False)

    normalized_target = target.strip().lower()

    async def log_recovery_button(status: str, returncode: int | None = None, error: str | None = None) -> None:
        if not recovery_button_id:
            return

        user = interaction.user
        entry = {
            "button_id": recovery_button_id,
            "button_label": recovery_button_label or recovery_button_id,
            "target": normalized_target,
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
                cfg.recovery_log_path,
                entry,
                int(cfg.recovery_log_retention_days),
                int(getattr(interaction.guild, "id", 0) or cfg.runtime_guild_id()),
            )
        except Exception:
            logger.exception("failed to record recovery control use")

    target_cfg = remote_target_cfg(cfg, normalized_target)
    if not target_cfg:
        await log_recovery_button("unknown_target", error=f"Unknown target: {target}")
        if notify:
            await interaction.followup.send(f"Unknown target: `{target}`.", ephemeral=True)
        else:
            logger.warning("unknown remote target", extra={"target": target})
        return

    command = getattr(target_cfg, command_key)
    if not command:
        await log_recovery_button("not_configured", error=f"{normalized_target} does not have {action_name} configured")
        if notify:
            await interaction.followup.send(
                f"`{normalized_target}` does not have `{action_name}` configured.",
                ephemeral=True,
            )
        else:
            logger.warning("remote action is not configured", extra={"target": normalized_target, "action": action_name})
        return

    returncode, stdout, stderr = await run_ssh_command(target_cfg, str(command))
    if returncode == 0:
        await log_recovery_button("ok", returncode=returncode)
        if command_key == "start_command":
            ttl_seconds = int(target_cfg.starting_ttl_seconds)
            mark_starting_status(getattr(interaction.guild, "id", 0) or cfg.runtime_guild_id(), target_cfg.status_placeholder, ttl_seconds)

        if notify:
            details = f"\n```{stdout[:1500]}```" if stdout else ""
            await interaction.followup.send(
                f"`{action_name}` sent for `{normalized_target}`.{details}",
                ephemeral=True,
            )
        return

    err = stderr or stdout or f"ssh exited with code {returncode}"
    await log_recovery_button("failed", returncode=returncode, error=err[:1500])
    if notify:
        await interaction.followup.send(
            f"`{action_name}` failed for `{normalized_target}`:\n```{err[:1500]}```",
            ephemeral=True,
        )
    else:
        logger.warning("remote action failed", extra={"target": normalized_target, "action": action_name, "returncode": returncode})
