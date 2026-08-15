variable "region" {
  description = "AWS region. Route53 health-check metrics are published in us-east-1 regardless."
  type        = string
  default     = "us-east-1"
}

variable "name" {
  description = <<-EOT
    Prefix applied to every resource name, and to the SSM parameter path.

    Changing it also means changing the image names in
    docker-compose.prod.yml and the NAME value in .github/workflows/deploy.yml,
    which reference the ECR repositories by their literal names.
  EOT
  type        = string
  default     = "support-agent"
}

# ---------------------------------------------------------------------------
# Public address
# ---------------------------------------------------------------------------

variable "domain_name" {
  description = <<-EOT
    Domain the demo is served on, e.g. support-demo.example.com. Required:
    Caddy obtains a Let's Encrypt certificate for it, and Let's Encrypt will
    not issue for a bare IP address. Point its A record at the Elastic IP in
    the outputs before first boot, or the ACME challenge fails.

    Without a domain, the alternative is a CloudFront distribution in front of
    the instance for a free *.cloudfront.net certificate — see docs/deployment.md.
  EOT
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9.-]+\\.[a-z]{2,}$", var.domain_name))
    error_message = "domain_name must be a bare hostname, with no scheme or trailing slash."
  }
}

variable "acme_email" {
  description = "Contact address Let's Encrypt uses for expiry warnings."
  type        = string
}

variable "create_route53_zone" {
  description = <<-EOT
    Create a hosted zone for domain_name and manage its A record here. Set
    false when DNS lives elsewhere; you then point the record at the Elastic IP
    yourself. A hosted zone is about $0.50/month.

    If the zone already exists — which is the case for any domain registered
    through Route 53 — leave this false and set route53_zone_id instead.
  EOT
  type        = bool
  default     = false

  validation {
    condition     = !(var.create_route53_zone && var.route53_zone_id != "")
    error_message = "Set create_route53_zone or route53_zone_id, not both: the first creates a zone, the second uses one that exists."
  }
}

variable "route53_zone_id" {
  description = <<-EOT
    Existing hosted zone to hold the A record, e.g. Z09461992V2SUE1NCQIMF.
    Registering a domain through Route 53 creates its zone for you, so this is
    the setting that case wants; create_route53_zone would make a second zone
    the registrar does not delegate to, and the record would never resolve.

      aws route53 list-hosted-zones --query 'HostedZones[].[Name,Id]' --output text

    Leave empty to manage DNS outside Terraform.
  EOT
  type        = string
  default     = ""
}

# ---------------------------------------------------------------------------
# Instance
# ---------------------------------------------------------------------------

variable "instance_type" {
  description = <<-EOT
    Deliberately x86_64. fastembed pulls onnxruntime, and aarch64 wheel
    availability is the one thing likely to break the image build; t4g.small
    saves about $3/month once you have confirmed it installs on arm64.
  EOT
  type        = string
  default     = "t3.small"
}

variable "root_volume_gb" {
  description = <<-EOT
    Shared by the Postgres volume, the docker image cache, and Caddy's
    certificate store. The disk-usage alarm watches it.
  EOT
  type        = number
  default     = 30
}

variable "swap_gb" {
  description = <<-EOT
    2 GB of RAM is tight with Postgres, the ONNX embedding runtime and the
    agent in one kernel. Swap absorbs the spikes instead of the OOM killer
    picking a container.
  EOT
  type        = number
  default     = 2
}

# ---------------------------------------------------------------------------
# Cost controls and alerting
# ---------------------------------------------------------------------------

variable "daily_budget_usd" {
  description = "Rolling 24h model-spend ceiling enforced by the app. Alarms watch the same figure."
  type        = number
  default     = 5.0
}

variable "budget_warn_ratio" {
  description = "Fraction of the ceiling that raises the warning alarm."
  type        = number
  default     = 0.6
}

variable "monthly_aws_budget_usd" {
  description = <<-EOT
    AWS Budgets threshold for infrastructure cost. Separate from the model
    spend ceiling, which CloudWatch cannot see. Expected steady state is
    roughly $25/month.
  EOT
  type        = number
  default     = 35
}

variable "alert_email" {
  description = <<-EOT
    Where alarms go. The SNS subscription must be confirmed by clicking the
    link in the first email; Terraform cannot do that for you, and until it is
    confirmed no alarm reaches you.
  EOT
  type        = string
}

variable "log_retention_days" {
  description = "CloudWatch Logs retention."
  type        = number
  default     = 14
}

variable "enable_observability" {
  description = <<-EOT
    Create the alarms, the log metric filters they read, the external health
    check, and the EBS snapshot schedule.

    On by default because that is the right posture for anything real. Turning
    it off is a deliberate cost trade for a demo deployment: it saves roughly
    $4/month — nine alarms at $0.10, five custom metrics at $0.30, and a
    Route53 health check at $0.50 — and none of it is visible to someone using
    the site. What survives is the log group (the container log driver writes
    to it regardless), the SNS topic, and the AWS budget, which are free and
    are what actually stop a surprise bill.

    The nightly pg_dump to S3 in bootstrap.sh is independent of this, so
    turning it off still leaves a logical backup of the database.
  EOT
  type        = bool
  default     = true
}

# ---------------------------------------------------------------------------
# CI/CD
# ---------------------------------------------------------------------------

variable "github_repository" {
  description = "owner/repo allowed to assume the deploy role via OIDC."
  type        = string
}

variable "github_deploy_ref" {
  description = "Git ref whose workflow runs may deploy. Keeps the role off arbitrary branches."
  type        = string
  default     = "refs/heads/main"
}
