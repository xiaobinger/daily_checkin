#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MiniMax Code 3.1 登录态、请求签名与公共参数。

逆向自 D:\\MiniMax\\MiniMax Code\\resources\\app.asar（3.1.1）

一、登录态
----------
3.1 起桌面端改用 OAuth 设备码流，凭证明文存在
    %USERPROFILE%\\.minimax\\auth\\<buildEnv>\\<region>\\mcode-public\\auth.json
        records[<service>\\0<account>] = {
            accessToken : "mmoat_..."   短期凭证，TTL 约 35~60 分钟
            refreshToken: "mmort_..."   长期凭证
            expiresAtMs, generation, loginEpoch, ...
        }
accessToken 过期用 refreshToken 换：
    POST https://account.minimax.cn/oauth2/token
    Content-Type: application/x-www-form-urlencoded
      grant_type=refresh_token & refresh_token=... & client_id=mcode-public
      & scope=agent.default & audience=agent-backend
（服务端同时下发新的 refresh_token，旧的不失效）

二、请求签名
------------
renderer 的 axios 拦截器（module 257708881）与 main process 的 cloud proxy
（module 285160007）用的是同一套：

    second     = floor(time_ms / 1000)          # time_ms = Date.parse(new Date().toString())
    signedPath = path + "?" + URLSearchParams(params).toString()
    yyBody     = POST ? JSON.stringify(body) : "{}"
    yy         = md5(encodeURIComponent(signedPath) + "_" + yyBody
                     + md5(String(time_ms)) + "ooui")
    x-signature= md5(String(second) + SIGNATURE_SALT + bodyText)
                   # bodyText = POST ? body : ""
    Authorization: Bearer <accessToken>

★ 关键细节：signedPath 里的 params 是 URLSearchParams 序列化的结果，它**包含**
  op_ticket=undefined 与 token=null（桌面端 c.token = null、c.op_ticket = undefined），
  而真正发到服务器的 URL 由 axios 序列化，会跳过 null/undefined 这两个键。
  两串不一样是正常的——服务端不比对二者，客户端自己也对不上。

★ 另一个细节：time_ms = Date.parse(new Date().toString()) 只到秒精度，
  所以 unix 参数与签名里的时间戳都是整秒 × 1000，不带毫秒。

