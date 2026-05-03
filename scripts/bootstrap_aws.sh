#!/usr/bin/env bash
# bootstrap_aws.sh
# Stand up the AWS resources csd-benchmark needs in the Webapper sandbox account.
#
# Creates (idempotent where AWS allows):
#   1. Two S3 buckets: csd-benchmark-flat-150k, csd-benchmark-oss-mirror
#   2. Public block disabled + public-read bucket policy on each
#   3. IAM user csd-benchmark-readonly with the policy in infra/iam-readonly-user.json
#   4. CloudWatch alarm on bucket BytesDownloaded (mitigates the public-read bandwidth risk in spec)
#
# Designed to run once per fresh sandbox. Re-running is safe: every step
# checks for existing resources before creating.
#
# Usage:
#   AWS_PROFILE=webapper-sandbox bash scripts/bootstrap_aws.sh
#
# Requires: aws CLI, jq, configured credentials with sufficient privilege
# in account 592920047652.

set -euo pipefail

REGION="${AWS_REGION:-us-east-1}"
ACCOUNT_ID_EXPECTED="592920047652"
BUCKET_FLAT="csd-benchmark-flat-150k"
BUCKET_OSS="csd-benchmark-oss-mirror"
IAM_USER="csd-benchmark-readonly"
IAM_POLICY="csd-benchmark-readonly-policy"
ALARM_THRESHOLD_BYTES=$((50 * 1024 * 1024 * 1024))  # 50 GB

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
INFRA_DIR="$REPO_ROOT/infra"

log() { echo "[bootstrap] $*"; }
err() { echo "[bootstrap][error] $*" >&2; }

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || { err "missing required command: $1"; exit 1; }
}

require_cmd aws
require_cmd jq

verify_account() {
  local actual
  actual="$(aws sts get-caller-identity --query Account --output text)"
  if [[ "$actual" != "$ACCOUNT_ID_EXPECTED" ]]; then
    err "wrong account. expected $ACCOUNT_ID_EXPECTED, got $actual. set AWS_PROFILE."
    exit 1
  fi
  log "verified account $actual"
}

bucket_exists() {
  aws s3api head-bucket --bucket "$1" 2>/dev/null
}

create_bucket() {
  local name="$1"
  if bucket_exists "$name"; then
    log "bucket exists: $name"
    return
  fi
  log "creating bucket: $name (region=$REGION)"
  if [[ "$REGION" == "us-east-1" ]]; then
    aws s3api create-bucket --bucket "$name" --region "$REGION"
  else
    aws s3api create-bucket \
      --bucket "$name" \
      --region "$REGION" \
      --create-bucket-configuration "LocationConstraint=$REGION"
  fi
}

unblock_public_access() {
  local name="$1"
  log "disabling public access block on: $name"
  aws s3api put-public-access-block \
    --bucket "$name" \
    --public-access-block-configuration \
      "BlockPublicAcls=false,IgnorePublicAcls=false,BlockPublicPolicy=false,RestrictPublicBuckets=false"
}

apply_public_policy() {
  local name="$1"
  local tmpfile
  tmpfile="$(mktemp)"
  jq --arg b "$name" \
     '. | del(._comment) | (.Statement[].Resource) |= map(gsub("BUCKET_NAME"; $b))' \
     "$INFRA_DIR/bucket-policy.json" > "$tmpfile"
  log "applying public-read policy to: $name"
  aws s3api put-bucket-policy --bucket "$name" --policy "file://$tmpfile"
  rm -f "$tmpfile"
}

ensure_iam_user() {
  if aws iam get-user --user-name "$IAM_USER" >/dev/null 2>&1; then
    log "iam user exists: $IAM_USER"
  else
    log "creating iam user: $IAM_USER"
    aws iam create-user --user-name "$IAM_USER"
  fi

  local tmpfile
  tmpfile="$(mktemp)"
  jq 'del(._comment)' "$INFRA_DIR/iam-readonly-user.json" > "$tmpfile"

  local policy_arn="arn:aws:iam::${ACCOUNT_ID_EXPECTED}:policy/${IAM_POLICY}"
  if aws iam get-policy --policy-arn "$policy_arn" >/dev/null 2>&1; then
    log "iam policy exists, creating new version: $IAM_POLICY"
    # Prune oldest non-default version if at the 5-version cap
    local versions
    versions="$(aws iam list-policy-versions --policy-arn "$policy_arn" \
        --query 'Versions[?IsDefaultVersion==`false`].[VersionId,CreateDate]' \
        --output text | sort -k2 | awk 'NR==1 {print $1}')"
    local count
    count="$(aws iam list-policy-versions --policy-arn "$policy_arn" \
        --query 'length(Versions)' --output text)"
    if [[ "$count" -ge 5 && -n "$versions" ]]; then
      aws iam delete-policy-version --policy-arn "$policy_arn" --version-id "$versions"
    fi
    aws iam create-policy-version \
      --policy-arn "$policy_arn" \
      --policy-document "file://$tmpfile" \
      --set-as-default >/dev/null
  else
    log "creating iam policy: $IAM_POLICY"
    aws iam create-policy \
      --policy-name "$IAM_POLICY" \
      --policy-document "file://$tmpfile" >/dev/null
  fi

  log "attaching policy to user: $IAM_USER"
  aws iam attach-user-policy \
    --user-name "$IAM_USER" \
    --policy-arn "$policy_arn"

  rm -f "$tmpfile"
}

create_bandwidth_alarm() {
  local name="$1"
  local alarm="csd-benchmark-${name}-bytes-out"
  log "creating CloudWatch alarm: $alarm (threshold ${ALARM_THRESHOLD_BYTES} bytes/hr)"
  aws cloudwatch put-metric-alarm \
    --alarm-name "$alarm" \
    --alarm-description "Alert when ${name} egress exceeds threshold (CSD trial usage risk)" \
    --metric-name "BytesDownloaded" \
    --namespace "AWS/S3" \
    --statistic Sum \
    --period 3600 \
    --evaluation-periods 1 \
    --threshold "$ALARM_THRESHOLD_BYTES" \
    --comparison-operator GreaterThanThreshold \
    --dimensions "Name=BucketName,Value=${name}" "Name=FilterId,Value=EntireBucket" \
    --treat-missing-data notBreaching
}

main() {
  verify_account

  for bucket in "$BUCKET_FLAT" "$BUCKET_OSS"; do
    create_bucket "$bucket"
    unblock_public_access "$bucket"
    apply_public_policy "$bucket"
    create_bandwidth_alarm "$bucket"
  done

  ensure_iam_user

  log "bootstrap complete"
  log "next steps:"
  log "  1. cd seed && pip install -r requirements.txt"
  log "  2. python seed_oss_mirror.py        # ~30-60 min depending on network"
  log "  3. python seed_flat_150k.py         # ~5-15 min"
  log "  4. python tag_audio_files.py        # < 1 min"
  log "  5. python generate_inventory.py --bucket oss_mirror"
  log "  6. python generate_inventory.py --bucket flat"
}

main "$@"
