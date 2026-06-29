#!/usr/bin/env bash
# One-command demo for OpsGentic.
#
# Brings up a full end-to-end playground:
#   1. a Kubernetes cluster (your current context, or a fresh k3d/minikube/kind)
#   2. ArgoCD                 (Helm)
#   3. kube-prometheus-stack  (Helm — Prometheus + Alertmanager, wired for the demo)
#   4. the demo-workload apps (ArgoCD Applications pointing at YOUR fork)
#   5. OpsGentic              (via ./bootstrap.sh)
#
# GitHub auth is a fine-grained PAT: the script prints the creation link, reads the
# token, then forks lehuannhatrang/demo-workload into your account automatically.
#
# Idempotent — safe to re-run. Most prompts can be pre-answered via env vars
# (see the variables block below); unset values are asked interactively.
set -euo pipefail
cd "$(dirname "$0")/.."   # repo root
REPO_ROOT="$(pwd)"

# ---- tunables (env overrides; interactive otherwise) ------------------------
UPSTREAM_OWNER="${UPSTREAM_OWNER:-lehuannhatrang}"
DEMO_REPO="${DEMO_REPO:-demo-workload}"
DEMO_BRANCH="${DEMO_BRANCH:-main}"
DEMO_APPS=(payments-api checkout-api orders-api inventory-api)

PROM_NS="${PROM_NS:-prometheus}"
PROM_RELEASE="kube-prometheus-stack"   # must match PROMETHEUS_URL in deploy/manifests/configmap.yaml
ARGOCD_NS="${ARGOCD_NS:-argocd}"
OPSGENTIC_NS="opsgentic"

GITHUB_TOKEN="${GITHUB_TOKEN:-}"
LLM_BASE_URL="${LLM_BASE_URL:-}"
LLM_API_KEY="${LLM_API_KEY:-}"
LLM_MODEL="${LLM_MODEL:-}"
CLUSTER_CHOICE="${CLUSTER_CHOICE:-}"     # k8s (current context) | k3d | minikube | kind
LOCAL_CLUSTER_NAME="${LOCAL_CLUSTER_NAME:-opsgentic-demo}"

