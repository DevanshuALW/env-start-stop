#!/usr/bin/env bash
# Apply the start-stop Role + RoleBinding to namespaces on one cluster.
# Usage:
#   export AWS_PROFILE=client-nonprod
#   export KUBE_CONTEXT=arn:aws:eks:us-east-1:123:cluster/nonprod-eks
#   export RBAC_GROUP=start-stop-automation
#   ./scripts/apply-rbac.sh argocd dev-app uat-app

set -euo pipefail

: "${KUBE_CONTEXT:?set KUBE_CONTEXT to the target cluster context}"
RBAC_GROUP="${RBAC_GROUP:-start-stop-automation}"
ARGOCD_NAMESPACE="${ARGOCD_NAMESPACE:-argocd}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

if [[ $# -lt 1 ]]; then
  echo "usage: $0 <namespace> [namespace...]" >&2
  exit 1
fi

for ns in "$@"; do
  if [[ "$ns" == "$ARGOCD_NAMESPACE" ]]; then
    rendered=$(sed -e "s/__ARGOCD_NAMESPACE__/${ns}/g" -e "s/__RBAC_GROUP__/${RBAC_GROUP}/g" "$ROOT/rbac/argocd-role.yaml")
  else
    rendered=$(sed -e "s/__NAMESPACE__/${ns}/g" -e "s/__RBAC_GROUP__/${RBAC_GROUP}/g" "$ROOT/rbac/namespace-role.yaml")
  fi
  echo "apply $ns"
  printf '%s\n' "$rendered" | kubectl --context "$KUBE_CONTEXT" apply -f -
done
