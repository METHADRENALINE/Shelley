# Shelley

Discord bot made for the METHADRENALINE ᵍʳᵒᵘᵖ server. It exists to keep the community Discord tidy, useful, and a little more alive while staying close to how the group actually plays and talks. It is a personal community bot, not a packaged product and not a ready setup for other communities. Changes are made when the community needs them, not to support every possible Discord server.

You can review it, ofc you can do whatever you want with the code and modify it to fit your own needs.

# What Shelley does for the server

## Activity points

Shelley keeps track of activity around the server through separate text and voice points. People who spend time talking and participating gradually build up a score without having to run commands or do anything specifically for the bot.

Text and voice activity are counted independently, with their own balances and leaderboards. The timing is configurable for each server.

Admins can correct the numbers when necessary. They can add or remove points from a member, reset text or voice points separately, or clear the whole points table. This is mostly there for maintenance, migrations, and experiments that went a little too well (yeah, this actually comes from bitter experience).

### Text points

Text points come naturally from conversation. When a member sends a message in one of the configured channels, Shelley can give them points. After that, there is a configured interval before another message from the same member can count again. This makes ordinary conversation useful while making message spam a rather inefficient career choice.

Shelley also remembers how far it has already processed a channel and periodically looks through recent message history. If the bot disconnects for a while or restarts, activity that happened during that gap can still be picked up instead of simply disappearing.

### Voice points

Voice activity works a little differently. Being connected to a voice channel is not enough by itself. Shelley looks for actual speaking activity and starts counting when there is someone else in the channel who can reasonably hear it. Staying muted, sitting alone, or quietly occupying a channel for several hours will not slowly generate points.

Once a member has been actively speaking for the configured interval, Shelley can award voice points and begins waiting for the next interval. The speaking state comes directly from Discord.

> [!CAUTION]
> Shelley does not record audio, save it, transcribe it, or inspect what anyone is saying. It only needs to know that mic activity is happening. So yeah, if your mom talks in the background and your sensitive condenser picks it up, congrats on the points, you damn cheater :D

## Welcome guide

Shelley keeps a single welcome message in the configured welcome channel. The message is built from a JSON template, so the actual text and embeds can be edited without changing the bot code.

When the template changes, Shelley updates the Discord message. If the message was deleted, Shelley recreates it.

## Announcements

An admin can send and edit text to the configured notification channel and attach files. Technically, it doesn’t really make sense, ik that, it does give the server some personality though.

## Star messages

Shelley watches selected chat channels for ⭐ reactions. When a message gets 3 stars from members (bot reactions don’t count), Shelley copies it to the star channel, keeping memorable community moments separate from regular pinned messages.

If the message later drops below the required number of stars, or if the original message is deleted, Shelley removes the saved copy from the star channel too.

In practice, star messages work like a community archive. Members decide what deserves to stay visible, Shelley just handles the boring part in the background.

## Game servers

### Status icons

🟢 means the server is online and responding normally. Players can join, and Shelley can show the current player count.

🟡 means the server is not fully available yet. It may be starting, stuck during startup, unstable connection, or only partly reachable.

🔴 means the server is offline or not responding. If recovery controls are enabled, Shelley can show emergency buttons for starting or restarting the server.

### Recovery controls

Recovery buttons for the emergency case where a game server has stopped and players simply want to bring it back without calling an admin.

When the server is offline, Shelley can show a Start button. When the server is offline or marked as starting, it can show Restart system button. These controls are only for unplanned outages.

Spammed recovery actions for the same target are protected by a cooldown. This prevents several people from sending Start or Restart system commands to the same server at almost the same time.

Admins also have slash commands for the same kind of remote action.

Shelley keeps a record of recovery actions and their results for the configured retention period. This includes actions started from recovery buttons as well as admin commands.
