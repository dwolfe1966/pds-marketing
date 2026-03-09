#!/usr/bin/env bash
#
# deploy.sh — Build and deploy the idlookup.ai advertising agent to AWS.
#
# Usage:
#   bash deploy.sh [STACK_NAME] [CODE_BUCKET] [ALERT_EMAIL] [DRY_RUN]
#
# Arguments (all optional, with defaults):
#   STACK_NAME   CloudFormation stack name           (default: idlookup-ai-agent)
#   CODE_BUCKET  S3 bucket for Lambda ZIPs           (default: idlookup-ai-agent-code)
#   ALERT_EMAIL  Email for SNS alert subscription    (default: "")
#   DRY_RUN      "true" or "false"                   (default: "true")
#
# Prerequisites:
#   - AWS CLI configured (aws configure)
#   - Python 3.11
#   - Docker (for sklearn layer build)
#   - pip
#

set -euo pipefail

STACK_NAME="${1:-idlookup-ai-agent}"
CODE_BUCKET="${2:-idlookup-ai-agent-code}"
ALERT_EMAIL="${3:-}"
DRY_RUN="${4:-true}"
REGION="us-east-1"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

echo "======================================================"
echo "  idlookup.ai Advertising Agent — Deploy"
echo "  Stack   : $STACK_NAME"
echo "  Bucket  : $CODE_BUCKET"
echo "  DRY_RUN : $DRY_RUN"
echo "  Region  : $REGION"
echo "======================================================"

# ── Validate AWS credentials ──────────────────────────────────────────────
if ! aws sts get-caller-identity >/dev/null 2>&1; then
  echo "ERROR: AWS credentials not configured. Run 'aws configure' first." >&2
  exit 1
fi

ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
echo "AWS Account: $ACCOUNT_ID"

# ── Ensure code bucket exists ─────────────────────────────────────────────
if ! aws s3api head-bucket --bucket "$CODE_BUCKET" 2>/dev/null; then
  echo "Creating S3 code bucket: $CODE_BUCKET"
  aws s3 mb "s3://$CODE_BUCKET" --region "$REGION"
fi

# ── Build sklearn Lambda layer (skip if already built) ───────────────────
LAYER_ZIP="$SCRIPT_DIR/sklearn-layer.zip"
if [ ! -f "$LAYER_ZIP" ]; then
  echo "Building sklearn Lambda layer..."
  bash "$SCRIPT_DIR/layers/build_sklearn_layer.sh"
else
  echo "Reusing existing sklearn-layer.zip (delete to rebuild)"
fi

echo "Publishing sklearn layer..."
SKLEARN_LAYER_ARN=$(aws lambda publish-layer-version \
  --layer-name sklearn-scipy-numpy \
  --description "scikit-learn 1.4 + scipy 1.12 + numpy 1.26 for python3.11" \
  --zip-file "fileb://$LAYER_ZIP" \
  --compatible-runtimes python3.11 \
  --region "$REGION" \
  --query LayerVersionArn \
  --output text)
echo "sklearn layer ARN: $SKLEARN_LAYER_ARN"

# ── Build Lambda ZIP packages ─────────────────────────────────────────────
# Each ZIP contains: handler file + lib/ + ads_agent package + pip deps
# (scikit-learn/scipy/numpy come from the layer, not the ZIP)

build_zip() {
  local handler_file="$1"
  local output_name="$2"
  local tmp_dir
  tmp_dir=$(mktemp -d)

  echo "Building $output_name..."

  # Install non-layer pip deps
  pip install \
    "google-ads>=24.1.0" \
    "bingads>=13.0.21" \
    "anthropic>=0.25.0" \
    "boto3>=1.34.0" \
    -t "$tmp_dir" --quiet --upgrade

  # Copy handler
  cp "$SCRIPT_DIR/handlers/$handler_file" "$tmp_dir/"

  # Copy lib package
  cp -r "$SCRIPT_DIR/lib" "$tmp_dir/lib"

  # Copy ads_agent package (single source of truth)
  cp -r "$REPO_ROOT/ads_agent_project/ads_agent" "$tmp_dir/ads_agent"

  # Create __init__.py for lib if missing
  touch "$tmp_dir/lib/__init__.py"

  # Package
  local zip_path="/tmp/${output_name}"
  (cd "$tmp_dir" && zip -r9 "$zip_path" . --quiet)
  rm -rf "$tmp_dir"

  local size_mb
  size_mb=$(du -m "$zip_path" | cut -f1)
  echo "  → $output_name (${size_mb} MB)"
  echo "$zip_path"
}

INGEST_ZIP=$(build_zip "ingest_handler.py" "ingest.zip")
AGENT_ZIP=$(build_zip "agent_handler.py" "agent.zip")
BING_ZIP=$(build_zip "bing_poll_handler.py" "bing_poller.zip")

# ── Upload ZIPs to S3 ─────────────────────────────────────────────────────
echo "Uploading Lambda packages to S3..."
aws s3 cp "$INGEST_ZIP"     "s3://$CODE_BUCKET/ingest.zip"
aws s3 cp "$AGENT_ZIP"      "s3://$CODE_BUCKET/agent.zip"
aws s3 cp "$BING_ZIP"       "s3://$CODE_BUCKET/bing_poller.zip"
aws s3 cp "$LAYER_ZIP"      "s3://$CODE_BUCKET/sklearn-layer.zip"

# ── Deploy CloudFormation stack ───────────────────────────────────────────
echo "Deploying CloudFormation stack: $STACK_NAME"
aws cloudformation deploy \
  --template-file "$SCRIPT_DIR/infra/cloudformation.yaml" \
  --stack-name "$STACK_NAME" \
  --capabilities CAPABILITY_IAM CAPABILITY_NAMED_IAM \
  --region "$REGION" \
  --no-fail-on-empty-changeset \
  --parameter-overrides \
    StackName="$STACK_NAME" \
    AlertEmail="$ALERT_EMAIL" \
    DryRun="$DRY_RUN" \
    CodeBucket="$CODE_BUCKET" \
    IngestZipKey="ingest.zip" \
    AgentZipKey="agent.zip" \
    BingPollerZipKey="bing_poller.zip" \
    SklearnLayerArn="$SKLEARN_LAYER_ARN"

# ── Print outputs ─────────────────────────────────────────────────────────
echo ""
echo "======================================================"
echo "  Deployment complete!"
echo "======================================================"
aws cloudformation describe-stacks \
  --stack-name "$STACK_NAME" \
  --region "$REGION" \
  --query "Stacks[0].Outputs[*].[OutputKey,OutputValue]" \
  --output table

echo ""
if [ "$DRY_RUN" = "true" ]; then
  echo "DRY_RUN=true — the agent will log recommendations but not apply mutations."
  echo ""
  echo "To review recommendations after the first run:"
  DATA_BUCKET="${STACK_NAME}-data-${ACCOUNT_ID}"
  echo "  aws s3 ls s3://${DATA_BUCKET}/recommendations/ --recursive"
  echo ""
  echo "To enable live writes, use the EnableLiveWritesCommand output above."
fi
