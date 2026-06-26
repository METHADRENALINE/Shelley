# Shelley

Discord bot made for the METHADRENALINE ᵍʳᵒᵘᵖ server. It exists to keep the community Discord tidy, useful, and a little more alive while staying close to how the group actually plays and talks.

It is a personal community bot, not a packaged product and not a ready setup for other communities. The code is shaped around private community, its Discord channels, and its game servers habits. Changes are made when the community needs them, not to support every possible Discord server.

You can review it, learn from it, or adapt ideas from it and ofc you can do whatever you want with the code and modify it to fit your own needs. :D

You can actually see some parts of our Discord server here, like the welcome message, our game servers, and stuff like that.

One thing will always stay private, you’ll never know the full community lineup or real details about its members unless you’ve actually been with us :)

# What Shelley does for the server

Shelley takes care of star messages, our small community archive for moments people don’t want to lose.

Shelley keeps the game servers info in one place. Members can open the status channel and see whether the configured servers are online, starting, partly available, or offline. The status cards also show the connection info, game version, player count, and download links for a modpack when that server has them in its template.

For the SMP&Creative setup, Shelley treats the network as a small group of connected servers. It shows the shared status first, then the separate state of Lobby, SMP, and Creative. That makes it clear whether the whole network is fine or only one part needs attention.

For the modded Minecraft server, like Magicway or Bizarre Machinery, Shelley shows a separate status card. When the server is online, the card stays focused on info for players. When the server is offline or still starting, Shelley adds recovery controls directly under the status message.

## Welcome guide

Shelley keeps a single welcome message in the configured welcome channel. The message is built from a JSON template, so the actual text and embeds can be edited without changing the bot code.

When the template changes, Shelley updates the Discord message. If the message was deleted, Shelley recreates it.

## Star messages

Shelley watches selected chat channels for ⭐ reactions. When a message gets 3 stars from members (bot reactions don’t count heh), Shelley copies it to the star channel, keeping memorable community moments separate from regular pinned messages.

If the message later drops below the required number of stars, or if the original message is deleted, Shelley removes the saved copy from the star channel too.

In practice, star messages work like a community archive. Members decide what deserves to stay visible, Shelley just handles the boring part in the background.

## Announcements

Shelley includes a small admin notification command. An administrator can send text to the configured notification channel and attach files. Technically, it doesn’t really make sense, ik that, it does give the server some personality though.

## Status icons

🟢 means the server is online and responding normally. Players can join, and Shelley can show the current player count.

🟡 means the server is not fully available yet. It may be starting, stuck during startup, unstable connection, or only partly reachable.

🔴 means the server is offline or not responding. If recovery controls are enabled, Shelley can show emergency buttons for starting or restarting the server.

## Game servers recovery controls

Recovery buttons for the emergency case where a configured game server has stopped and players simply want to bring it back without calling an admin.

When the server is offline, Shelley can show a Start button. When the server is offline or marked as starting, it can show Restart system button. These controls are only for unplanned outages.

Admins also have slash commands for the same kind of remote action. Those commands are restricted to users with administrator permission.
