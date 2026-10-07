# 参与贡献

chanapp 是个人维护的项目，大部分开发由维护者完成；欢迎 issue 和 PR，但不承诺响应时限。

## 先开 issue

缺陷、功能建议、新数据源或合作沟通，请先用对应模板开 issue 说明。改动较大的 PR 请先在 issue 里对齐做法，避免白做。安全问题不要开公开 issue，见 [SECURITY.md](SECURITY.md)。

## 本地开发

后端使用 Python 3.12 / FastAPI。前端使用原生 JavaScript 和随包的 lightweight-charts，不依赖外部 CDN。缠论计算使用固定版本的 chan.py，见 [计算核心说明](docs/periods-and-analysis.md#缠论规则预设)。Python 包在 `chanapp/` 子目录，命令都在仓库根运行。

环境准备见 [README · 安装](README.md#安装)；跑前端测试还需要 Node.js（用 `node --version` 确认，已在 Node 24 上通过）。克隆后启用仓库自带的 git hooks：

```bash
git config core.hooksPath chanapp/scripts/git-hooks
```

- `commit-msg`：提交信息须符合 [Conventional Commits](https://www.conventionalcommits.org/)，如 `fix(kline): 修正港股午休标签`；可用类型见脚本里的 `TYPES`。
- `pre-commit` 与 `commit-msg`：安装了 [gitleaks](https://github.com/gitleaks/gitleaks) 时扫描暂存内容和提交信息，命中凭据或本机路径即拒绝提交；规则在仓库根 `.gitleaks.toml`，CI 也会用同一份配置扫描。

## 测试

在仓库根运行全量 Python 测试和前端逻辑测试：

```bash
COLLECTOR_ENABLED=0 .venv/bin/python -m unittest discover -s chanapp/tests
bash chanapp/scripts/test_js.sh
```

第一条命令用 `COLLECTOR_ENABLED=0` 关闭后台采集；第二条用 Node.js 运行前端逻辑测试。CI 会运行这两项和浏览器验收。

- 修缺陷时附一个在修复前失败、修复后通过的测试。
- 测试不访问外网；provider 单测用 `chanapp/tests/fixtures/` 下的录制数据。
- 改了 `chanapp/web/` 时，除 JS 测试外再跑一次真实浏览器验收：`bash chanapp/scripts/test_browser.sh`（需要 playwright-cli；脚本自己起离线 demo 服务、跑完清理，不访问外网），并在浏览器里手动走一遍受影响的页面。CI 通过下述完整流程入口执行同一离线浏览器验收。

### 用户流程验收

完整用户流程使用 [tester-army/e2e](https://github.com/tester-army/e2e)。先完成 Python 环境安装，使用 Node.js 24.8 或更新版本，在仓库根运行：

```bash
npm ci
npx --no-install e2e-web install chromium
npm run test:e2e
```

依赖树由 `package-lock.json` 锁定；Chromium 通过测试引擎自己的安装入口获取，Linux 缺少系统库时在安装命令末尾加 `--with-deps`。安装依赖及首次下载 Chromium 需要网络；测试使用本地样本或外部边界 fixture，不需要模型 API key。运行器自动建立临时实例、选择空闲端口并清理服务。默认解释器为 `.venv/bin/python`，其他环境通过 `PYTHON` 指定。只验证一组流程时，把文件路径传给入口，例如：

```bash
npm run test:e2e -- chanapp/tests/e2e/watchlist.e2e.ts
```

报告、截图和 trace 保存在 `.e2e/`。覆盖清单、真实业务 API 与前端 mock 的区别见 [流程覆盖与验证边界](docs/testing.md)。Agent 可按 [verify-chanapp](.agents/skills/verify-chanapp/SKILL.md) 选择流程并保存证据。该入口补充上述既有本地检查；CI 的 browser job 使用 Node 24 执行完整流程，并在成功或失败后上传测试证据，保留 14 天。

## 约束

- `chanapp/engine/data.py` 中 `get_bars` 的签名与返回结构是冻结契约；接入数据走 provider 或 CSV 导入，见 [数据契约](docs/data-contract.md)。
- 凭据只经环境变量提供，不写进代码、配置、测试或提交信息。
- 新增依赖请在 PR 里说明理由。

提交的贡献按本仓库的 [MIT 许可证](LICENSE) 发布。

## 运行临时诊断脚本

直接运行 `python /path/to/probe.py` 时，Python 把脚本所在目录加入导入路径；切到仓库根并不会自动让该脚本找到 `chanapp`。项目安装步骤只安装依赖。运行仓库外或 `tmp/` 中的脚本时，在仓库根为这一次调用指定包的父目录：

```bash
PYTHONPATH="$PWD${PYTHONPATH:+:$PYTHONPATH}" COLLECTOR_ENABLED=0 \
  .venv/bin/python /path/to/probe.py
```

`PYTHONPATH` 指向含有 `chanapp/` 的仓库根，不是包目录；使用其他虚拟环境时替换解释器路径。需要调用应用接口的诊断，先按 [离线 demo](README.md#离线-demo) 初始化独立实例。
