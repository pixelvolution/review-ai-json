#!/usr/bin/env bash
# Package the WordPress plugin into an installable zip: dist/pixel-trends.zip
set -euo pipefail

cd "$(dirname "$0")"

rm -f dist/pixel-trends.zip
(cd dist && zip -rq pixel-trends.zip pixel-trends -x '*.DS_Store')

echo "Built dist/pixel-trends.zip"
