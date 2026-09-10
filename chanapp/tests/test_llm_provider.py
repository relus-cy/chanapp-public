"""Task 10: LLM provider 抽象（DeepSeek 先行）测试。

mock HTTP（unittest.mock 替换 requests.post），离线可跑，不打外网。
真 key 连通性冒烟待用户提供 DEEPSEEK_API_KEY 后手动执行。
"""
import os
import unittest
from unittest import mock

import requests  # noqa: F401  (确认 venv 已装 requests)


class _FakeResp:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


class _EnvMixin:
    """设置/还原 LLM_* 环境变量。"""

    def _set_env(self, **kwargs):
        saved = {}
        for key in ("LLM_PROVIDER", "LLM_API_KEY", "LLM_MODEL"):
            saved[key] = os.environ.get(key)
            if key in kwargs:
                os.environ[key] = kwargs[key]
            else:
                os.environ.pop(key, None)

        def _restore():
            for key, val in saved.items():
                if val is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = val

        self.addCleanup(_restore)


class TestLLMProvider(_EnvMixin, unittest.TestCase):
    def test_deepseek_payload_and_parse(self):
        self._set_env(
            LLM_PROVIDER="deepseek",
            LLM_API_KEY="k",
            LLM_MODEL="deepseek-chat",
        )
        sent = {}

        def fake_post(url, headers=None, json=None, timeout=None):
            sent.update(url=url, model=json["model"],
                        auth=headers.get("Authorization", ""), timeout=timeout)
            return _FakeResp({"choices": [{"message": {"content": "分析文本"}}]})

        with mock.patch("requests.post", side_effect=fake_post):
            from chanapp.engine import llm
            self.assertEqual(llm.analyze("p"), "分析文本")
        self.assertEqual(sent["model"], "deepseek-chat")
        self.assertIn("chat/completions", sent["url"])
        self.assertEqual(sent["auth"], "Bearer k")
        self.assertEqual(sent["timeout"], 30)

    def test_configurable_response_timeout(self):
        self._set_env(LLM_API_KEY="k")
        from chanapp.engine import llm
        with mock.patch.dict(os.environ, {"LLM_TIMEOUT_SECONDS": "120"}), mock.patch("requests.post", return_value=_FakeResp({"choices": [{"message": {"content": "ok"}}]})) as post:
            llm.analyze("p")
        self.assertEqual(post.call_args.kwargs["timeout"], 120)

    def test_default_model_and_provider(self):
        self._set_env(LLM_API_KEY="k")  # 不设 LLM_PROVIDER / LLM_MODEL
        sent = {}

        def fake_post(url, headers=None, json=None, timeout=None):
            sent.update(url=url, model=json["model"])
            return _FakeResp({"choices": [{"message": {"content": "ok"}}]})

        with mock.patch("requests.post", side_effect=fake_post):
            from chanapp.engine import llm
            self.assertEqual(llm.analyze("p"), "ok")
        self.assertEqual(sent["model"], "deepseek-flash")  # 缺省 provider/model

    def test_is_configured(self):
        self._set_env()  # 无 LLM_API_KEY
        from chanapp.engine import llm
        self.assertFalse(llm.is_configured())
        self._set_env(LLM_API_KEY="k")
        self.assertTrue(llm.is_configured())

    def test_http_error_raises_llm_error(self):
        self._set_env(LLM_API_KEY="k")

        def fake_post(url, headers=None, json=None, timeout=None):
            return _FakeResp({}, status_code=500)

        with mock.patch("requests.post", side_effect=fake_post):
            from chanapp.engine import llm
            with self.assertRaises(llm.LLMError):
                llm.analyze("p")

    def test_network_error_raises_llm_error(self):
        self._set_env(LLM_API_KEY="k")
        with mock.patch("requests.post",
                        side_effect=requests.ConnectionError("boom")):
            from chanapp.engine import llm
            with self.assertRaises(llm.LLMError):
                llm.analyze("p")

    def test_unconfigured_raises_llm_error(self):
        self._set_env()  # 无 LLM_API_KEY
        from chanapp.engine import llm
        with self.assertRaises(llm.LLMError):
            llm.analyze("p")


if __name__ == "__main__":
    unittest.main()
