# Shelley

Discord bot made for the METHADRENALINE ᵍʳᵒᵘᵖ server. It exists to keep the community Discord tidy, useful, and a little more alive while staying close to how the group actually plays and talks. It is a personal community bot, not a packaged product and not a ready setup for other servers.

You can review it, ofc you can do whatever you want with the code and modify it to fit your own needs.

# What Shelley does for the server

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

## Activity points

Shelley keeps track of activity around the server through separate text and voice points. People who spend time talking and participating gradually build up a score without having to run commands or do anything specifically for the bot.

Text and voice activity are counted independently, with their own balances and leaderboards. The timing can be adjusted for each server. Points are tied to the member and stored independently from their current server membership. Leaving the server does not erase anything. If the same person joins again later, their previous text and voice points are still there and they continue from the same totals.

Admins can correct the numbers when necessary. They can add or remove points from a member, reset text or voice points separately, or clear the whole points table. This is mostly there for maintenance, migrations, and experiments that went a little too well (yeah, this actually comes from bitter experience).

### Text points

Text points come naturally from conversation. When a member sends a message in one of the selected channels, Shelley can give them points. After that, another message from the same member will not count for a while. This keeps normal conversation rewarding without making spam useful for farming points.

Shelley also remembers how far it has already processed a channel and periodically looks through recent message history. If the bot disconnects for a while or restarts, activity that happened during that gap can still be picked up instead of simply disappearing.

### Voice points

Voice activity works a little differently. Being connected to a voice channel is not enough by itself. Shelley looks for actual speaking activity and starts counting when there is someone else in the channel who can reasonably hear it. Staying muted, sitting alone, or quietly occupying a channel for several hours will not slowly generate points.

After enough active speaking, Shelley can award voice points and starts waiting before counting again. The speaking state comes directly from Discord.

> [!CAUTION]
> Shelley does not record audio, save it, transcribe it, or inspect what anyone is saying. It only needs to know that mic activity is happening. So yeah, if your mom talks in the background and your sensitive condenser picks it up, congrats on the points, you damn cheater :D

### Leaderboards

Shelley keeps one leaderboard message in the points channel. Text and voice activity have separate boards, both showing the top ten members.

The boards use current server display names when possible and update in real time as point data changes.

## Trademarks

Shelley has a community trademark system for names, phrases, emoji, and pretty much anything people decide is important enough to claim as theirs.

The whole thing actually started as a joke. One person in the community asked something along the lines of "guys, imagine if I could patent the word «air» and you would only be allowed to breathe when I let you." Apparently, that was enough of an idea to build an entire goat system around.

Every trademark has an owner and keeps its history over time. Members can collect trademarks, put favorites on display, give them away, trade them with each other, or release something back into the wild when the joke has finally run its course.

The main `/tm` interface lives in the trademark channel and is private to the member using it. It is the central place for patents, inventories, showcases, search, trades, gifts, and requests, without filling the channel with menus every time somebody wants to check their collection.

Admins can release another member patent when necessary. Limits for patents, inventories, showcases, requests, and their lifetime can be adjusted for each server.

### Patents

A member can create a patent directly from the `/tm` interface.

When automatic patents are enabled, there is an even simpler option. A complete single line message ending with `™` becomes a patent attempt. This works from any channel on the server, so sometimes the entire patent process is just saying something questionable and putting a trademark symbol after it.

There is also a message action for patenting your own message from the trademark channel. Shelley asks for confirmation before actually claiming it, because accidentally patenting your own typo would be a remarkably permanent way to remember it.

Before accepting a patent, Shelley compares it against trademarks that already exist. Letter case and unnecessary spacing do not make something new, and visually similar Unicode characters are checked as well. Replacing a Latin letter with a similar Cyrillic one, using alternate punctuation, or slipping invisible characters into a name will not give you a second version that merely looks identical.

### Inventory and showcase

Every member has an inventory containing the trademarks they currently own. From `/tm`, members can open their own inventory or look through somebody else's collection.

A limited number of trademarks can also be placed in a showcase. These are basically the ones worth putting in the display cabinet.

Showcase entries can be added, removed, and reordered whenever the owner wants.

A trademark can also be released. This removes its current owner and makes it available for somebody else to patent, but its previous history is not erased.

### Gifts and trades

Members can give trademarks directly to each other or create an exchange when generosity has limits.

A trade can contain up to five trademarks from each side. Both members can therefore exchange several things at once.

Requests remain pending until they are accepted, declined, cancelled, expire, or stop being valid.

### History

Trademarks keep their history instead of forgetting everything whenever ownership changes!

Patents, releases, gifts, and exchanges are recorded, so a trademark can have an actual trail from its original patent through whoever managed to acquire it afterward.

Events, failed attempts, releases, and completed exchanges can also appear publicly in the trademark channel. The management interface itself stays private, while the interesting consequences can still become everybody else's business. :)

## Welcome guide

Shelley keeps a single welcome message in the welcome channel. The message is built from a JSON template, so the actual text and embeds can be edited without changing the bot code.

When the template changes, Shelley updates the Discord message. If the message was deleted, Shelley recreates it.

## Announcements

An admin can send text to the notification channel and attach files, edit them. Technically, it doesn’t really make sense, ik that, it does give the server some personality though.

## Star messages

Shelley watches configured chat channels for ⭐ reactions. When a message gets 3 stars from members (bot reactions don’t count), Shelley copies it to the star channel, keeping memorable community moments separate from regular pinned messages.

If the message later drops below the required number of stars, or if the original message is deleted, Shelley removes the saved copy from the star channel too.

In practice, star messages work like a community archive. Members decide what deserves to stay visible, Shelley just handles the boring part in the background.
