#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0
set -euo pipefail
workspace_source=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
export PYTHONPATH="$workspace_source${PYTHONPATH:+:$PYTHONPATH}"
exec python3 -m workspace --maximized "$@"
