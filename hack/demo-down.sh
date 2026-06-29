#!/usr/bin/env bash
# Tear down the OpsGentic demo created by hack/demo-up.sh.
#
# Removes the demo ArgoCD Applications, OpsGentic, and the Helm releases
# (ArgoCD + kube-prometheus-stack). It does NOT delete your cluster — it only
# prints the command to do so, so you stay in control.
set -uo pipefail
cd "$(dirname "$0")/.."

ARGOCD_NS="${ARGOCD_NS:-argocd}"
PROM_NS="${PROM_NS:-prometheus}"
PROM_RELEASE="kube-prometheus-stack"
OPSGENTIC_NS="opsgentic"
DEMO_APPS=(payments-api checkout-api orders-api inventory-api)
LOCAL_CLUSTER_NAME="${LOCAL_CLUSTER_NAME:-opsgentic-demo}"

if [ -t 1 ]; then C_B='\033[1m'; C_0='\033[0m'; else C_B=; C_0=; fi
step() { printf "\n${C_B}==> %s${C_0}\n" "$*"; }

step "Removing demo ArgoCD Applications and their workloads"
for app in "${DEMO_APPS[@]}"; do
  kubectl -n "$ARGOCD_NS" delete application "$app" --ignore-not-found --wait=false
  kubectl delete namespace "${app%-api}" --ignore-not-found --wait=false
done

step "Uninstalling OpsGentic"
kubectl delete namespace "$OPSGENTIC_NS" --ignore-not-found --wait=false

step "Uninstalling Helm releases"
helm uninstall "$PROM_RELEASE" -n "$PROM_NS" 2>/dev/null || true
kubectl delete namespace "$PROM_NS" --ignore-not-found --wait=false
helm uninstall argocd -n "$ARGOCD_NS" 2>/dev/null || true
kubectl delete namespace "$ARGOCD_NS" --ignore-not-found --wait=false

cat <<EOF

Done. The cluster itself was left running on purpose.
To delete a local cluster created by demo-up.sh, run ONE of:

  k3d cluster delete ${LOCAL_CLUSTER_NAME}
  minikube delete -p ${LOCAL_CLUSTER_NAME}
  kind delete cluster --name ${LOCAL_CLUSTER_NAME}

The generated bootstrap.env (contains your PAT) is still on disk — remove it with:
  rm -f bootstrap.env
EOF
