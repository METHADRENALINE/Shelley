from typing import Optional

import discord

from ..actions import run_remote_action

class BMStartButton(discord.ui.Button):
    def __init__(self) -> None:
        super().__init__(
            label="\u0421\u0442\u0430\u0440\u0442",
            style=discord.ButtonStyle.success,
            custom_id="bm_status_start",
        )

    async def callback(self, interaction: discord.Interaction):
        await run_remote_action(
            interaction,
            "bm",
            "start_command",
            "\u0421\u0442\u0430\u0440\u0442",
            notify=False,
            require_admin=False,
        )

class BMRebootButton(discord.ui.Button):
    def __init__(self) -> None:
        super().__init__(
            label="\u0420\u0435\u0441\u0442\u0430\u0440\u0442 \u0441\u0438\u0441\u0442\u0435\u043c\u044b",
            style=discord.ButtonStyle.danger,
            custom_id="bm_status_reboot",
        )

    async def callback(self, interaction: discord.Interaction):
        await run_remote_action(
            interaction,
            "bm",
            "reboot_command",
            "\u0420\u0435\u0441\u0442\u0430\u0440\u0442 \u0441\u0438\u0441\u0442\u0435\u043c\u044b",
            notify=False,
            require_admin=False,
        )

class BMStatusControlView(discord.ui.View):
    def __init__(self, status: str) -> None:
        super().__init__(timeout=None)

        if status == ":red_circle:":
            self.add_item(BMStartButton())

        if status in (":red_circle:", ":yellow_circle:"):
            self.add_item(BMRebootButton())


def status_message_view(message_cfg: dict, status: Optional[str] = None) -> Optional[discord.ui.View]:
    control_target = str(message_cfg.get("control_target", "")).strip().lower()
    if control_target != "bm":
        return None

    if status in (":red_circle:", ":yellow_circle:"):
        return BMStatusControlView(status)
    return None
