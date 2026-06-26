import argparse
import os

from .bot import ShelleyBot
from .settings import config_path, load_json


TOKEN_PLACEHOLDER = "replace-with-your-discord-bot-token"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="shelley")
    parser.add_argument(
        "--config",
        help="Path to the bot configuration file.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.config:
        os.environ["BOT_CONFIG_PATH"] = args.config

    path = config_path()
    cfg = load_json(path)
    token = str(cfg.get("token", "")).strip()
    if not token or token == TOKEN_PLACEHOLDER:
        raise SystemExit(f"Missing bot token in {path}. Set `token` in the config file.")

    try:
        client_id = int(cfg.get("client_id", 0) or 0)
    except (TypeError, ValueError):
        raise SystemExit(f"Invalid client_id in {path}. Set it to a Discord application ID or 0.") from None

    ShelleyBot(application_id=client_id or None).run(token)


if __name__ == "__main__":
    main()