# ---- pretty logging --------------------------------------------------------
if [ -t 1 ]; then C_B=$'\033[1m'; C_G=$'\033[32m'; C_Y=$'\033[33m'; C_R=$'\033[31m'; C_0=$'\033[0m'; else C_B=; C_G=; C_Y=; C_R=; C_0=; fi
step() { printf "\n${C_B}==> %s${C_0}\n" "$*"; }
info() { printf "    %s\n" "$*"; }
ok()   { printf "    ${C_G}ok${C_0} %s\n" "$*"; }
warn() { printf "    ${C_Y}!!${C_0} %s\n" "$*"; }
die()  { printf "${C_R}error:${C_0} %s\n" "$*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1; }

# ===========================================================================
step "Preflight — required tools"
MISSING=()
for t in kubectl helm git curl jq openssl; do need "$t" || MISSING+=("$t"); done
[ ${#MISSING[@]} -eq 0 ] || die "missing tools: ${MISSING[*]} — install them and re-run."
ok "kubectl helm git curl jq openssl"

# ===========================================================================
step "Kubernetes cluster"
CLUSTER_REACHABLE=false
kubectl cluster-info >/dev/null 2>&1 && CLUSTER_REACHABLE=true
CTX="$(kubectl config current-context 2>/dev/null || echo '?')"

# Decide the choice: explicit env var wins; otherwise prefer the current context if
# it's reachable, else ask.
if [ -z "$CLUSTER_CHOICE" ]; then
  if [ "$CLUSTER_REACHABLE" = true ]; then
    CLUSTER_CHOICE=k8s
  else
    warn "no reachable cluster from the current kubectl context ($CTX)."
    echo "    Choose a cluster: k8s (current context) / [k3d] / minikube / kind / abort"
    read -r -p "    cluster> " CLUSTER_CHOICE
    CLUSTER_CHOICE="${CLUSTER_CHOICE:-k3d}"
  fi
fi

case "$CLUSTER_CHOICE" in
  k8s|current|existing)
    [ "$CLUSTER_REACHABLE" = true ] || die "CLUSTER_CHOICE=k8s but the current context ($CTX) is not reachable."
    ok "using current context: $CTX"
    ;;
  *)
  case "$CLUSTER_CHOICE" in
    k3d)
      need k3d || die "k3d not installed — see https://k3d.io/#installation (needs Docker), then re-run."
      need docker || die "k3d needs Docker running."
      if k3d cluster list 2>/dev/null | awk '{print $1}' | grep -qx "$LOCAL_CLUSTER_NAME"; then
        info "k3d cluster '$LOCAL_CLUSTER_NAME' already exists."
      else
        info "creating k3d cluster '$LOCAL_CLUSTER_NAME' ..."
        k3d cluster create "$LOCAL_CLUSTER_NAME" --wait
      fi
      kubectl config use-context "k3d-$LOCAL_CLUSTER_NAME" >/dev/null
      ;;
    minikube)
      need minikube || die "minikube not installed — see https://minikube.sigs.k8s.io/docs/start/ then re-run."
      minikube status -p "$LOCAL_CLUSTER_NAME" >/dev/null 2>&1 || minikube start -p "$LOCAL_CLUSTER_NAME"
      kubectl config use-context "$LOCAL_CLUSTER_NAME" >/dev/null
      ;;
    kind)
      need kind || die "kind not installed — see https://kind.sigs.k8s.io/docs/user/quick-start/ then re-run."
      need docker || die "kind needs Docker running."
      kind get clusters 2>/dev/null | grep -qx "$LOCAL_CLUSTER_NAME" || kind create cluster --name "$LOCAL_CLUSTER_NAME"
      kubectl config use-context "kind-$LOCAL_CLUSTER_NAME" >/dev/null
      ;;
    abort|*) die "aborted — no cluster." ;;
  esac
  kubectl cluster-info >/dev/null 2>&1 || die "cluster still unreachable after setup."
  ok "local cluster ready ($CLUSTER_CHOICE)"
  ;;
esac

# ===========================================================================
step "GitHub authentication (fine-grained PAT)"
if [ -z "$GITHUB_TOKEN" ]; then
  cat <<EOF
    OpsGentic needs a GitHub token to read your fork and open remediation PRs.
    Create a fine-grained Personal Access Token here:

      ${C_B}https://github.com/settings/personal-access-tokens/new${C_0}

    Configure it as:
      - Resource owner:     your account
      - Repository access:  "All repositories"  (or "Only select repositories" -> add
                            your ${DEMO_REPO} fork). Do NOT pick "Public repositories
                            (read-only)" — that grants read only and PR creation will 403.
      - Permissions (Repository):
          Contents          : Read and write
          Pull requests     : Read and write
          Administration    : Read and write   (needed to create the fork)

EOF
  read -r -s -p "    Paste PAT (hidden): " GITHUB_TOKEN; echo
fi
[ -n "$GITHUB_TOKEN" ] || die "no PAT provided."

gh_api() { # gh_api METHOD PATH  -> body on stdout, http code on fd-less check
  curl -sS -X "$1" \
    -H "Authorization: Bearer $GITHUB_TOKEN" \
    -H "Accept: application/vnd.github+json" \
    -H "X-GitHub-Api-Version: 2022-11-28" \
    "https://api.github.com$2"
}
GH_USER="$(gh_api GET /user | jq -r '.login // empty')"
[ -n "$GH_USER" ] || die "PAT rejected by GitHub (GET /user failed) — check the token."
ok "authenticated as: $GH_USER"

# ---- fork the demo repo (idempotent) ---------------------------------------
step "Forking ${UPSTREAM_OWNER}/${DEMO_REPO} -> ${GH_USER}/${DEMO_REPO}"
if gh_api GET "/repos/${GH_USER}/${DEMO_REPO}" | jq -e '.full_name' >/dev/null 2>&1; then
  ok "fork already exists: ${GH_USER}/${DEMO_REPO}"
