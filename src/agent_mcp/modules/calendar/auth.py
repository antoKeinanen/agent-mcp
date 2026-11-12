"""One-time Google sign-in: `uv run agent-mcp-google-auth [calendar|todo]`.

Reads the module's OAuth client ID and secret from config (normally `.env`), signs you in
through the browser, and writes the refresh token back into the env file.
"""

import argparse
import sys
from typing import Any

from dotenv import set_key
from pydantic import BaseModel, ValidationError

from agent_mcp import config
from agent_mcp.cli import add_config_args, env_file, load_config
from agent_mcp.google_signin import AUTH_URI, TOKEN_URI, GoogleSignIn

from .. import todo
from . import CalendarSettings
from .signin import SIGN_IN

MODULES: dict[str, tuple[type[BaseModel], GoogleSignIn]] = {
    "calendar": (CalendarSettings, SIGN_IN),
    "todo": (todo.TodoSettings, todo.SIGN_IN),
}


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="agent-mcp-google-auth",
        description="Sign in with Google and save the module's REFRESH_TOKEN to the env file.",
    )
    parser.add_argument("module", nargs="?", choices=sorted(MODULES), default="calendar")
    add_config_args(parser)
    args = parser.parse_args()

    try:
        cfg = load_config(args)
    except config.ConfigError as e:
        sys.exit(str(e))
    model, sign_in = MODULES[args.module]
    mc = cfg.modules.get(args.module)
    try:
        settings: Any = model.model_validate(mc.settings if mc else {})
    except ValidationError as e:
        sys.exit(f"{args.module} settings: {e}")
    if not (settings.client_id and settings.client_secret):
        sys.exit(
            f"set {sign_in.var('CLIENT_ID')} and {sign_in.var('CLIENT_SECRET')} first "
            "(Google Cloud > APIs & Services > Credentials > OAuth client ID > Desktop app)"
        )

    from google_auth_oauthlib.flow import InstalledAppFlow

    client = {
        "installed": {
            "client_id": settings.client_id,
            "client_secret": settings.client_secret.get_secret_value(),
            "auth_uri": AUTH_URI,
            "token_uri": TOKEN_URI,
            "redirect_uris": ["http://localhost"],
        }
    }
    flow = InstalledAppFlow.from_client_config(client, list(sign_in.scopes))
    creds = flow.run_local_server(port=0, open_browser=True, access_type="offline", prompt="consent")
    if not creds.refresh_token:
        sys.exit("Google returned no refresh token; revoke the app's access at myaccount.google.com and retry")

    path = env_file(args)
    path.touch(mode=0o600, exist_ok=True)
    token_var = sign_in.var("REFRESH_TOKEN")
    set_key(path, token_var, creds.refresh_token)
    print(f"saved {token_var} to {path}")


if __name__ == "__main__":
    main()
