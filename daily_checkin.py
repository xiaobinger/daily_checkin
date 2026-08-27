#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TraeWorkCN (TRAE SOLO CN) + WorkBuddy + MiniMax Code 每日自动签到脚本
====================================================================
功能：
  1. WorkBuddy    —— 读取本机桌面端登录态，调官方签到接口领取每日积分（开箱即用）
  2. TraeWorkCN   —— 读取 auths/trae-*.json 凭证（traework2api 兼容格式），调官方签到接口
                     领取每日 Work 专属积分；accessToken 临近过期时自动用 refreshToken 轮换
  3. MiniMax Code —— 读取本机登录态 minimax-agent-cn-config.json，复现桌面端请求
                     （x-signature = md5(unix秒 + salt + body) + 设备参数）领取每日 400+ 积分

用法：
  python daily_checkin.py                 # 三个应用都签到
  python daily_checkin.py --workbuddy-only
  python daily_checkin.py --trae-only
  python daily_checkin.py --minimax-only
  python daily_checkin.py --check-only    # 只查询今日签到状态，不执行领取

依赖：Python 3.6+，仅标准库，无第三方包。

TraeWorkCN 凭证获取（一次性）：
  方式一（推荐）：克隆 https://github.com/Sliverkiss/traework2api ，运行 ./login.sh 走一次浏览器
                 登录，把生成的 trae-*.json 放到本脚本同目录 auths/ 文件夹下。
  方式二：自行抓包 TRAE SOLO CN 客户端的 Cloud-IDE-JWT token，按扁平格式写入 trae_auth.json：
          {"accessToken": "...", "refreshToken": "...", "deviceId": "...", "expiresAt": 0, "uid": "..."}

MiniMax Code 说明：
  依赖本机登录态文件（默认 %APPDATA%\\MiniMax\\minimax-agent-cn-config.json）。
  accessToken 为客户端签发 JWT（约 7 天有效），过期后需打开一次 MiniMax Code 让客户端续签。
  签到接口每日幂等（重复领取返回同一 claim_id，不重复计分），脚本同时以本地 claim_id
  记录做二次去重。