else
  gh_api POST "/repos/${UPSTREAM_OWNER}/${DEMO_REPO}/forks" >/dev/null
  info "fork requested — waiting for GitHub to provision it ..."
  for i in $(seq 1 30); do
    if gh_api GET "/repos/${GH_USER}/${DEMO_REPO}" | jq -e '.full_name' >/dev/null 2>&1; then
      ok "fork ready: ${GH_USER}/${DEMO_REPO}"; break
    fi
    sleep 3
    [ "$i" = 30 ] && die "fork did not appear in time — check https://github.com/${GH_USER}/${DEMO_REPO}"
  done
fi
FORK_URL="https://github.com/${GH_USER}/${DEMO_REPO}.git"

# Fail fast if the PAT can read but not WRITE the repo (the usual "read-only PAT" mistake):
# opsgentic needs push to create the remediation branch, else PR creation 403s at runtime.
CAN_PUSH="$(gh_api GET "/repos/${GH_USER}/${DEMO_REPO}" | jq -r '.permissions.push // false')"
if [ "$CAN_PUSH" != true ]; then
  die "PAT can read but not write ${GH_USER}/${DEMO_REPO} (.permissions.push=false).
       Re-create the token with Contents + Pull requests = Read AND write on this repo
       (not 'Public repositories (read-only)'), then re-run."
fi
ok "PAT has write access (push) to ${GH_USER}/${DEMO_REPO}"

# ===========================================================================
step "LLM endpoint (optional — leave blank for the canned-response fallback)"
if [ -z "$LLM_BASE_URL" ] && [ -z "${OPSGENTIC_NONINTERACTIVE:-}" ]; then
  read -r -p "    LLM_BASE_URL (OpenAI-compatible, e.g. http://vllm:8000/v1) [blank]: " LLM_BASE_URL
  if [ -n "$LLM_BASE_URL" ]; then
    read -r -s -p "    LLM_API_KEY (hidden) [blank]: " LLM_API_KEY; echo
    read -r -p "    LLM_MODEL [local-model]: " LLM_MODEL
  fi
fi
LLM_MODEL="${LLM_MODEL:-local-model}"
[ -n "$LLM_BASE_URL" ] && ok "LLM: $LLM_BASE_URL ($LLM_MODEL)" || warn "no LLM — RCA/remediation use the built-in canned fallback."

# ===========================================================================
step "Helm repositories"
helm repo add argo https://argoproj.github.io/argo-helm >/dev/null 2>&1 || true
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts >/dev/null 2>&1 || true
helm repo update >/dev/null
ok "argo, prometheus-community"

# ---- ArgoCD ----------------------------------------------------------------
step "Installing ArgoCD (Helm) in namespace '$ARGOCD_NS'"
helm upgrade --install argocd argo/argo-cd \
  --namespace "$ARGOCD_NS" --create-namespace \
  --set configs.params."server\.insecure"=true \
  --wait --timeout 8m
kubectl -n "$ARGOCD_NS" rollout status deploy/argocd-server --timeout=180s || warn "argocd-server not ready yet"
ok "ArgoCD installed"

# ---- kube-prometheus-stack -------------------------------------------------
step "Installing kube-prometheus-stack (Helm) in namespace '$PROM_NS'"
# Permissive AlertmanagerConfig selection so the demo apps' cross-namespace routes
# are picked up automatically (replaces the manual selector wiring in QUICKSTART B5).
PROM_VALUES="$(mktemp)"
cat > "$PROM_VALUES" <<'YAML'
alertmanager:
  alertmanagerSpec:
    alertmanagerConfigSelector: {}
    alertmanagerConfigNamespaceSelector: {}
    alertmanagerConfigMatcherStrategy:
      type: None
grafana:
  enabled: true
YAML
helm upgrade --install "$PROM_RELEASE" prometheus-community/kube-prometheus-stack \
  --namespace "$PROM_NS" --create-namespace \
  -f "$PROM_VALUES" \
  --wait --timeout 10m
rm -f "$PROM_VALUES"
ok "Prometheus + Alertmanager installed (release '$PROM_RELEASE')"

