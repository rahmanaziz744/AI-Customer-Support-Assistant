#!/usr/bin/env bash
#
# Brings the instance from a bare Amazon Linux 2023 image to a running stack.
#
# Runs on every boot (via user-data the first time, systemd afterwards) and on
# demand during a deploy, so every step is idempotent. Deliberately static and
# fetched from S3 rather than embedded in user-data: EC2 only reads user-data
# at first boot, so editing it there would mean replacing the instance — and
# replacing the instance destroys the Postgres volume.
#
# Configuration arrives as environment variables from the caller:
#   CONFIG_BUCKET, PARAM_PREFIX, AWS_REGION
set -euo pipefail

APP_DIR=/opt/support-agent
COMPOSE_VERSION=v2.29.7

log() { echo "[bootstrap] $*"; }

: "${CONFIG_BUCKET:?}" "${PARAM_PREFIX:?}" "${AWS_REGION:?}"
export AWS_DEFAULT_REGION="$AWS_REGION"

mkdir -p "$APP_DIR"

# ---------------------------------------------------------------------------
# Swap
# ---------------------------------------------------------------------------
# 2 GB of RAM is tight with Postgres, the ONNX embedding runtime, and the agent
# in one kernel. Without swap a memory spike during a run gets a container
# killed rather than slowed down.
SWAP_GB="${SWAP_GB:-2}"
if [[ ! -f /swapfile ]]; then
  log "creating ${SWAP_GB}G swapfile"
  dd if=/dev/zero of=/swapfile bs=1M count=$((SWAP_GB * 1024)) status=none
  chmod 600 /swapfile
  mkswap /swapfile
  swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

# ---------------------------------------------------------------------------
# Docker
# ---------------------------------------------------------------------------
if ! command -v docker >/dev/null 2>&1; then
  log "installing docker"
  dnf install -y docker
fi
systemctl enable --now docker

# The compose plugin is not in the AL2023 repositories.
PLUGIN_DIR=/usr/local/lib/docker/cli-plugins
if [[ ! -x "$PLUGIN_DIR/docker-compose" ]]; then
  log "installing docker compose $COMPOSE_VERSION"
  mkdir -p "$PLUGIN_DIR"
  curl -fsSL \
    "https://github.com/docker/compose/releases/download/${COMPOSE_VERSION}/docker-compose-linux-$(uname -m)" \
    -o "$PLUGIN_DIR/docker-compose"
  chmod +x "$PLUGIN_DIR/docker-compose"
fi

# ---------------------------------------------------------------------------
# CloudWatch agent
# ---------------------------------------------------------------------------
# Memory and disk are not native EC2 metrics. On a 2 GB box memory is the
# figure most likely to explain an outage, so it is worth the agent.
if ! rpm -q amazon-cloudwatch-agent >/dev/null 2>&1; then
  log "installing cloudwatch agent"
  dnf install -y amazon-cloudwatch-agent
fi
aws s3 cp "s3://${CONFIG_BUCKET}/config/cloudwatch-agent.json" \
  /opt/aws/amazon-cloudwatch-agent/etc/amazon-cloudwatch-agent.json
/opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl \
  -a fetch-config -m ec2 -s \
  -c file:/opt/aws/amazon-cloudwatch-agent/etc/amazon-cloudwatch-agent.json

# ---------------------------------------------------------------------------
# Runtime config and secrets
# ---------------------------------------------------------------------------
log "syncing config from s3"
aws s3 cp "s3://${CONFIG_BUCKET}/config/docker-compose.prod.yml" "$APP_DIR/docker-compose.prod.yml"
aws s3 cp "s3://${CONFIG_BUCKET}/config/Caddyfile" "$APP_DIR/Caddyfile"

param() {
  aws ssm get-parameter --name "${PARAM_PREFIX}/$1" --with-decryption \
    --query 'Parameter.Value' --output text
}

log "writing .env from parameter store"
# Written to a temporary file and moved into place so a failure part-way
# through cannot leave the stack reading a half-written .env.
ENV_TMP="$(mktemp)"
{
  echo "AWS_REGION=${AWS_REGION}"
  echo "ANTHROPIC_API_KEY=$(param anthropic_api_key)"
  echo "POSTGRES_PASSWORD=$(param postgres_password)"
  echo "DEMO_ADMIN_TOKEN=$(param demo_admin_token)"
  echo "SITE_ADDRESS=$(param site_address)"
  echo "ACME_EMAIL=$(param acme_email)"
  echo "ECR_REGISTRY=$(param ecr_registry)"
  echo "LOG_GROUP=$(param log_group)"
  echo "IMAGE_TAG=$(param image_tag)"
  echo "DAILY_BUDGET_USD=$(param daily_budget_usd)"
} > "$ENV_TMP"
chmod 600 "$ENV_TMP"
mv "$ENV_TMP" "$APP_DIR/.env"

# ---------------------------------------------------------------------------
# Nightly logical backup
# ---------------------------------------------------------------------------
# EBS snapshots cover losing the volume; this covers losing the data inside it,
# and restores into a local compose stack for debugging.
cat > /etc/cron.daily/support-agent-backup <<'CRON'
#!/usr/bin/env bash
set -euo pipefail
cd /opt/support-agent
# shellcheck disable=SC1091
source .env
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
docker compose -f docker-compose.prod.yml exec -T db \
  pg_dump -U support support_agent | gzip \
  | aws s3 cp - "s3://CONFIG_BUCKET_PLACEHOLDER/backups/support_agent-$STAMP.sql.gz"
CRON
sed -i "s/CONFIG_BUCKET_PLACEHOLDER/${CONFIG_BUCKET}/" /etc/cron.daily/support-agent-backup
chmod +x /etc/cron.daily/support-agent-backup

# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------
cat > /etc/systemd/system/support-agent.service <<'UNIT'
[Unit]
Description=Support agent stack
Requires=docker.service
After=docker.service network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=/opt/support-agent
ExecStart=/usr/bin/docker compose -f docker-compose.prod.yml up -d
ExecStop=/usr/bin/docker compose -f docker-compose.prod.yml down
TimeoutStartSec=600

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable support-agent.service

# ---------------------------------------------------------------------------
# Start
# ---------------------------------------------------------------------------
IMAGE_TAG="$(param image_tag)"
if [[ "$IMAGE_TAG" == "bootstrap" ]]; then
  # First apply: Terraform has created the repositories but CI has not pushed
  # an image yet. Stopping here is better than a confusing pull failure — the
  # first deploy will set the tag and start the stack.
  log "image_tag is still 'bootstrap'; skipping start. Run the deploy workflow."
  exit 0
fi

log "logging in to ecr"
ECR_REGISTRY="$(param ecr_registry)"
aws ecr get-login-password --region "$AWS_REGION" \
  | docker login --username AWS --password-stdin "$ECR_REGISTRY"

log "starting stack at $IMAGE_TAG"
cd "$APP_DIR"
docker compose -f docker-compose.prod.yml pull
systemctl restart support-agent.service

# Images from previous deploys accumulate on a 30 GB disk.
docker image prune -af --filter "until=168h" || true

log "done"
