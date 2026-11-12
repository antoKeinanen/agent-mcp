import argparse
import logging
import sys

from agent_mcp import config, registry, server
from agent_mcp.cli import add_config_args, load_config


def main() -> None:
    parser = argparse.ArgumentParser(prog="agent-mcp", description="Modular MCP server with per-tool allowlists.")
    add_config_args(parser)
    parser.add_argument("--list", action="store_true", help="print the tools this config exposes and exit")
    args = parser.parse_args()

    try:
        cfg = load_config(args)
        exposed = config.plan(cfg, registry.builtin())
    except config.ConfigError as e:
        sys.exit(str(e))

    if args.list:
        for e in exposed:
            for t in e.tools:
                print(server.exposed_name(e.module.name, t.name))
        return

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s", stream=sys.stderr)
    if cfg.server.audit_log:
        file = logging.FileHandler(cfg.server.audit_log)
        file.setFormatter(logging.Formatter("%(message)s"))
        server.audit.addHandler(file)

    app = server.build(cfg, exposed)
    if cfg.server.transport == "stdio":
        app.run("stdio")
    else:
        app.run(
            "streamable-http",
            host=cfg.server.host,
            port=cfg.server.port,
            transport_security=server.transport_security(cfg.server),
        )


if __name__ == "__main__":
    main()
