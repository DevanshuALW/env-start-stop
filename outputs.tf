output "function_name" {
  value = aws_lambda_function.start_stop.function_name
}

output "function_url" {
  value       = aws_lambda_function_url.start_stop.function_url
  description = "Paste this as the Slack app Request URL."
}

output "lambda_role_arn" {
  value       = aws_iam_role.lambda.arn
  description = "Use this principal on each EKS access entry."
}

output "rbac_group" {
  value = var.rbac_group
}

output "namespaces" {
  value = distinct([for t in var.targets : t.namespace])
}

output "rbac_namespaces_by_cluster" {
  description = "argocd + app namespaces to pass to scripts/apply-rbac.sh per cluster."
  value = {
    for r in var.regions :
    r.cluster => distinct(concat(
      [var.argocd_namespace],
      [for t in var.targets : t.namespace if t.region_key == r.key]
    ))
  }
}

output "clusters" {
  value = { for r in var.regions : r.key => { region = r.aws_region, cluster = r.cluster } }
}

output "slack_signing_secret_name" {
  value = aws_secretsmanager_secret.slack_signing_secret.name
}

output "slack_bot_token_secret_name" {
  value = aws_secretsmanager_secret.slack_bot_token.name
}

output "schedule_names" {
  value = [for s in aws_scheduler_schedule.nightly : s.name]
}
