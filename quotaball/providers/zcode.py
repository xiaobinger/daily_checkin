#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Zcode：解密自定义 enc:v1 凭证，查询 BigModel / Z.ai Coding Plan 窗口配额。

实测（S0）：
  - 密文 = enc:v1:<b64url(nonce 12B)>.<b64url(tag 16B)>.<b64url(ciphertext)>，aes-256-gcm
  - 密钥 = sha256(env.ZCODE_CREDENTIAL_SECRET
          || "zcode-credential-fallback:{platform}:{homedir}:{username}")
  - 接口 GET {host}/api/monitor/usage/quota/limit，头 Authorization: Bearer <token>
  - 双窗口：5 小时（unit=3）+ 周（unit=6），非每日
  - 响应字段：usage=配额总量、currentValue=已用、remaining=剩余、percentage=已用百分比
"""
import base64
import getpass
import hashlib
import json
import os
import sys
import time

from ..aesgcm import gcm_decrypt
from ..errors import NoSessionError, QuotaError
from .base import Provider, http_json, mask_account, mask_email

CRED_PATH = os.path.join(os.path.expanduser("~"), ".zcode", "v2", "credentials.json")
HOSTS = {"bigmodel": "https://open.bigmodel.cn", "zai": "https://api.z.ai"}
UNIT_LABEL = {3: "小时", 6: "周"}  # 实测：unit=3+number=5 → 5 小时窗口；unit=6+number=1 → 周窗口


def _b64u(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class ZcodeProvider(Provider):
    key = "zcode"
    label = "Zcode"

    def _secret(self):
        env = os.environ.get("ZCODE_CREDENTIAL_SECRET")
        if env:
            return env
        if sys.platform.startswith("win"):
            plat = "win32"
        elif sys.platform == "darwin":
            plat = "darwin"
        else:
            plat = "linux"
        return "zcode-credential-fallback:%s:%s:%s" % (
            plat, os.path.expanduser("~"), getpass.getuser())

    def _decrypt_all(self):
        if not os.path.isfile(CRED_PATH):
            raise NoSessionError("未找到 Zcode 凭证（~/.zcode/v2/credentials.json），请先登录 Zcode")
        try:
            raw = json.load(open(CRED_PATH, encoding="utf-8"))
        except Exception as e:
            raise NoSessionError("Zcode 凭证文件解析失败: %s" % e)
        key = hashlib.sha256(self._secret().encode("utf-8")).digest()
        out = {}
        for k, v in raw.items():
            if not (isinstance(v, str) and v.startswith("enc:v1:")):
                continue
            try:
                n, t, c = v[7:].split(".")
                pt, ok = gcm_decrypt(key, _b64u(n), _b64u(c), _b64u(t))
                if ok:
                    out[k] = pt.decode("utf-8", "replace")
            except Exception:
                continue
        if not out:
            raise NoSessionError(
                "Zcode 凭证解密失败（密钥不匹配：可能更换了系统用户，或设置了 ZCODE_CREDENTIAL_SECRET）")
        return out

    def fetch(self):
        started = time.time()
        cred = self._decrypt_all()
        active = cred.get("oauth:active_provider") or "bigmodel"
        token = cred.get("oauth:%s:access_token" % active) or cred.get("zcodejwttoken")
        if not token:
            raise NoSessionError("Zcode 凭证中未找到 access_token，请重新登录 Zcode")
        host = HOSTS.get(active, HOSTS["bigmodel"])

        code, body = http_json(
            host + "/api/monitor/usage/quota/limit",
            headers={"Authorization": "Bearer " + token})
        if code in (401, 403):
            raise NoSessionError("Zcode 凭证已失效（HTTP %s），请重新登录 Zcode" % code)
        if not (isinstance(body, dict) and body.get("code") == 200 and body.get("success") is True):
            raise QuotaError("Zcode 额度查询失败（HTTP %s）: %s"
                             % (code, json.dumps(body, ensure_ascii=False)[:160]))

        data = body.get("data") or {}
        windows = []
        for lim in data.get("limits") or []:
            unit = lim.get("unit")
            number = lim.get("number") or 0
            ul = UNIT_LABEL.get(unit, "unit%s" % unit)
            label = ("%d %s窗口" % (number, ul)) if number else ("%s窗口" % ul)
            total = lim.get("usage") or 0          # 注意：该接口 usage 字段实为配额总量
            used = lim.get("currentValue") or 0
            remaining = lim.get("remaining")
            if remaining is None:
                remaining = max(total - used, 0)
            reset_ms = lim.get("nextResetTime") or 0
            windows.append(self._window(
                label, used, total, remaining,
                reset_at=(reset_ms / 1000.0 if reset_ms else None),
                unit="tokens"))

        who = ""
        ui = cred.get("oauth:%s:user_info" % active)
        if ui:
            try:
                obj = json.loads(ui)
                who = (obj.get("username") or obj.get("email") or obj.get("nickname")
                       or obj.get("name") or "")
            except Exception:
                pass
        account = mask_email(who) if "@" in str(who) else mask_account(who)
        if not account:
            account = active

        return [self._record(
            account=account,
            checkin=None,  # Zcode 无签到概念
            quota=windows,
            absolute=None,
            plan=data.get("level"),
            started=started)]
