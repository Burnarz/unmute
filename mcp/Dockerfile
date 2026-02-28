FROM node:20-slim

WORKDIR /app

RUN apt-get update && apt-get install -y \
    python3 \
    python3-pip \
    curl \
    && rm -rf /var/lib/apt/lists/* \
    && curl -LsSf https://astral.sh/uv/install.sh | sh \
    && npm install -g pnpm@10.7.1

ENV PATH="/root/.local/bin:$PATH"

COPY package.json ./
RUN pnpm install --frozen-lockfile || pnpm install

COPY server.js ./
COPY mcp-servers.json ./

ENV NODE_ENV=production
ENV MCP_PORT=3001

EXPOSE 3001

CMD ["node", "server.js"]
