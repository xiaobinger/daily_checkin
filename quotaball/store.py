#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""状态层：quota_state.json 的读写。

结构：
  {
    "meta": {"fetched_at": ..., "version": 1},
    "latest": [账号记录...],
    "history": {"2026-09-01": {"zcode:bigmodel": {"v": 0.0, "kind": "pct_left", ...}, ...}}
  }
历史保留近 30 天，供面板画 7 日 sparkline。
"""
import json
import os
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_PATH = os.path.join(ROOT, "quota_state.json")
HISTORY_DAYS = 30


def rec_id(rec):
    """账号记录的唯一键：应用 + 脱敏账号（多账号应用靠账号区分）。"""
    acc = rec.get("account") or ""
    return rec["app"] + (":" + acc if acc else "")


def primary(rec):
    """主显示值：有窗口取最紧窗口的剩余百分比，否则取绝对值。返回 (值, 类型)。"""
    ws = [w for w in (rec.get("quota") or []) if w.get("total")]
    if ws:
        tight = min(ws, key=lambda w: w.get("percent_left", 100.0))
        return tight.get("percent_left", 100.0), "pct_left"
    a = rec.get("absolute")
    if a and a.get("value") is not None:
        return a.get("value"), "abs"
    return None, "none"


class Store(object):
    def __init__(self, path=STATE_PATH):
        self.path = path

    def load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def save(self, records):
        data = self.load()
        now = time.time()
        today = time.strftime("%Y-%m-%d", time.localtime(now))

        hist = data.get("history") or {}
        day = hist.setdefault(today, {})
        for rec in records:
            v, kind = primary(rec)
            ci = rec.get("checkin") or {}
            day[rec_id(rec)] = {
                "v": v,
                "kind": kind,
                "state": ci.get("state") if rec.get("checkin") else None,
                "gain": ci.get("today_gain"),
                "ok": rec.get("ok", False),
            }
        dates = sorted(hist.keys())
        while len(dates) > HISTORY_DAYS:
            hist.pop(dates.pop(0), None)

        data["history"] = hist
        data["latest"] = records
        data["meta"] = {"fetched_at": now, "version": 1}

        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    def latest(self):
        return self.load().get("latest") or []

    def fetched_at(self):
        return (self.load().get("meta") or {}).get("fetched_at") or 0

    def series(self, rid, days=7):
        """某个账号近 N 天的主显示值序列（sparkline 用），缺失补 None。"""
        hist = self.load().get("history") or {}
        out = []
        for d in sorted(hist.keys())[-days:]:
            e = (hist[d] or {}).get(rid)
            out.append(e.get("v") if e else None)
        return out