三、公共参数
------------
公共参数由 module 65776 的 mC() 生成，顺序固定（下面 MM_PARAM_ORDER）。
设备指纹（uuid / device_id / user_id / 屏幕尺寸等）会变，
所以每次从桌面端最新 API 日志里抓一份真实的 query 回来，套用同样的顺序重建。
"""
import glob
import hashlib
import json
import os
import re
import string
import time
import urllib.parse

# ------------------------------------------------------------------ 常量
MM_OAUTH_DIR = os.path.join(".minimax", "auth")
MM_TOKEN_ENDPOINT = "https://account.minimax.cn/oauth2/token"
MM_CLIENT_ID = "mcode-public"
MM_SCOPE = "agent.default"
MM_AUDIENCE = "agent-backend"
MM_SIGNATURE_SALT = "I*7Cf%WZ#S&%1RlZJ&C2"
MM_YY_SUFFIX = "ooui"
MM_UA = ("MiniMaxAgent Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
         "(KHTML, like Gecko) MiniMax/3.1.1 Chrome/148.0.7778.280 Safari/537.36")

# API 主机：桌面端走 agent.minimax.cn（国内区），minimaxi.com 作为兜底
MM_HOSTS = ("https://agent.minimax.cn", "https://agent.minimaxi.com")

# 公共参数的固定顺序（复刻 module 65776 的 mC()）
MM_PARAM_ORDER = (
    "device_platform", "biz_id", "app_id", "version_code", "unix",
    "timezone_offset", "is_desktop", "desktop_version", "desktop_web_version",
    "sys_language", "lang", "uuid", "device_id", "os_name", "browser_name",
    "device_memory", "cpu_core_num", "browser_language", "browser_platform",
    "user_id", "op_ticket", "screen_width", "screen_height", "token", "client",
)

# 兜底默认值（日志缺失时用）
MM_PARAM_DEFAULTS = {
    "device_platform": "web",
    "biz_id": "3",
    "app_id": "3001",
    "version_code": "22201",
    "timezone_offset": "28800",          # -60 * getTimezoneOffset()，UTC+8
    "is_desktop": "1",
    "desktop_version": "3.1.1",
    "sys_language": "zh",
    "lang": "zh",
    "os_name": "Windows",
    "browser_name": "Chrome",
    "browser_language": "zh-CN",
    "browser_platform": "Win32",
    "op_ticket": "undefined",            # 桌面端 c.op_ticket = undefined
    "token": "null",                     # 桌面端 c.token = null（走 Bearer 头）
    "client": "desktop",
}

# 桌面端 API 日志里的一行请求，形如：
#   [ts] [info] [IPC Proxy] GET agent.minimax.cn/minimax-cloud/api/v1/signin/status?device_platform=...&client=desktop {"duration":...}
_LOG_REQ_RE = re.compile(
    r"\b(?P<host>[\w\-]+(?:\.[\w\-]+)+)/(?P<path>/minimax-cloud/api/v1/[\w/\-]+)"
    r"\?(?P<query>[^\s\"]+)")


def _home():
    return os.path.expanduser("~")


# ------------------------------------------------------------------ OAuth 凭证
def oauth_credential_path():
    """找 ~/.minimax/auth/<env>/<region>/mcode-public/auth.json。"""
    override = os.environ.get("MINIMAX_OAUTH_FILE")
    if override and os.path.exists(override):
        return override
    root = os.path.join(_home(), MM_OAUTH_DIR)
    for cur, _dirs, files in os.walk(root):
        if "auth.json" in files:
            return os.path.join(cur, "auth.json")
    return None


def load_oauth_record():
    """返回 (record, 凭证文件路径)；没有则 (None, None)。"""
    path = oauth_credential_path()
    if not path:
        return None, None
    try:
        with open(path, "r", encoding="utf-8") as f:
            doc = json.load(f)
    except Exception:
        return None, path
    for rec in (doc.get("records") or {}).values():
        if isinstance(rec, dict) and rec.get("accessToken"):
            return rec, path
    return None, path


def save_oauth_record(path, record):
    """把刷新后的凭证写回（原子替换）。仅在不比磁盘上更旧时写。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            doc = json.load(f)
        records = doc.get("records") or {}
        for key, cur in records.items():
            if not isinstance(cur, dict):
                continue
            if cur.get("accessToken") == record.get("prevAccessToken"):
                records[key] = {k: v for k, v in record.items()
                                if k != "prevAccessToken"}
                tmp = path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(doc, f, ensure_ascii=False, indent=2)
                os.replace(tmp, path)
                return True
    except Exception:
        pass
    return False


