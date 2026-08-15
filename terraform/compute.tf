# Canonical AL2023 image, resolved through the public SSM parameter rather than
# an AMI name filter so it never resolves to something unexpected.
data "aws_ssm_parameter" "al2023" {
  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"
}

locals {
  ecr_registry = "${data.aws_caller_identity.current.account_id}.dkr.ecr.${var.region}.amazonaws.com"

  # Everything the instance needs that is not secret. Kept in Parameter Store
  # alongside the secrets so bootstrap.sh reads one source and stays static.
  plain_params = {
    site_address     = var.domain_name
    acme_email       = var.acme_email
    ecr_registry     = local.ecr_registry
    log_group        = aws_cloudwatch_log_group.app.name
    daily_budget_usd = tostring(var.daily_budget_usd)
  }
}

resource "aws_ssm_parameter" "plain" {
  for_each = local.plain_params

  name  = "/${var.name}/${each.key}"
  type  = "String"
  value = each.value
}

# Owned by the deploy workflow after the first run, which is why the value is
# ignored here — Terraform would otherwise roll production back to "bootstrap"
# on the next apply.
resource "aws_ssm_parameter" "image_tag" {
  name  = "/${var.name}/image_tag"
  type  = "String"
  value = "bootstrap"

  lifecycle {
    ignore_changes = [value]
  }
}

resource "aws_s3_object" "bootstrap" {
  bucket = aws_s3_bucket.config.id
  key    = "config/bootstrap.sh"
  source = "${path.module}/files/bootstrap.sh"
  etag   = filemd5("${path.module}/files/bootstrap.sh")
}

resource "aws_s3_object" "cw_agent_config" {
  bucket = aws_s3_bucket.config.id
  key    = "config/cloudwatch-agent.json"
  source = "${path.module}/files/cloudwatch-agent.json"
  etag   = filemd5("${path.module}/files/cloudwatch-agent.json")
}

resource "aws_instance" "app" {
  ami                    = data.aws_ssm_parameter.al2023.value
  instance_type          = var.instance_type
  subnet_id              = aws_subnet.public.id
  vpc_security_group_ids = [aws_security_group.app.id]
  iam_instance_profile   = aws_iam_instance_profile.app.name

  # Deliberately minimal: it only fetches and runs bootstrap.sh from S3. EC2
  # reads user-data once, at first boot, so anything written here can only be
  # changed by replacing the instance — and that destroys the database volume.
  # The settings are written to /etc/support-agent.conf rather than only
  # exported here, so the deploy workflow can source the same file and re-run
  # bootstrap.sh without having to rediscover the bucket name.
  user_data = <<-EOT
    #!/usr/bin/env bash
    set -euxo pipefail
    cat > /etc/support-agent.conf <<'CONF'
    export CONFIG_BUCKET=${aws_s3_bucket.config.id}
    export PARAM_PREFIX=/${var.name}
    export AWS_REGION=${var.region}
    export SWAP_GB=${var.swap_gb}
    CONF
    chmod 644 /etc/support-agent.conf
    # shellcheck disable=SC1091
    source /etc/support-agent.conf
    mkdir -p /opt/support-agent
    aws s3 cp "s3://$CONFIG_BUCKET/config/bootstrap.sh" /opt/support-agent/bootstrap.sh
    chmod +x /opt/support-agent/bootstrap.sh
    /opt/support-agent/bootstrap.sh 2>&1 | tee /var/log/support-agent-bootstrap.log
  EOT

  root_block_device {
    volume_size           = var.root_volume_gb
    volume_type           = "gp3"
    encrypted             = true
    delete_on_termination = true

    # Instance tags do not propagate to the volume, and the snapshot policy
    # selects volumes by this tag — without it, DLM matches nothing and the
    # backups silently never happen.
    tags = {
      Name   = var.name
      Backup = "daily"
    }
  }

  # IMDSv2 only: the metadata service hands out this instance's role
  # credentials, and IMDSv1's unauthenticated GET is reachable through an SSRF
  # in anything running on the box.
  metadata_options {
    http_endpoint = "enabled"
    http_tokens   = "required"
  }

  tags = {
    Name   = var.name
    Backup = "daily"
  }

  # Terraform cannot see whether bootstrap.sh changed on the instance, only in
  # S3. A change here is picked up by the next deploy or reboot, not by apply.
  depends_on = [
    aws_s3_object.bootstrap,
    aws_s3_object.cw_agent_config,
    aws_s3_object.compose,
    aws_s3_object.caddyfile,
    aws_ssm_parameter.plain,
    aws_ssm_parameter.image_tag,
    aws_ssm_parameter.secret,
    aws_ssm_parameter.postgres_password,
  ]
}

# ---------------------------------------------------------------------------
# Snapshots
# ---------------------------------------------------------------------------
# Covers losing the volume or the instance. The nightly pg_dump in bootstrap.sh
# covers losing the data inside it, and is not gated on enable_observability —
# so turning snapshots off still leaves a logical backup.

data "aws_iam_policy_document" "dlm_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["dlm.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "dlm" {
  count = local.obs

  name               = "${var.name}-dlm"
  assume_role_policy = data.aws_iam_policy_document.dlm_assume.json
}

resource "aws_iam_role_policy_attachment" "dlm" {
  count = local.obs

  role       = aws_iam_role.dlm[0].name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSDataLifecycleManagerServiceRole"
}

resource "aws_dlm_lifecycle_policy" "daily" {
  count = local.obs

  description        = "${var.name} daily snapshots"
  execution_role_arn = aws_iam_role.dlm[0].arn
  state              = "ENABLED"

  policy_details {
    resource_types = ["VOLUME"]
    target_tags    = { Backup = "daily" }

    schedule {
      name = "daily-7day"

      create_rule {
        interval      = 24
        interval_unit = "HOURS"
        times         = ["04:00"]
      }

      retain_rule {
        count = 7
      }

      copy_tags = true
    }
  }
}
