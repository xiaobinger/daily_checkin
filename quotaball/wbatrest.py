#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WorkBuddy 5.7+ 登录态信封解密。

逆向来源：D:\\WorkBuddy\\resources\\app.asar（5.7.6）

密钥体系：
    atRestSecretKey  —— 桌面端原生模块 electron_browser_workbuddy_storage
                        的 loggerGet() 返回，形如 base64(32 字节)，磁盘上无副本
    key   = sha256(atRestSecretKey 字符串的 utf-8 字节)
    keyId = sha256(key).hex[:16]                （= 信封里的 keyId）

信封（auth.accessToken 等字段）：
    {"$wbEncrypted":1,"envelope":"<base64 的 JSON>"}
    envelope = {suite:1, keyId, nonce(12B), authTag(16B), ciphertext}

sym-v1 的 AAD（app.asar 里的 Pe 函数）：
    "WB-AAD\\0" || 0x01 || u32str(framing标识) || u32str("sym-v1")
    || u32(suite) || u32str(keyId) || 0x01*framingId || sequence || finalFlag
    framing: file→"WBEF1"/1, field→"WBEV1"/2, record→"WBER1"/3, stream→"WBES1"/4

登录态文件里的字段用的是 field framing（无 sequence / final）。
"""
import base64
import hashlib
import json
import os
import struct

from .aesgcm import gcm_decrypt
from .dpapi import protect, unprotect

FRAMING = {"file": (b"WBEF1", 1), "field": (b"WBEV1", 2),
           "record": (b"WBER1", 3), "stream": (b"WBES1", 4)}
FIELD_FRAMING = "field"          # 登录态 JSON 里逐字段加密用的 framing


def _u32str(b):
    return struct.pack(">I", len(b)) + b


def sym_v1_aad(framing, suite, key_id_hex, sequence=None, final=None):
    """复刻 app.asar 的 Pe()（sym-v1 分支）。"""
    tag, fid = FRAMING[framing]
    parts = [b"WB-AAD\x00", bytes([1]), _u32str(tag), _u32str(b"sym-v1"),
             struct.pack(">I", suite), _u32str(key_id_hex.encode()), bytes([fid])]
    if sequence is None:
        parts.append(bytes([0]))
    else:
        parts.append(bytes([1]) + struct.pack(">Q", sequence))
    parts.append(bytes([0 if final is None else (2 if final else 1)]))
    return b"".join(parts)


def derive_key(at_rest_secret_key):
    """base64 字符串 -> (32 字节 key, keyId 十六进制)。"""
    key = hashlib.sha256(at_rest_secret_key.encode("utf-8")).digest()
    return key, hashlib.sha256(key).hexdigest()[:16]


def is_envelope(value):
    return (isinstance(value, dict) and value.get("$wbEncrypted") == 1
            and isinstance(value.get("envelope"), str))


def open_envelope(key, key_id, env, framing=FIELD_FRAMING):
    """解一个信封，明文 str。失败抛 ValueError。"""
    e = json.loads(base64.b64decode(env["envelope"]))
    if e.get("keyId") != key_id:
        raise ValueError("信封 keyId 不匹配: %s" % e.get("keyId"))
    aad = sym_v1_aad(framing, e.get("suite") or 1, key_id)
    pt, ok = gcm_decrypt(key,
                         base64.b64decode(e["nonce"]),
                         base64.b64decode(e["ciphertext"]),
                         base64.b64decode(e["authTag"]),
                         aad)
    if not ok:
        raise ValueError("信封认证校验失败（AAD 或密钥不对）")
    return pt.decode("utf-8", "replace")


# ---------------- 密钥缓存（DPAPI 保护，仅当前 Windows 用户可读）----------------

def cache_path():
    override = os.environ.get("WORKBUDDY_ATREST_KEY_FILE")
    if override:
        return override
    local = os.environ.get("LOCALAPPDATA") or os.path.join(
        os.path.expanduser("~"), "AppData", "Local")
    return os.path.join(local, "quotaball", "workbuddy-atrest.dpapi")


def save_secret(secret_b64):
    """把 atRestSecretKey 用 DPAPI 加密后落盘。"""
    raw = base64.b64decode(secret_b64)
    if len(raw) != 32:
        raise ValueError("atRestSecretKey 必须是 32 字节")
    blob = protect(secret_b64.encode("utf-8"), "quotaball WorkBuddy at-rest key")
    if blob is None:
        raise RuntimeError("DPAPI 保护失败")
    path = cache_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(blob)
    os.replace(tmp, path)
    try:                                   # NTFS 下其他用户/账号不可读
        os.chmod(path, 0o600)
    except Exception:
        pass
    return path


def load_secret():
    """读缓存的 atRestSecretKey；没有或解不开返回 None。"""
    path = cache_path()
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "rb") as f:
            blob = f.read()
    except Exception:
        return None
    try:
        raw = unprotect(blob)
    except Exception:
        raw = None
    if not raw:
        return None
    try:
        return raw.decode("utf-8")
    except Exception:
        return None


def clear_secret():
    path = cache_path()
    if os.path.isfile(path):
        try:
            os.remove(path)
            return True
        except Exception:
            return False
    return False


def open_auth_field(value, secret_b64=None):
    """解密登录态里的一个信封字段，返回明文 str。

    secret_b64 为空时自动读 DPAPI 缓存。
    """
    if not is_envelope(value):
        return None
    secret = secret_b64 or load_secret()
    if not secret:
        raise RuntimeError("缺少 WorkBuddy at-rest 密钥，请先执行 tools/wb_extract.js")
    key, key_id = derive_key(secret)
    return open_envelope(key, key_id, value)