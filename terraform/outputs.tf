output "public_ip" {
  description = "Point the domain's A record here before the first boot."
  value       = aws_eip.app.public_ip
}

output "url" {
  description = "The demo, once DNS resolves and the first deploy has run."
  value       = "https://${var.domain_name}"
}

output "instance_id" {
  description = "For `aws ssm start-session --target <id>`."
  value       = aws_instance.app.id
}

output "config_bucket" {
  description = "Runtime config and nightly pg_dump backups."
  value       = aws_s3_bucket.config.id
}

output "ecr_registry" {
  value = local.ecr_registry
}

output "deploy_role_arn" {
  description = "Set as the AWS_DEPLOY_ROLE repository variable in GitHub."
  value       = aws_iam_role.deploy.arn
}

output "nameservers" {
  description = "Delegate the domain to these if create_route53_zone is true."
  value       = var.create_route53_zone ? aws_route53_zone.main[0].name_servers : []
}

output "next_steps" {
  description = "What Terraform cannot do for you."
  value       = <<-EOT
    1. Set the secrets (Terraform created them with a placeholder so their
       real values never enter state):
         aws ssm put-parameter --name /${var.name}/anthropic_api_key \
           --type SecureString --overwrite --value 'sk-ant-...'
         aws ssm put-parameter --name /${var.name}/demo_admin_token \
           --type SecureString --overwrite --value "$(openssl rand -hex 16)"

    2. Point ${var.domain_name} at ${aws_eip.app.public_ip} and wait for it to
       resolve. Caddy cannot get a certificate before that.

    3. Confirm the SNS subscription emailed to ${var.alert_email}. Until you
       do, no alarm reaches you.

    4. Set AWS_DEPLOY_ROLE to ${aws_iam_role.deploy.arn} in the repository's
       Actions variables, then run the deploy workflow. The stack does not
       start until an image exists.

    5. Set a monthly spend limit on the Anthropic workspace this key belongs
       to. It is the only ceiling an application bug cannot bypass.
  EOT
}
