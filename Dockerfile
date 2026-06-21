FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml ./
COPY src ./src
COPY mcp-config ./mcp-config
COPY config ./config
# Baked-in fallback; in k8s the opsgentic-skills ConfigMap overrides /app/agent-skills.
COPY deploy/manifests/agent-skills ./agent-skills

RUN pip install --no-cache-dir .

EXPOSE 8080
ENTRYPOINT ["opsgentic-api"]
