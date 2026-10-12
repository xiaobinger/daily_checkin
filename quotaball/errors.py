#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""采集层错误类型。

分两类：可重试（网络抖动、限流）与不可重试（凭证失效、接口不存在）。
按用户要求，默认不做自动重试 —— 失败就如实上报，由 UI 呈现。
"""


class QuotaError(Exception):
    """provider 采集失败的基类。"""

    def __init__(self, message, kind="error"):
        super().__init__(message)
        self.kind = kind


class NoSessionError(QuotaError):
    """登录态缺失或已失效 —— 需要用户重新登录客户端。"""

    def __init__(self, message):
        super().__init__(message, kind="no_session")


class RetryableError(QuotaError):
    """网络超时 / 5xx / 限流。默认不重试，但保留类型以便将来开启。"""

    def __init__(self, message):
        super().__init__(message, kind="retryable")
