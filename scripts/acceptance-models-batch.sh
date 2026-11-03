#!/usr/bin/env bash
# t_3d4db435 验收一键跑（模型分区：moa + 批量设模型）
set -u
cd "$(dirname "$0")/.." || exit 1
PY="D:/hermes-data/hermes/hermes-agent/venv/Scripts/python.exe"
OUT="scripts/_probe_out/models-batch"
mkdir -p "$OUT"

echo "=== [1/5] 构建（src/ 改了必须重建） ==="
node scripts/build.mjs || exit 1

echo
echo "=== [2/5] 后端契约探针（沙盒，零真机写入） ==="
"$PY" scripts/probe_models_batch_api.py | tee "$OUT/probe_models_batch_api.txt" | tail -3

echo
echo "=== [3/5] 真机：真改一个 agent 再改回来（逐字节） ==="
"$PY" scripts/probe_models_batch_real.py | tee "$OUT/probe_models_batch_real.txt" | tail -3

echo
echo "=== [4/5] 浏览器验收 + 截图（沙盒 home） ==="
node scripts/probe-models-batch.mjs | tee "$OUT/probe_models_batch_ui.txt" | tail -3

echo
echo "=== [5/5] 回归：既有探针 ==="
"$PY" scripts/probe_models.py 2>&1 | tail -4
node scripts/probe-models.mjs 2>&1 | tail -2