"""
import glob
import hashlib
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# ==================== 配置 ====================
WORKBUDDY_ENDPOINT = "https://copilot.tencent.com"
WORKBUDDY_AUTH_BASENAME = os.path.join(
    "CodeBuddyExtension", "Data", "Public", "auth", "workbuddy-desktop.info")

TRAE_UG_HOST = "https://api.trae.cn"           # 签到 / 积分查询
TRAE_OAUTH_HOST = "https://api.trae.com.cn"    # ExchangeToken 刷新
TRAE_CLIENT_ID = "en1oxy7wnw8j9n"              # SOLO stable
TRAE_UA = "Trae/0.1.43"

MINIMAX_HOST = "https://agent.minimaxi.com"
MINIMAX_SIGN_SALT = "I*7Cf%WZ#S&%1RlZJ&C2"     # 静态 JS 内联的签名盐（chunk 4193）
MINIMAX_CONFIG_BASENAME = os.path.join("MiniMax", "minimax-agent-cn-config.json")

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TRAE_AUTH_DIR = os.path.join(SCRIPT_DIR, "auths")
TRAE_AUTH_FILE = os.environ.get(
    "TRAE_AUTH_FILE", os.path.join(SCRIPT_DIR, "trae_auth.json"))
LOG_FILE = os.path.join(SCRIPT_DIR, "checkin.log")
STATE_FILE = os.path.join(SCRIPT_DIR, "checkin_state.json")

CHECK_ONLY = "--check-only" in sys.argv
DO_WORKBUDDY = "--trae-only" not in sys.argv and "--minimax-only" not in sys.argv
DO_TRAE = "--workbuddy-only" not in sys.argv and "--minimax-only" not in sys.argv
DO_MINIMAX = "--workbuddy-only" not in sys.argv and "--trae-only" not in sys.argv


def log(msg):
    line = "[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _request(url, headers, method="GET", payload=None, timeout=30):
    body = None
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            raw = resp.read().decode("utf-8", "replace")
            try:
                return resp.status, json.loads(raw)
            except Exception:
                return resp.status, {"raw": raw[:500]}
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"raw": raw[:500]}
    except Exception as e:
        return -1, {"error": str(e)}


def post(url, headers, payload=None):
    return _request(url, headers, method="POST", payload=payload)


def get(url, headers):
    return _request(url, headers, method="GET")


def dig(obj, key):
    """在可能被 data/result 包裹的响应里找字段，兼容信封结构。"""
    if isinstance(obj, dict):
        if key in obj and obj[key] is not None:
            return obj[key]
        for k in ("data", "result", "resp", "response"):
            if k in obj and isinstance(obj[k], dict):
                r = dig(obj[k], key)
                if r is not None:
                    return r
    return None


# ==================== WorkBuddy 签到 ====================
def wb_find_auth_file():
    override = os.environ.get("WORKBUDDY_AUTH_FILE")
    if override and os.path.exists(override):
        return override
    local = os.environ.get("LOCALAPPDATA") or os.path.join(
        os.path.expanduser("~"), "AppData", "Local")
    cand = os.path.join(local, WORKBUDDY_AUTH_BASENAME)
    return cand if os.path.exists(cand) else None


def wb_headers(session):
    auth = session.get("auth") or {}
    account = session.get("account") or {}
    token = auth.get("accessToken")
    uid = account.get("uid")
    if not token or not uid:
        raise RuntimeError("登录态文件缺少 accessToken / uid")
    headers = {
        "Accept": "application/json",
        "Authorization": "Bearer %s" % token,
        "Content-Type": "application/json",
        "X-User-Id": uid,
        "User-Agent": "WorkBuddy",
    }
    if account.get("enterpriseId"):
        headers["X-Enterprise-Id"] = account["enterpriseId"]
        headers["X-Tenant-Id"] = account["enterpriseId"]
    if auth.get("domain"):
        headers["X-Domain"] = auth["domain"]
    return headers


def wb_is_already(cbody):
    if cbody is None:
        return True
    if isinstance(cbody, dict):
        msg = cbody.get("msg") or ""
        if cbody.get("code") == 10001 or "已签" in msg:
            return True
    return False


def workbuddy_checkin():
    auth_file = wb_find_auth_file()
    if not auth_file:
        return {"app": "WorkBuddy", "result": "NO_SESSION",
                "report": "未找到 WorkBuddy 登录态文件，请先登录桌面端"}
    try:
        with open(auth_file, "r", encoding="utf-8") as f:
            session = json.load(f)
        headers = wb_headers(session)
    except Exception as e:
        return {"app": "WorkBuddy", "result": "ERROR",
                "report": "解析登录态失败: %s" % e}

    scode, sbody = post(WORKBUDDY_ENDPOINT + "/v2/billing/meter/checkin-activity-status", headers)
    if scode in (401, 403):
        return {"app": "WorkBuddy", "result": "NO_SESSION",
                "report": "登录态已失效（HTTP %s），请重新登录 WorkBuddy 桌面端" % scode}
    if scode < 0:
        return {"app": "WorkBuddy", "result": "ERROR",
                "report": "网络请求失败: %s" % sbody.get("error")}
    if not (200 <= scode < 300):
        return {"app": "WorkBuddy", "result": "ERROR",
                "report": "查询状态失败（HTTP %s）: %s" % (scode, json.dumps(sbody, ensure_ascii=False)[:200])}

    checked = dig(sbody, "today_checked_in")
    today_credit = dig(sbody, "today_credit") or dig(sbody, "daily_credit")
    streak = dig(sbody, "streak_days")
    if checked in (True, "true", 1, "1"):
        extra = []
        if today_credit is not None:
            extra.append("今日 +%s" % today_credit)
        if streak is not None:
            extra.append("连续 %s 天" % streak)
        return {"app": "WorkBuddy", "result": "ALREADY",
                "report": "今日已签到（%s）" % ("，".join(extra) if extra else "无需重复")}

    if CHECK_ONLY:
        return {"app": "WorkBuddy", "result": "NOT_YET", "report": "今日尚未签到（未领取）"}

    ccode, cbody = post(WORKBUDDY_ENDPOINT + "/v2/billing/meter/daily-checkin", headers)
    if 200 <= ccode < 300 and cbody is not None and not isinstance(cbody, dict):
        return {"app": "WorkBuddy", "result": "OK",
                "report": "领取成功 +%s 积分" % cbody}
    if 200 <= ccode < 300:
        credit = dig(cbody, "credit")
        if credit is not None:
            return {"app": "WorkBuddy", "result": "OK",
                    "report": "领取成功 +%s 积分" % credit}
    if wb_is_already(cbody):
        return {"app": "WorkBuddy", "result": "ALREADY",
                "report": "今日已签到，无需重复"}
    return {"app": "WorkBuddy", "result": "ERROR",
            "report": "领取失败（HTTP %s）: %s" % (ccode, json.dumps(cbody, ensure_ascii=False)[:200])}


# ==================== TraeWorkCN 签到 ====================
def trae_find_auth_files():
    files = []
    if os.path.exists(TRAE_AUTH_FILE):
        files.append(TRAE_AUTH_FILE)
    files += sorted(glob.glob(os.path.join(TRAE_AUTH_DIR, "trae-*.json")))
    return files


def trae_parse(raw):
    if not raw.strip():
        raise RuntimeError("凭证文件为空")
    data = json.loads(raw)
    auth = data.get("auth", data)          # 兼容嵌套/扁平
    account = data.get("account", {})
    token = auth.get("accessToken") or auth.get("AccessToken")
    if not token:
        raise RuntimeError("凭证缺少 accessToken")
    return {
        "accessToken": token,
        "refreshToken": auth.get("refreshToken") or auth.get("RefreshToken") or "",
        "expiresAt": auth.get("expiresAt") or auth.get("ExpiresAt") or 0,
        "deviceId": auth.get("deviceId") or data.get("deviceId") or "",
        "uid": account.get("uid") or auth.get("uid") or data.get("uid") or "",
        "nickname": account.get("nickname") or data.get("nickname") or "",
    }


def trae_refresh(auth):
    """用 refreshToken 调 ExchangeToken 轮换 accessToken，返回新字段 dict。"""
    if not auth.get("refreshToken"):
        return None
    body = {"ClientID": TRAE_CLIENT_ID,
            "RefreshToken": auth["refreshToken"],
            "ClientSecret": "-",
            "UserID": ""}
    headers = {"Content-Type": "application/json", "Accept": "application/json",
               "User-Agent": TRAE_UA}
    scode, sbody = post(TRAE_OAUTH_HOST + "/cloudide/api/v3/trae/oauth/ExchangeToken",
                        headers, body)
    if not (200 <= scode < 300):
        return None
    result = dig(sbody, "Result") or sbody
    new_token = result.get("Token")
    if not new_token:
        return None
    new_auth = dict(auth)
    new_auth["accessToken"] = new_token
    if result.get("RefreshToken"):
        new_auth["refreshToken"] = result["RefreshToken"]
    exp = result.get("TokenExpireAt") or 0
    if exp > 1e12:                       # 毫秒 -> 秒
        exp = int(exp / 1000)
    if exp > 0:
        new_auth["expiresAt"] = exp
    return new_auth


def trae_save_auth(filepath, auth):
    doc = {
        "auth": {
            "accessToken": auth["accessToken"],
            "refreshToken": auth.get("refreshToken", ""),
            "expiresAt": auth.get("expiresAt", 0),
            "domain": "trae.cn",
            "apiHost": TRAE_OAUTH_HOST,
            "deviceId": auth.get("deviceId", ""),
        },
        "account": {
            "uid": auth.get("uid", ""),
            "nickname": auth.get("nickname", ""),
        },
    }
    tmp = filepath + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
    os.replace(tmp, filepath)


def trae_headers(auth):
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": TRAE_UA,
        "Authorization": "Cloud-IDE-JWT " + auth["accessToken"],
        "X-User-Region": "CN",
    }
    if auth.get("deviceId"):
        headers["X-Device-Id"] = auth["deviceId"]
    return headers


def trae_checkin_one(auth, filepath):
    # token 临近过期（<2h）时自动刷新
    exp = auth.get("expiresAt") or 0
    if exp and time.time() + 7200 >= exp:
        new_auth = trae_refresh(auth)
        if new_auth:
            auth = new_auth
            try:
                trae_save_auth(filepath, auth)
            except Exception:
                pass
            log("TraeWorkCN: accessToken 已自动刷新")

    headers = trae_headers(auth)
    scode, sbody = post(TRAE_UG_HOST + "/trae/api/v2/ug/checkin_credits/status", headers, {})
    if scode in (401, 403):
        return {"app": "TraeWorkCN", "result": "NO_SESSION",
                "report": "登录态失效（HTTP %s），请重新生成凭证" % scode}
    if scode < 0:
        return {"app": "TraeWorkCN", "result": "ERROR",
                "report": "网络请求失败: %s" % sbody.get("error")}
    if not (200 <= scode < 300):
        return {"app": "TraeWorkCN", "result": "ERROR",
                "report": "查询状态失败（HTTP %s）: %s" % (scode, json.dumps(sbody, ensure_ascii=False)[:200])}

    checked_in = dig(sbody, "checked_in")
    credits = dig(sbody, "credits")
    enable = dig(sbody, "enable")
    who = auth.get("nickname") or auth.get("uid") or "default"

    if checked_in in (True, "true", 1, "1"):
        extra = ("当前 %s 积分" % credits) if credits is not None else ""
        return {"app": "TraeWorkCN", "result": "ALREADY",
                "report": "今日已签到（%s）%s" % (who, ("，" + extra) if extra else "")}

    if CHECK_ONLY:
        return {"app": "TraeWorkCN", "result": "NOT_YET",
                "report": "今日尚未签到（%s）" % who}

    if enable is False:
        return {"app": "TraeWorkCN", "result": "ERROR",
                "report": "签到活动未开启（%s）" % who}

    ccode, cbody = post(TRAE_UG_HOST + "/trae/api/v2/ug/checkin_credits/claim", headers, {})
    if isinstance(cbody, dict):
        biz_code = cbody.get("code")
        msg = cbody.get("message") or cbody.get("msg") or ""
        if biz_code in (0, "0", None) and "已签" not in msg:
            return {"app": "TraeWorkCN", "result": "OK",
                    "report": "领取成功（%s）" % who}
        if "已签" in msg or biz_code in (10001, 10002):
            return {"app": "TraeWorkCN", "result": "ALREADY",
                    "report": "今日已签到（%s）" % who}
        return {"app": "TraeWorkCN", "result": "ERROR",
                "report": "领取失败（HTTP %s, code=%s）: %s" % (ccode, biz_code, msg[:200])}
    if 200 <= ccode < 300:
        return {"app": "TraeWorkCN", "result": "OK",
                "report": "领取成功（%s）" % who}
    return {"app": "TraeWorkCN", "result": "ERROR",
            "report": "领取失败（HTTP %s）: %s" % (ccode, (json.dumps(cbody, ensure_ascii=False))[:200])}


def traework_checkin():
    files = trae_find_auth_files()
    if not files:
        return {"app": "TraeWorkCN", "result": "NO_AUTH",
                "report": "未找到凭证文件（auths/trae-*.json 或 trae_auth.json），请先按 README 配置"}
    results = []
    for fp in files:
        try:
            with open(fp, "r", encoding="utf-8") as f:
                auth = trae_parse(f.read())
            r = trae_checkin_one(auth, fp)
        except Exception as e:
            r = {"app": "TraeWorkCN", "result": "ERROR",
                 "report": "凭证解析失败（%s）: %s" % (os.path.basename(fp), e)}
        results.append(r)
    if len(results) == 1:
        return results[0]
    return {"app": "TraeWorkCN", "result": "MULTI",
            "report": "；".join("[%s] %s" % (r["result"], r["report"]) for r in results)}


# ==================== MiniMax Code 签到 ====================
def mm_find_config():
    override = os.environ.get("MINIMAX_CONFIG")
    if override and os.path.exists(override):
        return override
    appdata = os.environ.get("APPDATA") or os.path.join(
        os.path.expanduser("~"), "AppData", "Roaming")
    cand = os.path.join(appdata, MINIMAX_CONFIG_BASENAME)
    return cand if os.path.exists(cand) else None


def mm_jwt_exp(payload):
    """JWT payload(base64url) -> exp 时间戳(秒)。失败返回 0。"""
    try:
        if not payload:
            return 0
        pad = "=" * (-len(payload) % 4)
        body = json.loads(
            __import__("base64").urlsafe_b64decode(payload + pad).decode("utf-8", "replace"))
        return int(body.get("exp") or 0)
    except Exception:
        return 0


def mm_sign(unix_sec, body):
    return hashlib.md5(("%d%s%s" % (unix_sec, MINIMAX_SIGN_SALT, body)).encode()).hexdigest()


def mm_build_params(cfg, token):
    user = cfg.get("user") or {}
    ts_ms = int(time.time() * 1000)
    ts = ts_ms // 1000
    params = {
        "device_platform": "web",
        "biz_id": "3",
        "app_id": "3001",
        "version_code": "22201",
        "unix": str(ts_ms),
        "timezone_offset": "-480",
        "is_desktop": "1",
        "desktop_version": "3.0.67",
        "device_id": user.get("deviceID", "") or "0",
        "os_name": "Windows",
        "browser_name": "Chrome",
        "user_id": user.get("realUserID", "") or "0",
        "token": token,
        "client": "desktop",
        "timezone_id": "Asia/Shanghai",
    }
    return ts_ms, ts, params


def mm_request(cfg, token, path, method="GET", body=""):
    ts_ms, ts, params = mm_build_params(cfg, token)
    url = MINIMAX_HOST + path + "?" + urllib.parse.urlencode(params)
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "token": token,
        "x-timestamp": str(ts),
        "x-signature": mm_sign(ts, body),
        "Origin": "https://agent.minimaxi.com",
        "User-Agent": "Mozilla/5.0 MiniMaxCode/3.0.67",
    }
    data = body.encode("utf-8") if body else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
            raw = resp.read().decode("utf-8", "replace")
            try:
                return resp.status, json.loads(raw)
            except Exception:
                return resp.status, {"raw": raw[:500]}
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"raw": raw[:500]}
    except Exception as e:
        return -1, {"error": str(e)}


def mm_load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            st = json.load(f)
        return st.get("minimax") or {}
    except Exception:
        return {}


def mm_save_state(rec):
    try:
        st = {}
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                st = json.load(f)
        except Exception:
            pass
        st["minimax"] = rec
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False)
        os.replace(tmp, STATE_FILE)
    except Exception:
        pass


def minimax_checkin():
    cfg_path = mm_find_config()
    if not cfg_path:
        return {"app": "MiniMaxCode", "result": "NO_SESSION",
                "report": "未找到 MiniMax Code 登录态（%s），请先登录桌面端" % MINIMAX_CONFIG_BASENAME}
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        token = (cfg.get("tokens") or {}).get("accessToken")
        if not token:
            raise RuntimeError("登录态缺少 accessToken")
    except Exception as e:
        return {"app": "MiniMaxCode", "result": "ERROR",
                "report": "解析登录态失败: %s" % e}

    # token 有效性
    _, _, exp = token.split(".")
    exp_ts = mm_jwt_exp(exp)
    if exp_ts and time.time() >= exp_ts:
        return {"app": "MiniMaxCode", "result": "NO_SESSION",
                "report": "accessToken 已过期，请打开一次 MiniMax Code 让客户端续签后重试"}

    # 查询状态
    scode, sbody = mm_request(cfg, token, "/minimax-cloud/api/v1/signin/status")
    if scode in (401, 400):
        return {"app": "MiniMaxCode", "result": "NO_SESSION",
                "report": "登录态失效（HTTP %s: %s），请重新打开 MiniMax Code" % (
                    scode, json.dumps(sbody, ensure_ascii=False)[:120])}
    if scode < 0:
        return {"app": "MiniMaxCode", "result": "ERROR",
                "report": "网络请求失败: %s" % sbody.get("error")}
    if not isinstance(sbody, dict) or (sbody.get("base_resp") or {}).get("status_code") != 0:
        return {"app": "MiniMaxCode", "result": "ERROR",
                "report": "查询状态失败（HTTP %s）: %s" % (
                    scode, json.dumps(sbody, ensure_ascii=False)[:200])}

    days = ((sbody.get("data") or {}).get("days")) or []
    today = next((d for d in days if d.get("is_today")), None)
    today_points = (today or {}).get("points", 0)

    if CHECK_ONLY:
        state = mm_load_state()
        done = state.get("date") == time.strftime("%Y-%m-%d")
        return {"app": "MiniMaxCode", "result": "ALREADY" if done else "NOT_YET",
                "report": (("今日已签到（+%s 积分）" % today_points) if done
                           else "今日尚未签到（可领 +%s 积分）" % today_points)}

    # 本地 claim_id 去重（二次幂等）
    state = mm_load_state()
    today_s = time.strftime("%Y-%m-%d")
    if state.get("date") == today_s and state.get("claim_id"):
        return {"app": "MiniMaxCode", "result": "ALREADY",
                "report": "今日已领取（claim_id=%s，+%s 积分）" % (state["claim_id"], today_points)}

    ccode, cbody = mm_request(cfg, token, "/minimax-cloud/api/v1/signin/claim",
                              method="POST", body="{}")
    if not isinstance(cbody, dict):
        if 200 <= ccode < 300:
            return {"app": "MiniMaxCode", "result": "ALREADY",
                    "report": "领取成功，今日已累计（+%s 积分）" % today_points}
        return {"app": "MiniMaxCode", "result": "ERROR",
                "report": "领取失败（HTTP %s）: %s" % (ccode, (json.dumps(cbody, ensure_ascii=False))[:200])}

    if (cbody.get("base_resp") or {}).get("status_code") != 0:
        msg = ((cbody.get("base_resp") or {}).get("status_msg")) or json.dumps(cbody, ensure_ascii=False)[:200]
        return {"app": "MiniMaxCode", "result": "ALREADY" if "already" in str(msg) else "ERROR",
                "report": "领取返回(%s): %s" % (msg, "可能今日已签" if "already" in str(msg) else "")}

    data = cbody.get("data") or {}
    claim_id = data.get("claim_id") or ""
    points = data.get("points") or today_points
    result_name = {1: "OK", 2: "OK", 0: "EMPTY"}.get(data.get("claim_result"), "")
    mm_save_state({"date": today_s, "claim_id": claim_id})
    if claim_id:
        return {"app": "MiniMaxCode", "result": "OK",
                "report": "领取成功 +%s 积分（claim_id=%s）" % (points, claim_id)}
    return {"app": "MiniMaxCode", "result": "OK",
            "report": "领取成功 +%s 积分" % points}


# ==================== 主流程 ====================
def main():
    log("==== 每日自动签到开始 ====")
    outputs = []
    if DO_WORKBUDDY:
        outputs.append(workbuddy_checkin())
    if DO_TRAE:
        outputs.append(traework_checkin())
    if DO_MINIMAX:
        outputs.append(minimax_checkin())
    for r in outputs:
        log("[%s] %s: %s" % (r["app"], r["result"], r["report"]))
    log("==== 签到结束 ====")
    print("\n--- 汇总(JSON) ---")
    print(json.dumps(outputs, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
