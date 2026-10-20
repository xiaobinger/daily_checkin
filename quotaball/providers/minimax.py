#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MiniMax Code：读本机登录态，复现桌面端签名，查询 7 日签到日历。

实测（S0）：signin/status 只有 days 数组，无余额接口。status==3 表示当日已领取。

3.1 桌面端改用 OAuth（~/.minimax/auth/**/mcode-public/auth.json），accessToken
是 mmoat_ 前缀的短期凭证，请求带 Authorization: Bearer + yy/x-timestamp/x-signature。
签名与取 token 都在 quotaball/mmauth.py，请求复用 daily_checkin.py 的 mm_request。
"""
import json
import time

import daily_checkin as dc  # noqa: E402

from ..errors import NoSessionError, QuotaError, RetryableError
from .base import (Provider, mask_account, STATE_ALREADY, STATE_PENDING)


class MiniMaxProvider(Provider):
    key = "minimax"
    label = "MiniMax Code"

    def _fetch_credit(self, cfg, token):
        """积分余额：/minimax-cloud/api/v1/credit/details（逐笔额度，含到期时间）。

        实测（2026-09-02）：details 数组每项含 granted_amount / remaining_amount /
        expire_at_ms（字符串小数）。汇总未过期条目得总余额；reset_at 取最近到期时间
        ——MiniMax 的积分是「先到期的先扣」，所以最近到期日就是最值得关注的重置点。
        """
        try:
            code, body = dc.mm_request(cfg, token,
                                       "/minimax-cloud/api/v1/credit/details")
        except Exception:
            return []
        if not isinstance(body, dict):
            return []
        details = body.get("details") or []
        tot = rem = 0.0
        soonest = None
        now_ms = time.time() * 1000.0
        for d in details:
            if not isinstance(d, dict):
                continue
            exp = d.get("expire_at_ms") or 0
            try:
                exp = float(exp)
            except (TypeError, ValueError):
                exp = 0
            if exp and exp < now_ms:          # 已过期额度不计入
                continue
            try:
                g = float(d.get("granted_amount") or 0)
                r = float(d.get("remaining_amount") or 0)
            except (TypeError, ValueError):
                continue
            tot += g
            rem += r
            if exp and (soonest is None or exp < soonest):
                soonest = exp
        if tot <= 0:
            return []
        return [self._window(label="积分余额", used=max(tot - rem, 0.0), total=tot,
                             remaining=rem,
                             reset_at=(soonest / 1000.0 if soonest else None),
                             unit="credits")]

    def fetch(self):
        started = time.time()
        cfg_path = dc.mm_find_config()
        cfg = {}
        if cfg_path:
            try:
                cfg = json.load(open(cfg_path, encoding="utf-8"))
            except Exception as e:
                raise NoSessionError("MiniMax Code 登录态解析失败: %s" % e)

        # 3.1 的凭证在 OAuth 文件里（accessToken 有效期只有几十分钟，
        # 过期时用 refreshToken 换新的），配置缺失也不影响
        token, source = dc.mm_load_token(cfg)
        if not token:
            raise NoSessionError("MiniMax Code 缺少 accessToken（%s）" % source)

        # mmoat_ 令牌的新鲜度由 mmauth.access_token() 保证；旧版 JWT 自带 exp
        if not token.startswith("mmoat_"):
            try:
                _, _, payload = token.split(".")
                exp_ts = dc.mm_jwt_exp(payload)
                if exp_ts and time.time() >= exp_ts:
                    raise NoSessionError("MiniMax Code accessToken 已过期，请打开客户端续签")
            except NoSessionError:
                raise
            except Exception:
                pass

        code, body = dc.mm_request(cfg, token, "/minimax-cloud/api/v1/signin/status")
        if code in (401, 400):
            raise NoSessionError("MiniMax Code 登录态失效（HTTP %s），请重新打开客户端" % code)
        if code < 0:
            raise RetryableError("MiniMax Code 网络请求失败: %s"
                                 % (body.get("error") if isinstance(body, dict) else body))
        if not isinstance(body, dict) or (body.get("base_resp") or {}).get("status_code") != 0:
            raise QuotaError("MiniMax Code 查询失败（HTTP %s）: %s"
                             % (code, json.dumps(body, ensure_ascii=False)[:160]))

        days = ((body.get("data") or {}).get("days")) or []
        today = next((d for d in days if d.get("is_today")), None)
        points = (today or {}).get("points", 0)
        checked = (today or {}).get("status") == 3

        user = cfg.get("user") or {}
        who = user.get("nickname") or user.get("userName") or user.get("realUserID") or ""
        return [self._record(
            account=mask_account(who),
            checkin={
                "state": STATE_ALREADY if checked else STATE_PENDING,
                "today_gain": points if checked else None,
                "streak_days": None,
            },
            quota=self._fetch_credit(cfg, token), absolute=None,
            note=None if checked else ("今日可领 +%s" % points if points else None),
            started=started)]
