# Slack slash command or nightly clock -> Function URL / Scheduler -> this Lambda.
# STOP: pause parent → pause apps → delete HPA → scale 0 → cron off → RDS stop
# START: RDS up → unpause apps → cron on → unpause parent → scale leftover 0 to HPA min

locals {
  region_by_key = { for r in var.regions : r.key => r }

  rds_instances = distinct(flatten([
    for t in var.targets : [
      for db in t.rds : {
        region = local.region_by_key[t.region_key].aws_region
        name   = db
      }
    ]
  ]))

  lambda_config = {
    argocd_namespace   = var.argocd_namespace
    slack_commands     = var.slack_commands
    modal_title        = var.modal_title
    intro_text         = "Pick a *region*. Defaults for that region are pre-selected."
    help_text          = "Use `${var.slack_commands[0]}` to open the form."
    region_placeholder = join(" or ", [for r in var.regions : r.short_name])
    schedule_reason    = "nightly schedule stop / start"
    defaults           = var.defaults
    regions = [
      for r in var.regions : {
        key   = r.key
        label = r.label
      }
    ]
    targets = {
      for t in var.targets : t.key => {
        region     = local.region_by_key[t.region_key].aws_region
        cluster    = local.region_by_key[t.region_key].cluster
        region_key = t.region_key
        label      = t.namespace
        display    = "${local.region_by_key[t.region_key].short_name} · ${t.namespace}"
        parent     = t.parent
        rds        = t.rds
        apps       = t.apps
      }
    }
  }

  target_keys = [for t in var.targets : t.key]
}

check "target_region_keys" {
  assert {
    condition     = alltrue([for t in var.targets : contains(keys(local.region_by_key), t.region_key)])
    error_message = "Every targets.region_key must match a regions.key."
  }
}

check "defaults_exist" {
  assert {
    condition = alltrue(flatten([
      for rk, keys in var.defaults : concat(
        [contains(keys(local.region_by_key), rk)],
        [for k in keys : contains(local.target_keys, k)]
      )
    ]))
    error_message = "defaults keys must be region keys, and each value must be a targets.key."
  }
}

check "slack_options_limit" {
  assert {
    condition = alltrue([
      for r in var.regions : length([for t in var.targets : t if t.region_key == r.key]) <= 100
    ])
    error_message = "Slack allows at most 100 namespaces per region in the form."
  }
}

locals {
  iam_statements = concat(
    [
      {
        Sid      = "Logs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:${var.aws_region}:${var.aws_account_id}:*"
      },
      {
        Sid    = "Secrets"
        Effect = "Allow"
        Action = ["secretsmanager:GetSecretValue"]
        Resource = [
          aws_secretsmanager_secret.slack_signing_secret.arn,
          aws_secretsmanager_secret.slack_bot_token.arn,
        ]
      },
      {
        Sid    = "DescribeEks"
        Effect = "Allow"
        Action = ["eks:DescribeCluster"]
        Resource = [
          for r in var.regions : "arn:aws:eks:${r.aws_region}:${var.aws_account_id}:cluster/${r.cluster}"
        ]
      },
      {
        Sid      = "InvokeSelf"
        Effect   = "Allow"
        Action   = ["lambda:InvokeFunction"]
        Resource = "arn:aws:lambda:${var.aws_region}:${var.aws_account_id}:function:${var.name_prefix}"
      },
    ],
    length(local.rds_instances) == 0 ? [] : [
      {
        Sid    = "RdsStartStop"
        Effect = "Allow"
        Action = ["rds:DescribeDBInstances", "rds:StartDBInstance", "rds:StopDBInstance"]
        Resource = [
          for db in local.rds_instances : "arn:aws:rds:${db.region}:${var.aws_account_id}:db:${db.name}"
        ]
      }
    ]
  )
}

data "archive_file" "lambda" {
  type        = "zip"
  output_path = "${path.module}/.build/lambda.zip"
  source {
    content  = file("${path.module}/lambda_function.py")
    filename = "lambda_function.py"
  }
  source {
    content  = jsonencode(local.lambda_config)
    filename = "config.json"
  }
}

