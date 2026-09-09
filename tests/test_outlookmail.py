"""outlookmail provider 核心逻辑测试（平台 API 用 mock 替身，不发真实请求）。"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from email_providers import outlookmail as provider  # noqa: E402


class FakeAPI:
    """替身：内存实现 /api/external/accounts、/api/external/accounts/tags、/api/external/emails。"""

    def __init__(self, accounts):
        self.accounts = {a["email"]: a for a in accounts}
        self.claims = {}  # email -> "Grok-使用中"
        self.calls = []   # (method, path, body) 记录

    def handle(self, method, path, body):
        self.calls.append((method, path, body))
        if method == "GET" and path.startswith("/api/external/accounts"):
            data = {
                "success": True,
                "total": len(self.accounts),
                "accounts": [
                    {**a, "tags": [t for t in a.get("tags", []) if True]}
                    for a in self.accounts.values()
                ],
            }
            return 200, data
        if method == "POST" and path == "/api/external/accounts/tags":
            email = body["email"]
            action = body.get("action")
            prefix = body.get("tag_prefix", "Grok-")
            acc = self.accounts.get(email)
            if not acc:
                return 404, {"success": False, "error": "邮箱账号不存在"}
            tags = acc.setdefault("tags", [])
            def names():
                return [t["name"] for t in tags]
            if action == "claim":
                if any(n.startswith(prefix) for n in names()):
                    return 409, {"success": True, "available": False, "tags": tags}
                if "Grok-使用中" not in names():
                    tags.append({"name": prefix + "使用中"})
                return 200, {"success": True, "available": True, "tags": tags}
            if action == "unclaim":
                tags[:] = [t for t in tags if t["name"] != prefix + "使用中"]
                return 200, {"success": True, "tags": tags}
            if action in ("set", "remove"):
                tags[:] = [t for t in tags if not t["name"].startswith(prefix)]
            if action in ("add", "set"):
                for name in body.get("tags") or []:
                    tags.append({"name": name})
            return 200, {"success": True, "tags": tags}
        if method == "GET" and path.startswith("/api/external/emails"):
            return 200, {"success": True, "emails": self.emails}
        raise AssertionError(f"unexpected call {method} {path}")

    def install(self, emails=None):
        self.emails = emails or []
        orig = provider._request

        def fake_request(base, api_key, method, path, body=None, timeout=30):
            return self.handle(method, path, body)

        provider._request = fake_request
        self._orig = orig

    def uninstall(self):
        provider._request = self._orig


def acc(email, group_id=1, group_name="g1", tags=None):
    return {"email": email, "group_id": group_id, "group_name": group_name,
            "tags": [{"name": t} for t in (tags or [])]}


class TestGroupStats(unittest.TestCase):
    def test_counts_by_group(self):
        fake = FakeAPI([
            acc("a@outlook.com", 4, "wzyp"),
            acc("b@outlook.com", 4, "wzyp", tags=["Grok-成功"]),
            acc("c@outlook.com", 6, "cheap"),
            acc("d@outlook.com", 6, "cheap", tags=["Grok-失败"]),
            acc("e@outlook.com", 6, "cheap", tags=["Grok-使用中"]),
        ])
        fake.install()
        try:
            stats = provider.group_stats("https://x.example", "k")
            by_id = {g["group_id"]: g for g in stats}
            self.assertEqual(by_id["4"]["total"], 2)
            self.assertEqual(by_id["4"]["used"], 1)
            self.assertEqual(by_id["4"]["available"], 1)
            self.assertEqual(by_id["6"]["total"], 3)
            self.assertEqual(by_id["6"]["available"], 0)
        finally:
            fake.uninstall()


class TestTakeMailbox(unittest.TestCase):
    def test_group_filter_and_claim(self):
        fake = FakeAPI([
            acc("used@outlook.com", 4, "wzyp", tags=["Grok-成功"]),
            acc("free@outlook.com", 4, "wzyp"),
        ])
        fake.install()
        try:
            email, token = provider.take_mailbox(base="https://x", api_key="k", group_id="4")
            self.assertEqual(email, "free@outlook.com")
            self.assertEqual(token, "free@outlook.com")
            self.assertEqual(len(fake.claims), 0)  # claims 由服务端 tag 承载
            # 再取 → 全部已被占/已用 → 抛错
            with self.assertRaises(RuntimeError) as ctx:
                provider.take_mailbox(base="https://x", api_key="k", group_id="4")
            self.assertIn("没有可用邮箱", str(ctx.exception))
        finally:
            fake.uninstall()

    def test_other_group_ignored(self):
        fake = FakeAPI([acc("free@outlook.com", 6, "cheap")])
        fake.install()
        try:
            with self.assertRaises(RuntimeError):
                provider.take_mailbox(base="https://x", api_key="k", group_id="4")
        finally:
            fake.uninstall()


class TestMarkAndRelease(unittest.TestCase):
    def test_mark_result_sets_terminal_tag(self):
        fake = FakeAPI([acc("m@outlook.com", 4, "wzyp")])
        fake.install()
        try:
            provider.mark_result("https://x", "k", "m@outlook.com", success=True)
            self.assertEqual(fake.accounts["m@outlook.com"]["tags"][-1]["name"], "Grok-成功")
            provider.mark_result("https://x", "k", "m@outlook.com", success=False)
            # set 语义：只保留最近一个前缀标签
            names = [t["name"] for t in fake.accounts["m@outlook.com"]["tags"]]
            self.assertEqual(names, ["Grok-失败"])
        finally:
            fake.uninstall()

    def test_release_only_removes_in_use(self):
        fake = FakeAPI([acc("r@outlook.com", 4, "wzyp", tags=["Grok-使用中", "Grok-成功"])])
        fake.install()
        try:
            provider.release_claim("https://x", "k", "r@outlook.com")
            names = [t["name"] for t in fake.accounts["r@outlook.com"]["tags"]]
            self.assertEqual(names, ["Grok-成功"])
        finally:
            fake.uninstall()


class TestFindCode(unittest.TestCase):
    def test_extract_from_subject(self):
        emails = [
            {"subject": "Your xAI verification code is 483920", "body_preview": ""},
            {"subject": "no code here", "body_preview": "hello"},
        ]
        self.assertEqual(provider.find_code_in_messages(emails), "483920")

    def test_skip_unrelated(self):
        self.assertIsNone(provider.find_code_in_messages(
            [{"subject": "Weekly report", "body_preview": "content"}]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
