#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
CONDA_SH="${CONDA_SH:-/home/jerico/miniconda3/etc/profile.d/conda.sh}"

cd "$PROJECT_DIR"
if [ ! -f "$CONDA_SH" ]; then
  echo "Cannot find conda activation script: $CONDA_SH" >&2
  exit 1
fi

source "$CONDA_SH"
conda activate a_share_quant
python -m quant_sim.cli rebalance --config config.json --account default