resource "aws_secretsmanager_secret" "slack_signing_secret" {
  name                    = "${var.name_prefix}/slack-signing-secret"
  description             = "Slack signing secret for ${var.name_prefix}."
  recovery_window_in_days = var.secret_recovery_days
  tags                    = { Name = "${var.name_prefix}-slack-signing-secret" }
}

resource "aws_secretsmanager_secret" "slack_bot_token" {
  name                    = "${var.name_prefix}/slack-bot-token"
  description             = "Slack bot token for ${var.name_prefix}."
  recovery_window_in_days = var.secret_recovery_days
  tags                    = { Name = "${var.name_prefix}-slack-bot-token" }
}

resource "aws_cloudwatch_log_group" "lambda" {
  name              = "/aws/lambda/${var.name_prefix}"
  retention_in_days = var.log_retention_days
  tags              = { Name = "${var.name_prefix}-logs" }
}

resource "aws_iam_role" "lambda" {
  name = "${var.name_prefix}-lambda"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "lambda.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
  tags = { Name = "${var.name_prefix}-lambda" }
}

resource "aws_iam_role_policy_attachment" "lambda_basic_execution" {
  role       = aws_iam_role.lambda.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "lambda" {
  name   = var.name_prefix
  role   = aws_iam_role.lambda.id
  policy = jsonencode({ Version = "2012-10-17", Statement = local.iam_statements })
}

resource "aws_lambda_function" "start_stop" {
  function_name    = var.name_prefix
  role             = aws_iam_role.lambda.arn
  runtime          = "python3.12"
  handler          = "lambda_function.lambda_handler"
  filename         = data.archive_file.lambda.output_path
  source_code_hash = data.archive_file.lambda.output_base64sha256
  timeout          = var.lambda_timeout
  memory_size      = var.lambda_memory
  depends_on       = [aws_cloudwatch_log_group.lambda]
  environment {
    variables = {
      SLACK_SIGNING_SECRET_ARN = aws_secretsmanager_secret.slack_signing_secret.arn
      SLACK_BOT_TOKEN_ARN      = aws_secretsmanager_secret.slack_bot_token.arn
      SLACK_CHANNEL_ID         = var.slack_channel_id
      ARGOCD_NAMESPACE         = var.argocd_namespace
    }
  }
  tags = { Name = var.name_prefix }
}

resource "aws_lambda_function_url" "start_stop" {
  function_name      = aws_lambda_function.start_stop.function_name
  authorization_type = "NONE"
}

resource "aws_lambda_permission" "function_url" {
  statement_id           = "FunctionURLAllowPublicAccess"
  action                 = "lambda:InvokeFunctionUrl"
  function_name          = aws_lambda_function.start_stop.function_name
  principal              = "*"
  function_url_auth_type = "NONE"
}

resource "aws_iam_role" "scheduler" {
  name = "${var.name_prefix}-scheduler"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "scheduler.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
  tags = { Name = "${var.name_prefix}-scheduler" }
}

resource "aws_iam_role_policy" "scheduler" {
  name = "InvokeStartStopLambda"
  role = aws_iam_role.scheduler.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "lambda:InvokeFunction"
      Resource = [aws_lambda_function.start_stop.arn, "${aws_lambda_function.start_stop.arn}:*"]
    }]
  })
}

resource "aws_scheduler_schedule" "nightly" {
  for_each = var.schedules

  name                         = "${var.name_prefix}-${each.key}"
  group_name                   = "default"
  schedule_expression          = each.value.cron
  schedule_expression_timezone = var.schedule_timezone
  state                        = "ENABLED"

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_lambda_function.start_stop.arn
    role_arn = aws_iam_role.scheduler.arn
    input = jsonencode({
      source = "scheduler"
      action = each.value.action
      phase  = each.value.phase
    })
    retry_policy {
      maximum_event_age_in_seconds = 86400
      maximum_retry_attempts       = 0
    }
  }
}
