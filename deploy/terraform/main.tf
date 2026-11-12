data "aws_caller_identity" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id
  # us.anthropic.claude-... -> anthropic.claude-...
  base_model_id = trimprefix(var.bedrock_model_id, "us.")
  key_param_arn = "arn:aws:ssm:${var.region}:${local.account_id}:parameter${var.key_parameter_name}"
  profile_arn   = "arn:aws:bedrock:${var.region}:${local.account_id}:inference-profile/${var.bedrock_model_id}"
}

# ---------------------------------------------------------------- ECR

resource "aws_ecr_repository" "service" {
  name                 = var.name
  image_tag_mutability = "IMMUTABLE"
  # Teardown removes the images with the repository.
  force_delete = true

  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ecr_lifecycle_policy" "service" {
  repository = aws_ecr_repository.service.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep only the last 3 images"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = 3
      }
      action = { type = "expire" }
    }]
  })
}

# ---------------------------------------------------------------- logs

resource "aws_cloudwatch_log_group" "service" {
  name              = "/aws/lambda/${var.name}"
  retention_in_days = 14
}

# ---------------------------------------------------------------- IAM

resource "aws_iam_role" "service" {
  name = "${var.name}-lambda"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

# Least privilege: one model, one parameter, one log group. Nothing else.
resource "aws_iam_role_policy" "service" {
  name = "${var.name}-runtime"
  role = aws_iam_role.service.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "InvokeTheInferenceProfile"
        Effect   = "Allow"
        Action   = "bedrock:InvokeModel"
        Resource = local.profile_arn
      },
      {
        # The profile routes to the model in these regions; the condition
        # allows that routing only, not calling the base model directly.
        Sid      = "ReachTheModelOnlyThroughTheProfile"
        Effect   = "Allow"
        Action   = "bedrock:InvokeModel"
        Resource = [for r in var.bedrock_regions : "arn:aws:bedrock:${r}::foundation-model/${local.base_model_id}"]
        Condition = {
          StringEquals = { "bedrock:InferenceProfileArn" = local.profile_arn }
        }
      },
      {
        Sid      = "ReadTheAccessKey"
        Effect   = "Allow"
        Action   = "ssm:GetParameter"
        Resource = local.key_param_arn
      },
      {
        Sid      = "WriteOwnLogs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "${aws_cloudwatch_log_group.service.arn}:*"
      }
    ]
  })
}

# ---------------------------------------------------------------- Lambda

resource "aws_lambda_function" "service" {
  function_name = var.name
  role          = aws_iam_role.service.arn
  package_type  = "Image"
  image_uri     = "${aws_ecr_repository.service.repository_url}:${var.image_tag}"
  architectures = ["x86_64"]
  memory_size   = var.memory_mb
  timeout       = var.timeout_seconds

  reserved_concurrent_executions = var.reserved_concurrency

  environment {
    variables = {
      OE_AGENT_BACKEND         = "bedrock"
      OE_BEDROCK_MODEL_ID      = var.bedrock_model_id
      OE_SERVICE_KEY_SSM_PARAM = var.key_parameter_name
    }
  }

  logging_config {
    log_format = "Text"
    log_group  = aws_cloudwatch_log_group.service.name
  }

  depends_on = [aws_iam_role_policy.service]

  lifecycle {
    precondition {
      condition     = var.image_tag != ""
      error_message = "Pass the pushed image tag: terraform apply -var image_tag=<tag>."
    }
  }
}

# The public address. The access key (checked inside the service) is the
# gate; AuthType NONE means AWS itself does not ask for credentials.
resource "aws_lambda_function_url" "service" {
  function_name      = aws_lambda_function.service.function_name
  authorization_type = "NONE"
  invoke_mode        = "RESPONSE_STREAM" # /ask/stream streams
}

# Function URLs with AuthType NONE need both statements; the second is
# limited to calls that arrive through the Function URL.
resource "aws_lambda_permission" "url" {
  statement_id           = "FunctionURLAllowPublicAccess"
  action                 = "lambda:InvokeFunctionUrl"
  function_name          = aws_lambda_function.service.function_name
  principal              = "*"
  function_url_auth_type = "NONE"
}

resource "aws_lambda_permission" "invoke_via_url" {
  statement_id             = "FunctionURLInvokeAllowPublicAccess"
  action                   = "lambda:InvokeFunction"
  function_name            = aws_lambda_function.service.function_name
  principal                = "*"
  invoked_via_function_url = true
}

# ---------------------------------------------------------------- budget

# An alarm, not a cap: it emails; it does not stop anything.
resource "aws_budgets_budget" "monthly" {
  name         = "${var.name}-monthly"
  budget_type  = "COST"
  limit_amount = tostring(var.monthly_budget_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 80
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.alert_email]
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.alert_email]
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = [var.alert_email]
  }
}
