import discord

from .security import require_administrator
from .settings import config_path, env_name, load_json
from .state import mark_starting_status
from .services.remote import remote_target_cfg, run_ssh_command


async def run_remote_action(
    interaction: discord.Interaction,
    target: str,
    command_key: str,
    action_name: str,
    notify: bool = True,
    require_admin: bool = True,
) -> None:
    cfg = load_json(config_path())
    if require_admin and not await require_administrator(interaction):
        return

    if notify:
        await interaction.response.defer(ephemeral=True, thinking=True)
    else:
        await interaction.response.defer(thinking=False)

    normalized_target = target.strip().lower()
    target_cfg = remote_target_cfg(cfg, normalized_target)
    if not target_cfg:
        if notify:
            await interaction.followup.send(f"Unknown target: `{target}`.", ephemeral=True)
        else:
            print(f"[remote_action] Unknown target: {target}")
        return

    command = target_cfg.get(command_key)
    if not command:
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
    if notify:
        await interaction.followup.send(
            f"`{action_name}` failed for `{normalized_target}`:\n```{err[:1500]}```",
            ephemeral=True,
        )
    else:
        print(f"[remote_action] {action_name} failed for {normalized_target}: {err}")
