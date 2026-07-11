from dataclasses import dataclass, field


@dataclass
class ServerComponentCfg:
    label: str
    address: str
    tmux_session: str | None = None


@dataclass
class ServerCfg:
    placeholder: str
    kind: str = "minecraft"
    address: str | None = None
    version_edition_override: str | None = None
    components: list[ServerComponentCfg] = field(default_factory=list)
