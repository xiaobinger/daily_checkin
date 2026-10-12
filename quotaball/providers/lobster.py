#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""LobsterAI：读桌面端 sqlite 登录态，查询每日签到活动状态。

实测（2026-10-12，客户端 2026.9.23）：
  - 登录态：%APPDATA%/LobsterAI/lobsterai.sqlite 的 kv 表
      auth_tokens -> {"accessToken","refreshToken",...}（HS512 JWT，约 30 天）
      auth_user   -> {"nickname","id",...}
  - 服务端：https://lobsterai-server.youdao.com
  - 活动槽位：GET  /api/client-activities/slot?placement=desktop_sidebar
                     &clientVersion=&containerApiVersion=2&platform=win32
  - 签到状态：GET  /api/client-activities/{activityCode}/context?configRevision=N
  - 领取动作：POST /api/client-activities/{activityCode}/actions/check_in
              body {configRevision, idempotencyKey, payload:{}}（由 daily_checkin.py 发起）

活动码 / 配置版本随灰度变化，必须每次先拉 slot，不能写死。
本 provider 只读状态，领取动作在 daily_checkin.lobster_checkin()。
"""
import json
import os
import sqlite3
import time

from ..errors import NoSessionError, QuotaError, RetryableError
from .base import (Provider, http_json, mask_account,
                   STATE_ALREADY, STATE_PENDING, STATE_NO_ACTIVITY)

ENDPOINT = "https://lobsterai-server.youdao.com"
CLIENT_VERSION = "2026.9.23"        # 仅用于 slot 查询（服务端按此灰度活动）
CONTAINER_API_VERSION = 2           # native_daily_check_in_v1 容器协议版本
PLACEMENT = "desktop_sidebar"
DAILY_TYPE = "daily_check_in"
DB_REL = os.path.join("LobsterAI", "lobsterai.sqlite")


def unwrap(code, body, what):
    """统一处理业务信封 {code, message, data}，返回 data。"""
    if code in (401, 403):
        raise NoSessionError("LobsterAI 登录态已失效（HTTP %s），请重新登录桌面端" % code)
    if code < 0:
        raise RetryableError("LobsterAI 网络请求失败: %s"
                             % (body.get("error") if isinstance(body, dict) else body))
    if not isinstance(body, dict):
        raise QuotaError("LobsterAI %s响应异常（HTTP %s）: %s"
                         % (what, code, json.dumps(body, ensure_ascii=False)[:160]))
    biz = body.get("code")
    if biz not in (0, "0", None):
        raise QuotaError("LobsterAI %s业务错误（code=%s）: %s"
                         % (what, biz, str(body.get("message") or "")[:120]))
    return body.get("data") or {}


def load_session():
    """从 sqlite 读登录态（只读连接，桌面端写库走 WAL，不冲突）。"""
    override = os.environ.get("LOBSTERAI_DB")
    if override and os.path.exists(override):
        path = override
    else:
        appdata = os.environ.get("APPDATA") or os.path.join(
            os.path.expanduser("~"), "AppData", "Roaming")
        cand = os.path.join(appdata, DB_REL)
        if not os.path.exists(cand):
            raise NoSessionError("未找到 LobsterAI 登录库（%s），请先登录桌面端" % DB_REL)
        path = cand
    try:
        con = sqlite3.connect("file:%s?mode=ro" % path.replace("\\", "/"), uri=True, timeout=5)
    except Exception as e:
        raise NoSessionError("LobsterAI 登录库打开失败: %s" % e)
    try:
        def kv(key):
            row = con.execute("select value from kv where key=?", (key,)).fetchone()
            if not row or not row[0]:
                return {}
            try:
                return json.loads(row[0])
            except Exception:
                return {}
        tokens, user = kv("auth_tokens"), kv("auth_user")
    finally:
        con.close()
    token = tokens.get("accessToken")
    if not token:
        raise NoSessionError("LobsterAI 登录态缺少 accessToken，请重新登录桌面端")
    return token, user


def headers(token):
    return {
        "Authorization": "Bearer %s" % token,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "LobsterAI/%s" % CLIENT_VERSION,
        "Cache-Control": "no-store",
    }


def get_slot(token):
    """返回活动槽位 data（活动码 / 配置版本 / 周期文案）。"""
    code, body = http_json(
        "%s/api/client-activities/slot?placement=%s&clientVersion=%s"
        "&containerApiVersion=%d&platform=win32"
        % (ENDPOINT, PLACEMENT, CLIENT_VERSION, CONTAINER_API_VERSION),
        headers=headers(token))
    return unwrap(code, body, "活动槽位")


def get_context(token, activity_code, revision):
    """返回签到上下文 data（含 state.claimedToday / claimedDays）。"""
    code, body = http_json(
        "%s/api/client-activities/%s/context?configRevision=%d"
        % (ENDPOINT, activity_code, revision), headers=headers(token))
    return unwrap(code, body, "签到状态")


class LobsterProvider(Provider):
    key = "lobster"
    label = "LobsterAI"

    def fetch(self):
        started = time.time()
        token, user = load_session()
        who = mask_account(user.get("nickname") or user.get("yid"))

        slot = get_slot(token)
        act = slot.get("activity")
        if slot.get("slotState") != "available" or not act:
            return [self._record(
                account=who,
                checkin={"state": STATE_NO_ACTIVITY, "today_gain": None, "streak_days": None},
                quota=[], absolute=None, note="当前无每日签到活动", started=started)]

        activity_code = act.get("activityCode")
        revision = act.get("configRevision")
        if act.get("activityType") != DAILY_TYPE or not activity_code or not revision:
            return [self._record(
                account=who,
                checkin={"state": STATE_NO_ACTIVITY, "today_gain": None, "streak_days": None},
                quota=[], absolute=None,
                note="当前活动非每日签到（%s）" % (act.get("activityType") or "未知"),
                started=started)]

        ctx = get_context(token, activity_code, revision)
        state = ctx.get("state") or {}
        claimed_today = bool(state.get("claimedToday"))
        reward = state.get("rewardCredits") or 0
        claimed_credits = state.get("claimedCredits")

        return [self._record(
            account=who,
            checkin={
                "state": STATE_ALREADY if claimed_today else STATE_PENDING,
                "today_gain": reward if claimed_today else None,
                "streak_days": state.get("claimedDays"),
            },
            quota=[],
            absolute={"value": claimed_credits, "unit": "credits"}
            if claimed_credits is not None else None,
            note=None if claimed_today else ("今日可领 +%s credits" % reward if reward else None),
            started=started)]