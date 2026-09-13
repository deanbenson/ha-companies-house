#!/usr/bin/env bash
# Run hassfest locally against the integration, the way CI does.
# Needs a sparse checkout of home-assistant/core's script/ directory:
#   git clone --depth 1 --filter=blob:none --sparse --branch 2026.9.2 \
#       https://github.com/home-assistant/core.git "$CORE" && (cd "$CORE" && git sparse-checkout set script)
set -euo pipefail
CORE="${CORE:-/tmp/hacore}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$CORE"
PATH="$ROOT/.venv/bin:$PATH" python -m script.hassfest --action validate \
  --integration-path "$ROOT/custom_components/companies_house"
