# SDK-only image for the open-source tryx402 gateway.
# (The hosted backend deploys from the private core repo — this image
#  exposes the CLI and MCP stdio server, not the HTTP API.)
FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml ./
COPY gateway/ gateway/
COPY tryx402/ tryx402/
RUN pip install --no-cache-dir -e .

# Default entrypoint is a shell so users pick `gateway ...` or
# `python3 -m gateway.mcp_server` with their own budget env:
#   docker run -e GATEWAY_MAX_BUDGET_USD=1.00 tryx402 gateway --help
CMD ["bash"]
