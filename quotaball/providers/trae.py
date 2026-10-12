#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TraeWork CN：读 auths/trae-*.json 凭证（支持多账号），查询签到状态与积分余额。

实测（S0）：status 接口返回 credits（余额）+ checked_in；业务码 9074 为限流（可重试）。
凭证轮换逻辑直接复用 daily_checkin.py（refreshToken 临近过期自动刷新并写回），不重写。
"""
import json
import time

import daily_checkin as dc  # noqa: E402  复用已验证的凭证读取/轮换逻辑

from ..errors import NoSessionError, QuotaError, RetryableError
from .base import (Provider, http_json, mask_account, dig,
                   STATE_ALREADY, STATE_PENDING)


class TraeProvider(Provider):
    key = "trae"
    label = "TraeWork CN"

    def fetch(self):
        files = dc.trae_find_auth_files()
        if not files:
            raise NoSessionError(
                "未找到 TraeWork CN 凭证（auths/trae-*.json），请先运行 trae_login.py")
        return [self._one(fp) for fp in files]

    def _one(self, filepath):
        started = time.time()
        auth = dc.trae_parse(open(filepath, encoding="utf-8").read())
        who = auth.get("nickname") or auth.get("uid") or "default"

        # accessToken 临近过期（<2h）时自动用 refreshToken 轮换并写回
        exp = auth.get("expiresAt") or 0
        if exp and time.time() + 7200 >= exp:
            new_auth = dc.trae_refresh(auth)
            if new_auth:
                auth = new_auth
                try:
                    dc.trae_save_auth(filepath, auth)
                except Exception:
                    pass

        code, body = http_json(
            dc.TRAE_UG_HOST + "/trae/api/v2/ug/checkin_credits/status",
            headers=dc.trae_headers(auth), method="POST", payload={})
        if code in (401, 403):
            raise NoSessionError("TraeWork CN 凭证已失效（HTTP %s），请重新生成凭证" % code)
        if not (200 <= code < 300):
            raise QuotaError("TraeWork CN 查询失败（HTTP %s）: %s"
                             % (code, json.dumps(body, ensure_ascii=False)[:160]))

        biz = body.get("code") if isinstance(body, dict) else None
        if biz not in (0, "0", None):
            msg = (body.get("message") or body.get("msg") or "") if isinstance(body, dict) else ""
            if biz == 9074 or "稍后" in str(msg):
                raise RetryableError("TraeWork CN 限流（code=%s）: %s" % (biz, msg))
            raise QuotaError("TraeWork CN 业务错误（code=%s）: %s" % (biz, str(msg)[:120]))

        checked = dig(body, "checked_in") in (True, "true", 1, "1")
        credits = dig(body, "credits")
        return self._record(
            account=mask_account(who),
            checkin={
                "state": STATE_ALREADY if checked else STATE_PENDING,
                "today_gain": None,
                "streak_days": None,
            },
            quota=[],
            absolute={"value": credits, "unit": "积分"} if credits is not None else None,
            started=started)
