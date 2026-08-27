#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""统一入口（由计划任务调用）：

  1) 先按 checkin_config.json 同步计划任务配置（时间 / 启动程序 / 脚本参数，有变更才更新）
  2) 再按 config 中的 script_args 执行 daily_checkin.py 签到

因此：每次运行都会自动让计划任务与最新配置对齐，无需手动改计划任务。
也可手动执行：python run_checkin.py
"""
import json
import os
import shlex
import subprocess
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(SCRIPT_DIR, "checkin_config.json")
SYNC = os.path.join(SCRIPT_DIR, "sync_task.py")


def read_args():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = json.load(f)
    script_path = str(cfg.get("script_path") or "").strip()
    script_args = str(cfg.get("script_args") or "").strip()
    return script_path, script_args


def main():
    # 1) 同步计划任务配置（有差异才写入，零副作用）
    log_path = os.path.join(SCRIPT_DIR, "checkin.log")
    try:
        with open(log_path, "a", encoding="utf-8") as lf:
            p = subprocess.run([sys.executable, SYNC],
                               cwd=SCRIPT_DIR, capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=40)
            if p.stdout.strip():
                lf.write("\n".join("SYNC| " + ln for ln in p.stdout.splitlines() if ln.strip()) + "\n")
    except Exception as e:
        subprocess.run([sys.executable, SYNC], cwd=SCRIPT_DIR)

    # 2) 读取签到脚本与参数并执行
    script_path, script_args = read_args()
    if not script_path or not os.path.isfile(script_path):
        print("ERROR: 未找到签到脚本: %s" % script_path)
        return 1
    cmd = [sys.executable, script_path]
    if script_args:
        cmd += shlex.split(script_args)
    return subprocess.call(cmd, cwd=SCRIPT_DIR)


if __name__ == "__main__":
    sys.exit(main())
