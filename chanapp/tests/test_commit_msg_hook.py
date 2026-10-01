"""提交信息闸门 scripts/git-hooks/commit-msg：在临时仓库里走真实的 git commit/merge/revert/worktree。"""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / 'scripts/git-hooks/commit-msg'
# 去掉外层 GIT_* 变量（防止误操作真实仓库），并隔离使用者的全局/系统 git 配置
GIT_ENV = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
GIT_ENV.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM='1')


class CommitMsgHookTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.repo = self.tmp / 'repo'
        (self.repo / 'scripts/git-hooks').mkdir(parents=True)
        shutil.copy2(HOOK, self.repo / 'scripts/git-hooks/commit-msg')  # copy2 保留可执行位
        self.git('init', '-q', '-b', 'main')
        self.git('config', 'user.name', 't')
        self.git('config', 'user.email', 't@example.com')
        # 与 README「启动」的启用命令一致：相对路径，按各工作区根解析
        self.git('config', 'core.hooksPath', 'scripts/git-hooks')
        self.git('add', '-A')
        self.git('commit', '-q', '-m', 'chore: 初始化')

    def git(self, *args, cwd=None, env=None, check=True):
        return subprocess.run(['git', *args], cwd=cwd or self.repo, env=env or GIT_ENV,
                              capture_output=True, encoding='utf-8', check=check)

    def commit(self, msg, cwd=None, env=None):
        return self.git('commit', '-q', '--allow-empty', '-m', msg, cwd=cwd, env=env, check=False)

    def change(self, name):
        (self.repo / name).write_text(name)
        self.git('add', name)

    @unittest.skipUnless(shutil.which('gitleaks'), 'gitleaks 未安装')
    def test_rejects_message_matching_configured_leak_rule(self):
        config = self.tmp / 'leaks.toml'
        config.write_text('[[rules]]\nid = "marker"\nregex = "forbidden-marker"\n')
        self.git('config', 'chanapp.gitleaksConfig', str(config))
        rejected = self.commit('docs: 提到 forbidden-marker')
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn('提交信息含凭据或个人信息', rejected.stderr)
        self.assertEqual(self.commit('docs: 正常说明').returncode, 0)

    @unittest.skipUnless(shutil.which('gitleaks'), 'gitleaks 未安装')
    def test_rejects_when_configured_leak_rules_are_missing(self):
        self.git('config', 'chanapp.gitleaksConfig', str(self.tmp / 'moved.toml'))
        rejected = self.commit('docs: 正常说明')
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn('chanapp.gitleaksConfig 指向的配置不存在', rejected.stderr)

    def test_hook_is_executable(self):
        # 没有可执行位的 hook 会被 git 静默跳过
        self.assertTrue(os.access(HOOK, os.X_OK))

    def test_rejects_nonconforming_headers(self):
        for msg in ('release: v1.8.0', 'wip: 暂存', 'fix+docs: 修复并补文档', 'deploy: 排除文件',
                    '自选股：星标置顶', 'feat：全角冒号', 'feat:缺空格', 'feat: ', 'Feat: 大写类型'):
            with self.subTest(msg=msg):
                result = self.commit(msg)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('可用 type', result.stderr)  # 被拒时给出类型表，agent 据此改写

    def test_accepts_conventional_headers(self):
        for msg in ('feat: 中文描述', 'fix(kline): 修复', 'feat(api,web)!: 不兼容改动',
                    'chore(release): v1.8.0', 'revert: 回滚某提交', 'docs: Q3 数值确认'):
            with self.subTest(msg=msg):
                result = self.commit(msg)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_chinese_description_under_c_locale(self):
        result = self.commit('feat(kline): 中文描述', env={**GIT_ENV, 'LC_ALL': 'C'})
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_skips_comment_lines_before_header(self):
        # 编辑器模板里的 # 注释在 hook 之后才由 git 清理，hook 须取首个非注释行
        msg = self.tmp / 'msg.txt'
        msg.write_text('# 注释\n\nfix: 修复\n', encoding='utf-8')
        result = self.git('commit', '-q', '--allow-empty', '--cleanup=strip', '-F', str(msg), check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.git('log', '-1', '--format=%s').stdout.strip(), 'fix: 修复')

    def test_allows_git_generated_messages(self):
        self.git('checkout', '-q', '-b', 'feature')
        self.change('b.txt')
        self.assertEqual(self.commit('feat: 分支改动').returncode, 0)
        self.git('checkout', '-q', 'main')
        merge = self.git('merge', '-q', '--no-ff', '-m', 'Merge: 合并 feature', 'feature', check=False)
        self.assertEqual(merge.returncode, 0, merge.stderr)
        self.change('c.txt')
        self.assertEqual(self.commit('fix: 主干改动').returncode, 0)
        revert = self.git('revert', '--no-edit', 'HEAD', check=False)
        self.assertEqual(revert.returncode, 0, revert.stderr)
        fixup = self.git('commit', '-q', '--allow-empty', '--fixup', 'HEAD', check=False)
        self.assertEqual(fixup.returncode, 0, fixup.stderr)
        subjects = self.git('log', '-3', '--format=%s').stdout.splitlines()
        self.assertEqual(subjects, ['fixup! Revert "fix: 主干改动"', 'Revert "fix: 主干改动"', 'fix: 主干改动'])

    def test_enforced_in_linked_worktree(self):
        wt = self.tmp / 'wt'
        self.git('worktree', 'add', '-q', '-b', 'side', str(wt))
        self.assertNotEqual(self.commit('wip: 暂存', cwd=wt).returncode, 0)
        self.assertEqual(self.commit('feat: 新功能', cwd=wt).returncode, 0)


if __name__ == '__main__':
    unittest.main()
