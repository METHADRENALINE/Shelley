# Shelley

Shelley is a Discord bot developed specifically for the private METHADRENALINE community.

This repository is published for source review and development transparency. It is not a product for third-party Discord servers, and it is not intended to be installed, configured, or operated by outside communities.

## Scope

The code shows the logic behind Shelley: Minecraft server status rendering, welcome-message synchronization, star-forwarding, and private server controls used inside the community.

Deployment-specific material is intentionally excluded. The repository uses a safe configuration and placeholder message templates instead of real Discord tokens, SSH keys, passwords, private hostnames, runtime state, or infrastructure details.

All runtime settings are represented in `config.json`: Discord application/client ID, the bot token placeholder, channel IDs, Minecraft server targets, BM SSH settings, status messages, and state paths.

## Structure

The Python code is split by responsibility:

- `shelley/cogs/` extension modules.
- `shelley/services/` external service integrations.
- `shelley/renderers/` message rendering logic.
- `shelley/views/` discord UI components.
- `templates/` placeholder message templates.
- `config.json` safe bot configuration.

Real private `config.*.json`, runtime state files, SSH keys, passwords, tokens, and private infrastructure values must stay outside the repository.
