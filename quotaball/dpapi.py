#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Windows DPAPI 加解密封装（CryptProtectData / CryptUnprotectData）。

用于读取 Chromium 系客户端（Qoder CN 等）用 os_crypt.encrypted_key 保护的 AES 密钥，
以及保存本项目自己需要长期保管的小块密钥（如 WorkBuddy at-rest 密钥缓存）。
仅 Windows 可用，其他平台调用会抛 RuntimeError。
"""
import ctypes
import sys


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint), ("pbData", ctypes.c_void_p)]


def _local_free(ptr):
    """LocalFree 需要明确的指针类型，c_void_p 字段被读出来是 int，直接传会溢出。"""
    if ptr:
        ctypes.windll.kernel32.LocalFree(ctypes.c_void_p(ptr))


def unprotect(data):
    """解密 DPAPI 保护的字节串，失败返回 None。"""
    if not sys.platform.startswith("win"):
        raise RuntimeError("DPAPI 仅支持 Windows")
    buf = ctypes.create_string_buffer(bytes(data), len(data))
    inp = _DATA_BLOB(len(data), ctypes.cast(buf, ctypes.c_void_p))
    out = _DATA_BLOB()
    ok = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(inp), None, None, None, None, 0, ctypes.byref(out))
    if not ok:
        return None
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        _local_free(out.pbData)


def protect(data, description=None):
    """用 DPAPI 加密字节串（仅当前 Windows 用户可解密），失败返回 None。"""
    if not sys.platform.startswith("win"):
        raise RuntimeError("DPAPI 仅支持 Windows")
    raw = bytes(data)
    buf = ctypes.create_string_buffer(raw, len(raw))
    inp = _DATA_BLOB(len(raw), ctypes.cast(buf, ctypes.c_void_p))
    out = _DATA_BLOB()
    desc = ctypes.c_wchar_p(description) if description else None
    ok = ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(inp), desc, None, None, None, 0, ctypes.byref(out))
    if not ok:
        return None
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        _local_free(out.pbData)
