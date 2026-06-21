#!/usr/bin/env bash
# Bootstrap opsgentic on a Kubernetes cluster: generate secrets from bootstrap.env, set the
# image, apply the kustomize base, and wait for the rollouts. Idempotent — safe to re-run.
set -euo pipefail
cd "$(dirname "$0")"

NS=opsgentic
ENV_FILE="${1:-bootstrap.env}"
MANIFESTS=deploy/manifests

[ -f "$ENV_FILE" ] || { echo "Missing $ENV_FILE — copy bootstrap.env.example to bootstrap.env and fill it in."; exit 1; }
command -v kubectl >/dev/null || { echo "kubectl is required."; exit 1; }

set -a; . "./$ENV_FILE"; set +a

# GitHub auth: a PAT (GITHUB_TOKEN) is the default; a GitHub App is optional. Require one.
GITHUB_MCP_TOKEN="${GITHUB_MCP_TOKEN:-${GITHUB_TOKEN:-}}"   # reuse the PAT for github-mcp reads
APP_KEY=""
if [ -n "${GITHUB_APP_PRIVATE_KEY_PATH:-}" ]; then
  [ -f "$GITHUB_APP_PRIVATE_KEY_PATH" ] || { echo "GitHub App key not found: $GITHUB_APP_PRIVATE_KEY_PATH"; exit 1; }
  APP_KEY="$GITHUB_APP_PRIVATE_KEY_PATH"
fi
if [ -z "${GITHUB_TOKEN:-}" ] && [ -z "$APP_KEY" ]; then
  echo "Set GITHUB_TOKEN (PAT, default) or a GitHub App (GITHUB_APP_* + PEM) in $ENV_FILE."; exit 1
fi

PGPASS="${POSTGRES_PASSWORD:-}"
[ -n "$PGPASS" ] || PGPASS="$(openssl rand -base64 24 | tr -d '/+=' | cut -c1-28)"

echo "==> Ensuring namespace '$NS'"
kubectl create namespace "$NS" --dry-run=client -o yaml | kubectl apply -f -

echo "==> Generating $MANIFESTS/secrets.yaml (gitignored)"
SECRET_ARGS=(
  --from-literal=LLM_BASE_URL="${LLM_BASE_URL:-}"
  --from-literal=LLM_API_KEY="${LLM_API_KEY:-}"
  --from-literal=GITHUB_TOKEN="${GITHUB_TOKEN:-}"
  --from-literal=GITHUB_MCP_TOKEN="${GITHUB_MCP_TOKEN:-}"
  --from-literal=DATABASE_URL="postgresql://opsgentic:${PGPASS}@postgres.${NS}.svc:5432/opsgentic"
)
[ -n "$APP_KEY" ] && SECRET_ARGS+=(--from-file=GITHUB_APP_PRIVATE_KEY="$APP_KEY")
kubectl create secret generic opsgentic-secrets -n "$NS" "${SECRET_ARGS[@]}" --dry-run=client -o yaml > "$MANIFESTS/secrets.yaml"
echo '---' >> "$MANIFESTS/secrets.yaml"
kubectl create secret generic postgres-secrets -n "$NS" \
  --from-literal=POSTGRES_USER=opsgentic \
  --from-literal=POSTGRES_PASSWORD="$PGPASS" \
  --from-literal=POSTGRES_DB=opsgentic \
  --dry-run=client -o yaml >> "$MANIFESTS/secrets.yaml"

if [ -n "${IMAGE:-}" ] && command -v kustomize >/dev/null; then
  echo "==> Setting image to ${IMAGE}:${IMAGE_TAG:-latest}"
  ( cd "$MANIFESTS" && kustomize edit set image "opsgentic=${IMAGE}:${IMAGE_TAG:-latest}" )
else
  echo "==> Skipping image edit (set images: in $MANIFESTS/kustomization.yaml manually if needed)"
fi

echo "==> kubectl apply -k $MANIFESTS"
kubectl apply -k "$MANIFESTS"

echo "==> Patching opsgentic-config (App IDs / model / auto-approve)"
kubectl -n "$NS" patch configmap opsgentic-config --type merge -p \
  "{\"data\":{\"GITHUB_APP_ID\":\"${GITHUB_APP_ID:-}\",\"GITHUB_APP_INSTALLATION_ID\":\"${GITHUB_APP_INSTALLATION_ID:-}\",\"LLM_MODEL\":\"${LLM_MODEL:-local-model}\",\"AUTO_APPROVE\":\"${AUTO_APPROVE:-false}\"}}"

echo "==> Rolling out"
kubectl -n "$NS" rollout restart deploy/opsgentic deploy/opsgentic-worker
for r in statefulset/postgres deploy/kubernetes-mcp deploy/github-mcp deploy/opsgentic deploy/opsgentic-worker; do
  kubectl -n "$NS" rollout status "$r" --timeout=180s || echo "   (warning: $r not ready yet)"
done

echo
echo "==> Done. Pods:"
kubectl -n "$NS" get pods
cat <<'NOTE'

Test (NodePort 31080 on any node):
  NODE=<node-ip>
  TID=$(curl -s $NODE:31080/chat -XPOST -H 'content-type: application/json' \
    -d '{"title":"t","message":"payments-api high memory","labels":{"namespace":"payments","app":"payments-api"}}' | jq -r .thread_id)
  curl -s $NODE:31080/runs/$TID | jq '{status, awaiting_approval, pr_url: .state.pr_url}'
NOTE