# ===========================================================================
step "Deploying demo workloads via ArgoCD (pointing at your fork)"
kubectl wait --for=condition=Established crd/applications.argoproj.io --timeout=120s >/dev/null 2>&1 || true
for app in "${DEMO_APPS[@]}"; do
  kubectl apply -f - >/dev/null <<YAML
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: ${app}
  namespace: ${ARGOCD_NS}
  finalizers:
    - resources-finalizer.argocd.argoproj.io
spec:
  project: default
  source:
    repoURL: ${FORK_URL}
    targetRevision: ${DEMO_BRANCH}
    path: apps/${app}
  destination:
    server: https://kubernetes.default.svc
    namespace: ${app%-api}
  syncPolicy:
    automated:
      prune: true
      selfHeal: true
    syncOptions:
      - CreateNamespace=true
YAML
  info "ArgoCD Application: $app -> apps/$app"
done
ok "demo applications created (auto-sync) — they will start failing and firing alerts"

# ===========================================================================
step "Deploying OpsGentic via bootstrap.sh"
BOOT_ENV="$REPO_ROOT/bootstrap.env"
{
  echo "# Generated by hack/demo-up.sh — gitignored. Contains your PAT; do not commit."
  echo "LLM_BASE_URL=${LLM_BASE_URL}"
  echo "LLM_API_KEY=${LLM_API_KEY}"
  echo "LLM_MODEL=${LLM_MODEL}"
  echo "GITHUB_TOKEN=${GITHUB_TOKEN}"
  echo "GITHUB_MCP_TOKEN="
  echo "GITHUB_APP_ID="
  echo "GITHUB_APP_INSTALLATION_ID="
  echo "GITHUB_APP_PRIVATE_KEY_PATH="
  echo "# Image left unset on purpose -> bootstrap keeps the prebuilt tag in kustomization.yaml."
  echo "IMAGE="
  echo "IMAGE_TAG="
  echo "POSTGRES_PASSWORD="
  echo "AUTO_APPROVE=true"
} > "$BOOT_ENV"
chmod 600 "$BOOT_ENV"
ok "wrote $BOOT_ENV (chmod 600)"
# bootstrap.sh cd's to the repo root and sources "./$1", so pass a basename, not an absolute path.
"$REPO_ROOT/bootstrap.sh" "bootstrap.env"

# ===========================================================================
ARGO_PW="$(kubectl -n "$ARGOCD_NS" get secret argocd-initial-admin-secret -o jsonpath='{.data.password}' 2>/dev/null | base64 -d 2>/dev/null || echo '<not-found>')"
cat <<EOF

${C_B}========================================================================${C_0}
${C_G} OpsGentic demo is up.${C_0}
${C_B}========================================================================${C_0}

  Fork (PRs land here):  https://github.com/${GH_USER}/${DEMO_REPO}

  ${C_B}ArgoCD${C_0}
    kubectl -n ${ARGOCD_NS} port-forward svc/argocd-server 8080:80
    open http://localhost:8080   (user: admin  pass: ${ARGO_PW})

  ${C_B}Prometheus / Grafana${C_0}
    kubectl -n ${PROM_NS} port-forward svc/${PROM_RELEASE}-prometheus 9090:9090
    kubectl -n ${PROM_NS} port-forward svc/${PROM_RELEASE}-grafana 3000:80   (admin / prom-operator)

  ${C_B}OpsGentic API + Console${C_0}
    kubectl -n ${OPSGENTIC_NS} port-forward svc/opsgentic 31080:80
    kubectl -n ${OPSGENTIC_NS} port-forward svc/opsgentic-console 8088:80   (open http://localhost:8088)

  ${C_B}Verify end-to-end${C_0} (with the API port-forward above running):
    TID=\$(curl -s localhost:31080/chat -XPOST -H 'content-type: application/json' \\
      -d '{"title":"high memory","message":"payments-api high memory","labels":{"namespace":"payments","app":"payments-api"}}' | jq -r .thread_id)
    curl -s localhost:31080/runs/\$TID | jq '{status, awaiting_approval, pr_url: .state.pr_url}'

  Tear it all down with:  ./hack/demo-down.sh
EOF
