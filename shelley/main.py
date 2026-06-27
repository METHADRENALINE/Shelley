import argparse
import logging
import os

from .bot import ShelleyBot
from .config import ConfigError
from .db import DatabaseUnavailable, apply_schema, get_database
from .settings import config_path, env_name, load_config, reset_config_cache

logger = logging.getLogger(__name__)


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

    load_env_files()
    logging.basicConfig(
        level=os.getenv("SHELLEY_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("discord.ext.voice_recv.gateway").setLevel(logging.WARNING)
    logging.getLogger("discord.ext.voice_recv.reader").setLevel(logging.WARNING)
    reset_config_cache()
    path = config_path()
    try:
        cfg = load_config(path, validate_runtime=True)
    except ConfigError as e:
        raise SystemExit(str(e)) from e
    token = cfg.discord_token()
    if not token:
        raise SystemExit(f"Missing Discord token. Set DISCORD_TOKEN in .env.{env_name()} or in the local config file.")
    try:
        db = get_database(cfg)
        applied = apply_schema(db)
        if applied:
            logger.info("applied database schema", extra={"schema_versions": applied})
    except DatabaseUnavailable as e:
        raise SystemExit(str(e)) from e

    ShelleyBot(config=cfg).run(token, log_handler=None)


def load_env_files() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(f".env.{env_name()}", override=False)
    load_dotenv(".env", override=False)


if __name__ == "__main__":
    main()
