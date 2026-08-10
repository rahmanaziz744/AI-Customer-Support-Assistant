terraform {
  required_version = ">= 1.10"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.70"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }

  # Partial configuration: supply bucket/key/region from backend.hcl so the
  # state location is not baked into the repository.
  #
  #   aws s3 mb s3://<your-state-bucket>
  #   terraform init -backend-config=backend.hcl
  #
  # use_lockfile keeps concurrent applies from colliding via a lock object in
  # the same bucket, so no DynamoDB table is needed (Terraform >= 1.10).
  backend "s3" {
    encrypt      = true
    use_lockfile = true
  }
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project   = "support-agent"
      ManagedBy = "terraform"
    }
  }
}
