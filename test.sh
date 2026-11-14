#!/usr/bin/env bash
# Run the vault's tests. ./test.sh --slow also does real PlatformIO builds (Uno + ESP32).
set -euo pipefail
cd "$(dirname "$0")"
[ "${1:-}" = "--slow" ] && export FV_SLOW=1
exec .venv/bin/python -m unittest discover -s tests -v
