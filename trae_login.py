#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TraeWorkCN (TRAE SOLO CN) 登录凭证获取脚本（纯 Python，无需 Go 环境）
====================================================================
原理：复刻 traework2api 的 login.sh 登录流程——
  1. 生成 machine_id / device_id
  2. 构造 trae.cn 授权登录链接并自动打开浏览器
  3. 本地起回调服务监听 127.0.0.1:18080，自动捕获登录回调（无需手动复制链接）
  4. 回调中提取 refreshToken → 调 ExchangeToken 换取 accessToken
  5. GetUserInfo 获取账号信息 → 落盘 auths/trae-{uid}.json（与签到脚本格式一致）
  6. 立即执行一次签到验证凭证可用

用法：
  python trae_login.py

如果 18080 端口被占用（自动捕获失败），脚本会降级为「手动粘贴回调链接」模式：
  登录成功后浏览器会跳到打不开的 127.0.0.1:18080/authorize 地址，
  复制地址栏完整链接粘贴到终端即可。

依赖：Python 3.6+，仅标准库（http.server / urllib / json / secrets）。
"""
import json
import os
import secrets
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

# ==================== 常量 ====================
CLIENT_ID = "en1oxy7wnw8j9n"              # SOLO stable
APP_VERSION = "0.1.43"
PLUGIN_VERSION = "2.3.62834"
API_HOST = "https://api.trae.com.cn"      # ExchangeToken / GetUserInfo host
UG_HOST = "https://api.trae.cn"           # 签到 host
CALLBACK_HOST = "127.0.0.1"
CALLBACK_PORT = 18080
CALLBACK_PATH = "/authorize"

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
AUTH_DIR = os.path.join(SCRIPT_DIR, "auths")

UA = "Trae/" + APP_VERSION

# ==================== 回调捕获 ====================
_callback_captured = {}   # 线程间传递回调 URL


class CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith(CALLBACK_PATH):
            _callback_captured["url"] = "http://%s:%d%s" % (CALLBACK_HOST, CALLBACK_PORT, self.path)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(
                "<html><head><meta charset='utf-8'></head>"
                "<body><h3>登录成功，可以关闭此页面回到终端</h3></body></html>"
                .encode("utf-8"))
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args):
        pass


def wait_callback(timeout=300):
    """起本地回调服务等待登录回调，返回完整回调 URL；超时返回 None。"""
    server = HTTPServer((CALLBACK_HOST, CALLBACK_PORT), CallbackHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    print("[*] 已启动回调服务 http://%s:%d%s，等待浏览器跳转..." % (CALLBACK_HOST, CALLBACK_PORT, CALLBACK_PATH))
    deadline = time.time() + timeout
    while time.time() < deadline:
        if "url" in _callback_captured:
            server.shutdown()
            return _callback_captured["url"]
        time.sleep(0.5)
    server.shutdown()
    return None


# ==================== HTTP 工具 ====================
def http_post_json(url, body, headers, timeout=60, retries=2):
    data = json.dumps(body).encode("utf-8")
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, method="POST", data=data)
            for k, v in headers.items():
                req.add_header(k, v)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
                return json.loads(raw) if raw.strip() else {}
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            raise RuntimeError("HTTP %s: %s" % (e.code, raw[:400]))
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            if attempt < retries:
                time.sleep(1)
                continue
            raise RuntimeError("请求失败: %s" % e)


# ==================== 登录流程 ====================
def build_login_url():
    machine_id = secrets.token_hex(16)
    device_id = secrets.token_hex(16)
    params = {
        "login_version": "1",
        "auth_from": "solo",
        "login_channel": "native_ide",
        "plugin_version": PLUGIN_VERSION,
        "auth_type": "local",
        "client_id": CLIENT_ID,
        "redirect": "0",
        "login_trace_id": secrets.token_hex(8),
        "auth_callback_url": "http://%s:%d%s" % (CALLBACK_HOST, CALLBACK_PORT, CALLBACK_PATH),
        "machine_id": machine_id,
        "device_id": device_id,
        "x_device_id": device_id,
        "x_machine_id": machine_id,
        "x_device_brand": "PC",
        "x_device_type": "PC",
        "x_os_version": "1.0",
        "x_app_version": APP_VERSION,
        "x_app_type": "stable",
    }
    return "https://www.trae.cn/authorization?" + urllib.parse.urlencode(params), machine_id, device_id


def parse_json_param(raw):
    if not raw:
        return None
    for val in (raw, urllib.parse.unquote(raw)):
        try:
            obj = json.loads(val)
            if isinstance(obj, dict):
                return obj
        except Exception:
            continue
    return None


def parse_callback(callback_url):
    qs = urllib.parse.parse_qs(urllib.parse.urlparse(callback_url).query)
    refresh_token = (qs.get("refreshToken") or [""])[0]
    user_info = parse_json_param((qs.get("userInfo") or [""])[0]) or {}
    user_jwt = parse_json_param((qs.get("userJwt") or [""])[0]) or {}
    uid = str(user_info.get("UserID") or "")
    nickname = str(user_info.get("ScreenName") or "")
    ent_id = str(user_info.get("TenantID") or "")
    jwt_token = str(user_jwt.get("Token") or "")
    jwt_refresh = str(user_jwt.get("RefreshToken") or "")
    if not refresh_token:
        refresh_token = jwt_refresh
    return {
        "refresh_token": refresh_token,
        "jwt_token": jwt_token,
        "uid": uid,
        "nickname": nickname,
        "enterprise_id": ent_id,
    }


def exchange_token(refresh_token):
    body = {"ClientID": CLIENT_ID, "RefreshToken": refresh_token, "ClientSecret": "-", "UserID": ""}
    resp = http_post_json(API_HOST + "/cloudide/api/v3/trae/oauth/ExchangeToken", body,
                          {"Content-Type": "application/json", "User-Agent": UA})
    result = resp.get("Result") or {}
    token = result.get("Token") or ""
    if not token:
        raise RuntimeError("ExchangeToken 失败: " + json.dumps(resp, ensure_ascii=False)[:300])
    new_refresh = result.get("RefreshToken") or refresh_token
    expires_at = int(result.get("TokenExpireAt") or 0)
    if expires_at > 10 ** 12:          # 毫秒 -> 秒
        expires_at //= 1000
    if expires_at <= time.time():
        expires_at = int(time.time()) + int(result.get("TokenExpireDuration") or 1209600)
    return token, new_refresh, expires_at


def get_user_info(token):
    try:
        resp = http_post_json(API_HOST + "/cloudide/api/v3/trae/GetUserInfo",
                              {"ReqSource": "IDE", "IDEVersion": APP_VERSION},
                              {"Content-Type": "application/json", "x-cloudide-token": token,
                               "User-Agent": UA})
        u = resp.get("Result") or resp
        return {"uid": str(u.get("UserID") or ""), "nickname": str(u.get("ScreenName") or ""),
                "enterprise_id": str(u.get("EnterpriseID") or "")}
    except Exception as e:
        print("[*] GetUserInfo 失败（将使用回调 userInfo）: %s" % e)
        return {}


def save_auth(auth):
    os.makedirs(AUTH_DIR, exist_ok=True)
    filepath = os.path.join(AUTH_DIR, "trae-%s.json" % auth["uid"])
    doc = {
        "account": {"uid": auth["uid"], "enterpriseId": auth["enterprise_id"], "nickname": auth["nickname"]},
        "auth": {
            "accessToken": auth["access_token"],
            "refreshToken": auth["refresh_token"],
            "expiresAt": auth["expires_at"],
            "domain": "trae.cn",
            "apiHost": API_HOST,
            "machineId": auth["machine_id"],
            "deviceId": auth["device_id"],
        },
    }
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=1, ensure_ascii=False)
    return filepath


def verify_checkin(auth):
    """用刚获取的凭证立即签到验证（幂等）。"""
    headers = {
        "Content-Type": "application/json",
        "Authorization": "Cloud-IDE-JWT " + auth["access_token"],
        "X-User-Region": "CN",
        "X-Device-Id": auth["device_id"],
        "User-Agent": UA,
    }

    def post(path):
        req = urllib.request.Request(UG_HOST + path, method="POST", data=b"{}", headers=headers)
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode("utf-8", "replace") or "{}")

    try:
        st = post("/trae/api/v2/ug/checkin_credits/status")
        print("[*] 签到状态: checked_in=%s credits=%s enable=%s" % (
            st.get("checked_in"), st.get("credits"), st.get("enable")))
        if not st.get("checked_in") and st.get("enable"):
            r = post("/trae/api/v2/ug/checkin_credits/claim")
            print("[*] 签到结果: %s" % (r.get("message") or json.dumps(r, ensure_ascii=False)[:200]))
        else:
            print("[*] 今日已签到，跳过领取")
    except Exception as e:
        print("[!] 签到验证失败: %s" % e)

    try:
        ent = post("/trae/api/v2/pay/ide_user_ent_usage")
        packs = ent.get("user_entitlement_pack_list") or []
        total = sum(p.get("entitlement_base_info", {}).get("quota", {}).get("credits_limit", 0) for p in packs)
        print("[*] 当前 Work 积分: %s" % total)
    except Exception as e:
        print("[!] 查积分失败: %s" % e)


# ==================== 主流程 ====================
def main():
    print("=" * 60)
    print("  TRAE SOLO 登录（纯 Python 版）")
    print("=" * 60)

    login_url, machine_id, device_id = build_login_url()

    print("\n请在弹出的浏览器中完成登录（手机号/验证码）")
    print("若浏览器未自动打开，请手动访问：\n")
    print("  %s\n" % login_url)

    try:
        webbrowser.open(login_url)
    except Exception:
        pass

    callback_url = None
    try:
        callback_url = wait_callback(timeout=300)
        if callback_url:
            print("[*] 已自动捕获登录回调")
    except OSError as e:
        print("[!] 回调服务启动失败（%s），转为手动粘贴模式" % e)

    if not callback_url:
        print("\n登录成功后浏览器会跳到打不开的 127.0.0.1 地址，")
        print("请复制浏览器地址栏的完整链接，粘贴到下面回车：")
        callback_url = input("> ").strip()
        if not callback_url:
            print("未输入，已取消")
            sys.exit(1)

    cb = parse_callback(callback_url)
    refresh_token = cb["refresh_token"]
    token, new_refresh, expires_at = "", refresh_token, 0

    if refresh_token:
        print("[*] 正在调用 ExchangeToken 换取 accessToken...")
        token, new_refresh, expires_at = exchange_token(refresh_token)
        print("[*] ExchangeToken 成功: Token %s...（已隐藏）" % token[:20])
    else:
        token = cb["jwt_token"]
        if not token:
            print("[!] 回调链接缺少 refreshToken 且 userJwt 无 Token，请确认登录成功且链接完整")
            sys.exit(1)
        print("[*] 无 refreshToken，使用 userJwt Token 兜底")

    info = get_user_info(token)
    uid = info.get("uid") or cb["uid"]
    nickname = info.get("nickname") or cb["nickname"]
    ent_id = info.get("enterprise_id") or cb["enterprise_id"]
    if not uid:
        print("[!] 未能获取 uid，请检查 token 是否有效")
        sys.exit(1)

    auth = {
        "uid": uid, "nickname": nickname, "enterprise_id": ent_id,
        "access_token": token, "refresh_token": new_refresh, "expires_at": expires_at,
        "api_host": API_HOST, "machine_id": machine_id, "device_id": device_id,
    }
    filepath = save_auth(auth)
    print("[*] 凭证已保存: %s" % filepath)

    print("\n--- 签到验证 ---")
    verify_checkin(auth)

    print("\n登录完成！")
    print("  UID: %s" % uid)
    print("  Nickname: %s" % (nickname or "（未获取到）"))
    print("  Token: %s...（已隐藏）" % token[:20])
    print("  有效期至: %s" % time.strftime("%Y-%m-%d %H:%M", time.localtime(expires_at)))
    print("\n现在可以直接运行 python daily_checkin.py 自动签到（每日 09:00 计划任务已注册）")


if __name__ == "__main__":
    main()
