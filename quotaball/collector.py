#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""采集编排：并发跑五个 provider，落快照。

决策（与吊毛确认过）：
  - 默认不自动重试 —— 失败如实上报，由 UI 呈现，用户手动触发刷新；
  - 单家失败不影响其他四家；
  - 10 分钟内重复调用直接返回缓存（节流），force=True 强制实时。
"""
import time
from concurrent.futures import ThreadPoolExecutor

from . import providers
from .errors import NoSessionError, QuotaError, RetryableError
from .store import Store

MIN_INTERVAL = 600        # 节流：10 分钟
RETRY_ENABLED = False     # 默认关；将来要开自动重试时改 True
BACKOFF = (2, 4, 8)       # 指数退避（秒）


def _run(p):
    started = time.time()
    attempts = 1 + (len(BACKOFF) if RETRY_ENABLED else 0)
    last = None
    for i in range(attempts):
        if i:
            time.sleep(BACKOFF[i - 1])
        try:
            return p.fetch()
        except RetryableError as e:
            last = e
            continue
        except NoSessionError as e:
            return [p._failed(started, "no_session", str(e))]
        except QuotaError as e:
            return [p._failed(started, "error", str(e))]
        except Exception as e:
            return [p._failed(started, "error", "未分类异常: %s" % e)]
    return [p._failed(started, "error", "重试 %d 次仍失败: %s" % (attempts - 1, last))]


def collect(force=False, store=None, only_app=None):
    """采集账号。返回 (records, 是否缓存)。

    only_app 非空时只采集该应用（key），并把结果与其余应用的缓存合并返回 ——
    供球体「切换即刷新单家」用，避免每切一次打满五家接口。
    """
    store = store or Store()
    if not force and not only_app and time.time() - store.fetched_at() < MIN_INTERVAL:
        cached = store.latest()
        if cached:
            return cached, True
    ps = providers.all_providers()
    if only_app:
        ps = [p for p in ps if p.key == only_app]
        if not ps:
            return store.latest(), True
    with ThreadPoolExecutor(max_workers=len(ps)) as ex:
        results = list(ex.map(_run, ps))
    fresh = [r for sub in results for r in sub]
    if only_app:
        # 单家刷新：把新结果按 app 覆盖到缓存，其余应用保留旧数据
        cached = store.latest()
        fresh_keys = {r.get("app") for r in fresh}
        records = [r for r in cached if r.get("app") not in fresh_keys] + fresh
    else:
        records = fresh
    store.save(records)
    return records, False
