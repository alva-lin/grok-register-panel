"""导出到 Grok2API（面板内一键导入）。

流程：
  1) 平台标签=真相源：仅导入带「Grok-成功」标签的账号（risk/cpa_fail 已标失败，天然排除）。
  2) 跳过 grok2api 已存在的账号。
  3) 为每个新账号自动创建 runtime 出口节点（身份=grok-<seq>），并 1:1 分配（手动→auto）。
  4) 复用上游 multipart grok_build import。

配置全部来自容器环境变量（compose 注入），不落 config.json：
  G2A_URL / G2A_ADMIN_USER / G2A_ADMIN_PASSWORD / OUTLOOKMAIL_API / OUTLOOKMAIL_KEY
  PROXY_TOKEN / EGRESS_PLATFORM / EGRESS_GATEWAY
"""
from __future__ import annotations

import base64
import datetime as dt
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional, Tuple

DEFAULT_CLIENT = "b1a00492-073a-47ea-816f-4c329264a828"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/126.0 Safari/537.36"
AUTH_DIRS = ["cpa_auth", "grok2api_auth"]


def _json_req(method, url, body=None, token=None, api_key=None, timeout=60):
    headers = {"Content-Type": "application/json", "User-Agent": UA}
    if token:
        headers["Authorization"] = "Bearer " + token
    if api_key:
        headers["X-API-Key"] = api_key
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode())
        except Exception:
            return exc.code, {"raw": exc.read().decode(errors="replace")[:200]}


def jwt_payload(tok: str) -> dict:
    try:
        parts = tok.split(".")
        pad = "=" * ((4 - len(parts[1]) % 4) % 4)
        return json.loads(base64.urlsafe_b64decode(parts[1] + pad))
    except Exception:
        return {}


def unwrap_g2a(obj: dict) -> dict:
    if obj.get("access_token") or obj.get("refresh_token"):
        return obj
    if len(obj) == 1:
        inner = next(iter(obj.values()))
        if isinstance(inner, dict):
            return {
                "email": inner.get("email"),
                "access_token": inner.get("key") or inner.get("access_token") or "",
                "refresh_token": inner.get("refresh_token") or "",
                "id_token": inner.get("id_token") or "",
                "expires_at": inner.get("expires_at"),
                "client_id": inner.get("oidc_client_id"),
            }
    return obj


