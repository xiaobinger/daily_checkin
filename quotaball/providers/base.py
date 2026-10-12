#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""provider 基类：统一 HTTP、账号脱敏、返回结构。

所有 provider 的 fetch() 返回「账号记录列表」—— 一个应用可能对应多个账号
（TraeWork CN 支持多凭证，WorkBuddy / Qoder 可能有企业/个人双身份），
所以统一按列表输出，单账号应用返回长度为 1 的列表。
"""
import json
import ssl
import threading
import time
import urllib.error
import urllib.request

from ..errors import NoSessionError, QuotaError, RetryableError

UA = "quotaball/1.0"

# SSL context 全局单例：ssl.create_default_context() 在 Windows 上并发调用
# 会触发 access violation（底层 Schannel/BCrypt 初始化非线程安全）。采集层
# 用 5 线程并发跑 provider，每个都调 http_json 时就会同时 create，实测直接
# 崩溃拖垮进程。这里加锁 + 惰性单例，只初始化一次。
_ssl_lock = threading.Lock()
_ssl_ctx = None


def _default_ssl_context():
    global _ssl_ctx
    if _ssl_ctx is None:
        with _ssl_lock:
            if _ssl_ctx is None:
                _ssl_ctx = ssl.create_default_context()
    return _ssl_ctx

# 签到状态枚举
STATE_OK = "ok"                # 本次成功领取
STATE_ALREADY = "already"      # 今日已签到
STATE_PENDING = "pending"      # 今日尚未签到
STATE_NO_ACTIVITY = "no_activity"        # 当前无活动可领
STATE_QUOTA_EXHAUSTED = "quota_exhausted"  # 额度耗尽导致无可领取
STATE_NO_SESSION = "no_session"          # 登录态缺失/失效
STATE_ERROR = "error"                    # 请求或解析失败

_STATE_TEXT = {
    STATE_OK: "今日已领取",
    STATE_ALREADY: "今日已签到",
    STATE_PENDING: "待签到",
    STATE_NO_ACTIVITY: "暂无活动",
    STATE_QUOTA_EXHAUSTED: "额度已用尽",
    STATE_NO_SESSION: "登录态失效",
    STATE_ERROR: "查询失败",
}


def state_text(state):
    return _STATE_TEXT.get(state, state)


def _tidy(v):
    """数值收敛：整数就用 int，否则保留 1 位小数。

    多个额度包累加会算出 169.97999999999956 这类浮点毛刺，直接显示很难看。
    """
    try:
        f = float(v)
    except (TypeError, ValueError):
        return v
    return int(round(f)) if abs(f - round(f)) < 0.05 else round(f, 1)


def dig(obj, key):
    """在可能被 data/result 包裹的响应里递归找字段，兼容信封结构。"""
    if isinstance(obj, dict):
        if key in obj and obj[key] is not None:
            return obj[key]
        for k in ("data", "result", "resp", "response"):
            if k in obj and isinstance(obj[k], dict):
                r = dig(obj[k], key)
                if r is not None:
                    return r
    return None


def http_json(url, headers=None, method="GET", payload=None, timeout=20):
    """发一个 JSON 请求，返回 (http_code, 解析后的 body)。

    非 2xx 不会抛异常，交由 provider 自行判断 —— 401/403 要转成 NoSessionError，
    404 说明接口变更，5xx 才是可重试的。
    """
    body = None
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
    hdrs = {"Accept": "application/json", "User-Agent": UA}
    hdrs.update(headers or {})
    if body is not None:
        hdrs.setdefault("Content-Type", "application/json")

    req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
    ctx = _default_ssl_context()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return resp.status, _loads(raw)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        return e.code, _loads(raw)
    except urllib.error.URLError as e:
        raise RetryableError("网络请求失败: %s" % e.reason)
    except Exception as e:
        raise RetryableError("网络请求异常: %s" % e)


def _loads(raw):
    try:
        return json.loads(raw)
    except Exception:
        return {"raw": raw[:400]}


def mask_account(name, head=4, tail=2):
    """账号昵称脱敏，避免截图/录屏时泄露。

    xiaobinger -> xiao***er
    """
    s = (name or "").strip()
    if not s:
        return ""
    if len(s) <= head + tail:
        return s[0] + "*" * max(1, len(s) - 1)
    return "%s***%s" % (s[:head], s[-tail:])


def mask_email(email):
    """邮箱脱敏：xiongbing@juheba.com -> xio***ng@juheba.com"""
    s = (email or "").strip()
    if "@" not in s:
        return mask_account(s)
    local, domain = s.split("@", 1)
    return "%s@%s" % (mask_account(local, head=3, tail=2), domain)


class Provider(object):
    key = ""
    label = ""

    def fetch(self):
        """返回一个或多个账号记录（dict）。失败时抛 QuotaError 子类。"""
        raise NotImplementedError

    # ---- 子类常用的构造工具 ----

    def _record(self, account="", ok=True, checkin=None, quota=None,
                absolute=None, plan=None, note=None, error=None, started=0.0):
        return {
            "app": self.key,
            "app_label": self.label,
            "account": account,
            "ok": ok,
            "fetched_at": time.time(),
            "checkin": checkin,
            "quota": quota or [],
            "absolute": absolute,
            "plan": plan,
            "note": note,
            "error": error,
            "elapsed_ms": int((time.time() - started) * 1000) if started else 0,
        }

    def _window(self, label, used, total, remaining, reset_at=None, unit=""):
        """构造一个配额窗口。total <= 0 时百分比按 0 处理，避免除零。
        数值做收敛：浮点累加会产生 169.97999999999956 这种毛刺。"""
        pct = (float(used) / float(total) * 100.0) if total and total > 0 else 0.0
        return {
            "label": label,
            "used": _tidy(used),
            "total": _tidy(total),
            "remaining": _tidy(remaining),
            "percent_used": round(min(pct, 100.0), 1),
            "percent_left": round(max(100.0 - pct, 0.0), 1),
            "reset_at": reset_at,
            "unit": unit,
        }

    def _failed(self, started, error_kind, message, account=""):
        """构造一条失败记录 —— 单家失败不能影响其他家。"""
        return self._record(
            account=account, ok=False,
            checkin={"state": error_kind, "today_gain": None, "streak_days": None},
            error=message, started=started)


def summarize(records):
    """从所有记录里挑出「最紧张的那个窗口」（木桶原理），供球体渲染。

    返回 (record, window) 或 (None, None)。
    """
    best = None
    for rec in records:
        if not rec.get("ok"):
            continue
        for w in rec.get("quota") or []:
            if not w.get("total"):
                continue
            key = w.get("percent_left", 100.0)
            if best is None or key < best[1].get("percent_left", 100.0):
                best = (rec, w)
    return best if best else (None, None)
