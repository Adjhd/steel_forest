#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

# 改这里的参数，或在命令末尾传同名参数覆盖。每次运行使用一个未占用的名称。
VERSION="trial-1"
SCENARIO="nonmagnetic"
PYTHON="${STEEL_HF_PYTHON:-/mnt/vepfs/Djinhan/conda_env/steel/bin/python}"

exec "$PYTHON" -B -m campaign.run \
  --version "$VERSION" \
  --scenario "$SCENARIO" \
  --space campaign/space.json \
  --rounds 3 \
  --evaluations 12 \
  --literature-queries 2 \
  --llm-calls 15 \
  --recommendations 3 \
  "$@"
