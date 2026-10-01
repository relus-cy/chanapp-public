#!/usr/bin/env bash
# 真实浏览器验收入口（无人值守）：在临时目录起离线 demo 服务（tests/support/demo_offline_server.py，
# 业务接口不拦截），用 playwright-cli 打开页面并执行 tests/browser_demo_offline.js，结束后停服务并清理。
# 依赖：playwright-cli（含浏览器）、能运行本项目的 Python（默认仓库根 .venv/bin/python，可用 PYTHON 覆盖）。
# 用法（任意目录）：bash chanapp/scripts/test_browser.sh；KEEP_TMP=1 保留临时目录与服务日志。
# 验收脚本内任一断言失败、服务记录到对外连接或服务提前退出，都以非零退出并打印失败项。
# 截图写到仓库根 tmp/demo-offline-browser/（已被 .gitignore 忽略）。
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
REPO="$(cd "$ROOT/.." && pwd)"
cd "$REPO"

command -v playwright-cli >/dev/null || {
  echo 'playwright-cli not found in PATH; install it (npm i -g @playwright/cli) and its browser (playwright-cli install-browser)' >&2
  exit 2
}
PYTHON="${PYTHON:-}"
if [[ -z "$PYTHON" ]]; then
  if [[ -x .venv/bin/python ]]; then PYTHON=.venv/bin/python; else PYTHON=python3; fi
fi
"$PYTHON" -c 'import uvicorn, chanapp.api.main' 2>/dev/null || {
  echo "Python at '$PYTHON' cannot import the app; set PYTHON to the project's interpreter" >&2
  exit 2
}

PORT="$("$PYTHON" -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()')"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/chanapp-browser.XXXXXX")"
SESSION="chanapp-browser-$$"
SERVER_PID=""
pw() { playwright-cli -s="$SESSION" "$@"; }

cleanup() {
  pw close >/dev/null 2>&1 || true
  if [[ -n "$SERVER_PID" ]] && kill -0 "$SERVER_PID" 2>/dev/null; then
    kill "$SERVER_PID" 2>/dev/null || true
    wait "$SERVER_PID" 2>/dev/null || true
  fi
  if [[ "${KEEP_TMP:-}" == 1 ]]; then echo "kept $WORK" >&2; else rm -rf "$WORK"; fi
}
trap cleanup EXIT

"$PYTHON" -m chanapp.tests.support.demo_offline_server --root "$WORK/root" --port "$PORT" \
  >"$WORK/server.log" 2>&1 &
SERVER_PID=$!

ready=0
for _ in $(seq 1 240); do
  if ! kill -0 "$SERVER_PID" 2>/dev/null; then break; fi
  if curl -fs -o /dev/null "http://127.0.0.1:$PORT/"; then ready=1; break; fi
  sleep 0.25
done
if ((!ready)); then
  echo "FAIL demo server did not become ready on port $PORT; log:" >&2
  tail -n 40 "$WORK/server.log" >&2
  exit 1
fi

mkdir -p tmp/demo-offline-browser
pw open "http://127.0.0.1:$PORT/" >/dev/null
status=0
pw run-code --filename="$ROOT/tests/browser_demo_offline.js" >"$WORK/result.txt" 2>&1 || status=$?

failed=0
# run-code 把脚本抛出的错误写进输出；以退出码与错误标记双重判定，避免工具版本差异吞掉失败
if ((status)) || grep -qE '^### Error|"passed":false' "$WORK/result.txt"; then
  failed=1
  echo 'FAIL tests/browser_demo_offline.js' >&2
  # 结果 JSON 里逐条列出 failures，便于直接定位
  "$PYTHON" - "$WORK/result.txt" >&2 <<'EOF' || cat "$WORK/result.txt" >&2
import json, re, sys
text = open(sys.argv[1], encoding='utf-8').read()
match = re.search(r'\{"passed":false.*\}', text)
if not match:
    raise SystemExit(1)
for item in json.loads(match.group(0))['failures']:
    print('  - ' + item)
EOF
else
  echo 'PASS tests/browser_demo_offline.js'
fi

if [[ -s "$WORK/root/outbound.jsonl" ]]; then
  failed=1
  echo 'FAIL demo server attempted outbound transport:' >&2
  sort "$WORK/root/outbound.jsonl" | uniq -c >&2
fi
if ! kill -0 "$SERVER_PID" 2>/dev/null; then
  failed=1
  echo 'FAIL demo server exited during the run; log tail:' >&2
  tail -n 40 "$WORK/server.log" >&2
fi

if ((failed)); then
  echo 'Browser acceptance failed' >&2
  exit 1
fi
echo 'Browser acceptance passed'
