#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""系统托盘：纯 ctypes Win32 实现，零第三方依赖。

设计要点
--------
* 独立线程 + message-only 窗口（HWND_MESSAGE）跑消息泵，不占用 pywebview 的
  WinForms UI 线程，也不与之争抢消息循环。
* 图标在运行时用纯 Python 生成 ICO 字节流，交给 CreateIconFromResourceEx
  转成 HICON —— 不需要 Pillow，也不需要打包 .ico 资源文件。
* 图标内容 = 环形进度（木桶原理那家的剩余百分比），颜色随阈值变化，
  不点开也能一眼看出额度紧张程度。
* 菜单：显示/隐藏悬浮球、立即刷新、开机自启（勾选态）、退出。
"""
import ctypes
import math
import threading
from ctypes import wintypes

# 关键：不能用 ctypes.windll.user32（全局共享单例）。tray 给这些函数设 argtypes
# 会污染 pywebview 内部对同一个全局 user32 对象的使用（winforms.py 也用
# ctypes.windll.user32 调 SetWindowPos/CreateWindowEx 等），两边传参约定冲突 →
# access violation 拖垮整个进程。用独立 WinDLL 实例隔离。
user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

# ---- 常量 ----
WM_USER = 0x0400
WM_COMMAND = 0x0111
WM_DESTROY = 0x0002
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205
WM_RBUTTONDBLCLK = 0x0206
WM_CONTEXTMENU = 0x007B
WM_CLOSE = 0x0010
WM_QUIT = 0x0012

NIM_ADD = 0x00000000
NIM_MODIFY = 0x00000001
NIM_DELETE = 0x00000002
NIF_MESSAGE = 0x00000001
NIF_ICON = 0x00000002
NIF_TIP = 0x00000004

MF_STRING = 0x00000000
MF_SEPARATOR = 0x00000800
MF_CHECKED = 0x00000008
MF_UNCHECKED = 0x00000000
TPM_LEFTALIGN = 0x0000
TPM_RIGHTBUTTON = 0x0002
TPM_NONOTIFY = 0x0080
TPM_RETURNCMD = 0x0100

HWND_MESSAGE = ctypes.c_void_p(-3)
LR_DEFAULTCOLOR = 0x00000000
IMAGE_ICON = 1

ID_TOGGLE = 1001
ID_REFRESH = 1002
ID_AUTORUN = 1003
ID_QUIT = 1004

# 阈值配色（与前端球体一致）：充足 / 偏紧 / 告急 / 无数据
TINT_OK = (52, 211, 153)        # #34d399
TINT_WARN = (251, 191, 36)      # #fbbf24
TINT_DANGER = (248, 113, 113)   # #f87171
TINT_UNKNOWN = (154, 162, 177)  # #9aa2b1


def tint_for(percent_left):
    if percent_left is None:
        return TINT_UNKNOWN
    if percent_left < 5:
        return TINT_DANGER
    if percent_left < 20:
        return TINT_WARN
    return TINT_OK


# ---------------------------------------------------------------- 图标生成

def _render_rgba(size, ss, pct, tint):
    """把托盘图标画成 RGBA 像素（自上而下），4x 超采样抗锯齿。

    构图：深色玻璃底盘 + 环形进度（12 点起顺时针）+ 顶部一点高光。
    """
    S = size * ss
    buf = bytearray(S * S * 4)
    c = S / 2.0
    # 以下半径全部使用「归一化」尺度（除以半边长 c），与循环里的 d 同单位
    r_out = 0.960              # 外轮廓（留边给抗锯齿）
    r_ring_out = 0.900
    r_ring_in = 0.625
    edge = 2.0 / S             # 一个输出像素对应的归一化宽度，用于边缘抗锯齿
    sweep = 2.0 * math.pi * max(0.0, min(1.0, (pct or 0.0) / 100.0))
    start = -math.pi / 2.0
    tr, tg, tb = tint
    for y in range(S):
        ny = (y + 0.5 - c) / c
        row = y * S * 4
        t = y / float(S - 1)                 # 0(顶) → 1(底)
        # 底盘竖向渐变（顶亮底暗）
        br = 44 - 26 * t
        bg = 50 - 30 * t
        bb = 66 - 40 * t
        for x in range(S):
            nx = (x + 0.5 - c) / c
            d = math.sqrt(nx * nx + ny * ny)
            i = row + x * 4
            if d > r_out:
                continue                      # 圆外：透明
            # 边缘一圈抗锯齿
            a = 1.0 if d <= r_out - edge else (r_out - d) / edge
            a = 0.0 if a < 0 else (1.0 if a > 1 else a)
            R, G, B = br, bg, bb
            if r_ring_in <= d <= r_ring_out:
                ang = math.atan2(ny, nx)
                rel = (ang - start) % (2.0 * math.pi)
                if rel <= sweep:
                    R, G, B = tr, tg, tb
                else:
                    R, G, B = 255 * 0.14, 255 * 0.15, 255 * 0.18
            # 顶部釉面高光：靠上区域提亮（底盘与环统一加，保持整体是一个球）
            if ny < -0.15:
                k = ((-0.15 - ny) / 1.15) * 0.30 * (1.0 - d / r_out)
                R += (255 - R) * k
                G += (255 - G) * k
                B += (255 - B) * k
            buf[i] = int(max(0, min(255, R)))
            buf[i + 1] = int(max(0, min(255, G)))
            buf[i + 2] = int(max(0, min(255, B)))
            buf[i + 3] = int(255 * a)
    # 4x4 盒式降采样
    out = bytearray(size * size * 4)
    n = ss * ss
    for y in range(size):
        for x in range(size):
            r = g = b = a = 0
            for dy in range(ss):
                base = ((y * ss + dy) * S + x * ss) * 4
                for dx in range(ss):
                    j = base + dx * 4
                    r += buf[j]; g += buf[j + 1]; b += buf[j + 2]; a += buf[j + 3]
            o = (y * size + x) * 4
            out[o] = r // n
            out[o + 1] = g // n
            out[o + 2] = b // n
            out[o + 3] = a // n
    return bytes(out)


def make_hicon(size, pct, tint, ss=4):
    """RGBA 像素 → HICON（CreateIconIndirect + 32bpp DIB）。

    不用 CreateIconFromResourceEx：实测它对我们拼出的 ICO 字节流静默返回 NULL
    （GetLastError 也为 0），而 CreateIconIndirect 走 DIB 位图，稳定可靠。
    像素做预乘 alpha，避免半透明边缘出现黑边。
    """
    rgba = _render_rgba(size, ss, pct, tint)
    stride = size * 4
    rows = bytearray()
    for y in range(size - 1, -1, -1):              # DIB 自下而上
        r = bytearray(rgba[y * stride:(y + 1) * stride])
        for i in range(0, stride, 4):
            a = r[i + 3]
            if a != 255:                            # 预乘 alpha
                r[i] = r[i] * a // 255
                r[i + 1] = r[i + 1] * a // 255
                r[i + 2] = r[i + 2] * a // 255
            r[i], r[i + 2] = r[i + 2], r[i]         # RGBA → BGRA
        rows += r
    hdc = user32.GetDC(0)
    bi = BITMAPINFO()
    bi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bi.bmiHeader.biWidth = size
    bi.bmiHeader.biHeight = size
    bi.bmiHeader.biPlanes = 1
    bi.bmiHeader.biBitCount = 32
    bits = ctypes.c_void_p()
    gdi32.CreateDIBSection.argtypes = [
        wintypes.HDC, ctypes.POINTER(BITMAPINFO), wintypes.UINT,
        ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD]
    gdi32.CreateDIBSection.restype = wintypes.HBITMAP
    hb = gdi32.CreateDIBSection(hdc, ctypes.byref(bi), 0,
                                ctypes.byref(bits), None, 0)
    if not hb:
        user32.ReleaseDC(0, hdc)
        return None
    ctypes.memmove(bits, bytes(rows), size * size * 4)
    mask = gdi32.CreateBitmap(size, size, 1, 1, None)
    ii = ICONINFO()
    ii.fIcon = True
    ii.hbmMask = mask
    ii.hbmColor = hb
    user32.CreateIconIndirect.restype = wintypes.HICON
    hicon = user32.CreateIconIndirect(ctypes.byref(ii))
    gdi32.DeleteObject(hb)          # CreateIconIndirect 已复制位图
    gdi32.DeleteObject(mask)
    user32.ReleaseDC(0, hdc)
    return hicon


# ---------------------------------------------------------------- 结构

class GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", wintypes.HICON),
        ("szTip", wintypes.WCHAR * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", wintypes.WCHAR * 256),
        ("uVersion", wintypes.UINT),
        ("szInfoTitle", wintypes.WCHAR * 64),
        ("dwInfoFlags", wintypes.DWORD),
        ("guidItem", GUID),
        ("hBalloonIcon", wintypes.HICON),
    ]


class ICONINFO(ctypes.Structure):
    _fields_ = [("fIcon", wintypes.BOOL), ("xHotspot", wintypes.DWORD),
                ("yHotspot", wintypes.DWORD), ("hbmMask", wintypes.HBITMAP),
                ("hbmColor", wintypes.HBITMAP)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long),
                ("biHeight", ctypes.c_long), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD),
                ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long),
                ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.UINT), ("style", wintypes.UINT),
        ("lpfnWndProc", ctypes.c_void_p), ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON), ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HANDLE), ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR), ("hIconSm", wintypes.HICON),
    ]


class MSG(ctypes.Structure):
    _fields_ = [("hWnd", wintypes.HWND), ("message", wintypes.UINT),
                ("wParam", wintypes.WPARAM), ("lParam", wintypes.LPARAM),
                ("time", wintypes.DWORD), ("pt", wintypes.POINT)]


WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_longlong, wintypes.HWND, wintypes.UINT,
                             wintypes.WPARAM, wintypes.LPARAM)

TRAY_MSG = WM_USER + 20


def _bind_signatures():
    """统一声明 Win32 函数签名。

    句柄在 64 位上是 void* —— 不声明 argtypes 时 ctypes 会按 32 位 C int 传参，
    句柄值一旦超过 2^31 就抛 OverflowError。放在结构定义之后调用。
    """
    user32.DestroyIcon.argtypes = [wintypes.HICON]
    user32.DestroyIcon.restype = wintypes.BOOL
    user32.CreateIconIndirect.restype = wintypes.HICON
    gdi32.DeleteObject.argtypes = [wintypes.HANDLE]
    gdi32.DeleteObject.restype = wintypes.BOOL
    gdi32.CreateBitmap.argtypes = [ctypes.c_int, ctypes.c_int, wintypes.UINT,
                                   wintypes.UINT, ctypes.c_void_p]
    gdi32.CreateBitmap.restype = wintypes.HBITMAP
    gdi32.CreateDIBSection.argtypes = [
        wintypes.HDC, ctypes.POINTER(BITMAPINFO), wintypes.UINT,
        ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD]
    gdi32.CreateDIBSection.restype = wintypes.HBITMAP
    user32.GetDC.argtypes = [wintypes.HWND]
    user32.GetDC.restype = wintypes.HDC
    user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    user32.ReleaseDC.restype = ctypes.c_int
    user32.CreatePopupMenu.restype = wintypes.HMENU
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.TrackPopupMenu.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_int,
                                      ctypes.c_int, ctypes.c_int, wintypes.HWND,
                                      ctypes.POINTER(wintypes.RECT)]
    user32.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT,
                                   ctypes.c_void_p, wintypes.LPCWSTR]
    user32.DestroyMenu.argtypes = [wintypes.HMENU]
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT,
                                    wintypes.WPARAM, wintypes.LPARAM]
    user32.CreateWindowExW.argtypes = [
        wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND,
        wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT,
                                      wintypes.WPARAM, wintypes.LPARAM]
    user32.DefWindowProcW.restype = ctypes.c_longlong
    user32.GetMessageW.argtypes = [ctypes.POINTER(MSG), wintypes.HWND,
                                   wintypes.UINT, wintypes.UINT]
    user32.GetMessageW.restype = wintypes.BOOL
    user32.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEXW)]
    user32.RegisterClassExW.restype = wintypes.WORD
    kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    kernel32.GetModuleHandleW.restype = wintypes.HINSTANCE
    user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
    user32.GetCursorPos.restype = wintypes.BOOL
    user32.PostQuitMessage.argtypes = [ctypes.c_int]
    user32.TranslateMessage.argtypes = [ctypes.POINTER(MSG)]
    user32.TranslateMessage.restype = wintypes.BOOL
    user32.DispatchMessageW.argtypes = [ctypes.POINTER(MSG)]
    user32.DispatchMessageW.restype = ctypes.c_longlong
    shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD,
                                          ctypes.POINTER(NOTIFYICONDATAW)]
    shell32.Shell_NotifyIconW.restype = wintypes.BOOL


_bind_signatures()


# ---------------------------------------------------------------- 托盘

class Tray(object):
    """托盘图标 + 右键菜单。所有回调在托盘线程执行，注意不要直接做 UI 操作。"""

    def __init__(self, tip="quotaball", on_toggle=None, on_refresh=None,
                 on_autorun=None, on_quit=None, log=None):
        self.tip = tip
        self.on_toggle = on_toggle
        self.on_refresh = on_refresh
        self.on_autorun = on_autorun
        self.on_quit = on_quit
        self._log = log or (lambda msg: None)
        self._hwnd = None
        self._hicon = None
        self._thread = None
        self._ready = threading.Event()
        self._wndproc_ref = None
        self._lock = threading.Lock()
        self._state = {"pct": None, "tint": TINT_UNKNOWN}

    # ---- 生命周期 ----
    def start(self):
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return self._ready.wait(timeout=8)

    def stop(self):
        if self._hwnd:
            user32.PostMessageW(self._hwnd, WM_CLOSE, 0, 0)

    # ---- 状态更新 ----
    def update(self, percent_left, tip=None):
        """更新图标（环形进度）与悬浮提示。可在任意线程调用。"""
        with self._lock:
            self._state = {"pct": percent_left,
                           "tint": tint_for(percent_left)}
            if tip is not None:
                self.tip = tip[:127]
        if self._hwnd:
            user32.PostMessageW(self._hwnd, WM_USER + 21, 0, 0)

    # ---- 内部 ----
    def _refresh_icon(self, modify=True):
        """modify=False 用于首次注册前：只生成图标，图标还没 ADD 时
        NIM_MODIFY 必然失败（无害，但会污染日志）。"""
        with self._lock:
            pct, tint, tip = self._state["pct"], self._state["tint"], self.tip
        h = make_hicon(32, pct, tint)
        if not h:
            return
        old = self._hicon
        self._hicon = h
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = self._hwnd
        nid.uID = 1
        nid.uFlags = NIF_ICON | NIF_TIP | NIF_MESSAGE
        nid.uCallbackMessage = TRAY_MSG
        nid.hIcon = ctypes.cast(h, wintypes.HICON)
        nid.szTip = tip
        if modify:
            ok = shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))
            if not ok:
                self._log("托盘图标更新失败 NIM_MODIFY")
        if old:
            user32.DestroyIcon(ctypes.cast(old, wintypes.HICON))

    def _popup_menu(self):
        hmenu = user32.CreatePopupMenu()
        user32.AppendMenuW(hmenu, MF_STRING, ID_TOGGLE, "显示 / 隐藏悬浮球")
        user32.AppendMenuW(hmenu, MF_STRING, ID_REFRESH, "立即刷新")
        checked = bool(self.on_autorun and self.on_autorun("query"))
        user32.AppendMenuW(hmenu, MF_STRING | (MF_CHECKED if checked else MF_UNCHECKED),
                           ID_AUTORUN, "开机自启")
        user32.AppendMenuW(hmenu, MF_SEPARATOR, 0, None)
        user32.AppendMenuW(hmenu, MF_STRING, ID_QUIT, "退出")
        pt = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        user32.SetForegroundWindow(self._hwnd)
        cmd = user32.TrackPopupMenu(
            hmenu, TPM_LEFTALIGN | TPM_RIGHTBUTTON | TPM_NONOTIFY | TPM_RETURNCMD,
            pt.x, pt.y, 0, self._hwnd, None)
        user32.PostMessageW(self._hwnd, WM_USER + 22, 0, 0)  # 消菜单残留
        user32.DestroyMenu(hmenu)
        if cmd == ID_TOGGLE and self.on_toggle:
            self.on_toggle()
        elif cmd == ID_REFRESH and self.on_refresh:
            self.on_refresh()
        elif cmd == ID_AUTORUN and self.on_autorun:
            self.on_autorun("toggle")
        elif cmd == ID_QUIT and self.on_quit:
            self.on_quit()

    def _wndproc(self, hwnd, msg, wparam, lparam):
        try:
            if msg == TRAY_MSG:
                evt = lparam & 0xFFFF
                if evt in (WM_RBUTTONUP, WM_CONTEXTMENU):
                    self._popup_menu()
                elif evt in (WM_LBUTTONUP, WM_LBUTTONDBLCLK):
                    if self.on_toggle:
                        self.on_toggle()
            elif msg == WM_USER + 21:
                self._refresh_icon()
            elif msg == WM_CLOSE:
                nid = NOTIFYICONDATAW()
                nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
                nid.hWnd = hwnd
                nid.uID = 1
                shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))
                if self._hicon:
                    user32.DestroyIcon(ctypes.cast(self._hicon, wintypes.HICON))
                    self._hicon = None
                user32.PostQuitMessage(0)
                return 0
        except Exception as e:
            self._log("托盘回调异常: %r" % e)
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _loop(self):
        try:
            self._loop_inner()
        except Exception as e:
            # 托盘是辅助功能：任何异常都不该拖垮主窗口（悬浮球）。
            # 标记未就绪，让 app 侧跳过托盘相关调用。
            self._ready.set()
            self._log("托盘线程异常（悬浮球不受影响）: %r" % e)

    def _loop_inner(self):
        cb = WNDPROC(self._wndproc)
        self._wndproc_ref = cb                    # 防止回调被 GC
        wc = WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wc.lpfnWndProc = ctypes.cast(cb, ctypes.c_void_p)
        wc.hInstance = kernel32.GetModuleHandleW(None)
        wc.lpszClassName = "quotaball_tray_wnd"
        registered = user32.RegisterClassExW(ctypes.byref(wc))
        if not registered:
            # 类已存在（同进程重复启动）不是致命错，继续创建窗口；
            # 但若连 GetLastError 都不给正常错误码，说明是异常环境，直接放弃。
            err = ctypes.get_last_error()
            self._log("RegisterClassExW 返回 0 (last_error=%d)，尝试沿用已注册类" % err)
        hwnd = user32.CreateWindowExW(
            0, "quotaball_tray_wnd", "quotaball_tray", 0, 0, 0, 0, 0,
            HWND_MESSAGE, None, wc.hInstance, None)
        if not hwnd:
            self._log("托盘消息窗口创建失败")
            return
        self._hwnd = hwnd
        self._refresh_icon(modify=False)
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = hwnd
        nid.uID = 1
        nid.uFlags = NIF_ICON | NIF_TIP | NIF_MESSAGE
        nid.uCallbackMessage = TRAY_MSG
        nid.hIcon = ctypes.cast(self._hicon, wintypes.HICON)
        nid.szTip = self.tip[:127]
        ok = shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid))
        self._log("托盘图标注册 %s (icon=%s)" % ("成功" if ok else "失败", self._hicon))
        self._ready.set()
        msg = MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
