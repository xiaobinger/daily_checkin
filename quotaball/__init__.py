#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""quotaball —— 桌面悬浮球的采集内核（S1）。

分层：UI(悬浮球) -> 状态层(quota_state.json) -> 采集层(本包) -> 数据源(登录态/官方接口)
设计见 docs/SPEC.md。UI 只读快照，永不直接碰网络和凭证。
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)  # 让 provider 能复用 daily_checkin.py 的登录态逻辑
