#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一次性提取 WorkBuddy at-rest 密钥并用 DPAPI 缓存。

用法：先退出 WorkBuddy 桌面端，然后
    node tools/wb_extract.js                 :: 打印密钥载荷
    python tools/wb_cache_key.py "<atRestSecretKey>"

缓存后（%LOCALAPPDATA%\\quotaball\\workbuddy-atrest.dpapi）每天自动解密，无需重复提取。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from quotaball import wbatrest  # noqa: E402


def main():
    secret = sys.argv[1] if len(sys.argv) > 1 else wbatrest.load_secret()
    if not secret:
        print("用法: python tools/wb_cache_key.py <atRestSecretKey>")
        return 1
    key, key_id = wbatrest.derive_key(secret)
    print("derived keyId:", key_id)
    path = wbatrest.save_secret(secret)
    print("已缓存 ->", path)
    print("校验:", "OK" if wbatrest.load_secret() == secret else "FAIL")
    return 0


if __name__ == "__main__":
    sys.exit(main())