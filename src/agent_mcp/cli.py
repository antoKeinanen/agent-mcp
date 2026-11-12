"""Config loading shared by the command-line entry points."""

import argparse
import os
from pathlib import Path

from dotenv import dotenv_values

from agent_mcp import config, registry

DEFAULT_CONFIG = Path("agent-mcp.toml")
DEFAULT_ENV = Path(".env")


def add_config_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("-c", "--config", type=Path, help=f"TOML config (optional; default {DEFAULT_CONFIG} if present)")
    parser.add_argument("-e", "--env-file", type=Path, help=f"dotenv file (default {DEFAULT_ENV} if present)")


def env_file(args: argparse.Namespace) -> Path:
    return args.env_file or DEFAULT_ENV


def load_config(args: argparse.Namespace) -> config.Config:
    """TOML file, then the env file, then the real environment; later ones win."""
    path = args.config or (DEFAULT_CONFIG if DEFAULT_CONFIG.exists() else None)
    dotenv = env_file(args)
    if args.env_file and not dotenv.exists():
        raise config.ConfigError([f"{dotenv}: no such file"])
    env = {k: v for k, v in dotenv_values(dotenv).items() if v is not None} if dotenv.exists() else {}
    env |= os.environ
    return config.load(path, env, registry.builtin())
