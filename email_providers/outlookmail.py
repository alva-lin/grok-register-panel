"""OutlookMail 平台邮箱 provider（B 方案：平台=唯一真相源，标签=使用状态）。

设计（docs/grok-chain.md §3.2 / R47-R48）：
- 邮箱清单与「已用/结果」状态全部保存在 OutlookMail 平台（标签系统）：
    {prefix}使用中 / {prefix}成功 / {prefix}失败 （默认前缀 Grok-）
- 取号 = 平台原子 claim（POST /api/external/accounts/tags?action=claim）：
    账号没有任何该前缀标签才占用成功；409 则跳过下一个。
- 收码 = GET /api/external/emails（平台内置 Graph→IMAP 三级回退）。
- 结果 = set 标签（成功/失败）；放弃 = unclaim（仅移除「使用中」）。
- 凭据：仅平台 API Key（X-API-Key）+ 浏览器 UA；不接触任何 RT/密码。
"""

from __future__ import annotations

import json
import re
import threading
import time
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from email_providers.common import extract_verification_code

DEFAULT_API_BASE = "https://mail-pool.yuheng.site"
DEFAULT_TAG_PREFIX = "Grok-"
PAGE_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
CODE_KEYWORDS = (
    "x.ai", "xai", "spacexai", "grok", "verification", "verify",
    "code", "confirm", "security", "one-time", "验证码", "确认",
)
TAKE_MAX_SKIP = 20
POLL_DEFAULT_INTERVAL = 4
POLL_DEFAULT_TIMEOUT = 180

_lock = threading.Lock()
_runtime_cache: Dict[str, Dict] = {}
# 每次取号/轮询的 API 调用共享同一缓存（进程内），避免重复拉全量账号列表
_cache_lock = threading.Lock()


def reset_runtime_state() -> None:
    with _cache_lock:
        _runtime_cache.clear()


# ---------------------------------------------------------------- helpers

def normalize_base(base_url: str = "") -> str:
    raw = str(base_url or "").strip() or DEFAULT_API_BASE
    if "://" not in raw:
        raw = "https://" + raw
    parsed = urlparse(raw)
    origin = f"{parsed.scheme}://{parsed.netloc}".rstrip("/")
    path = (parsed.path or "").rstrip("/")
    while path.endswith("/api") or path.endswith("/api/v1"):
        path = path[: -len("/api/v1")] if path.endswith("/api/v1") else path[: -len("/api")]
        path = path.rstrip("/")
    return f"{origin}{path}"


def _request(
    base: str,
    api_key: str,
    method: str,
    path: str,
    body: Optional[dict] = None,
    timeout: int = 30,
):
    import requests
    url = normalize_base(base) + path
    headers = {"X-API-Key": api_key, "User-Agent": PAGE_UA}
    if body is not None:
        headers["Content-Type"] = "application/json"
    resp = requests.request(
        method, url, json=body, headers=headers, timeout=timeout
    )
    try:
        data = resp.json()
    except Exception:
        data = {}
    if resp.status_code >= 400:
        return resp.status_code, data
    return resp.status_code, data


def _api_ok(status: int, data: dict) -> bool:
    return status == 200 and (data.get("success", True) is not False)


# ---------------------------------------------------------------- list / stats

def list_accounts(base: str = "", api_key: str = "") -> List[Dict]:
    """拉取平台全部账号（email/group_id/group_name/tags）。"""
    status, data = _request(base, api_key, "GET", "/api/external/accounts")
    if not _api_ok(status, data):
        raise RuntimeError(f"获取平台账号列表失败: HTTP {status} {str(data)[:150]}")
    accounts = data.get("accounts") or []
    with _cache_lock:
        _runtime_cache["accounts"] = (time.time(), accounts)
    return accounts


def _tag_names(account: Dict) -> List[str]:
    return [str(t.get("name") or "") for t in (account.get("tags") or [])]


def _has_prefix(account: Dict, prefix: str) -> bool:
    return any(n.startswith(prefix) for n in _tag_names(account))


def group_stats(
    base: str = "", api_key: str = "", prefix: str = DEFAULT_TAG_PREFIX
) -> List[Dict]:
    """按分组统计：{group_id, group_name, total, used, available}（供 UI 下拉与面板展示）。"""
    accounts = list_accounts(base, api_key)
    groups: Dict[str, Dict] = {}
    for acc in accounts:
        gid = str(acc.get("group_id") or "")
        name = str(acc.get("group_name") or "默认分组")
        g = groups.setdefault(gid, {
            "group_id": gid, "group_name": name, "total": 0, "used": 0, "available": 0,
        })
        g["total"] += 1
        if _has_prefix(acc, prefix):
            g["used"] += 1
        else:
            g["available"] += 1
    return sorted(groups.values(), key=lambda g: (-g["available"], g["group_name"]))


# ---------------------------------------------------------------- take / claim

