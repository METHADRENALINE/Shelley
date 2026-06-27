import asyncio
import os
from dataclasses import dataclass

from ..config import BotConfig, RemoteTargetConfig


@dataclass(frozen=True)
class RemoteCommandResult:
    returncode: int
    stdout: str
    stderr: str


def remote_target_cfg(cfg: BotConfig, target: str) -> RemoteTargetConfig | None:
    return cfg.remote_targets.get(target.strip().lower())


def remote_host_argument(target_cfg: RemoteTargetConfig) -> str:
    if target_cfg.ssh_profile:
        return str(target_cfg.ssh_profile)
    return f"{target_cfg.user}@{target_cfg.host}"


def build_ssh_command(target_cfg: RemoteTargetConfig, command: str) -> list[str]:
    ssh_args = ["ssh"]
    if target_cfg.key_path:
        ssh_args.extend(["-i", os.path.expandvars(os.path.expanduser(str(target_cfg.key_path)))])
    ssh_args.extend(
        [
            "-o",
            "BatchMode=yes",
            "-o",
            f"ConnectTimeout={int(target_cfg.connect_timeout)}",
            remote_host_argument(target_cfg),
            command,
        ]
    )
    return ssh_args


def parse_remote_result(returncode: int | None, stdout: bytes, stderr: bytes) -> RemoteCommandResult:
    return RemoteCommandResult(
        returncode=int(returncode or 0),
        stdout=stdout.decode("utf-8", errors="replace").strip(),
        stderr=stderr.decode("utf-8", errors="replace").strip(),
    )


async def run_ssh_command(target_cfg: RemoteTargetConfig, command: str) -> RemoteCommandResult:
    proc = await asyncio.create_subprocess_exec(
        *build_ssh_command(target_cfg, command),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    command_timeout = float(target_cfg.command_timeout)
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=command_timeout)
    except TimeoutError:
        proc.kill()
        await proc.communicate()
        return RemoteCommandResult(124, "", f"Remote command timed out after {command_timeout:g}s")

    return parse_remote_result(proc.returncode, stdout, stderr)
