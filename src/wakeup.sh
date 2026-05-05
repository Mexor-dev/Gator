#!/usr/bin/env bash
# wakeup.sh — Gator ignition entry point
# Activates venv, sets PYTHONPATH, then delegates to ~/Gator/wakeup

set -euo pipefail

GATOR_ROOT="${HOME}/Gator"

# Activate Python venv so `python` resolves to the project interpreter
# (only meaningful for interactive sub-shells spawned after this point;
#  the wakeup script already calls venv/bin/python directly)
if [[ -f "${GATOR_ROOT}/venv/bin/activate" ]]; then
    # shellcheck disable=SC1091
    source "${GATOR_ROOT}/venv/bin/activate"
fi

# Export PYTHONPATH so any direct `python` invocations resolve Gator modules
export PYTHONPATH="${GATOR_ROOT}:${GATOR_ROOT}/src${PYTHONPATH:+:${PYTHONPATH}}"

# Delegate to the canonical wakeup script, forwarding all arguments
exec bash "${GATOR_ROOT}/wakeup" "$@"
