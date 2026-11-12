<img src="assets/icon.svg" width="64" height="64" alt="">

# agent-mcp

An MCP server that gives an agent (Hermes) access to a chosen subset of your tools. Tools live in modules. A config file decides which modules load and which of their tools are exposed. Nothing is exposed unless the config names it.

## Run

```sh
uv sync
cp .env.example .env        # and/or: cp agent-mcp.example.toml agent-mcp.toml
uv run agent-mcp --list     # print the tools this config exposes
uv run agent-mcp            # serve over stdio
```

Point Hermes at it in `~/.hermes/config.yaml`:

```yaml
mcp_servers:
  agent-mcp:
    command: uv
    args: ["run", "--directory", "/path/to/agent-mcp", "agent-mcp", "-e", "/path/to/agent-mcp/.env"]
```

`.env` and `agent-mcp.toml` are read from the current directory if present. `-e` and `-c` point elsewhere.

## Docker

`compose.yaml` runs the server in its own container over streamable HTTP. It publishes no ports. Other containers on the `agents` network reach it by Docker DNS at `http://agent-mcp:8765/mcp`, with no HTTP auth and no stdio.

```sh
docker compose pull && docker compose up -d     # or: docker compose up -d --build
docker compose logs agent-mcp                   # shows the Google sign-in link on first start
```

Sign-in needs no shell in the container:

1. Open the sign-in link from the log. Until you sign in, the calendar tools return the same link, so the agent can hand it to you.
2. Allow access. The browser lands on a `http://localhost/?code=…` page that doesn't load. Copy its URL.
3. Add it to `.env` as `AGENT_MCP__CALENDAR__AUTH_CODE="<that URL>"`, then run `docker compose up -d` within 10 minutes, before the code expires.

The container exchanges the code once and saves the refresh token in the `agent-mcp-data` volume, so restarts and image updates keep you signed in. To switch accounts, paste a new code. If Google revokes access, the tools return a fresh link.

`.github/workflows/container.yml` runs the tests, then builds the image for amd64 and arm64. On `main` it pushes `ghcr.io/antokeinanen/agent-mcp:latest` and `:sha-<commit>`. On a `v1.2.3` tag it also pushes `:1.2.3` and `:1.2`. Pull requests only build. The repository is private, so the package is too. Before pulling, run `docker login ghcr.io` with a personal access token that has the `read:packages` scope.

The container reads `.env` through `env_file`. Compose pins the transport, host and port, so a `stdio` value in `.env` can't break it. After editing `.env`, run `docker compose up -d` (not `restart`) so the new values load.

Put Hermes on the same network. In its compose file:

```yaml
services:
  hermes:
    networks: [agents]
networks:
  agents:
    external: true
```

and in `~/.hermes/config.yaml`:

```yaml
mcp_servers:
  agent-mcp:
    url: http://agent-mcp:8765/mcp
```

Without auth, the protection is network reachability, plus a Host check. `AGENT_MCP__SERVER__ALLOWED_HOSTS=agent-mcp` makes the server refuse any request whose Host header isn't `agent-mcp`, and any request that carries a browser `Origin`. That stops a web page in your browser from reaching it through the container's bridge IP. Anything that can join the `agents` network can call the tools, so attach only the agent to it.

## Config

Config can live in `.env`, in `agent-mcp.toml`, or both. The env file wins over the TOML file, and real environment variables win over both.

### .env

Variable names follow the TOML layout, with `__` between levels:

```sh
AGENT_MCP__SERVER__TRANSPORT=stdio                  # [server] transport
AGENT_MCP__EXAMPLE__TOOLS=greet                     # [modules.example] tools (also ENABLED)
AGENT_MCP__EXAMPLE__GREETING=hei                    # [modules.example.settings] greeting
AGENT_MCP__CALENDAR__ORGANIZATIONS__TKO__IGNORE=a,b # nested settings; dict keys come out lowercase
```

Lists are comma-separated, or JSON. A variable naming a setting that doesn't exist is an error. Setting a module's settings without its `TOOLS` exposes nothing and fails startup. See `.env.example`.

### TOML

```toml
[server]
transport = "stdio"           # or "streamable-http" (binds host/port, default 127.0.0.1:8765)
audit_log = "audit.jsonl"     # optional; one JSON line per tool call

[modules.example]
tools = ["greet"]             # required; ["*"] exposes every tool in the module
# enabled = false             # keep the section but load nothing

[modules.example.settings]    # validated against the module's Settings model
greeting = "hei"
```

Startup fails, listing every problem, on an unknown module, an unknown tool name, or invalid settings. Exposed tools are named `<module>_<tool>`.

## Calendar module

