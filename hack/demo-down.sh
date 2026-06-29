#!/usr/bin/env bash
# Tear down the OpsGentic demo created by hack/demo-up.sh.
#
# Removes the demo ArgoCD Applications + their workloads and OpsGentic, then
# uninstalls the Helm releases (ArgoCD + kube-prometheus-stack).
#
# Safety:
#   - Application finalizers are stripped BEFORE deletion, so the argocd namespace
#     never gets stuck in Terminating.
#   - The argocd / prometheus namespaces are NOT deleted by default (they may have
#     pre-existed this demo). Pass --purge-namespaces (or answer the prompt) to drop them.
#   - The cluster is never deleted — the delete command is only printed.
set -uo pipefail
cd "$(dirname "$0")/.."

ARGOCD_NS="${ARGOCD_NS:-argocd}"
PROM_NS="${PROM_NS:-prometheus}"
PROM_RELEASE="kube-prometheus-stack"
ARGOCD_RELEASE="${ARGOCD_RELEASE:-argocd}"
OPSGENTIC_NS="opsgentic"
DEMO_APPS=(payments-api checkout-api orders-api inventory-api)
LOCAL_CLUSTER_NAME="${LOCAL_CLUSTER_NAME:-opsgentic-demo}"

PURGE_NS=false
[ "${1:-}" = "--purge-namespaces" ] && PURGE_NS=true

if [ -t 1 ]; then C_B=$'\033[1m'; C_0=$'\033[0m'; else C_B=; C_0=; fi
step() { printf "\n${C_B}==> %s${C_0}\n" "$*"; }

# Drop the Argo finalizer first so the CR deletes even after the controller is gone.
del_app() {
  kubectl -n "$ARGOCD_NS" patch application "$1" --type merge \
    -p '{"metadata":{"finalizers":null}}' >/dev/null 2>&1 || true
  kubectl -n "$ARGOCD_NS" delete application "$1" --ignore-not-found --wait=false
}

step "Removing demo ArgoCD Applications and their workloads"
for app in "${DEMO_APPS[@]}"; do
  del_app "$app"
  kubectl delete namespace "${app%-api}" --ignore-not-found --wait=false
done

step "Uninstalling OpsGentic"
kubectl delete namespace "$OPSGENTIC_NS" --ignore-not-found --wait=false

step "Uninstalling Helm releases"
helm uninstall "$PROM_RELEASE" -n "$PROM_NS" 2>/dev/null || true
helm uninstall "$ARGOCD_RELEASE" -n "$ARGOCD_NS" 2>/dev/null || true

# Optionally delete the argocd/prometheus namespaces. These may contain resources that
# pre-existed the demo, so this is opt-in. Strip any leftover Argo finalizers first.
if [ "$PURGE_NS" != true ] && [ -t 0 ]; then
  read -r -p $'\n    Also delete the \''"$ARGOCD_NS"$'\' and \''"$PROM_NS"$'\' namespaces? This removes EVERYTHING in them [y/N]: ' ans
  [[ "${ans:-}" =~ ^[Yy]$ ]] && PURGE_NS=true
fi
if [ "$PURGE_NS" = true ]; then
  step "Deleting namespaces $ARGOCD_NS and $PROM_NS"
  kubectl -n "$ARGOCD_NS" get applications.argoproj.io -o name 2>/dev/null | while read -r a; do
    kubectl -n "$ARGOCD_NS" patch "$a" --type merge -p '{"metadata":{"finalizers":null}}' >/dev/null 2>&1 || true
  done
  kubectl delete namespace "$ARGOCD_NS" --ignore-not-found --wait=false
  kubectl delete namespace "$PROM_NS" --ignore-not-found --wait=false
else
  printf "    Left namespaces '%s' and '%s' in place. Remove them with:\n" "$ARGOCD_NS" "$PROM_NS"
  printf "      kubectl delete namespace %s %s\n" "$ARGOCD_NS" "$PROM_NS"
fi

cat <<EOF

Done. The cluster itself was left running on purpose.
To delete a local cluster created by demo-up.sh, run ONE of:

  k3d cluster delete ${LOCAL_CLUSTER_NAME}
  minikube delete -p ${LOCAL_CLUSTER_NAME}
  kind delete cluster --name ${LOCAL_CLUSTER_NAME}

The generated bootstrap.env (contains your PAT) is still on disk — remove it with:
  rm -f bootstrap.env
EOF
