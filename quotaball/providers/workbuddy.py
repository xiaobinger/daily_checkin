#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WorkBuddy：读本机桌面端登录态，查询签到状态。

实测（S0）：查询接口是 POST（GET 会 404）；无余额接口，只能拿到签到状态与当日领取。

WorkBuddy 5.7+（2026-10-12 适配）：auth.accessToken 不再是明文，而是
`{"$wbEncrypted":1,"envelope":{suite,keyId,nonce,authTag,ciphertext}}` 的
AES-256-GCM 信封。解密见 ../wbatrest.py：密钥由桌面端原生模块
electron_browser_workbuddy_storage.loggerGet() 提供，磁盘无副本，需一次性提取
后用 DPAPI 缓存（tools/wb_extract.js + tools/wb_cache_key.py）。
没有缓存时仍可用环境变量 WORKBUDDY_TOKEN 注入明文 token。
"""
import json
import os
import time

from .. import wbatrest
from ..errors import NoSessionError, QuotaError
from .base import (Provider, http_json, mask_account, dig,
                   STATE_ALREADY, STATE_PENDING)

ENDPOINT = "https://copilot.tencent.com"
AUTH_BASENAME = os.path.join(
    "CodeBuddyExtension", "Data", "Public", "auth", "workbuddy-desktop.info")


def _read_token(auth):
    """从 auth 段取明文 accessToken，自动解开 5.7+ 的信封。"""
    raw = auth.get("accessToken")
    override = os.environ.get("WORKBUDDY_TOKEN")
    if override:
        return override
    if wbatrest.is_envelope(raw):
        try:
            return wbatrest.open_auth_field(raw)
        except Exception as e:
            raise NoSessionError("WorkBuddy 登录态信封解密失败: %s" % e)
    return raw


def _plain(value):
    """账号段里的敏感字段同样是信封；解密不了就返回 None（不是 str 就当空）。"""
    if isinstance(value, str):
        return value
    if wbatrest.is_envelope(value):
        try:
            return wbatrest.open_auth_field(value)
        except Exception:
            return None
    return None


class WorkBuddyProvider(Provider):
    key = "workbuddy"
    label = "WorkBuddy"

    def _auth_path(self):
        override = os.environ.get("WORKBUDDY_AUTH_FILE")
        if override and os.path.exists(override):
            return override
        local = os.environ.get("LOCALAPPDATA") or os.path.join(
            os.path.expanduser("~"), "AppData", "Local")
        cand = os.path.join(local, AUTH_BASENAME)
        return cand if os.path.exists(cand) else None

    def _fetch_quota(self, headers):
        """积分额度：聚合资源包（POST /v2/billing/meter/get-user-resource）。

        实测（2026-09-02）：返回 23 个资源包，每个含 CapacitySize/CapacityUsed/
        CapacityRemain（单位 credits）。已过期包（ExpiredTime 非空）不计入。
        额度接口失败只降级，不影响签到状态。
        """
        try:
            code, body = http_json(
                ENDPOINT + "/v2/billing/meter/get-user-resource",
                headers=headers, method="POST", payload={"ProductCode": "p_tcaca"})
        except Exception:
            return []
        if not (200 <= code < 300) or not isinstance(body, dict):
            return []
        data = ((body.get("data") or {}).get("Response") or {}).get("Data") or {}
        accounts = data.get("Accounts") or []
        now_str = time.strftime("%Y-%m-%d %H:%M:%S")   # 与接口同格式，字典序即时序
        tot = used = rem = 0.0
        soonest_end = None
        for a in accounts:
            if not isinstance(a, dict):
                continue
            expired = (a.get("ExpiredTime") or "").strip()
            if expired and expired <= now_str:        # 已过期的资源包不计入
                continue
            try:
                # 用 Cycle* 口径：Capacity* 字段在部分包上不扣周期内用量（实测
                # CapacityUsed=0 而 CycleCapacityUsed=72），会虚高剩余百分比。
                size = float(a.get("CycleCapacitySize") or 0)
                cons = float(a.get("CycleCapacityUsed") or 0)
                left = float(a.get("CycleCapacityRemain") or 0)
            except (TypeError, ValueError):
                continue
            if size <= 0:
                continue
            tot += size
            used += cons
            rem += left
            end = (a.get("CycleEndTime") or "").strip()
            if end and (soonest_end is None or end < soonest_end):
                soonest_end = end
        if tot <= 0:
            return []
        reset_at = None
        if soonest_end:
            try:
                reset_at = time.mktime(time.strptime(soonest_end, "%Y-%m-%d %H:%M:%S"))
            except (ValueError, OverflowError):
                reset_at = None
        return [self._window(label="积分额度", used=used, total=tot, remaining=rem,
                             reset_at=reset_at, unit="credits")]

    def fetch(self):
        started = time.time()
        path = self._auth_path()
        if not path:
            raise NoSessionError("未找到 WorkBuddy 登录态文件，请先登录桌面端")
        try:
            session = json.load(open(path, encoding="utf-8"))
        except Exception as e:
            raise NoSessionError("WorkBuddy 登录态解析失败: %s" % e)

        auth = session.get("auth") or {}
        account = session.get("account") or {}
        token = _read_token(auth)
        uid = account.get("uid")
        if not token or not uid:
            raise NoSessionError(
                "WorkBuddy 登录态缺少 accessToken / uid（5.7+ 为加密信封，"
                "需先执行 tools/wb_extract.js 提取密钥，或设置 WORKBUDDY_TOKEN）")

        headers = {
            "Authorization": "Bearer %s" % token,
            "Content-Type": "application/json",
            "X-User-Id": uid,
            "User-Agent": "WorkBuddy",
        }
        if account.get("enterpriseId"):
            headers["X-Enterprise-Id"] = account["enterpriseId"]
            headers["X-Tenant-Id"] = account["enterpriseId"]
        code, body = http_json(
            ENDPOINT + "/v2/billing/meter/checkin-activity-status",
            headers=headers, method="POST", payload={})
        if code in (401, 403):
            raise NoSessionError("WorkBuddy 登录态已失效（HTTP %s），请重新登录桌面端" % code)
        if not (200 <= code < 300):
            raise QuotaError("WorkBuddy 查询失败（HTTP %s）: %s"
                             % (code, json.dumps(body, ensure_ascii=False)[:160]))

        checked = dig(body, "today_checked_in")
        if checked is None:
            raise QuotaError("WorkBuddy 响应缺少 today_checked_in: %s"
                             % json.dumps(body, ensure_ascii=False)[:160])
        checked = checked in (True, "true", 1, "1")
        today_credit = dig(body, "today_credit")
        daily_credit = dig(body, "daily_credit")
        streak = dig(body, "streak_days")

        return [self._record(
            account=mask_account(_plain(account.get("nickname"))
                                 or _plain(account.get("email")) or uid),
            checkin={
                "state": STATE_ALREADY if checked else STATE_PENDING,
                "today_gain": today_credit if checked else None,
                "streak_days": streak,
            },
            quota=self._fetch_quota(headers), absolute=None,
            note=None if checked else ("今日可领 +%s" % daily_credit if daily_credit else None),
            started=started)]
