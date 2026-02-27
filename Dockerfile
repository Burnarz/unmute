FROM ghcr.io/astral-sh/uv:0.6.17-debian AS build
WORKDIR /app

# Installation de Node.js 20 via le script Nodesource
RUN apt-get update && apt-get install -y curl ca-certificates gnupg \
    && mkdir -p /etc/apt/keyrings \
    && curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg \
    && echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_20.x nodistro main" | tee /etc/apt/sources.list.d/nodesource.list \
    && apt-get update && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

# Configure npm to install global packages in a known location
RUN npm config set prefix /app/node_modules

# Install MCP memory server
RUN mkdir -p /app/node_modules && \
    npm install -g @modelcontextprotocol/server-memory

ENV UV_COMPILE_BYTECODE=1 UV_LOCKED=1

RUN --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv run --no-dev echo hello

COPY . .
ENV HOSTNAME="0.0.0.0"

HEALTHCHECK --start-period=15s \
    CMD curl --fail http://localhost:80/metrics || exit 1

FROM build AS prod
# Running through uvicorn directly to be able to deactive the Websocket per message deflate which is slowing
# down the replies by a few ms.
CMD ["uv", "run", "--no-dev", "uvicorn", "unmute.main_websocket:app", "--host", "0.0.0.0", "--port", "80", "--ws-per-message-deflate=false"]


FROM build AS hot-reloading
CMD ["uv", "run", "--no-dev", "uvicorn", "unmute.main_websocket:app", "--reload", "--host", "0.0.0.0", "--port", "80", "--ws-per-message-deflate=false"]
