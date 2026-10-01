"""Service unit and deployment target boundaries, without remote connections."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class DeployContractTests(unittest.TestCase):
    def test_explicit_unit_and_required_host(self):
        unit = (ROOT / 'scripts/chanapp.service').read_text()
        self.assertIn('--proxy-headers --forwarded-allow-ips=127.0.0.1 --workers 1', unit)
        # 供数方案退役（spec D5b）：无运行时目录与供数状态；采集器随服务启动（D13）。
        self.assertNotIn('RuntimeDirectory', unit)
        self.assertNotIn('SUPPLY_', unit)
        self.assertIn('Environment=COLLECTOR_ENABLED=1', unit)
        self.assertIn('EnvironmentFile=-/home/@USER@/.config/chanapp/secrets.env', unit)
        missing = subprocess.run(['bash', str(ROOT / 'scripts/deploy.sh'), '--dry-run'], capture_output=True)
        self.assertEqual(missing.returncode, 2)
        result = subprocess.run(['bash', str(ROOT / 'scripts/deploy.sh'), 'app-host', '--dry-run'],
                                capture_output=True, text=True, check=True)
        self.assertIn('node=app-host remote=app-host', result.stdout)
        self.assertIn('port=8899', result.stdout)
        self.assertIn('--exclude .runtime', result.stdout)
        self.assertIn('--exclude /requirements.txt', result.stdout)
        self.assertIn(f"{(ROOT.parent / 'requirements.txt')} app-host:/home/ubuntu/chanapp/requirements.txt",
                      result.stdout)
        unsafe = subprocess.run(['bash', str(ROOT / 'scripts/deploy.sh'), 'bad host;', '--dry-run'],
                                capture_output=True)
        self.assertEqual(unsafe.returncode, 2)

    def _deploy_with_stubs(self, ssh_exit, fail_call=1):
        """用假 ssh/rsync 跑一次非 dry-run 部署，返回 (进程结果, 调用记录)。"""
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / 'calls.log'
            for name, code in (('ssh', ssh_exit), ('rsync', 0)):
                stub = Path(tmp) / name
                stub.write_text(f'#!/usr/bin/env bash\necho {name} >> "{log}"\ncat >/dev/null\n'
                                f'if [[ $(wc -l < "{log}") -eq {fail_call} ]]; then exit {code}; fi\nexit 0\n')
                stub.chmod(0o755)
            env = {**os.environ, 'PATH': f"{tmp}:{os.environ['PATH']}"}
            result = subprocess.run(['bash', str(ROOT / 'scripts/deploy.sh'), 'app-host'],
                                    capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL)
            calls = log.read_text().split() if log.exists() else []
        return result, calls

    def test_instance_config_checked_before_sync_and_never_written(self):
        out = subprocess.run(['bash', str(ROOT / 'scripts/deploy.sh'), 'app-host', '--dry-run'],
                             capture_output=True, text=True, check=True).stdout
        self.assertLess(out.index("<<'PREFLIGHT'"), out.index('rsync'))
        script = out.split("<<'PREFLIGHT'\n", 1)[1].split("\nPREFLIGHT\n", 1)[0]
        with tempfile.TemporaryDirectory() as tmp:
            app = Path(tmp) / 'chanapp'
            app.mkdir()
            config = Path(tmp) / 'instance.json'
            for content in (None, '{', '[]', '{"mode":"demo"}', '{}', '{"mode":"real"}'):
                with self.subTest(content=content):
                    if content is not None:
                        config.write_text(content)
                    result = subprocess.run(['bash', '-s', '--', str(app), str(config), 'python3'],
                                            input=script, capture_output=True, text=True)
                    self.assertEqual(result.returncode == 0, content == '{"mode":"real"}')
                    if content is not None:
                        self.assertEqual(config.read_text(), content)
            internal = app / 'instance.json'
            internal.write_text('{"mode":"real"}')
            config.unlink()
            config.symlink_to(internal)
            for path in (internal, config):
                result = subprocess.run(['bash', '-s', '--', str(app), str(path), 'python3'],
                                        input=script, capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0)
        unit = (ROOT / 'scripts/chanapp.service').read_text()
        self.assertIn('Environment=CHANAPP_INSTANCE_CONFIG=@INSTANCE_CONFIG@', unit)

    def test_full_instance_validation_runs_after_sync_before_restart(self):
        # 缺凭据或字段错误时应用会拒绝启动；必须在重启前拦下，旧进程不受影响
        out = subprocess.run(['bash', str(ROOT / 'scripts/deploy.sh'), 'app-host', '--dry-run'],
                             capture_output=True, text=True, check=True).stdout
        check = out.index('scripts/check_instance_config.py')
        self.assertLess(out.index('rsync'), check)
        self.assertLess(check, out.index('systemctl restart chanapp'))
        self.assertIn('/home/$account/.config/chanapp/secrets.env', out[check:out.index('\n', check)])

    def test_instance_check_script_reads_service_env_files(self):
        script = ROOT / 'scripts/check_instance_config.py'
        names = ('MAIRUI_LICENCE', 'LONGBRIDGE_APP_KEY', 'LONGBRIDGE_APP_SECRET', 'LONGBRIDGE_ACCESS_TOKEN')
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / 'instance.json'
            config.write_text('{"mode":"real"}')
            secrets = Path(tmp) / 'secrets.env'
            env = {'PATH': os.environ['PATH']}

            def run(*files):
                return subprocess.run([sys.executable, str(script), str(config), *map(str, files)],
                                      capture_output=True, text=True, env=env)
            secrets.write_text('# comment\n' + ''.join(f'{n}="value-{n}"\n' for n in names[:-1]))
            missing = run(secrets, Path(tmp) / 'absent.env')
            self.assertEqual(missing.returncode, 1)
            self.assertIn('LONGBRIDGE_ACCESS_TOKEN', missing.stderr)
            self.assertNotIn('value-', missing.stderr + missing.stdout)
            override = Path(tmp) / 'app.env'
            override.write_text(f'{names[-1]}=late-value\n')
            self.assertEqual(run(secrets, override).returncode, 0)
            config.write_text('{"mode":"real","quota":{"per_day":0}}')
            self.assertEqual(run(secrets, override).returncode, 1)

    def test_remote_backup_runs_before_rsync_delete(self):
        result = subprocess.run(['bash', str(ROOT / 'scripts/deploy.sh'), 'app-host', '--dry-run'],
                                capture_output=True, text=True, check=True)
        out = result.stdout
        self.assertIn('chanapp-deploy-backups', out)
        self.assertLess(out.index('chanapp-deploy-backups'), out.index('rsync'))
        self.assertNotIn('recover_supply', out)

    def test_config_preflight_failure_aborts_before_backup_or_sync(self):
        result, calls = self._deploy_with_stubs(ssh_exit=1)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(calls, ['ssh'])

    def test_backup_failure_aborts_before_rsync(self):
        result, calls = self._deploy_with_stubs(ssh_exit=1, fail_call=2)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(calls, ['ssh', 'ssh'])  # 前置通过，备份失败后不得同步

    def test_backup_aborts_when_dependency_freeze_fails(self):
        # 恢复点必须带依赖版本清单：pip freeze 失败不能静默产出不完整的备份（astra 复审 P2-17）
        out = subprocess.run(['bash', str(ROOT / 'scripts/deploy.sh'), 'app-host', '--dry-run'],
                             capture_output=True, text=True, check=True).stdout
        script = out.split("<<'BACKUP'\n", 1)[1].split("\nBACKUP\n", 1)[0]
        with tempfile.TemporaryDirectory() as tmp:
            app = Path(tmp) / 'srv' / 'chanapp'
            (app / '.venv' / 'bin').mkdir(parents=True)
            python = app / '.venv' / 'bin' / 'python'
            python.write_text('#!/bin/sh\nexit 1\n')
            python.chmod(0o755)
            env = {**os.environ, 'HOME': tmp}
            result = subprocess.run(['bash', '-s', '--', str(app), '3'], input=script, text=True,
                                    capture_output=True, env=env)
            backups = list((Path(tmp) / 'chanapp-deploy-backups').glob('deploy-*.tar'))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(backups, [])

    def test_backup_keep_count_validated(self):
        env = {**os.environ, 'DEPLOY_BACKUP_KEEP': '0'}
        result = subprocess.run(['bash', str(ROOT / 'scripts/deploy.sh'), 'app-host', '--dry-run'],
                                capture_output=True, env=env)
        self.assertEqual(result.returncode, 2)

    def test_duplicate_node_argument_rejected(self):
        result = subprocess.run(['bash', str(ROOT / 'scripts/deploy.sh'), 'app-host', 'other-host'],
                                capture_output=True)
        self.assertEqual(result.returncode, 2)

    def test_scripts_bash_syntax(self):
        scripts = sorted((ROOT / 'scripts').glob('**/*.sh'))
        self.assertTrue(scripts)
        for script in scripts:
            subprocess.run(['bash', '-n', str(script)], check=True)
