#!/usr/bin/env bash
# Map the Lambda IAM role to the Kubernetes group used by the RoleBindings.
# Run once per cluster.
# Usage:
#   export AWS_PROFILE=client-nonprod
#   ./scripts/grant-eks-access.sh CLUSTER_NAME AWS_REGION LAMBDA_ROLE_ARN [RBAC_GROUP]

set -euo pipefail

CLUSTER="${1:?cluster name}"
REGION="${2:?aws region}"
ROLE_ARN="${3:?lambda role arn}"
GROUP="${4:-start-stop-automation}"

if aws eks create-access-entry \
  --region "$REGION" \
  --cluster-name "$CLUSTER" \
  --principal-arn "$ROLE_ARN" \
  --type STANDARD \
  --kubernetes-groups "$GROUP" \
  --no-cli-pager; then
  echo "created access entry: $ROLE_ARN -> group $GROUP on $CLUSTER"
else
  aws eks update-access-entry \
    --region "$REGION" \
    --cluster-name "$CLUSTER" \
    --principal-arn "$ROLE_ARN" \
    --kubernetes-groups "$GROUP" \
    --no-cli-pager
  echo "updated access entry: $ROLE_ARN -> group $GROUP on $CLUSTER"
fi
