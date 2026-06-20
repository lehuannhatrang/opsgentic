FROM python:3.12-slim

# Node.js is needed to run kubernetes-mcp-server via npx (stdio transport).
RUN apt-get update \
    && apt-get install -y --no-install-recommends nodejs npm \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml ./
COPY src ./src
COPY mcp-config ./mcp-config
COPY config ./config

RUN pip install --no-cache-dir .

EXPOSE 8080
ENTRYPOINT ["opsgentic-api"]
