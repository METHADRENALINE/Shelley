# Shelley

Discord bot made for the METHADRENALINE ᵍʳᵒᵘᵖ server. It exists to keep the community Discord tidy, useful, and a little more alive while staying close to how the group actually plays and talks.

It is a personal community bot, not a packaged product and not a ready setup for other communities. The code is shaped around private group, its Discord channels, and its game servers habits. Changes are made when the community needs them, not to support every possible Discord server.

You can review it, ofc you can do whatever you want with the code and modify it to fit your own needs. :D

You can actually see some parts of our Discord server here, like the welcome message, our game servers (just their name you know), and stuff like that. You won't be able to connect to game servers if you're not on the whitelist though. 

You’ll never know the full community lineup or real details about its members, IPs, unless you’ve actually been with us. :)

# What Shelley does for the server

## Welcome guide

Shelley keeps a single welcome message in the configured welcome channel. The message is built from a JSON template, so the actual text and embeds can be edited without changing the bot code.

When the template changes, Shelley updates the Discord message. If the message was deleted, Shelley recreates it.

## Star messages

Shelley watches selected chat channels for ⭐ reactions. When a message gets 3 stars from members (bot reactions don’t count heh), Shelley copies it to the star channel, keeping memorable community moments separate from regular pinned messages.

If the message later drops below the required number of stars, or if the original message is deleted, Shelley removes the saved copy from the star channel too.

In practice, star messages work like a community archive. Members decide what deserves to stay visible, Shelley just handles the boring part in the background.

## Announcements

Shelley includes a small admin notification command. An administrator can send text to the configured notification channel and attach files. Technically, it doesn’t really make sense, ik that, it does give the server some personality though.

## Game servers status icons

🟢 means the server is online and responding normally. Players can join, and Shelley can show the current player count.

🟡 means the server is not fully available yet. It may be starting, stuck during startup, unstable connection, or only partly reachable.

🔴 means the server is offline or not responding. If recovery controls are enabled, Shelley can show emergency buttons for starting or restarting the server.

## Game servers recovery controls

Recovery buttons for the emergency case where a game server has stopped and players simply want to bring it back without calling an admin.

When the server is offline, Shelley can show a Start button. When the server is offline or marked as starting, it can show Restart system button. These controls are only for unplanned outages.

Admins also have slash commands for the same kind of remote action. Those commands are restricted to users with administrator permission.
