#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""读取 checkin_config.json，将计划任务的三个可配置项（执行时间 / 启动程序路径 / 脚本参数）同步到系统计划任务。

用法:
  python sync_task.py            同步（配置与任务不一致时才更新）
  python sync_task.py --check    仅检查，不修改（返回码: 0=一致 2=有差异 1=错误）

行为:
  - 任务不存在      -> 按配置创建
  - 任务已存在但配置不一致 -> 以 /F 覆盖重建（时间 + 命令）
  - 配置一致        -> 不改动，零副作用
"""
import json
import os
import subprocess
import sys
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(SCRIPT_DIR, "checkin_config.json")
RUNNER = os.path.join(SCRIPT_DIR, "run_checkin.py")  # 计划任务实际会先跑统一入口做同步


def log(msg):
    print("[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))


def load_config():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = json.load(f)
    task_name = str(cfg.get("task_name") or "Marvis_DailyCheckin").strip()
    schedule_time = str(cfg.get("schedule_time") or "09:00").strip()
    python_path = str(cfg.get("python_path") or "").strip()
    script_path = str(cfg.get("script_path") or "").strip()
    script_args = str(cfg.get("script_args") or "").strip()
    if not python_path or not script_path:
        raise ValueError("config 缺少 python_path 或 script_path")
    if not os.path.isfile(python_path):
        raise ValueError("python_path 不存在: %s" % python_path)
    if not os.path.isfile(RUNNER):
        raise ValueError("run_checkin.py 不存在: %s" % RUNNER)
    return task_name, schedule_time, python_path, script_path, script_args


def norm_command(s):
    """规范化命令字符串用于比较（统一斜杠、去引号、压缩空白）。"""
    return " ".join(s.replace("\\", "/").replace('"', "").split())


def norm_time(s):
    """'09:00:00' -> '09:00'，'10:30' -> '10:30'。"""
    s = s.strip()
    if ":" not in s:
        return s
    parts = s.split(":")
    try:
        return "%02d:%02d" % (int(parts[0]), int(parts[1]))
    except ValueError:
        return s


def build_cmd(python_path, script_args):
    tr = '"%s" "%s"' % (python_path, RUNNER)
    if script_args:
        tr += " " + script_args
    return tr


def query_task(task_name):
    """返回 (returncode, stdout_text, stderr_text)。"""
    try:
        r = subprocess.run(
            ["schtasks", "/Query", "/TN", task_name, "/FO", "LIST", "/V"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=25,
        )
        return r.returncode, r.stdout, r.stderr
    except Exception as e:
        return -1, "", str(e)


def parse_task(stdout):
    """从 schtasks /V 输出中提取 开始时间 与 任务要运行的命令。"""
    start_time, run_cmd = None, None
    keys = [
        ("开始时间", "任务要运行的命令"),
        ("Start Time", "Task To Run"),
    ]
    for start_key, run_key in keys:
        for line in stdout.splitlines():
            line = line.strip()
            if start_time is None and line.startswith(start_key + ":"):
                start_time = line.split(":", 1)[1].strip()
            if run_cmd is None and line.startswith(run_key + ":"):
                run_cmd = line.split(":", 1)[1].strip()
        if start_time is not None and run_cmd is not None:
            break
    return start_time, run_cmd


def apply(python_path, script_args, schedule_time, task_name):
    tr = build_cmd(python_path, script_args)
    st = norm_time(schedule_time)
    cmd = ["schtasks", "/Create", "/TN", task_name, "/TR", tr,
           "/SC", "DAILY", "/ST", st, "/F"]
    log("执行更新: %s" % " ".join(cmd))
    r = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=30)
    out = (r.stdout or "").strip() + " " + (r.stderr or "").strip()
    if r.returncode != 0:
        log("更新失败: %s" % out.strip())
        return False
    log("已更新计划任务 %s (每天 %s)" % (task_name, st))
    return True


def main():
    check_only = "--check" in sys.argv
    try:
        task_name, schedule_time, python_path, script_path, script_args = load_config()
    except Exception as e:
        log("配置读取失败: %s" % e)
        return 1

    ret, stdout, stderr = query_task(task_name)
    expected_tr = build_cmd(python_path, script_args)
    expected_st = norm_time(schedule_time)

    if ret != 0:
        log("计划任务 %s 不存在，需要按配置创建。" % task_name)
        if check_only:
            log("[检查] 目标: %s / %s" % (expected_st, expected_tr))
            return 2
        return 0 if apply(python_path, script_args, schedule_time, task_name) else 1

    start_time, run_cmd = parse_task(stdout or "")
    if run_cmd is None:
        log("未能从查询结果解析任务命令，跳过（避免误改）。")
        return 1

    time_diff = norm_time(start_time or "") != expected_st
    cmd_diff = norm_command(run_cmd) != norm_command(expected_tr)

    if not time_diff and not cmd_diff:
        log("计划任务 %s 与配置一致，无需修改 (每天 %s)。" % (task_name, expected_st))
        return 0

    log("检测到配置差异 (%s%s)，开始同步..." % (
        "时间" if time_diff else "", ("和命令" if time_diff and cmd_diff else ("命令" if cmd_diff else ""))))
    log("  当前: 每天 %s | %s" % (start_time, run_cmd))
    log("  期望: 每天 %s | %s" % (expected_st, expected_tr))
    if check_only:
        return 2
    return 0 if apply(python_path, script_args, schedule_time, task_name) else 1


if __name__ == "__main__":
    sys.exit(main())
