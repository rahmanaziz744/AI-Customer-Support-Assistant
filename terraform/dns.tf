# DNS is optional here because the registrar is often somewhere else. Either
# way the A record must resolve to the Elastic IP before the instance boots, or
# Caddy's ACME challenge fails and it will back off before retrying.

resource "aws_route53_zone" "main" {
  count = var.create_route53_zone ? 1 : 0
  name  = var.domain_name
}

resource "aws_route53_record" "app" {
  count = var.create_route53_zone ? 1 : 0

  zone_id = aws_route53_zone.main[0].zone_id
  name    = var.domain_name
  type    = "A"
  ttl     = 300
  records = [aws_eip.app.public_ip]
}