def to_grok_build(rec: dict) -> dict:
    rec = unwrap_g2a(rec)
    access = rec.get("access_token") or rec.get("key") or ""
    refresh = rec.get("refresh_token") or ""
    ap = jwt_payload(access)
    email = (rec.get("email") or "").strip().lower()
    exp = ap.get("exp")
    expires_at = rec.get("expires_at")
    if exp and not expires_at:
        expires_at = dt.datetime.fromtimestamp(int(exp), dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000000Z")
    return {
        "provider": "grok_build",
        "name": email,
        "email": email,
        "access_token": access,
        "refresh_token": refresh,
        "token_type": rec.get("token_type") or "Bearer",
        "id_token": rec.get("id_token") or "",
        "client_id": rec.get("client_id") or ap.get("client_id") or ap.get("aud") or DEFAULT_CLIENT,
        "scope": ap.get("scope") or "openid profile email offline_access grok-cli:access api:access",
        "sub": ap.get("sub") or "",
        "expires_at": expires_at,
        "enabled": True,
    }


def load_local_records(dirs: List[str]) -> Dict[str, dict]:
    found: Dict[str, dict] = {}
    for folder in dirs:
        p = Path(folder)
        if not p.is_dir():
            continue
        for path in list(p.glob("xai-*.json")) + list(p.glob("g2a-*.json")):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(raw, dict) or raw.get("bfs") is True:
                continue
            rec = unwrap_g2a(raw)
            email = (rec.get("email") or path.stem.split("-", 1)[-1]).strip().lower()
            if "@" not in email:
                continue
            if not (rec.get("refresh_token") or rec.get("access_token") or rec.get("key")):
                continue
            rec["email"] = email
            prev = found.get(email)
            if prev is None or len(str(rec.get("refresh_token") or "")) >= len(str(prev.get("refresh_token") or "")):
                found[email] = rec
    return found


def multipart_import(base: str, token: str, files: list) -> dict:
    boundary = "----PanelG2AExport"
    chunks = []
    for name, content in files:
        chunks.append(("--%s\r\n" % boundary).encode())
        chunks.append(('Content-Disposition: form-data; name="files"; filename="%s"\r\nContent-Type: application/json\r\n\r\n' % name).encode())
        chunks.append(content)
        chunks.append(b"\r\n")
    chunks.append(("--%s--\r\n" % boundary).encode())
    req = urllib.request.Request(
        base.rstrip("/") + "/api/admin/v1/accounts/import",
        data=b"".join(chunks),
        headers={"Authorization": "Bearer " + token, "Content-Type": "multipart/form-data; boundary=%s" % boundary, "Accept": "text/event-stream", "User-Agent": UA},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=600) as resp:
            raw = resp.read().decode("utf-8", "replace")
            code = resp.status
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        code = exc.code
    complete = None
    for line in raw.splitlines():
        if line.startswith("data: "):
            try:
                obj = json.loads(line[6:])
            except Exception:
                continue
            if isinstance(obj, dict) and ("created" in obj or "updated" in obj):
                complete = obj
    return {"http": code, "complete": complete}


def export_to_grok2api() -> dict:
    """执行导出。返回 {to_export, created, nodes, synced_failed, errors}。"""
    g2a_url = os.environ.get("G2A_URL", "http://grok2api:8000")
    g2a_user = os.environ.get("G2A_ADMIN_USER", "admin")
    g2a_pass = os.environ.get("G2A_ADMIN_PASSWORD", "")
    mail_api = os.environ.get("OUTLOOKMAIL_API", "https://mail-pool.yuheng.site")
    mail_key = os.environ.get("OUTLOOKMAIL_KEY", "")
    proxy_token = os.environ.get("PROXY_TOKEN", "")
    platform = os.environ.get("EGRESS_PLATFORM", "runtime")
    gateway = os.environ.get("EGRESS_GATEWAY", "100.64.20.6:50001")
    errors: List[str] = []

    # 1) 平台有效名单
    code, d = _json_req("GET", mail_api.rstrip("/") + "/api/external/accounts", api_key=mail_key)
    if code != 200:
        return {"ok": False, "error": f"平台账号列表失败 HTTP {code}"}
    valid = [
        a.get("email") for a in (d.get("accounts") or [])
        if any((t.get("name") == "Grok-成功") for t in (a.get("tags") or []))
    ]
    # 2) 登录
    code, lg = _json_req("POST", g2a_url.rstrip("/") + "/api/admin/v1/auth/login", {"username": g2a_user, "password": g2a_pass})
    if code != 200:
        return {"ok": False, "error": f"grok2api 登录失败 HTTP {code}"}
    token = (lg.get("data") or {}).get("tokens", {}).get("accessToken", "")
    if not token:
        return {"ok": False, "error": "grok2api 登录无 token"}
    # 3) 已有
    existing = set()
    page = 1
    while page <= 400:
        code, data = _json_req("GET", g2a_url.rstrip("/") + f"/api/admin/v1/accounts?provider=grok_build&page={page}&pageSize=100", token=token)
        items = (data.get("data") or {}).get("items") or []
        if not items:
            break
        for it in items:
            existing.add((it.get("email") or it.get("name") or "").lower())
        if len(items) < 100:
            break
        page += 1
    # 4) 本地候选
    local = load_local_records(AUTH_DIRS)
    todo = [email.lower() for email in valid if email.lower() in local and email.lower() not in existing]
    if not todo:
        return {"ok": True, "to_export": 0, "message": "没有待导出的账号（全部已入库或无有效标签）"}
    # 5) 自动建节点：从当前最大节点 id + 1 起
    code, nd = _json_req("GET", g2a_url.rstrip("/") + "/api/admin/v1/egress-nodes?page=1&pageSize=500", token=token)
    nodes = (nd.get("data") or {}).get("items") or []
    max_id = max([int(n.get("id") or 0) for n in nodes] or [0])
    node_ids = []
    for i, email in enumerate(todo):
        seq = max_id + 1 + i
        proxy = f"http://{platform}.grok-{seq:03d}:{proxy_token}@{gateway}"
        code, r = _json_req("POST", g2a_url.rstrip("/") + "/api/admin/v1/egress-nodes",
                            {"name": f"eg-{seq:03d}", "scope": "grok_build", "proxyURL": proxy}, token=token)
        nid = (r.get("data") or {}).get("id") or r.get("id")
        node_ids.append(nid)
    # 6) multipart 导入
    files, emails = [], []
    for email in todo:
        rec = local[email]
        payload = to_grok_build(rec)
        if not (payload["refresh_token"] or payload["access_token"]):
            continue
        files.append((f"xai-{email}.json", json.dumps(payload).encode()))
        emails.append(email)
    created = 0
    for i in range(0, len(files), 20):
        res = multipart_import(g2a_url, token, files[i:i + 20])
        if res.get("complete"):
            created += int(res["complete"].get("created") or 0)
    # 7) enabled + assign（1:1 节点）
    ids = []
    for email in emails:
        q = urllib.parse.quote(email)
        code, data = _json_req("GET", g2a_url.rstrip("/") + f"/api/admin/v1/accounts?provider=grok_build&search={q}&page=1&pageSize=10", token=token)
        for it in (data.get("data") or {}).get("items") or []:
            if (it.get("email") or it.get("name") or "").lower() == email:
                ids.append(str(it.get("id")))
                break
    if ids:
        _json_req("PATCH", g2a_url.rstrip("/") + "/api/admin/v1/accounts/batch", {"ids": ids, "enabled": True, "provider": "grok_build"}, token=token)
    for nid, aid in zip(node_ids, ids):
        _json_req("POST", g2a_url.rstrip("/") + f"/api/admin/v1/egress-nodes/{nid}/accounts",
                  {"provider": "grok_build", "ids": [aid], "mode": "auto"}, token=token)
    return {
        "ok": True,
        "to_export": len(todo),
        "created": created,
        "nodes": len(node_ids),
        "errors": errors,
        "message": f"导出完成：候选 {len(todo)}，导入 {created}，建节点 {len(node_ids)}",
    }
