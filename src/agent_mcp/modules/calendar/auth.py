"""One-time Google sign-in for the calendar module: `uv run agent-mcp-google-auth`.

Reads the OAuth client ID and secret from config (normally `.env`), signs you in through
the browser, and writes the refresh token back into the env file.
"""

import argparse
import sys

from dotenv import set_key

from agent_mcp import config
from agent_mcp.cli import add_config_args, env_file, load_config

from . import CalendarSettings
from .google import AUTH_URI, SCOPES, TOKEN_URI

TOKEN_VAR = f"{config.ENV_PREFIX}CALENDAR__REFRESH_TOKEN"


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="agent-mcp-google-auth",
        description=f"Sign in with Google and save {TOKEN_VAR} to the env file.",
    )
    add_config_args(parser)
    args = parser.parse_args()

    try:
        cfg = load_config(args)
    except config.ConfigError as e:
        sys.exit(str(e))
    mc = cfg.modules.get("calendar")
    settings = CalendarSettings.model_validate(mc.settings if mc else {})
    if not (settings.client_id and settings.client_secret):
        sys.exit(
            f"set {config.ENV_PREFIX}CALENDAR__CLIENT_ID and {config.ENV_PREFIX}CALENDAR__CLIENT_SECRET first "
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
    flow = InstalledAppFlow.from_client_config(client, SCOPES)
    creds = flow.run_local_server(port=0, open_browser=True, access_type="offline", prompt="consent")
    if not creds.refresh_token:
        sys.exit("Google returned no refresh token; revoke the app's access at myaccount.google.com and retry")

    path = env_file(args)
    path.touch(mode=0o600, exist_ok=True)
    set_key(path, TOKEN_VAR, creds.refresh_token)
    print(f"saved {TOKEN_VAR} to {path}")


if __name__ == "__main__":
    main()
