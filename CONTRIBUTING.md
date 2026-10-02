# 参与贡献

chanapp 是个人维护的项目，大部分开发由维护者完成；欢迎 issue 和 PR，但不承诺响应时限。

## 先开 issue

缺陷、功能建议、新数据源或合作沟通，请先用对应模板开 issue 说明。改动较大的 PR 请先在 issue 里对齐做法，避免白做。安全问题不要开公开 issue，见 [SECURITY.md](SECURITY.md)。

## 本地开发

环境准备见 [README · 安装](README.md#安装)；跑前端测试还需要 Node.js（用 `node --version` 确认，已在 Node 24 上通过）。克隆后启用仓库自带的 git hooks：

```bash
git config core.hooksPath chanapp/scripts/git-hooks
```

- `commit-msg`：提交信息须符合 [Conventional Commits](https://www.conventionalcommits.org/)，如 `fix(kline): 修正港股午休标签`；可用类型见脚本里的 `TYPES`。
- `pre-commit` 与 `commit-msg`：安装了 [gitleaks](https://github.com/gitleaks/gitleaks) 时扫描暂存内容和提交信息，命中凭据或本机路径即拒绝提交；规则在仓库根 `.gitleaks.toml`，CI 也会用同一份配置扫描。

### 运行临时诊断脚本

直接运行 `python /path/to/probe.py` 时，Python 把脚本所在目录加入导入路径；切到仓库根并不会自动让该脚本找到 `chanapp`。项目安装步骤只安装依赖。运行仓库外或 `tmp/` 中的脚本时，在仓库根为这一次调用指定包的父目录：

```bash
PYTHONPATH="$PWD${PYTHONPATH:+:$PYTHONPATH}" COLLECTOR_ENABLED=0 \
  .venv/bin/python /path/to/probe.py
```

`PYTHONPATH` 指向含有 `chanapp/` 的仓库根，不是包目录；使用其他虚拟环境时替换解释器路径。需要调用应用接口的诊断，先按 [离线 demo](README.md#离线-demo) 初始化独立实例。

## 测试

在仓库根运行：

```bash
COLLECTOR_ENABLED=0 .venv/bin/python -m unittest discover -s chanapp/tests
bash chanapp/scripts/test_js.sh
```

- 修缺陷时附一个在修复前失败、修复后通过的测试。
- 测试不访问外网；provider 单测用 `chanapp/tests/fixtures/` 下的录制数据。
- 改了 `chanapp/web/` 时，除 JS 测试外再跑一次真实浏览器验收：`bash chanapp/scripts/test_browser.sh`（需要 playwright-cli；脚本自己起离线 demo 服务、跑完清理，不访问外网；CI 也会跑），并在浏览器里手动走一遍受影响的页面。

## 约束

- `chanapp/engine/data.py` 中 `get_bars` 的签名与返回结构是冻结契约；接入数据走 provider 或 CSV 导入，见 [数据契约](docs/data-contract.md)。
- 凭据只经环境变量提供，不写进代码、配置、测试或提交信息。
- 新增依赖请在 PR 里说明理由。

提交的贡献按本仓库的 [MIT 许可证](LICENSE) 发布。
