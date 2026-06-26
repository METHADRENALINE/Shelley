import asyncio
import os
from typing import Optional

def remote_target_cfg(cfg: dict, target: str) -> Optional[dict]:
    targets = cfg.get("remote_targets", {})
    if not isinstance(targets, dict):
        return None

    target_cfg = targets.get(target.strip().lower())
    return target_cfg if isinstance(target_cfg, dict) else None

async def run_ssh_command(target_cfg: dict, command: str) -> tuple[int, str, str]:
    host = str(target_cfg["host"])
    user = str(target_cfg["user"])
    key_path = os.path.expandvars(os.path.expanduser(str(target_cfg["key_path"])))
    connect_timeout = str(int(target_cfg.get("connect_timeout", 10)))
    command_timeout = float(target_cfg.get("command_timeout", 30))

    proc = await asyncio.create_subprocess_exec(
        "ssh",
        "-i",
        key_path,
        "-o",
        "BatchMode=yes",
        "-o",
        f"ConnectTimeout={connect_timeout}",
        f"{user}@{host}",
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
