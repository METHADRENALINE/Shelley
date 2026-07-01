import asyncio
import logging
import re
from collections.abc import Iterable
from typing import Any

from mcstatus import BedrockServer, JavaServer

from ..config import ServerComponentConfig, ServerConfig

logger = logging.getLogger(__name__)

JAVA_MODLOADERS = (
    ("neoforge", "NeoForge"),
    ("minecraftforge", "Forge"),
    ("forge", "Forge"),
    ("fabricloader", "Fabric"),
    ("fabric", "Fabric"),
    ("quilt_loader", "Quilt"),
    ("quilt", "Quilt"),
)

JAVA_SOFTWARE_PREFIXES = (
    "paper",
    "purpur",
    "spigot",
    "bukkit",
    "craftbukkit",
    "pufferfish",
    "folia",
    "neoforge",
    "minecraftforge",
    "forge",
    "fabricloader",
    "fabric",
    "quilt_loader",
    "quilt",
)


def iter_status_strings(value: Any) -> Iterable[str]:
    if value is None:
        return

    if isinstance(value, str):
        yield value
        return

    if isinstance(value, dict):
        for item in value.values():
            yield from iter_status_strings(item)
        return

    if isinstance(value, (list, tuple, set)):
        for item in value:
            yield from iter_status_strings(item)
        return

    if hasattr(value, "__dict__"):
        yield from iter_status_strings(vars(value))


def detect_java_modloader(status: Any) -> str | None:
    candidates: list[str] = []

    version = getattr(getattr(status, "version", None), "name", None)
    if version:
        candidates.append(str(version))

    as_dict = getattr(status, "as_dict", None)
    if callable(as_dict):
        try:
            candidates.extend(iter_status_strings(as_dict()))
        except Exception:
            logger.debug("cannot inspect minecraft status dictionary", exc_info=True)

    candidates.extend(iter_status_strings(getattr(status, "forge_data", None)))
    candidates.extend(iter_status_strings(getattr(status, "raw", None)))

    haystack = "\n".join(candidates).lower()
    for needle, label in JAVA_MODLOADERS:
        if needle in haystack:
            return label

    return None


def clean_version_name(edition: str, version_name: object) -> str:
    version = str(version_name or "").strip()
    if not version:
        return ""
    mc_match = re.search(r"\bMC:\s*(?P<minecraft_version>[^)]+)\)", version, re.IGNORECASE)
    if mc_match:
        return mc_match.group("minecraft_version").strip()
    velocity_match = re.match(r"^Velocity\s+\S+-(?P<minecraft_version>.+)$", version, re.IGNORECASE)
    if velocity_match:
        version = velocity_match.group("minecraft_version").strip()
    prefix_match = re.match(r"^(?P<prefix>[A-Za-z_]+)\s+(?P<minecraft_version>.+)$", version)
    if prefix_match:
        prefix = prefix_match.group("prefix").strip().lower()
        edition_key = str(edition or "").strip().lower().replace(" ", "")
        if prefix in JAVA_SOFTWARE_PREFIXES or prefix == edition_key:
            version = prefix_match.group("minecraft_version").strip()
    return version


def status_version_label(edition: str, version_name: object) -> str:
    version = clean_version_name(edition, version_name)
    return f"{edition} {version}".strip()


async def minecraft_java_status(
    address: str,
    timeout: float,
    edition_override: str | None = None,
) -> tuple[bool, int | None, str | None]:
    try:
        server = await JavaServer.async_lookup(address, timeout=timeout)
        st = await server.async_status()
        online = getattr(st.players, "online", None)
        version = getattr(getattr(st, "version", None), "name", None)
        edition = str(edition_override or "").strip() or detect_java_modloader(st) or "Java Edition"
        return True, int(online) if online is not None else None, status_version_label(edition, version)
    except Exception:
        logger.debug("minecraft java probe failed", extra={"address": address}, exc_info=True)
        return False, None, None


async def minecraft_bedrock_status(address: str, timeout: float) -> tuple[bool, int | None, str | None]:
    try:
        server = BedrockServer.lookup(address, timeout=timeout)
        st = await server.async_status()
        online = getattr(st.players, "online", None)
        version = getattr(getattr(st, "version", None), "name", None)
        return True, int(online) if online is not None else None, status_version_label("Bedrock", version)
    except Exception:
        logger.debug("minecraft bedrock probe failed", extra={"address": address}, exc_info=True)
        return False, None, None


async def probe_server(server: ServerConfig, timeout: float) -> tuple[bool, int | None, str | None]:
    kind = (server.kind or "minecraft").strip().lower()

    if kind in ("minecraft_java", "minecraft_java_cluster"):
        if not server.address:
            return False, None, None
        return await minecraft_java_status(server.address, timeout, server.version_edition_override)

    if kind in ("minecraft_bedrock", "minecraft_bedrock_cluster"):
        if not server.address:
            return False, None, None
        return await minecraft_bedrock_status(server.address, timeout)

    if kind in ("minecraft", "minecraft_auto", "minecraft_cluster"):
        if not server.address:
            return False, None, None

        java_online, java_players, java_version = await minecraft_java_status(
            server.address,
            timeout,
            server.version_edition_override,
        )
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
    except (TimeoutError, FileNotFoundError):
        return False


async def probe_server_component(component: ServerComponentConfig, timeout: float) -> tuple[str, int, str | None]:
    online, players, version = await minecraft_java_status(component.address, timeout)
    if online:
        return ":green_circle:", int(players or 0), version

    if component.tmux_session and await tmux_session_exists(component.tmux_session):
        return ":yellow_circle:", 0, None

    return ":red_circle:", 0, None


def aggregate_cluster_status(component_statuses: list[str], gateway_online: bool) -> str:
    if not component_statuses or all(status == ":red_circle:" for status in component_statuses):
        return ":red_circle:"

    if gateway_online and all(status == ":green_circle:" for status in component_statuses):
        return ":green_circle:"

    return ":yellow_circle:"


async def with_hard_timeout(coro, seconds: float):
    return await asyncio.wait_for(coro, timeout=seconds)
