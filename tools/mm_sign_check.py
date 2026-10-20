#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MiniMax Code 3.1 请求签名的回归自检。

3.1 的 yy / x-signature 是从 app.asar 逆向出来的，公式很容易被"顺手简化"改坏
（比如漏掉签名串里的 op_ticket=undefined / token=null，或者把时间戳写成带毫秒的）。
这个脚本用一组固定的占位参数校验 quotaball/mmauth.py 的实现。

期望值由独立的 Node 实现算出（URLSearchParams 语义 + 同一套公式），
该 Node 实现已用桌面端日志里明文的 yy 校验过（2026-10-20 的真实请求）。
占位标识不包含任何真实设备/用户信息。

用法：
  python tools\\mm_sign_check.py
退出码 0 = 通过，1 = 失败。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from quotaball import mmauth  # noqa: E402

TIME_MS = 1792464566000
PATH = "/minimax-cloud/api/v1/signin/status"

# 公共参数（顺序即客户端 mC() 的顺序，op_ticket / token 固定 undefined / null）
PARAMS = [
    ("device_platform", "web"),
    ("biz_id", "3"),
    ("app_id", "3001"),
    ("version_code", "22201"),
    ("unix", str(TIME_MS)),
    ("timezone_offset", "28800"),
    ("is_desktop", "1"),
    ("desktop_version", "3.1.1"),
    ("sys_language", "zh"),
    ("lang", "zh"),
    ("uuid", "00000000-0000-0000-0000-000000000000"),
    ("device_id", "1"),
    ("os_name", "Windows"),
    ("browser_name", "Chrome"),
    ("device_memory", "32"),
    ("cpu_core_num", "12"),
    ("browser_language", "zh-CN"),
    ("browser_platform", "Win32"),
    ("user_id", "1"),
    ("op_ticket", "undefined"),
    ("screen_width", "1920"),
    ("screen_height", "1080"),
    ("token", "null"),
    ("client", "desktop"),
]

EXPECT_SIGNED_PATH = (
    "/minimax-cloud/api/v1/signin/status?device_platform=web&biz_id=3&app_id=3001"
    "&version_code=22201&unix=1792464566000&timezone_offset=28800&is_desktop=1"
    "&desktop_version=3.1.1&sys_language=zh&lang=zh"
    "&uuid=00000000-0000-0000-0000-000000000000&device_id=1&os_name=Windows"
    "&browser_name=Chrome&device_memory=32&cpu_core_num=12&browser_language=zh-CN"
    "&browser_platform=Win32&user_id=1&op_ticket=undefined&screen_width=1920"
    "&screen_height=1080&token=null&client=desktop")
EXPECT_YY = "a2a3f55076ad486079c292d2a0b6b221"
EXPECT_SIG = "bb5434b5653203db589b9d5caee638f4"
EXPECT_TS = "1792464566"


def main():
    params = dict(PARAMS)
    signed_path = "%s?%s" % (PATH, mmauth._query_string(params))
    headers = mmauth.sign_headers(signed_path, TIME_MS, "GET", None)

    checks = [
        ("URLSearchParams 等价序列化", signed_path, EXPECT_SIGNED_PATH),
        ("yy", headers["yy"], EXPECT_YY),
        ("x-signature", headers["x-signature"], EXPECT_SIG),
        ("x-timestamp", headers["x-timestamp"], EXPECT_TS),
    ]
    ok = True
    for name, got, want in checks:
        good = got == want
        ok = ok and good
        print("%-24s %s" % (name, "PASS" if good else "FAIL"))
        if not good:
            print("    got : %s" % got)
            print("    want: %s" % want)

    # POST 的 yy/x-signature 要带上请求体
    post = mmauth.sign_headers(signed_path, TIME_MS, "POST", "{}")
    print("%-24s %s" % ("POST body 进入签名", "PASS" if post["yy"] else "FAIL"))
    ok = ok and bool(post["yy"])

    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
