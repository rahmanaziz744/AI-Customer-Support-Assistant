# One public subnet is all a single instance needs. There is no load balancer
# and no managed database, so nothing here requires a second availability zone
# or a private subnet — and adding a NAT gateway for egress would cost more per
# month than the instance it serves.

data "aws_availability_zones" "available" {
  state = "available"
}

resource "aws_vpc" "main" {
  cidr_block           = "10.20.0.0/16"
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = { Name = var.name }
}

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = var.name }
}

resource "aws_subnet" "public" {
  vpc_id            = aws_vpc.main.id
  cidr_block        = "10.20.1.0/24"
  availability_zone = data.aws_availability_zones.available.names[0]

  # The Elastic IP is what DNS points at; this only affects the address the
  # instance gets before the EIP is associated.
  map_public_ip_on_launch = true

  tags = { Name = "${var.name}-public" }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }

  tags = { Name = "${var.name}-public" }
}

resource "aws_route_table_association" "public" {
  subnet_id      = aws_subnet.public.id
  route_table_id = aws_route_table.public.id
}

resource "aws_security_group" "app" {
  name        = "${var.name}-app"
  description = "Public HTTP/HTTPS to Caddy. No SSH: shell access is via SSM Session Manager."
  vpc_id      = aws_vpc.main.id

  tags = { Name = "${var.name}-app" }
}

# Port 80 stays open alongside 443 because Let's Encrypt's HTTP-01 challenge
# uses it, and Caddy redirects everything else to HTTPS.
resource "aws_vpc_security_group_ingress_rule" "http" {
  security_group_id = aws_security_group.app.id
  description       = "HTTP: ACME challenge and the redirect to HTTPS"
  cidr_ipv4         = "0.0.0.0/0"
  from_port         = 80
  to_port           = 80
  ip_protocol       = "tcp"
}

resource "aws_vpc_security_group_ingress_rule" "https" {
  security_group_id = aws_security_group.app.id
  description       = "HTTPS"
  cidr_ipv4         = "0.0.0.0/0"
  from_port         = 443
  to_port           = 443
  ip_protocol       = "tcp"
}

# There is deliberately no rule for 22. The instance profile carries
# AmazonSSMManagedInstanceCore, so `aws ssm start-session` gives a shell with
# no key to manage, no bastion, and no open port.

resource "aws_vpc_security_group_egress_rule" "all" {
  security_group_id = aws_security_group.app.id
  description       = "Outbound to the Anthropic API, ECR, SSM, and CloudWatch"
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
}

resource "aws_eip" "app" {
  domain   = "vpc"
  instance = aws_instance.app.id

  tags = { Name = var.name }

  depends_on = [aws_internet_gateway.main]
}
