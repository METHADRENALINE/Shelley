import asyncio
from typing import List, Optional

from mcstatus import BedrockServer, JavaServer

from ..models import ServerCfg, ServerComponentCfg

def status_version_label(edition: str, version_name: object) -> str:
    version = str(version_name or "").strip()
    return f"{edition} {version}".strip()

async def minecraft_java_status(address: str, timeout: float) -> tuple[bool, Optional[int], Optional[str]]:
    try:
        server = await JavaServer.async_lookup(address, timeout=timeout)
        st = await server.async_status()
        online = getattr(st.players, "online", None)
        version = getattr(getattr(st, "version", None), "name", None)
        return True, int(online) if online is not None else None, status_version_label("Java Edition", version)
    except Exception:
        return False, None, None

async def minecraft_bedrock_status(address: str, timeout: float) -> tuple[bool, Optional[int], Optional[str]]:
    try:
        server = BedrockServer.lookup(address, timeout=timeout)
        st = await server.async_status()
        online = getattr(st.players, "online", None)
        version = getattr(getattr(st, "version", None), "name", None)
        return True, int(online) if online is not None else None, status_version_label("Bedrock", version)
    except Exception:
        return False, None, None

async def probe_server(server: "ServerCfg", timeout: float) -> tuple[bool, Optional[int], Optional[str]]:
    kind = (server.kind or "minecraft").strip().lower()

    if kind in ("minecraft_java", "minecraft_java_cluster"):
        if not server.address:
            return False, None, None
        return await minecraft_java_status(server.address, timeout)

    if kind in ("minecraft_bedrock", "minecraft_bedrock_cluster"):
        if not server.address:
            return False, None, None
        return await minecraft_bedrock_status(server.address, timeout)

    if kind in ("minecraft", "minecraft_auto", "minecraft_cluster"):
        if not server.address:
            return False, None, None

        java_online, java_players, java_version = await minecraft_java_status(server.address, timeout)
        if java_online:
            return java_online, java_players, java_version
        return await minecraft_bedrock_status(server.address, timeout)

    return False, None, None

async def tmux_session_exists(session: str, timeout: float = 2.0) -> bool:
    if not session:
        return False

    try:
        proc = await asyncio.create_subprocess_exec(
            "tmux",
            "has-session",
            "-t",
            session,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.communicate(), timeout=max(0.5, timeout))
        return proc.returncode == 0
    except (FileNotFoundError, asyncio.TimeoutError):
        return False

async def probe_server_component(
    component: "ServerComponentCfg",
    timeout: float,
) -> tuple[str, int]:
    online, players, _version = await minecraft_java_status(component.address, timeout)
    if online:
        return ":green_circle:", int(players or 0)

    if component.tmux_session and await tmux_session_exists(component.tmux_session):
        return ":yellow_circle:", 0

    return ":red_circle:", 0

def aggregate_cluster_status(component_statuses: List[str], gateway_online: bool) -> str:
    if not component_statuses or all(status == ":red_circle:" for status in component_statuses):
        return ":red_circle:"

    if gateway_online and all(status == ":green_circle:" for status in component_statuses):
        return ":green_circle:"

    return ":yellow_circle:"

async def with_hard_timeout(coro, seconds: float):
    return await asyncio.wait_for(coro, timeout=seconds)