def take_mailbox(
    base: str = "",
    api_key: str = "",
    group_id: str = "",
    prefix: str = DEFAULT_TAG_PREFIX,
    log_callback: Optional[Callable] = None,
) -> Tuple[str, str]:
    """领取一个未被使用过的邮箱（平台 claim，原子）。

    返回 (email, token_key=email)；无可用邮箱时抛出 RuntimeError。
    """
    accounts = list_accounts(base, api_key)
    eligible: List[Dict] = []
    for acc in accounts:
        gid = str(acc.get("group_id") or "")
        if group_id and gid != str(group_id):
            continue
        if _has_prefix(acc, prefix):
            continue
        eligible.append(acc)
    if not eligible:
        raise RuntimeError(
            f"没有可用邮箱（分组={group_id or '全部'}，前缀={prefix}）。"
            "无标签=未用；已用邮箱带 Grok-* 标签，可在平台查看/重置。"
        )
    for attempt, acc in enumerate(eligible[:TAKE_MAX_SKIP]):
        email = str(acc.get("email") or "")
        status, data = _request(
            base, api_key, "POST", "/api/external/accounts/tags",
            {"email": email, "action": "claim", "tag_prefix": prefix},
        )
        if status == 200 and data.get("available"):
            if log_callback:
                log_callback(f"[outlookmail] 占用 {email}（claim ok）")
            return email, email
        # 409=已被占用/已使用 → 跳过
        if log_callback:
            log_callback(f"[outlookmail] {email} claim 失败(409)，换下一个")
        continue
    raise RuntimeError(f"连续尝试 {TAKE_MAX_SKIP} 个邮箱均被占用（并发冲突）")


def release_claim(
    base: str = "", api_key: str = "", email: str = "", prefix: str = DEFAULT_TAG_PREFIX
) -> None:
    """放弃：仅移除 {prefix}使用中（终态标签不受影响）。"""
    if not email:
        return
    _request(
        base, api_key, "POST", "/api/external/accounts/tags",
        {"email": email, "action": "unclaim", "tag_prefix": prefix},
    )


def mark_result(
    base: str = "",
    api_key: str = "",
    email: str = "",
    success: bool = False,
    prefix: str = DEFAULT_TAG_PREFIX,
) -> None:
    """注册结果落平台标签：成功 → {prefix}成功；失败 → {prefix}失败（set 语义，清除使用中）。"""
    if not email:
        return
    tag = f"{prefix}成功" if success else f"{prefix}失败"
    _request(
        base, api_key, "POST", "/api/external/accounts/tags",
        {"email": email, "action": "set", "tags": [tag], "tag_prefix": prefix},
    )


# ---------------------------------------------------------------- wait / code

def _message_text(subject: str, body_preview: str = "") -> str:
    return f"{subject or ''} {body_preview or ''}"


def find_code_in_messages(emails: List[Dict], keywords=CODE_KEYWORDS) -> Optional[str]:
    for item in emails:
        subject = str(item.get("subject") or "")
        body = str(item.get("body_preview") or "")
        blob = _message_text(subject, body)
        if not any(k.lower() in blob.lower() for k in keywords):
            continue
        code = extract_verification_code(f"{subject}\n{body}")
        if code:
            return code
    return None


def wait_for_code(
    base: str = "",
    api_key: str = "",
    email: str = "",
    timeout: int = POLL_DEFAULT_TIMEOUT,
    poll_interval: float = POLL_DEFAULT_INTERVAL,
    log_callback: Optional[Callable] = None,
    cancel_callback: Optional[Callable] = None,
) -> str:
    """轮询平台收 xAI 验证码（内置 Graph→IMAP 回退），返回验证码；超时抛 RuntimeError。"""
    import requests
    deadline = time.time() + max(30, int(timeout))
    last_errors: List[str] = []
    while time.time() < deadline:
        if cancel_callback and cancel_callback():
            raise RuntimeError("用户取消")
        try:
            encoded = requests.utils.quote(email, safe="")
            status, data = _request(
                base, api_key, "GET",
                f"/api/external/emails?email={encoded}&folder=inbox&top=20",
                timeout=35,
            )
            if status == 200 and data.get("success"):
                code = find_code_in_messages(data.get("emails") or [])
                if code:
                    if log_callback:
                        log_callback(f"[outlookmail] 收到验证码（{email[:6]}...）")
                    return code
            else:
                last_errors.append(str(data.get("error") or f"HTTP{status}")[:80])
        except Exception as exc:  # 网络抖动不中断轮询
            last_errors.append(f"{type(exc).__name__}:{str(exc)[:60]}")
        time.sleep(max(2.0, float(poll_interval)))
    suffix = f"（最近错误: {last_errors[-1]}）" if last_errors else ""
    raise RuntimeError(f"等待验证码超时：{email[:6]}...{suffix}")


# ---------------------------------------------------------------- probe / config

def probe_config(
    base: str = "", api_key: str = "", group_id: str = "", prefix: str = DEFAULT_TAG_PREFIX
) -> dict:
    """连通性测试（面板『测试当前提供商』）。返回统计；出错抛异常。"""
    stats = group_stats(base, api_key, prefix)
    total = sum(g["total"] for g in stats)
    available = sum(g["available"] for g in stats)
    return {
        "total": total,
        "available": available,
        "groups": stats,
        "message": f"平台连通 ✓ 共 {total} 个账号，可用 {available} 个（分组 {len(stats)} 个）",
    }