def refresh_access_token(record):
    """用 refreshToken 换新的 accessToken。失败抛 RuntimeError。"""
    import urllib.error
    import urllib.request
    form = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": record["refreshToken"],
        "client_id": MM_CLIENT_ID,
        "scope": MM_SCOPE,
        "audience": MM_AUDIENCE,
    }).encode("utf-8")
    req = urllib.request.Request(
        MM_TOKEN_ENDPOINT, data=form, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        raise RuntimeError("刷新失败（HTTP %s）: %s"
                           % (e.code, e.read().decode("utf-8", "replace")[:160]))
    except Exception as e:
        raise RuntimeError("刷新请求失败: %s" % e)
    if not body.get("access_token"):
        raise RuntimeError("刷新响应缺少 access_token: %s"
                           % json.dumps(body, ensure_ascii=False)[:160])
    return body


def access_token(max_skew_ms=5 * 60 * 1000):
    """拿一个可用的 accessToken；快过期就先用 refreshToken 换新的。

    返回 (token, 说明)；没有凭证返回 (None, 原因)。
    """
    rec, path = load_oauth_record()
    if rec:
        exp = int(rec.get("expiresAtMs") or 0)
        if exp and (exp - time.time() * 1000) > max_skew_ms:
            return rec["accessToken"], "oauth"
        try:
            fresh = refresh_access_token(rec)
        except Exception as e:
            if exp and exp > time.time() * 1000:
                return rec["accessToken"], "oauth(刷新失败，沿用未过期凭证: %s)" % e
            return None, "刷新失败: %s" % e
        new_rec = dict(rec)
        # 刷新响应用的是 snake_case（access_token / refresh_token / expires_in），
        # 落盘回本地格式才是 camelCase
        new_rec["accessToken"] = fresh["access_token"]
        new_rec["refreshToken"] = fresh.get("refresh_token") or rec["refreshToken"]
        try:
            new_rec["expiresAtMs"] = int(time.time() * 1000) + int(
                fresh.get("expires_in") or 3600) * 1000
        except Exception:
            pass
        new_rec["generation"] = int(rec.get("generation") or 0) + 1
        new_rec["prevAccessToken"] = rec.get("accessToken")
        if path:
            save_oauth_record(path, new_rec)
        return new_rec["accessToken"], "oauth(已刷新)"
    return None, "未找到 OAuth 凭证（%s）" % os.path.join(
        MM_OAUTH_DIR, "<env>/<region>/mcode-public/auth.json")


# ------------------------------------------------------------------ 序列化
_URLENC_SAFE = set(string.ascii_letters + string.digits + "*-._")


def _urlencode_component(s):
    """等价于 JS 的 URLSearchParams.toString() 里的单键编码。

    application/x-www-form-urlencoded：空格 -> '+'，
    仅保留 A-Za-z0-9 * - . _ ，其余一律 %XX（大写十六进制）。
    """
    out = []
    for b in str(s).encode("utf-8"):
        ch = chr(b)
        if ch == " ":
            out.append("+")
        elif ch in _URLENC_SAFE:
            out.append(ch)
        else:
            out.append("%%%02X" % b)
    return "".join(out)


def _js_encode_uri(s):
    """等价于 JS 的 encodeURIComponent。"""
    return urllib.parse.quote(str(s), safe="-_.!~*'()")


def _query_string(params):
    """URLSearchParams(record).toString() 的等价实现（保持插入顺序）。"""
    return "&".join("%s=%s" % (_urlencode_component(k), _urlencode_component(v))
                    for k, v in params.items())


# ------------------------------------------------------------------ 公共参数
def _appdata():
    return os.environ.get("APPDATA") or os.path.join(_home(), "AppData", "Roaming")


def _log_files():
    log_dir = os.path.join(_appdata(), "MiniMax", "logs")
    try:
        files = glob.glob(os.path.join(log_dir, "api-*.log"))
    except Exception:
        return []
    return sorted(files, key=lambda p: os.path.getmtime(p), reverse=True)


def _log_tail(path, limit=600000):
    """只读文件末尾 limit 字节（日志可能很大，避免全量读进内存）。"""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - limit))
            return f.read().decode("utf-8", "replace")
    except OSError:
        return ""


# 解析结果缓存：(文件, mtime, size) -> 参数
_LOG_CACHE = {"key": None, "params": None, "host": None}