Lets the agent read your whole Google Calendar and offer you events, without being able to change anything you already have.

It uses a Google Calendar quirk. Events are created in a **hidden** "scheduling" calendar and invite a **visible** organization calendar as the only attendee. They show up in the visible calendar as pending invitations, and you RSVP to the ones you want to go to.

| Tool | Does |
| --- | --- |
| `calendar_calendars` | Lists every calendar, hidden ones included |
| `calendar_events` | Lists events in a calendar between two dates (name or ID) |
| `calendar_event` | Gets one event in full |
| `calendar_organizations` | Shows the configured organizations: sources, feeds, ignore lists, instructions |
| `calendar_scan` | Reads an organization's event feed and labels each upcoming event `missing`, `duplicate` or `ignored` |
| `calendar_schedule` | The only write. See below |

`calendar_schedule(organization, title, start, end?, description, location, source_url?, end_estimated, dry_run)`:

- creates the event only in the organization's scheduling calendar
- invites only the organization's calendar and leaves its RSVP as `needsAction`
- returns `ignored` if the title fuzzy-matches the organization's `ignore` list
- returns `duplicate` if the same `source_url` was scheduled before, or a similar title is already on either calendar that day
- with no `end` (or `end_estimated = true`), adds `[#]` to the title, uses `default_duration_minutes`, and says in the description that the end time was estimated

The client has no update, delete or RSVP calls at all. The OAuth token does have write scope, though, so the module's code is what limits writes. If you want Google itself to enforce the limit, put the scheduling calendar on a separate Google account and sign in as that account.

### Event feeds

An organization with a `feed` can be scanned. `calendar_scan(organization, days=60)` fetches the upcoming events and checks each one with the same rules `schedule` applies. Every event comes back with the `schedule` arguments ready to pass on: title, start, description, location and `source_url`. Scanning writes nothing.

| Feed | Source | Notes |
| --- | --- | --- |
| `tko-aly` | `https://event-api.tko-aly.fi/api/events` | No end times, so `schedule` estimates them and adds `[#]`. Times are Helsinki local. Descriptions are HTML converted to text, with links, price, registration times and the event page appended. `source_url` is the event's page on members.tko-aly.fi |

To add a feed, write an async function in `src/agent_mcp/modules/calendar/feeds.py` that returns `FeedEvent`s, and register it in `FEEDS`. For an organization without a feed, give the agent a fetch tool and let it read `sources`.

### Setup

1. In Google Cloud, enable the Calendar API and create an OAuth client of type **Desktop app**. Put its ID and secret in `.env` as `AGENT_MCP__CALENDAR__CLIENT_ID` and `AGENT_MCP__CALENDAR__CLIENT_SECRET`.
2. Create the scheduling calendar (e.g. "ai scheduling") and hide it in Google Calendar.
3. Fill in the calendar block of `.env` (see `.env.example`). Each `AGENT_MCP__CALENDAR__ORGANIZATIONS__<KEY>__*` group is one organization.
4. Sign in. In Docker, follow the steps under [Docker](#docker). On a machine with uv and a browser, `uv run agent-mcp-google-auth` signs you in and writes `AGENT_MCP__CALENDAR__REFRESH_TOKEN` into `.env`.

### Agent prompt

With the rules in config, the prompt can be short:

> For each organization from `calendar_organizations`, run `calendar_scan`. For each event marked `missing`, check that it isn't really the same as an existing or ignored event, then call `calendar_schedule` with its `schedule` arguments. Add an `end` only if the description clearly states one. Report what you scheduled and what you skipped.

## Writing a module

Add a file under `src/agent_mcp/modules/`. Any `Module` subclass there is discovered by its `name`.

```python
from pydantic import BaseModel
from agent_mcp.module import Module, tool

class NotesSettings(BaseModel):
    root: str

class Notes(Module[NotesSettings]):
    name = "notes"
    Settings = NotesSettings

    async def start(self) -> None: ...   # open clients before calls arrive
    async def stop(self) -> None: ...    # close them on shutdown

    @tool(read_only=True)
    def search(self, query: str) -> list[str]:
        """Search notes for a phrase."""   # becomes the tool description
        ...

    @tool(destructive=True)
    async def delete(self, note_id: str) -> None:
        """Delete a note."""
        ...
```

Parameters and return types become the tool's JSON schema. Raise `agent_mcp.module.ToolError` for failures the agent should read about, such as bad arguments or an upstream error. Any other exception reaches the agent only as "Error executing tool …", and its text stays in the server log. `read_only` and `destructive` are sent to the agent as MCP tool hints. They don't enforce anything. The allowlist is what restricts access.

## Test

```sh
uv run pytest
```
