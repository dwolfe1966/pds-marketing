#!/usr/bin/env bash
#
# build_sklearn_layer.sh
#
# Builds an AWS Lambda layer containing scikit-learn, scipy, and numpy
# compiled for the python3.11 Lambda runtime (Amazon Linux 2023).
#
# Must be run from the platform/ directory.
# Requires: Docker
#
# Output: sklearn-layer.zip in the platform/ directory
#
# Usage:
#   bash layers/build_sklearn_layer.sh
#

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLATFORM_DIR="$(dirname "$SCRIPT_DIR")"
OUTPUT_ZIP="$PLATFORM_DIR/sklearn-layer.zip"

if ! command -v docker >/dev/null 2>&1; then
  echo "ERROR: Docker is required to build the Lambda layer." >&2
  exit 1
fi

echo "Building sklearn/scipy/numpy Lambda layer for python3.11..."
echo "Output: $OUTPUT_ZIP"

# Remove any previous build artifacts
rm -rf "$PLATFORM_DIR/layer-build"
rm -f "$OUTPUT_ZIP"

mkdir -p "$PLATFORM_DIR/layer-build/python"

# Use the official AWS Lambda Python 3.11 image to ensure binary compatibility
docker run --rm \
  --platform linux/amd64 \
  -v "$PLATFORM_DIR/layer-build/python:/opt/python" \
  public.ecr.aws/lambda/python:3.11 \
  pip install \
    "scikit-learn>=1.4.0" \
    "scipy>=1.12.0" \
    "numpy>=1.26.0" \
    --target /opt/python \
    --quiet \
    --upgrade

echo "Packaging layer..."
(
  cd "$PLATFORM_DIR/layer-build"
  zip -r9 "$OUTPUT_ZIP" python/ --quiet
)

rm -rf "$PLATFORM_DIR/layer-build"

SIZE_MB=$(du -m "$OUTPUT_ZIP" | cut -f1)
echo "Layer built successfully: $OUTPUT_ZIP (${SIZE_MB} MB)"
echo ""
echo "Publish with:"
echo "  aws lambda publish-layer-version \\"
echo "    --layer-name sklearn-scipy-numpy \\"
echo "    --description 'scikit-learn 1.4 + scipy 1.12 + numpy 1.26 for python3.11' \\"
echo "    --zip-file fileb://$OUTPUT_ZIP \\"
echo "    --compatible-runtimes python3.11 \\"
echo "    --region us-east-1"
