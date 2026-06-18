#!/usr/bin/env bash
set -euo pipefail

export API_URL="${API_URL:-http://localhost:8081}"
exec "$(dirname "${BASH_SOURCE[0]}")/run_e2e.sh" "$@"