def params_from_desktop_log():
    """从桌面端最新 API 日志里抄一份真实 query，解析成参数 dict。

    设备指纹（uuid / device_id / user_id / 屏幕尺寸等）会随安装/换机变化，
    硬编码容易失效，所以每次从日志取最新的。

    返回 (params, host)；没有则 (None, None)。
    """
    for path in _log_files()[:3]:
        try:
            st = os.stat(path)
        except OSError:
            continue
        key = (path, st.st_mtime, st.st_size)
        if _LOG_CACHE["key"] == key:
            return _LOG_CACHE["params"], _LOG_CACHE["host"]

        hits = list(_LOG_REQ_RE.finditer(_log_tail(path)))
        if not hits:
            continue
        # 优先 signin/* 的请求，其次任意带公共参数的请求
        hits.sort(key=lambda m: (0 if "/signin/" in m.group("path") else 1, -m.start()))
        m = hits[0]
        params = {}
        for k, v in urllib.parse.parse_qsl(m.group("query"), keep_blank_values=True):
            params.setdefault(k, v)
        if "device_platform" not in params:
            continue
        host = "https://" + m.group("host")
        _LOG_CACHE.update({"key": key, "params": params, "host": host})
        return params, host
    return None, None


def build_params(now_ms=None, path_hint=None):
    """构造本次请求的公共参数（有序 dict）。

    参数来源优先级：
      1. 桌面端日志里的真实 query（设备指纹最准）
      2. 硬编码默认值
    并按 module 65776 的固定顺序重排，op_ticket / token 固定为 undefined / null。
    """
    now_ms = int(now_ms if now_ms is not None else time.time()) * 1000
    logged, _host = params_from_desktop_log()

    values = dict(MM_PARAM_DEFAULTS)
    if logged:
        for k, v in logged.items():
            if k in ("unix", "op_ticket", "token", "client"):
                continue
            values[k] = v
    values["unix"] = str(now_ms)
    values["op_ticket"] = "undefined"
    values["token"] = "null"
    values["client"] = "desktop"

    ordered = {}
    for key in MM_PARAM_ORDER:
        if key in values:
            ordered[key] = values[key]
    # 日志里若出现顺序表以外的参数，追加在后面（保持与客户端一致的概率最大）
    for key, val in values.items():
        if key not in ordered:
            ordered[key] = val
    return ordered


# ------------------------------------------------------------------ 签名
def body_text(body):
    """请求体的字符串形式。None/空 -> ""（GET 没有 body）。"""
    if body is None:
        return ""
    if isinstance(body, bytes):
        return body.decode("utf-8", "replace")
    if isinstance(body, str):
        return body
    return json.dumps(body, separators=(",", ":"), ensure_ascii=False)


def sign_headers(signed_path, now_ms, method="GET", body=None):
    """生成 3.1 的 yy / x-timestamp / x-signature 头。"""
    second = now_ms // 1000
    text = body_text(body)
    yy_body = text if (method or "GET").lower() == "post" else "{}"
    yy = hashlib.md5(("%s_%s%s%s" % (_js_encode_uri(signed_path), yy_body,
                                     hashlib.md5(str(now_ms).encode()).hexdigest(),
                                     MM_YY_SUFFIX)).encode("utf-8")).hexdigest()
    sig = hashlib.md5(("%d%s%s" % (second, MM_SIGNATURE_SALT, text)).encode()).hexdigest()
    return {"yy": yy, "x-timestamp": str(second), "x-signature": sig}


def build_request(path, method="GET", body=None, token=None, host=None):
    """构造一个完整请求。

    返回 (url, headers, data_bytes)。
    """
    now_ms = int(time.time()) * 1000
    params = build_params(now_ms)

    # 签名串用 URLSearchParams 序列化（含 op_ticket=undefined / token=null）
    signed_path = "%s?%s" % (path, _query_string(params))

    # 真正发出去的 query：axios 序列化会跳过 null/undefined 这两个键
    sent = {k: v for k, v in params.items()
            if v is not None and v != "null" and v != "undefined"}
    url = "%s%s?%s" % (host or MM_HOSTS[0], path, _query_string(sent))

    headers = sign_headers(signed_path, now_ms, method, body)
    headers.update({
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
        "Origin": host or MM_HOSTS[0],
        "Referer": (host or MM_HOSTS[0]) + "/",
        "User-Agent": MM_UA,
    })
    if token:
        headers["Authorization"] = "Bearer %s" % token

    data = None
    text = body_text(body)
    if text:
        data = text.encode("utf-8")
    return url, headers, data
