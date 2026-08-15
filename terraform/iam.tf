# ---------------------------------------------------------------------------
# Instance role
# ---------------------------------------------------------------------------

data "aws_iam_policy_document" "ec2_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "instance" {
  name               = "${var.name}-instance"
  assume_role_policy = data.aws_iam_policy_document.ec2_assume.json
}

# Shell access and remote command execution, which is what replaces SSH.
resource "aws_iam_role_policy_attachment" "ssm_core" {
  role       = aws_iam_role.instance.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

# Memory and disk are not native EC2 metrics; the CloudWatch agent publishes
# them, and this policy is what lets it.
resource "aws_iam_role_policy_attachment" "cw_agent" {
  role       = aws_iam_role.instance.name
  policy_arn = "arn:aws:iam::aws:policy/CloudWatchAgentServerPolicy"
}

resource "aws_iam_role_policy_attachment" "ecr_read" {
  role       = aws_iam_role.instance.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly"
}

data "aws_iam_policy_document" "instance_extra" {
  # Only this application's parameters, not every secret in the account.
  statement {
    sid       = "ReadOwnParameters"
    actions   = ["ssm:GetParameter", "ssm:GetParameters", "ssm:GetParametersByPath"]
    resources = ["arn:aws:ssm:${var.region}:${data.aws_caller_identity.current.account_id}:parameter/${var.name}/*"]
  }

  statement {
    sid       = "DecryptParameters"
    actions   = ["kms:Decrypt"]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["ssm.${var.region}.amazonaws.com"]
    }
  }

  statement {
    sid       = "ReadDeployConfig"
    actions   = ["s3:GetObject", "s3:ListBucket"]
    resources = [aws_s3_bucket.config.arn, "${aws_s3_bucket.config.arn}/*"]
  }

  # Nightly pg_dump.
  statement {
    sid       = "WriteBackups"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.config.arn}/backups/*"]
  }

  # The awslogs docker driver writes into the group Terraform created. It is
  # not granted CreateLogGroup, so a typo produces a visible failure rather
  # than a stray group with no retention policy quietly collecting logs.
  statement {
    sid       = "WriteContainerLogs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.app.arn}:*"]
  }
}

resource "aws_iam_role_policy" "instance_extra" {
  name   = "${var.name}-instance"
  role   = aws_iam_role.instance.id
  policy = data.aws_iam_policy_document.instance_extra.json
}

resource "aws_iam_instance_profile" "app" {
  name = "${var.name}-instance"
  role = aws_iam_role.instance.name
}

# ---------------------------------------------------------------------------
# GitHub Actions deploy role (OIDC — no long-lived AWS keys)
# ---------------------------------------------------------------------------

resource "aws_iam_openid_connect_provider" "github" {
  url            = "https://token.actions.githubusercontent.com"
  client_id_list = ["sts.amazonaws.com"]

  # AWS validates GitHub's certificate chain against its own trust store, so
  # these are effectively vestigial — but the field is still required.
  thumbprint_list = [
    "6938fd4d98bab03faadb97b34396831e3780aea1",
    "1c58a3a8518e8759bf075b76b750d4f2df264fce",
  ]
}

data "aws_iam_policy_document" "github_assume" {
  statement {
    # sts:TagSession is required, not optional: configure-aws-credentials
    # passes seven session tags (repository, workflow, actor, branch, commit
    # and so on) unless role-skip-session-tagging is set, and STS refuses the
    # whole call without permission to apply them. The refusal is reported
    # against AssumeRoleWithWebIdentity — "Not authorized to perform
    # sts:AssumeRoleWithWebIdentity" — which points at the wrong action and
    # reads exactly like a mismatched `sub` condition. The tags are worth
    # keeping: they are what attributes a CloudTrail event to a workflow run.
    actions = ["sts:AssumeRoleWithWebIdentity", "sts:TagSession"]

    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.github.arn]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }

    # Scoped to one repository and one ref. Without the `sub` condition any
    # GitHub Actions workflow anywhere could assume this role.
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values   = ["repo:${var.github_repository}:ref:${var.github_deploy_ref}"]
    }
  }
}

resource "aws_iam_role" "deploy" {
  name               = "${var.name}-deploy"
  assume_role_policy = data.aws_iam_policy_document.github_assume.json
}

data "aws_iam_policy_document" "deploy" {
  statement {
    sid       = "EcrAuth"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }

  statement {
    sid = "EcrPush"
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:BatchGetImage",
      "ecr:CompleteLayerUpload",
      # DescribeImages lets the workflow notice a tag it already published and
      # skip rebuilding it, since the repositories are immutable.
      "ecr:DescribeImages",
      "ecr:GetDownloadUrlForLayer",
      "ecr:InitiateLayerUpload",
      "ecr:PutImage",
      "ecr:UploadLayerPart",
    ]
    resources = [aws_ecr_repository.api.arn, aws_ecr_repository.ui.arn]
  }

  # Deploys are a remote command, not an SSH session, so the workflow needs no
  # network path to the instance and no private key.
  statement {
    sid     = "RunDeployCommand"
    actions = ["ssm:SendCommand"]
    resources = [
      aws_instance.app.arn,
      "arn:aws:ssm:${var.region}::document/AWS-RunShellScript",
    ]
  }

  statement {
    sid       = "ReadCommandResult"
    actions   = ["ssm:GetCommandInvocation", "ssm:ListCommandInvocations"]
    resources = ["*"]
  }

  # Finds the instance to deploy to by its Name tag. DescribeInstances does not
  # support resource-level permissions, hence the wildcard.
  statement {
    sid       = "FindInstance"
    actions   = ["ec2:DescribeInstances"]
    resources = ["*"]
  }

  # Records the deployed image tag, and reads the domain back for the smoke
  # test. Scoped to this application's parameters.
  statement {
    sid       = "RecordImageTag"
    actions   = ["ssm:PutParameter", "ssm:GetParameter"]
    resources = ["arn:aws:ssm:${var.region}:${data.aws_caller_identity.current.account_id}:parameter/${var.name}/*"]
  }
}

resource "aws_iam_role_policy" "deploy" {
  name   = "${var.name}-deploy"
  role   = aws_iam_role.deploy.id
  policy = data.aws_iam_policy_document.deploy.json
}
