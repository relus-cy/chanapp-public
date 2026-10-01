#!/usr/bin/env bash
# 前端 UI 测试入口（node 直跑）：逐个执行 tests/test_*.js。这些用例用 node:vm 切片
# web/app.js 的闭包，不需要浏览器与本地服务，也不打外网。
# tests/browser_ui_regression.js 不在本套件内：它依赖 playwright-cli 与本地运行中的服务，
# 只能手动执行，步骤见该文件头注释。
# 用法（任意目录）：bash scripts/test_js.sh；逐个打印 PASS/FAIL，任一失败即非零退出。
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
command -v node >/dev/null || { echo 'node not found in PATH' >&2; exit 2; }
shopt -s nullglob
failed=0
total=0
for test_file in tests/test_*.js; do
  total=$((total + 1))
  if node "$test_file"; then
    printf 'PASS %s\n' "$test_file"
  else
    printf 'FAIL %s\n' "$test_file" >&2
    failed=$((failed + 1))
  fi
done
((total)) || { echo 'No tests/test_*.js found' >&2; exit 2; }
if ((failed)); then
  printf '%d/%d JS UI tests failed\n' "$failed" "$total" >&2
  exit 1
fi
printf '%d JS UI tests passed\n' "$total"
