#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""provider 适配器。顺序即 UI 展示顺序。"""
from .workbuddy import WorkBuddyProvider
from .trae import TraeProvider
from .qoder import QoderProvider
from .minimax import MiniMaxProvider
from .lobster import LobsterProvider
from .zcode import ZcodeProvider

__all__ = ["WorkBuddyProvider", "TraeProvider", "QoderProvider",
           "MiniMaxProvider", "LobsterProvider", "ZcodeProvider", "all_providers"]


def all_providers():
    return [WorkBuddyProvider(), TraeProvider(), QoderProvider(),
            MiniMaxProvider(), LobsterProvider(), ZcodeProvider()]