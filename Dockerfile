FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml ./
COPY src ./src
COPY mcp-config ./mcp-config
COPY config ./config

RUN pip install --no-cache-dir .

EXPOSE 8080
ENTRYPOINT ["opsgentic-api"]
