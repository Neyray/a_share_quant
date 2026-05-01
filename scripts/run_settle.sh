#!/usr/bin/env bash
set -euo pipefail

cd /home/jerico/projects/a_share_quant_sim
source "$(conda info --base)/etc/profile.d/conda.sh"
conda activate a_share_quant
python -m quant_sim.cli settle --config config.json --account default
