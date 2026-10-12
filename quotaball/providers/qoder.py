#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Qoder CN：读 Chromium vscdb 登录态（AES-GCM + DPAPI 解密），查询可领取活动。

实测（S0）：eligibility 常返回 data:[]（无活动）—— 这不是失败，是独立状态；
余额类路径全部 503（路由不存在）。解密逻辑复用 daily_checkin.py 的 qoder_find_config。
"""
import time

import daily_checkin as dc  # noqa: E402

from ..errors import NoSessionError, QuotaError
from .base import (Provider, http_json, mask_account, mask_email,
                   STATE_ALREADY, STATE_PENDING, STATE_NO_ACTIVITY,
                   STATE_QUOTA_EXHAUSTED)


class QoderProvider(Provider):
    key = "qoder"
    label = "Qoder CN"

    def _fetch_quota(self, headers):
        """额度：GET /api/v2/quota/usage。

        实测（2026-09-02）：返回 userQuota{total,used,remaining,unit} 与 expiresAt(ms)。
        注意这条路径是 200，而 /api/v2/user/quota 等是 503（ALB 层路由不存在）。
        返回 (quota列表, plan)；额度耗尽时用 "__exhausted__" 标记。
        """
        try:
            code, body = http_json(
                dc.QODER_OPENAPI_HOST + "/api/v2/quota/usage", headers=headers)
        except Exception:
            return [], None
        if not (200 <= code < 300) or not isinstance(body, dict):
            return [], None
        q = body.get("userQuota") or {}
        try:
            tot = float(q.get("total") or 0)
            used = float(q.get("used") or 0)
        except (TypeError, ValueError):
            return [], None
        rem = q.get("remaining")
        try:
            rem = float(rem) if rem is not None else max(tot - used, 0.0)
        except (TypeError, ValueError):
            rem = max(tot - used, 0.0)
        exp = body.get("expiresAt")
        try:
            reset_at = float(exp) / 1000.0 if exp else None
        except (TypeError, ValueError):
            reset_at = None
        quota = []
        if tot > 0:
            quota.append(self._window(label="额度", used=used, total=tot,
                                      remaining=rem, reset_at=reset_at,
                                      unit=q.get("unit") or "credits"))
        plan = "__exhausted__" if body.get("isQuotaExceeded") else (
            body.get("userType") or None)
        return quota, plan

    def fetch(self):
        started = time.time()
        try:
            cfg = dc.qoder_find_config()
        except Exception as e:
            raise NoSessionError("Qoder CN 登录态解密失败: %s" % e)
        if not cfg:
            raise NoSessionError("未找到 Qoder CN 登录态，请先登录桌面端")
        token = cfg.get("token")
        if not token:
            raise NoSessionError("Qoder CN 登录态缺少 token")
        exp = cfg.get("expireTime")
        if exp and int(exp) < int(time.time() * 1000):
            raise NoSessionError("Qoder CN 登录态已过期，请重新打开 Qoder CN 续签")

        headers = {"Authorization": "Bearer " + token,
                   "User-Agent": "QoderCN/1.0", "Accept-Language": "zh-CN"}
        code, body = http_json(
            dc.QODER_OPENAPI_HOST + "/api/v2/activity/claim/eligibility",
            headers=headers)
        if code in (401, 403):
            raise NoSessionError("Qoder CN 登录态已失效（HTTP %s）" % code)
        if not (200 <= code < 300) or not isinstance(body, dict) or body.get("code") != 0:
            raise QuotaError("Qoder CN 查询失败（HTTP %s, code=%s）"
                             % (code, body.get("code") if isinstance(body, dict) else None))

        activities = body.get("data") or []
        claimable = [a for a in activities if a.get("canClaim") is True]
        if not activities:
            state = STATE_NO_ACTIVITY
        elif claimable:
            state = STATE_PENDING
        else:
            state = STATE_ALREADY

        quota, plan = self._fetch_quota(headers)
        # 无活动且额度已用尽 → 不是「没活动」，是额度耗尽导致无活动可领
        if plan == "__exhausted__":
            plan = None
            if state == STATE_NO_ACTIVITY:
                state = STATE_QUOTA_EXHAUSTED

        who = (cfg.get("email") or cfg.get("nickname")
               or cfg.get("username") or cfg.get("uid") or "")
        account = mask_email(who) if "@" in str(who) else mask_account(who)
        return [self._record(
            account=account,
            checkin={"state": state, "today_gain": None, "streak_days": None},
            quota=quota, absolute=None,
            note=("发现 %d 个可领取活动" % len(claimable)) if claimable else None,
            started=started)]
