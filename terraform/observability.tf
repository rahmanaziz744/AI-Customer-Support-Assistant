resource "aws_cloudwatch_log_group" "app" {
  name              = "/${var.name}/containers"
  retention_in_days = var.log_retention_days
}

resource "aws_sns_topic" "alerts" {
  name = "${var.name}-alerts"
}

# Confirm this by clicking the link in the first email. Until you do, every
# alarm below fires into nothing.
resource "aws_sns_topic_subscription" "email" {
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

locals {
  alarm_actions = [aws_sns_topic.alerts.arn]
  namespace     = "SupportAgent"
}

# ---------------------------------------------------------------------------
# Metric filters over the structured application logs
# ---------------------------------------------------------------------------
# The app emits one budget_snapshot line per minute as flat JSON, so spend
# becomes an alarmable metric without a boto3 dependency, an IAM policy for
# PutMetricData, or a per-call metric charge.

resource "aws_cloudwatch_log_metric_filter" "daily_spend" {
  name           = "${var.name}-daily-spend"
  log_group_name = aws_cloudwatch_log_group.app.name
  pattern        = "{ $.event = \"budget_snapshot\" }"

  metric_transformation {
    name      = "DailySpendUSD"
    namespace = local.namespace
    value     = "$.daily_spend_usd"
    unit      = "None"
  }
}

resource "aws_cloudwatch_log_metric_filter" "errors" {
  name           = "${var.name}-errors"
  log_group_name = aws_cloudwatch_log_group.app.name
  pattern        = "{ $.level = \"error\" }"

  metric_transformation {
    name          = "ApplicationErrors"
    namespace     = local.namespace
    value         = "1"
    default_value = "0"
  }
}

resource "aws_cloudwatch_log_metric_filter" "run_failures" {
  name           = "${var.name}-run-failures"
  log_group_name = aws_cloudwatch_log_group.app.name
  pattern        = "{ $.event = \"background_processing_failed\" }"

  metric_transformation {
    name          = "AgentRunFailures"
    namespace     = local.namespace
    value         = "1"
    default_value = "0"
  }
}

# ---------------------------------------------------------------------------
# Spend
# ---------------------------------------------------------------------------

resource "aws_cloudwatch_metric_alarm" "spend_warning" {
  alarm_name        = "${var.name}-spend-warning"
  alarm_description = "Model spend passed ${var.budget_warn_ratio * 100}% of the daily ceiling. Nothing is refused yet."

  namespace           = local.namespace
  metric_name         = "DailySpendUSD"
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  threshold           = var.daily_budget_usd * var.budget_warn_ratio
  comparison_operator = "GreaterThanOrEqualToThreshold"

  # No datapoints means the app is not reporting, which is an availability
  # problem the uptime alarm covers — not an overspend.
  treat_missing_data = "notBreaching"
  alarm_actions      = local.alarm_actions
  ok_actions         = local.alarm_actions
}

resource "aws_cloudwatch_metric_alarm" "spend_exhausted" {
  alarm_name        = "${var.name}-spend-exhausted"
  alarm_description = "Daily model budget is spent; the app is refusing new agent runs with 429."

  namespace           = local.namespace
  metric_name         = "DailySpendUSD"
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  threshold           = var.daily_budget_usd
  comparison_operator = "GreaterThanOrEqualToThreshold"

  treat_missing_data = "notBreaching"
  alarm_actions      = local.alarm_actions
  ok_actions         = local.alarm_actions
}

# CloudWatch cannot see AWS spend, only what the app reports. This is the other
# half: infrastructure cost, on both actual and forecast.
resource "aws_budgets_budget" "monthly" {
  name         = "${var.name}-monthly"
  budget_type  = "COST"
  limit_amount = tostring(var.monthly_aws_budget_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

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

# ---------------------------------------------------------------------------
# Availability
# ---------------------------------------------------------------------------
# There is no load balancer doing health checks and nothing that replaces a
# sick instance, so an external prober is the only thing that will notice the
# demo is down. A Synthetics canary would do more but costs ~$10/month against
# a ~$25/month deployment.

resource "aws_route53_health_check" "public" {
  fqdn              = var.domain_name
  type              = "HTTPS"
  resource_path     = "/health/ready"
  port              = 443
  request_interval  = 30
  failure_threshold = 3

  tags = { Name = "${var.name}-public" }
}

resource "aws_cloudwatch_metric_alarm" "uptime" {
  alarm_name        = "${var.name}-unreachable"
  alarm_description = "The public endpoint failed its health check."

  namespace           = "AWS/Route53"
  metric_name         = "HealthCheckStatus"
  dimensions          = { HealthCheckId = aws_route53_health_check.public.id }
  statistic           = "Minimum"
  period              = 60
  evaluation_periods  = 3
  threshold           = 1
  comparison_operator = "LessThanThreshold"

  treat_missing_data = "breaching"
  alarm_actions      = local.alarm_actions
  ok_actions         = local.alarm_actions
}

resource "aws_cloudwatch_metric_alarm" "instance_status" {
  alarm_name        = "${var.name}-status-check-failed"
  alarm_description = "EC2 instance or host status check failed."

  namespace           = "AWS/EC2"
  metric_name         = "StatusCheckFailed"
  dimensions          = { InstanceId = aws_instance.app.id }
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 3
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"

  treat_missing_data = "breaching"
  alarm_actions      = local.alarm_actions
}

# ---------------------------------------------------------------------------
# Resources
# ---------------------------------------------------------------------------

resource "aws_cloudwatch_metric_alarm" "cpu" {
  alarm_name        = "${var.name}-cpu-high"
  alarm_description = "Sustained high CPU; agent runs are queueing behind each other."

  namespace           = "AWS/EC2"
  metric_name         = "CPUUtilization"
  dimensions          = { InstanceId = aws_instance.app.id }
  statistic           = "Average"
  period              = 300
  evaluation_periods  = 2
  threshold           = 80
  comparison_operator = "GreaterThanThreshold"

  treat_missing_data = "missing"
  alarm_actions      = local.alarm_actions
}

# The figure most likely to explain an outage on a 2 GB instance.
resource "aws_cloudwatch_metric_alarm" "memory" {
  alarm_name        = "${var.name}-memory-high"
  alarm_description = "Memory pressure. Postgres, the ONNX runtime, and the agent share 2 GB."

  namespace           = "CWAgent"
  metric_name         = "mem_used_percent"
  dimensions          = { InstanceId = aws_instance.app.id }
  statistic           = "Average"
  period              = 300
  evaluation_periods  = 2
  threshold           = 85
  comparison_operator = "GreaterThanThreshold"

  treat_missing_data = "missing"
  alarm_actions      = local.alarm_actions
}

# The Postgres volume, the docker image cache, and Caddy's certificate store
# all share one volume.
resource "aws_cloudwatch_metric_alarm" "disk" {
  alarm_name        = "${var.name}-disk-high"
  alarm_description = "Root volume filling up."

  namespace           = "CWAgent"
  metric_name         = "disk_used_percent"
  dimensions          = { InstanceId = aws_instance.app.id, path = "/" }
  statistic           = "Average"
  period              = 300
  evaluation_periods  = 2
  threshold           = 80
  comparison_operator = "GreaterThanThreshold"

  treat_missing_data = "missing"
  alarm_actions      = local.alarm_actions
}

# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------

resource "aws_cloudwatch_metric_alarm" "errors" {
  alarm_name        = "${var.name}-error-rate"
  alarm_description = "Elevated application error rate."

  namespace           = local.namespace
  metric_name         = "ApplicationErrors"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 10
  comparison_operator = "GreaterThanThreshold"

  treat_missing_data = "notBreaching"
  alarm_actions      = local.alarm_actions
}

# A failed background run leaves its ticket in FAILED and is not retried, so
# these need a human.
resource "aws_cloudwatch_metric_alarm" "run_failures" {
  alarm_name        = "${var.name}-agent-run-failures"
  alarm_description = "Agent runs are failing; affected tickets stay FAILED until re-driven."

  namespace           = local.namespace
  metric_name         = "AgentRunFailures"
  statistic           = "Sum"
  period              = 900
  evaluation_periods  = 1
  threshold           = 3
  comparison_operator = "GreaterThanThreshold"

  treat_missing_data = "notBreaching"
  alarm_actions      = local.alarm_actions
}
