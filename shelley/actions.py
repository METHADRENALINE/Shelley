import discord

from .security import require_administrator
from .settings import config_path, env_name, load_json
from .state import mark_starting_status
from .services.recovery_log import append_recovery_control_log
from .services.remote import remote_target_cfg, run_ssh_command


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
    cfg = load_json(config_path())
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
                str(cfg.get("recovery_log_path", "data/recovery-controls.jsonl")),
                entry,
                int(cfg.get("recovery_log_retention_days", 365)),
            )
        except Exception as e:
            print(f"[recovery_log] failed to record recovery control use: {e!r}")

    target_cfg = remote_target_cfg(cfg, normalized_target)
    if not target_cfg:
        await log_recovery_button("unknown_target", error=f"Unknown target: {target}")
        if notify:
            await interaction.followup.send(f"Unknown target: `{target}`.", ephemeral=True)
        else:
            print(f"[remote_action] Unknown target: {target}")
        return

    command = target_cfg.get(command_key)
    if not command:
        await log_recovery_button("not_configured", error=f"{normalized_target} does not have {action_name} configured")
        if notify:
            await interaction.followup.send(
                f"`{normalized_target}` does not have `{action_name}` configured.",
                ephemeral=True,
            )
        else:
            print(f"[remote_action] {normalized_target} does not have {action_name} configured")
        return

    returncode, stdout, stderr = await run_ssh_command(target_cfg, str(command))
    if returncode == 0:
        await log_recovery_button("ok", returncode=returncode)
        if command_key == "start_command":
            state_path = cfg.get("state_path", f"data/state-{env_name()}.json")
            ttl_seconds = int(target_cfg.get("starting_ttl_seconds", cfg.get("starting_ttl_seconds", 600)))
            mark_starting_status(state_path, target_cfg.get("status_placeholder"), ttl_seconds)

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
        print(f"[remote_action] {action_name} failed for {normalized_target}: {err}")
