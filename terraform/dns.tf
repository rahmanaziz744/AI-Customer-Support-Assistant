# DNS is optional here because the registrar is often somewhere else. Either
# way the A record must resolve to the Elastic IP before the instance boots, or
# Caddy's ACME challenge fails and it will back off before retrying.
#
# Three cases, and picking the wrong one fails silently:
#   route53_zone_id set    — the zone already exists; manage only the A record.
#   create_route53_zone    — no zone yet; create it, then delegate the domain
#                            to the nameservers in the `nameservers` output.
#   neither                — DNS lives elsewhere; point the record yourself.
#
# Registering a domain through Route 53 creates a hosted zone for it
# automatically, so that case wants route53_zone_id, *not* create_route53_zone.
# Creating a second zone for a name that already has one is the silent failure:
# it gets a different nameserver set, the registrar keeps delegating to the
# original, and the A record sits in a zone nothing ever queries.

locals {
  route53_zone_id = var.create_route53_zone ? aws_route53_zone.main[0].zone_id : var.route53_zone_id
  manage_dns      = var.create_route53_zone || var.route53_zone_id != ""

  dns_next_step = local.manage_dns ? join(" ", [
    "The A record for ${var.domain_name} is managed here and already points at",
    "${aws_eip.app.public_ip}. Allow a few minutes for it to resolve; Caddy",
    "cannot get a certificate before it does.",
    ]) : join(" ", [
    "Point ${var.domain_name} at ${aws_eip.app.public_ip} and wait for it to",
    "resolve. Caddy cannot get a certificate before that.",
  ])
}

resource "aws_route53_zone" "main" {
  count = var.create_route53_zone ? 1 : 0
  name  = var.domain_name
}

resource "aws_route53_record" "app" {
  count = local.manage_dns ? 1 : 0

  zone_id = local.route53_zone_id
  name    = var.domain_name
  type    = "A"
  ttl     = 300
  records = [aws_eip.app.public_ip]
}
