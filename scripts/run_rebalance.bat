@echo off
setlocal
cd /d D:\a_share_quant_sim
conda run -n a_share_quant python -m quant_sim.cli rebalance --config config.json --account default
