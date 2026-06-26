from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class ServerComponentCfg:
    label: str
    address: str
    tmux_session: Optional[str] = None


@dataclass
class ServerCfg:
    placeholder: str
    kind: str = "minecraft"
    address: Optional[str] = None
    components: List[ServerComponentCfg] = field(default_factory=list)
