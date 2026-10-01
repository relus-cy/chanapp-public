"""仓库根 .gitleaks.toml：hooks 与 CI 共用的扫描规则要拦住本项目的凭据形态与本机路径，并放行仓库自身。
样例在运行时拼接，避免测试文件本身被扫描命中。"""
from pathlib import Path
import shutil
import subprocess
import unittest

REPO = Path(__file__).resolve().parents[2]
CONFIG = REPO / '.gitleaks.toml'


def scan(text):
    return subprocess.run(['gitleaks', 'stdin', '--no-banner', '--log-level', 'error', '--config', str(CONFIG)],
                          input=text, capture_output=True, text=True).returncode


@unittest.skipUnless(shutil.which('gitleaks'), 'gitleaks 未安装')
class GitleaksConfigTests(unittest.TestCase):
    def test_rejects_project_credentials_and_local_paths(self):
        value = 'a1b2c3d4' * 4
        for text in ('MAIRUI_' + 'LICENCE=' + value,
                     'export MAIRUI_' + 'LICENCE="' + value + '"',
                     'https://api.' + 'mairuiapi.com/hsstock/history/600036.SH/d/n/' + value,
                     '/' + 'Users/someone',
                     '/' + 'Users/someone/project',
                     '/' + 'home/someone/project',
                     'C:\\' + 'Users\\someone\\project'):
            with self.subTest(text=text):
                self.assertEqual(scan(text + '\n'), 1)

    def test_allows_placeholders_and_template_paths(self):
        for text in ('MAIRUI_' + 'LICENCE=',
                     'https://api.' + 'mairuiapi.com/hsstock/history/600036.SH/d/n/{L}',
                     'APP_DIR=/' + 'home/ubuntu/chanapp',
                     'APP_DIR=/' + 'home/$DEPLOY_ACCOUNT/chanapp',
                     'EnvironmentFile=-/' + 'home/@USER@/.config/chanapp/secrets.env'):
            with self.subTest(text=text):
                self.assertEqual(scan(text + '\n'), 0)
