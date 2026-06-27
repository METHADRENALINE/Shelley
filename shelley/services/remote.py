import asyncio
import os
from typing import Optional

from ..config import BotConfig, RemoteTargetConfig

def remote_target_cfg(cfg: BotConfig, target: str) -> Optional[RemoteTargetConfig]:
    return cfg.remote_targets.get(target.strip().lower())

async def run_ssh_command(target_cfg: RemoteTargetConfig, command: str) -> tuple[int, str, str]:
    host = str(target_cfg.host)
    user = str(target_cfg.user)
    connect_timeout = str(int(target_cfg.connect_timeout))
    command_timeout = float(target_cfg.command_timeout)
    ssh_args = ["ssh"]
    if target_cfg.key_path:
        ssh_args.extend(["-i", os.path.expandvars(os.path.expanduser(str(target_cfg.key_path)))])
    if target_cfg.ssh_profile:
        host_arg = str(target_cfg.ssh_profile)
    else:
        host_arg = f"{user}@{host}"

    proc = await asyncio.create_subprocess_exec(
        *ssh_args,
        "-o",
        "BatchMode=yes",
        "-o",
        f"ConnectTimeout={connect_timeout}",
        host_arg,
        command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=command_timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        return 124, "", f"Remote command timed out after {command_timeout:g}s"

    return (
        int(proc.returncode or 0),
        stdout.decode("utf-8", errors="replace").strip(),
        stderr.decode("utf-8", errors="replace").strip(),
    )
