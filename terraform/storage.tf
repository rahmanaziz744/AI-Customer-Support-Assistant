data "aws_caller_identity" "current" {}

# ---------------------------------------------------------------------------
# Container images
# ---------------------------------------------------------------------------

resource "aws_ecr_repository" "api" {
  name                 = "${var.name}-api"
  image_tag_mutability = "IMMUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ecr_repository" "ui" {
  name                 = "${var.name}-ui"
  image_tag_mutability = "IMMUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }
}

# Images are tagged with the commit SHA, so without expiry every deploy leaves
# a copy behind forever. Ten is enough to roll back through.
locals {
  ecr_lifecycle = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep the 10 most recent images"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = 10
      }
      action = { type = "expire" }
    }]
  })
}

resource "aws_ecr_lifecycle_policy" "api" {
  repository = aws_ecr_repository.api.name
  policy     = local.ecr_lifecycle
}

resource "aws_ecr_lifecycle_policy" "ui" {
  repository = aws_ecr_repository.ui.name
  policy     = local.ecr_lifecycle
}

# ---------------------------------------------------------------------------
# Deploy config and backups
# ---------------------------------------------------------------------------

resource "random_id" "bucket_suffix" {
  byte_length = 4
}

# Holds the compose file and Caddyfile the instance pulls at boot and on every
# deploy, plus nightly pg_dump output. Keeping the runtime config here rather
# than in user-data means changing it is a `terraform apply` and a re-sync,
# not an instance replacement.
resource "aws_s3_bucket" "config" {
  bucket = "${var.name}-${random_id.bucket_suffix.hex}"
}

resource "aws_s3_bucket_public_access_block" "config" {
  bucket                  = aws_s3_bucket.config.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "config" {
  bucket = aws_s3_bucket.config.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_versioning" "config" {
  bucket = aws_s3_bucket.config.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "config" {
  bucket = aws_s3_bucket.config.id

  rule {
    id     = "expire-backups"
    status = "Enabled"
    filter {
      prefix = "backups/"
    }
    expiration {
      days = 14
    }
  }

  rule {
    id     = "expire-old-config-versions"
    status = "Enabled"
    filter {}
    noncurrent_version_expiration {
      noncurrent_days = 30
    }
  }
}

# Terraform owns these objects: the runtime config has a single source of
# truth, and the deploy workflow only moves the image tag. Editing the compose
# file is therefore a reviewed `terraform apply`, not an ad-hoc edit on the box.
resource "aws_s3_object" "compose" {
  bucket       = aws_s3_bucket.config.id
  key          = "config/docker-compose.prod.yml"
  source       = "${path.module}/../docker-compose.prod.yml"
  etag         = filemd5("${path.module}/../docker-compose.prod.yml")
  content_type = "text/yaml"
}

resource "aws_s3_object" "caddyfile" {
  bucket       = aws_s3_bucket.config.id
  key          = "config/Caddyfile"
  source       = "${path.module}/../Caddyfile"
  etag         = filemd5("${path.module}/../Caddyfile")
  content_type = "text/plain"
}

# ---------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------

# Standard SSM parameters are free; Secrets Manager would be $0.40/month each
# for rotation this deployment does not use.
#
# Values are deliberately NOT set here — putting them in a variable would write
# them to state. Terraform creates the parameters with a placeholder and
# `ignore_changes` keeps it from reverting the real value you set with:
#
#   aws ssm put-parameter --name /support-agent/anthropic_api_key \
#     --type SecureString --value 'sk-ant-...' --overwrite
locals {
  secret_names = ["anthropic_api_key", "demo_admin_token"]
}

resource "aws_ssm_parameter" "secret" {
  for_each = toset(local.secret_names)

  name  = "/${var.name}/${each.value}"
  type  = "SecureString"
  value = "PLACEHOLDER-set-with-aws-ssm-put-parameter"

  lifecycle {
    ignore_changes = [value]
  }
}

# The database is only reachable on the compose network, but a generated
# password still beats the shipped default. Generated here rather than by hand
# because nothing outside the instance ever needs to know it.
resource "random_password" "postgres" {
  length  = 32
  special = false
}

resource "aws_ssm_parameter" "postgres_password" {
  name  = "/${var.name}/postgres_password"
  type  = "SecureString"
  value = random_password.postgres.result
}
