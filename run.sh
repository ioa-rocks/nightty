#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0
set -euo pipefail
workspace_source=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
export PYTHONPATH="$workspace_source${PYTHONPATH:+:$PYTHONPATH}"

for argument in "$@"; do
    if [[ "$argument" == --safe-mode ]]; then
        exec python3 -m workspace --maximized "$@"
    fi
done

if python3 -m workspace --maximized "$@"; then
    exit 0
else
    status=$?
fi

# A failed normal launch should still leave the operator with a usable shell.
# Invalid options and deliberate termination should retain their exit status.
if (( status != 2 && status != 130 && status != 143 )); then
    printf 'Nightty exited with status %s; starting Recovery Terminal.\n' "$status" >&2
    exec python3 -m workspace --maximized --safe-mode "$@"
fi
exit "$status"
