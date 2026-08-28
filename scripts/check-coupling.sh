#!/usr/bin/env bash
set -euo pipefail

# check-coupling.sh — CI static check for backend coupling
# Run from the orchestratord repo root.

RESULTS=0

echo "=== Check 1: direct external extension imports ==="
if grep -rE '^\s*(from|import)\s+extensions(\.|\s|$)' \
    src/orchestratord --include='*.py'; then
  echo "FAIL: core files import extensions.* directly"
  RESULTS=1
else
  echo "OK"
fi

# -- 副匹配：相对路径形式的穿透 import ----------------------------------------
echo "=== Check 2: relative-path penetration imports ==="
if grep -rE 'from \.\.+api(\.|\s|$)' \
    src/orchestratord --include='*.py'; then
  echo "FAIL: relative-path penetration imports found"
  RESULTS=1
else
  echo "OK"
fi

# -- 绝对路径 pythonpath ------------------------------------------------------
echo "=== Check 3: hardcoded external paths in pyproject.toml ==="
if grep -E '/mnt/c/WorkSpace/clawcodex' pyproject.toml; then
  echo "FAIL: hardcoded clawcodex paths in pyproject.toml"
  RESULTS=1
else
  echo "OK"
fi

if [ "$RESULTS" -eq 0 ]; then
  echo ""
  echo "ALL CHECKS PASSED"
fi

exit "$RESULTS"
