#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""命令行入口：python -m quotaball.cli [--force] [--json]

  --force   跳过 10 分钟节流，强制实时采集
  --json    末尾追加完整 JSON 快照
"""
import json
import sys
import time

from .collector import collect
from .providers.base import state_text, summarize


def _fmt_record(r):
    head = "  %-13s %-16s" % (r["app_label"], r.get("account") or "-")
    if not r.get("ok"):
        return head + " [失败] %s" % r.get("error")
    parts = [head]
    ci = r.get("checkin")
    if ci:
        seg = state_text(ci.get("state"))
        if ci.get("today_gain") is not None:
            seg += " +%s" % ci["today_gain"]
        if ci.get("streak_days"):
            seg += " · 连续%s天" % ci["streak_days"]
        parts.append(seg)
    for w in r.get("quota") or []:
        parts.append("%s %s/%s（剩 %s%%）" % (w["label"], w["used"], w["total"], w["percent_left"]))
    a = r.get("absolute")
    if a:
        parts.append("余额 %s %s" % (a["value"], a["unit"]))
    if r.get("plan"):
        parts.append("套餐 %s" % r["plan"])
    if r.get("note"):
        parts.append(r["note"])
    return " ｜ ".join(parts)


def main():
    force = "--force" in sys.argv
    t0 = time.time()
    records, cached = collect(force=force)
    print("[%s] %d 条账号记录 · 耗时 %.1fs" % ("缓存" if cached else "实时", len(records), time.time() - t0))
    for r in records:
        print(_fmt_record(r))

    rec, w = summarize(records)
    print()
    if rec and w:
        print("球体（木桶原理）: %s · %s · 剩 %s%%" % (rec["app_label"], w["label"], w["percent_left"]))
    else:
        print("球体: 无可量化窗口（所有应用均无总量配额）")

    if "--json" in sys.argv:
        print("\n--- JSON ---")
        print(json.dumps(records, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
