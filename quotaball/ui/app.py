#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""悬浮球宿主：pywebview 透明置顶窗口 + JS API 桥。

窗口形态：
  收起 = 76x76 圆球（点击展开/收起，按住 4px 以上拖动移动位置）
  展开 = 球 + 玻璃面板；屏幕右侧空间不足时翻转到球左侧（flip 模式）

模式：
  默认      真透明（per-pixel alpha，GPU 正常时效果最好）
  --rgn    降级：不透明窗口 + SetWindowRgn 裁形（GPU 合成损坏时用）
"""
import ctypes
import json
import os
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path

import webview

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from quotaball.collector import collect          # noqa: E402
from quotaball.providers.base import summarize   # noqa: E402
from quotaball.store import Store, rec_id        # noqa: E402
from quotaball.ui.tray import Tray               # noqa: E402

TITLE = "quotaball-ball"
# 收起态窗口 100x100：球 64px + 四周 18px 边距。边距是给 box-shadow 留的 ——
# 边距不足时阴影会被窗口边界硬裁切，在透明窗口上就是一圈脏灰边（"重影"）。
BALL_W, BALL_H = 100, 100
GAP = 14
MARGIN = 12
HERE = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(HERE, "ui.log")

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetWindowLongPtrW.restype = ctypes.c_longlong
user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_longlong]
user32.SetWindowLongPtrW.restype = ctypes.c_longlong
# 不要给 user32.SetWindowPos 声明 argtypes：pywebview 内部的 move() 会给
# cx/cy 传 None（winforms.py:635），默认转换下 None 当 NULL 指针能用，
# 一旦声明成 c_int 就会抛 TypeError，导致窗口移动静默失效。

GWL_EXSTYLE = -20
WS_EX_APPWINDOW = 0x00040000
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_LAYERED = 0x00080000    # 关键：缺它 transparent=True 路径 WebView2 仍画白底
LWA_ALPHA = 0x00000002        # 配合 WS_EX_LAYERED 用 LWA_ALPHA 整窗 alpha 化
SWP_NOSIZE, SWP_NOMOVE, SWP_NOZORDER = 0x0001, 0x0002, 0x0004
SWP_NOACTIVATE, SWP_FRAMECHANGED = 0x0010, 0x0020
SW_HIDE, SW_SHOW = 0, 5
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "QuotaBall"

_tray = None      # 全局托盘句柄（collect_bg 更新图标用）

RGN_OR = 2
_collect_lock = threading.Lock()
_last_collect = 0.0


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


def log(msg):
    line = "[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def build_payload():
    """给前端的完整数据：记录 + sparkline 序列 + 木桶摘要 + 轮播队列。只读快照。"""
    store = Store()
    records = store.latest()
    payload = {
        "meta": {"fetched_at": store.fetched_at()},
        "records": records,
        "series": {},
        "summary": None,
        "carousel": [],
    }
    for r in records:
        payload["series"][rec_id(r)] = store.series(rec_id(r), 7)
    rec, w = summarize(records)
    if rec and w:
        payload["summary"] = {
            "app_key": rec["app"],
            "app": rec["app_label"],
            "label": w["label"],
            "percent_left": w["percent_left"],
        }
    # 轮播队列：有额度可看的账号（ok 且有 quota 窗口或绝对值），按 records 顺序。
    for r in records:
        if not r.get("ok"):
            continue
        ws = [x for x in (r.get("quota") or []) if x.get("total")]
        if ws:
            tight = min(ws, key=lambda x: x.get("percent_left", 100.0))
            payload["carousel"].append({
                "app_key": r["app"], "app": r["app_label"],
                "label": tight.get("label"), "percent_left": tight.get("percent_left"),
            })
        elif r.get("absolute") and r["absolute"].get("value") is not None:
            a = r["absolute"]
            payload["carousel"].append({
                "app_key": r["app"], "app": r["app_label"],
                "label": "积分余额", "percent_left": None,
                "absolute": {"value": a.get("value"), "unit": a.get("unit")},
            })
    return payload


def _autorun_cmd():
    """开机自启命令：用 pythonw（无控制台闪窗）直接跑 app.py。"""
    pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if not os.path.exists(pyw):
        pyw = sys.executable
    return '"%s" "%s"' % (pyw, os.path.abspath(__file__))


def set_autorun(action):
    """query -> 返回当前是否自启；toggle -> 反转并返回新状态。"""
    import winreg
    if action == "query":
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
                val, _ = winreg.QueryValueEx(k, RUN_NAME)
                return bool(val)
        except OSError:
            return False
    cur = set_autorun("query")
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_WRITE) as k:
            if cur:
                winreg.DeleteValue(k, RUN_NAME)
            else:
                winreg.SetValueEx(k, RUN_NAME, 0, winreg.REG_SZ, _autorun_cmd())
    except OSError as e:
        log("自启设置失败: %s" % e)
        return cur
    log("开机自启 -> %s" % (not cur))
    return not cur


def hide_from_taskbar():
    """把窗口从任务栏 / Alt+Tab 移除：去掉 WS_EX_APPWINDOW，加 WS_EX_TOOLWINDOW。

    置顶（WS_EX_TOPMOST）不受影响 —— 悬浮球仍然浮在最上层。
    transparent 路径的 WS_EX_LAYERED 在 main() 里设（与 rgn_mode 互斥）。
    """
    hwnd = user32.FindWindowW(None, TITLE)
    if not hwnd:
        return
    ex = user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
    new = (ex & ~WS_EX_APPWINDOW) | WS_EX_TOOLWINDOW
    if new != ex:
        user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, new)
        user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0,
                            SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER
                            | SWP_NOACTIVATE | SWP_FRAMECHANGED)
        log("任务栏移除: exstyle %08X -> %08X" % (ex & 0xFFFFFFFF,
                                                  new & 0xFFFFFFFF))


def build_tip(payload):
    """托盘悬浮提示：第一行是木桶那家，第二行是各家剩余一览。"""
    lines = ["quotaball"]
    s = payload.get("summary")
    if s:
        lines.append("最紧 %s %s %.0f%%" % (s["app"], s["label"],
                                            s.get("percent_left") or 0))
    else:
        lines.append("暂无额度数据")
    brief = []
    for r in payload.get("records") or []:
        if not r.get("ok"):
            brief.append("%s:异常" % r.get("app_label", "?"))
            continue
        w = (r.get("quota") or [None])[0]
        if w and w.get("total"):
            brief.append("%s %.0f%%" % (r.get("app_label", "?"),
                                        w.get("percent_left") or 0))
        elif r.get("absolute"):
            a = r["absolute"]
            brief.append("%s %s%s" % (r.get("app_label", "?"),
                                      a.get("value"), a.get("unit") or ""))
    if brief:
        lines.append(" · ".join(brief))
    return "\n".join(lines)[:127]


def update_tray(payload=None):
    """把最新数据同步到托盘图标（环形进度 + 悬浮提示）。"""
    if not _tray:
        return
    try:
        payload = payload if payload is not None else build_payload()
        s = payload.get("summary")
        pct = s.get("percent_left") if s else None
        _tray.update(pct, build_tip(payload))
    except Exception as e:
        log("托盘更新失败: %s" % e)


def collect_bg(force=False):
    """后台采集（60 秒去重），完成后把新快照推给前端。永不阻塞桥调用。"""
    global _last_collect
    win = getattr(Api, "_shared_window", None)
    if not force and time.time() - _last_collect < 60:
        return
    _last_collect = time.time()

    def _run():
        try:
            with _collect_lock:
                collect(force=bool(force))
        except Exception as e:
            log("采集失败: %s" % e)
            return
        p = build_payload()
        update_tray(p)                      # 托盘图标 + 悬浮提示
        if win:
            try:
                # evaluate_js 内部已通过 webview.Invoke 切 UI 线程（线程安全），
                # 但要求 WebView2 控件 handle 已就绪。窗口刚创建的头几秒可能
                # 还没 ready，直接调会 access violation。这里等 loaded 事件。
                if not win.events.loaded.wait(20):
                    log("推送跳过：页面未就绪")
                    return
                win.evaluate_js("window.__pushPayload && window.__pushPayload(%s)"
                                % json.dumps(p, ensure_ascii=False))
                log("采集完成，已推送前端")
            except Exception as e:
                log("推送失败: %r" % e)

    threading.Thread(target=_run, daemon=True).start()


def apply_region(w, h, mode):
    """--rgn 模式：SetWindowRgn 形状裁剪。按【请求几何】计算区域而非
    GetWindowRect——resize 是异步的，测量旧尺寸会导致收起时椭圆被算到
    窗口外（悬浮球"消失"的元凶）。区域所有权移交系统。"""
    hwnd = user32.FindWindowW(None, TITLE)
    if not hwnd:
        return
    if w <= BALL_W + 6:                        # 收起：窗口即球，整体椭圆
        rgn = gdi32.CreateEllipticRgn(2, 2, w - 2, h - 2)
    else:                                      # 展开：球圆 + 面板圆角矩形
        if mode == "flip":
            ball = gdi32.CreateEllipticRgn(w - BALL_W + 4, 4, w - 4, BALL_H - 4)
            panel = gdi32.CreateRoundRectRgn(0, 0, w - BALL_W - GAP + 1, h + 1, 18, 18)
        else:
            ball = gdi32.CreateEllipticRgn(4, 4, BALL_W - 4, BALL_H - 4)
            panel = gdi32.CreateRoundRectRgn(BALL_W + GAP, 0, w + 1, h + 1, 18, 18)
        rgn = gdi32.CreateRectRgn(0, 0, 0, 0)
        gdi32.CombineRgn(rgn, ball, panel, RGN_OR)
        gdi32.DeleteObject(ball)
        gdi32.DeleteObject(panel)
    user32.SetWindowRgn(hwnd, rgn, True)


class Api(object):
    _shared_window = None      # 类属性：collect_bg 推送用（下划线避开桥扫描）

    def __init__(self):
        self.mode = "normal"          # normal: 面板在球右侧；flip: 在左侧
        self.geom = (BALL_W, BALL_H)  # 当前窗口请求几何（rgn 计算基准）
        self._window = None
        self._drag_origin = None
        self._drag_n = 0
        self.expanded = False         # 托盘隐藏/显示时要先收起

    # ---- 数据 ----
    def get_payload(self):
        """立即返回缓存快照（启动采集由 collect_bg 后台跑，完成后主动推送）。"""
        collect_bg()
        return build_payload()

    def refresh(self, force, only_app=None):
        """手动刷新：同步采集（按钮转圈等待），返回新快照。

        only_app 非空时只刷新该应用（球体切换即刷新单家）。
        """
        global _last_collect
        try:
            with _collect_lock:
                collect(force=bool(force), only_app=only_app or None)
            _last_collect = time.time()
        except Exception as e:
            log("刷新失败: %s" % e)
        return build_payload()

    # ---- 窗口几何 ----
    def _sync_child(self):
        """WinForms 布局时序缺陷：程序化 resize+move 后 WebView2 子控件可能
        不跟随（实测内容整体偏移出窗口，点击全部落空）。强制把浏览器控件
        钉回客户区。必须在 UI 线程执行 → BeginInvoke。"""
        try:
            form = self._window.native
            if not form:
                return
            from System import Action

            def _fix():
                try:
                    if form.Controls.Count:
                        form.Controls[0].Bounds = form.ClientRectangle
                except Exception:
                    pass

            form.BeginInvoke(Action(_fix))
        except Exception:
            pass

    def _rect(self):
        hwnd = user32.FindWindowW(None, TITLE)
        if not hwnd:
            return None
        r = RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(r)):
            return None
        return r

    def set_visible(self, show):
        """托盘菜单「显示 / 隐藏悬浮球」。隐藏前先收起，避免再次显示时
        窗口是展开态而面板位置与球对不上。"""
        hwnd = user32.FindWindowW(None, TITLE)
        if not hwnd:
            return False
        if show:
            user32.ShowWindow(hwnd, SW_SHOW)
            user32.SetForegroundWindow(hwnd)
        else:
            if self.expanded:
                self.collapse(1.0)
                time.sleep(0.35)
            user32.ShowWindow(hwnd, SW_HIDE)
        log("窗口 %s" % ("显示" if show else "隐藏"))
        return bool(user32.IsWindowVisible(hwnd))

    def expand(self, panel_w, panel_h, dpr):
        """展开为 球+面板。返回 normal / flip。"""
        dpr = float(dpr or 1.0)
        rect = self._rect()
        if not rect or not self._window:
            return "normal"
        bw, bh = int(BALL_W * dpr), int(BALL_H * dpr)
        gap = int(GAP * dpr)
        pw, ph = int(panel_w * dpr), int(panel_h * dpr)
        total_w, total_h = bw + gap + pw, max(bh, ph)
        sw, sh = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
        x, y = rect.left, rect.top
        if x + total_w > sw - MARGIN:      # 右侧空间不足 → 面板翻到左边
            self.mode = "flip"
            x = max(MARGIN, rect.right - total_w)
        else:
            self.mode = "normal"
        y = max(MARGIN, min(y, sh - total_h - MARGIN))
        self.geom = (total_w, total_h)
        self.expanded = True
        self._window.resize(total_w, total_h)
        self._window.move(x, y)
        self._sync_child()
        apply_region(total_w, total_h, self.mode)
        log("expand: %s %dx%d @(%d,%d)" % (self.mode, total_w, total_h, x, y))
        return self.mode

    def collapse(self, dpr):
        """收起为 76x76 圆球。flip 模式下球在窗口右缘，按右缘锚定防跳动。"""
        dpr = float(dpr or 1.0)
        rect = self._rect()
        if not rect or not self._window:
            return
        bw, bh = int(BALL_W * dpr), int(BALL_H * dpr)
        x = rect.right - bw if self.mode == "flip" else rect.left
        self.geom = (bw, bh)
        self.expanded = False
        self._window.resize(bw, bh)
        self._window.move(x, rect.top)
        self._sync_child()
        apply_region(bw, bh, "collapsed")
        log("collapse: @(%d,%d) mode=%s" % (x, rect.top, self.mode))

    # ---- 拖拽（JS 报增量，Python 直接 move）----
    def drag_begin(self):
        rect = self._rect()
        if not rect:
            self._drag_origin = None
            return False
        self._drag_origin = (rect.left, rect.top)
        self._drag_n = 0
        log("drag_begin @(%d,%d)" % (rect.left, rect.top))
        return True

    def drag_move(self, dx, dy):
        if not self._drag_origin or not self._window:
            return False
        self._window.move(self._drag_origin[0] + int(dx),
                          self._drag_origin[1] + int(dy))
        self._drag_n += 1
        if self._drag_n % 15 == 1:
            log("drag_move n=%d d=(%d,%d)" % (self._drag_n, dx, dy))
        return True

    def heartbeat(self):
        log("hb")
        return True

    def ping(self):
        return "pong"


def main():
    # transparent 模式（默认）：pywebview WinForms+WebView2 透明路径。
    # 缺 WS_EX_LAYERED → WebView2 即便 DefaultBackgroundColor=Transparent 也
    # 会被 DWM 当不透明窗口画成白板。_detach_taskbar 里强制加 WS_EX_LAYERED。
    # --rgn 走不透明 + SetWindowRgn 裁形（白板稳退，球周围阴影会随 region
    # 被裁；GPU 合成坏掉的环境兜底用）。
    rgn_mode = "--rgn" in sys.argv
    if rgn_mode:
        # GPU 合成损坏时的降级：软件渲染 + 区域裁剪
        os.environ.setdefault("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS", "--disable-gpu")
    try:
        import faulthandler
        _fa = open(LOG_FILE, "a", encoding="utf-8")
        faulthandler.enable(_fa)      # 原生崩溃栈写入 ui.log
    except Exception:
        pass

    api = Api()
    # 单实例守卫：开机自启 + 手动启动场景下防止出现多个球
    if user32.FindWindowW(None, TITLE):
        log("已有实例在运行，退出")
        return
    sw, sh = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
    x, y = sw - BALL_W - 84, sh - BALL_H - 200

    url = Path(os.path.join(HERE, "index.html")).as_uri()
    win = webview.create_window(
        title=TITLE, url=url, js_api=api,
        width=BALL_W, height=BALL_H, x=x, y=y,
        frameless=True, easy_drag=False, on_top=True, shadow=False,
        transparent=not rgn_mode, background_color="#101218",
        resizable=False, min_size=(60, 60), focus=True)
    api._window = win
    Api._shared_window = win
    log("窗口创建 %dx%d @(%d,%d) transparent=%s" % (BALL_W, BALL_H, x, y, not rgn_mode))

    def _ensure_size():
        """兜底：WebView2 初始化竞争可能重置窗口尺寸（120x60/284x161 等），
        shown/loaded 后强制校正回请求几何。"""
        try:
            hwnd = user32.FindWindowW(None, TITLE)
            if hwnd:
                r = RECT()
                user32.GetWindowRect(hwnd, ctypes.byref(r))
                if (r.right - r.left, r.bottom - r.top) != api.geom:
                    log("尺寸漂移 %dx%d -> 校正 %dx%d"
                        % (r.right - r.left, r.bottom - r.top, api.geom[0], api.geom[1]))
                    win.resize(api.geom[0], api.geom[1])
                    win.move(r.left, r.top)
                    api._sync_child()
                if rgn_mode:
                    apply_region(api.geom[0], api.geom[1], api.mode)
        except Exception as e:
            log("尺寸校正失败: %s" % e)

    def _detach_taskbar():
        """窗口是常驻悬浮件，不该占任务栏 —— 放到 shown 之后，避免与
        pywebview 自身的窗口样式初始化竞争。"""
        time.sleep(0.3)
        hide_from_taskbar()
        if not rgn_mode:
            # transparent 路径的"白板"修补：必须给窗口加 WS_EX_LAYERED 扩展
            # 样式，pywebview WinForms 后端不会自动设这个 —— 没有它，DWM 把
            # WebView2 当不透明窗口画，DefaultBackgroundColor.Transparent
            # 被忽略，整块白色矩形包着球。
            hwnd = user32.FindWindowW(None, TITLE)
            if hwnd:
                ex = user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
                if not (ex & WS_EX_LAYERED):
                    user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE,
                                             ex | WS_EX_LAYERED)
                    # LWA_ALPHA=255：per-pixel alpha 由 WebView2 自带；
                    # LWA_ALPHA 仅起"让 DWM 走 layered 合成路径"的作用。
                    user32.SetLayeredWindowAttributes(hwnd, 0, 255, LWA_ALPHA)
                    log("layered 修补: exstyle %08X -> %08X"
                        % (ex & 0xFFFFFFFF, (ex | WS_EX_LAYERED) & 0xFFFFFFFF))

    def _on_tray_toggle():
        api.set_visible(not user32.IsWindowVisible(
            user32.FindWindowW(None, TITLE)))

    def _on_tray_refresh():
        log("托盘：立即刷新")
        collect_bg(force=True)

    def _on_tray_quit():
        log("托盘：退出")
        try:
            if _tray:
                _tray.stop()
        except Exception:
            pass
        hwnd = user32.FindWindowW(None, TITLE)
        if hwnd:
            user32.PostMessageW(hwnd, 0x0010, 0, 0)     # WM_CLOSE
        try:
            win.destroy()
        except Exception:
            pass

    global _tray
    _tray = Tray(tip="quotaball", on_toggle=_on_tray_toggle,
                 on_refresh=_on_tray_refresh, on_autorun=set_autorun,
                 on_quit=_on_tray_quit, log=log)

    win.events.shown += _ensure_size
    win.events.loaded += _ensure_size
    win.events.shown += _detach_taskbar

    # 托盘必须等 WinForms 消息循环真正跑起来后再启动。若在 webview.start()
    # 之前抢跑，托盘线程的 Win32 调用会与 WebView2/WinForms 初始化竞争，
    # 偶发 access violation 拖垮整个进程（实测在 RegisterClassExW /
    # CreateWindowExW 处崩溃）。
    _tray_started = {"v": False}

    def _start_tray():
        if _tray_started["v"]:
            return
        _tray_started["v"] = True
        if not _tray.start():
            log("托盘启动失败")
        else:
            log("托盘就绪")
            update_tray()          # 启动即显示缓存数据，不等首次采集

    win.events.loaded += lambda: threading.Thread(target=_start_tray,
                                                  daemon=True).start()

    def _page_probe_loop():
        """页面存活探测：只在状态变化时写日志（正常时安静）。"""
        last = None
        while True:
            time.sleep(15)
            try:
                if not win.events.loaded.is_set():
                    continue
                r = win.evaluate_js(
                    "JSON.stringify({api:!!(window.pywebview&&window.pywebview.api),"
                    "booted:!!window.__booted, expanded:!!document.body.classList.contains('expanded')})")
                if r != last:
                    log("probe: %s" % r)
                    last = r
            except Exception as e:
                msg = repr(e)[:80]
                if msg != last:
                    log("probe fail: %s" % msg)
                    last = msg

    threading.Thread(target=_page_probe_loop, daemon=True).start()

    # 退出原因监控：进程静默消失时留痕，便于定位（后台 pythonw 启动的
    # 实例偶发 15-30 秒后无日志退出，与 access violation 不同，属静默退出）。
    import atexit

    def _on_exit():
        try:
            log("进程退出（atexit）")
        except Exception:
            pass

    atexit.register(_on_exit)

    try:
        webview.start()
    except Exception as e:
        log("启动失败: %s" % e)
        raise
    log("退出")


if __name__ == "__main__":
    main()
