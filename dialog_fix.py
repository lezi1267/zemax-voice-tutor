# -*- coding: utf-8 -*-
"""驯服 Zemax 交互扩展小窗:挪到屏幕角落、取消置顶、解锁被它锁住的主窗口。

用法:
    python dialog_fix.py          # 摆放 + 解锁(开着交互扩展时运行)
    python dialog_fix.py list     # 只列出相关窗口,调试用
"""
import ctypes
import ctypes.wintypes as wt
import sys

u = ctypes.windll.user32

KEY = ("交互", "扩展", "Interactive", "Extension")  # 扩展小窗标题特征
MAIN = ("OpticStudio", "LENS", "Zemax")            # 主窗口标题特征

EnumProc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)


def all_windows():
    out = []

    @EnumProc
    def cb(h, l):
        if not u.IsWindowVisible(h):
            return True
        n = u.GetWindowTextLengthW(h)
        if n == 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        u.GetWindowTextW(h, buf, n + 1)
        title = buf.value
        if any(k in title for k in KEY + MAIN):
            enabled = bool(u.IsWindowEnabled(h))
            topmost = bool(u.GetWindowLongW(h, -20) & 0x8)  # WS_EX_TOPMOST
            rect = wt.RECT()
            u.GetWindowRect(h, ctypes.byref(rect))
            out.append(dict(h=h, title=title, enabled=enabled, topmost=topmost,
                            w=rect.right - rect.left, ht=rect.bottom - rect.top,
                            x=rect.left, y=rect.top))
        return True

    u.EnumWindows(cb, 0)
    return out


def find(wins):
    dlg = [w for w in wins if any(k in w["title"] for k in KEY)
           and "OpticStudio 20" not in w["title"]]
    main = [w for w in wins if any(k in w["title"] for k in MAIN)
            and w["w"] > 800]  # 主窗口是大窗口
    return (dlg or [None])[0], (sorted(main, key=lambda w: -w["w"]) or [None])[0]


def fix():
    wins = all_windows()
    dlg, main = find(wins)
    if not main:
        print("没找到 Zemax 主窗口,先打开 Zemax")
        return
    if main and not main["enabled"]:
        u.EnableWindow(main["h"], True)
        print("已解锁主窗口(之前被小窗锁住了)")
    elif main:
        print("主窗口本来就是可点的,不用解锁")
    if not dlg:
        print("没找到交互扩展小窗:先点 编程 → 交互扩展 把它打开,再跑一次本脚本")
        return
    # 取消置顶
    if dlg["topmost"]:
        u.SetWindowPos(dlg["h"], -2, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)  # HWND_NOTOPMOST
        print("已取消小窗置顶")
    # 挪到主屏右下角(任务栏上方)
    sw, sh = u.GetSystemMetrics(0), u.GetSystemMetrics(1)
    x = max(sw - dlg["w"] - 20, 0)
    y = max(sh - dlg["ht"] - 60, 0)
    u.SetWindowPos(dlg["h"], 0, x, y, 0, 0, 0x0001 | 0x0004 | 0x0010)  # NOSIZE|NOZORDER
    print("小窗已挪到屏幕右下角(%d,%d)" % (x, y))


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "list":
        for w in all_windows():
            print(w)
    else:
        fix()
