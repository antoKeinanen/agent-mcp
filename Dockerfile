FROM python:3.13-slim

COPY --from=ghcr.io/astral-sh/uv:0.13.6 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never

WORKDIR /app
# Dependencies first so code changes don't reinstall them.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --no-install-project
COPY src ./src
RUN uv sync --locked --no-dev

RUN useradd --system --uid 10001 agent && mkdir /data && chown agent /data
# Saved Google sign-in lives here; a named volume inherits this ownership.
VOLUME /data
USER agent
ENV PATH="/app/.venv/bin:$PATH" \
    AGENT_MCP__SERVER__TRANSPORT=streamable-http \
    AGENT_MCP__SERVER__HOST=0.0.0.0 \
    AGENT_MCP__SERVER__PORT=8765
EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=3s CMD ["python", "-c", "import socket; socket.create_connection(('127.0.0.1', 8765), 2)"]
CMD ["agent-mcp"]
