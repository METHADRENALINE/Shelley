from __future__ import annotations

import asyncio

from shelley.cogs.information import (
    PRIVACY_URL,
    SUPPORT_CONTACT,
    TERMS_URL,
    InformationCog,
)


class Response:
    def __init__(self) -> None:
        self.messages: list[tuple[str, bool]] = []

    async def send_message(self, content: str, *, ephemeral: bool) -> None:
        self.messages.append((content, ephemeral))


class Interaction:
    def __init__(self) -> None:
        self.response = Response()


def test_information_commands_respond_privately_with_expected_values() -> None:
    async def scenario() -> None:
        cog = InformationCog()
        cases = (
            (InformationCog.support, SUPPORT_CONTACT),
            (InformationCog.privacy, PRIVACY_URL),
            (InformationCog.terms, TERMS_URL),
        )

        for command, expected in cases:
            interaction = Interaction()
            await command.callback(cog, interaction)
            assert interaction.response.messages == [(expected, True)]

    asyncio.run(scenario())
