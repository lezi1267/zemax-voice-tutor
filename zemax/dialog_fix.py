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
    pids = optic_pids()
    out = []

    @EnumProc
    def cb(h, l):
        if not u.IsWindowVisible(h):
            return True
        pid = wt.DWORD()
        u.GetWindowThreadProcessId(h, ctypes.byref(pid))
        if pid.value not in pids:
            return True  # 只看 OpticStudio 名下的窗口(按进程,不按标题,防漏)
        n = u.GetWindowTextLengthW(h)
        buf = ctypes.create_unicode_buffer(max(n + 1, 2))
        if n:
            u.GetWindowTextW(h, buf, n + 1)
        title = buf.value
        enabled = bool(u.IsWindowEnabled(h))
        topmost = bool(u.GetWindowLongW(h, -20) & 0x8)  # WS_EX_TOPMOST
        rect = wt.RECT()
        u.GetWindowRect(h, ctypes.byref(rect))
        out.append(dict(h=h, title=title, enabled=enabled, topmost=topmost,
                        w=rect.right - rect.left, ht=rect.bottom - rect.top,
                        x=rect.left, y=rect.top, pid=pid.value))
        return True

    u.EnumWindows(cb, 0)
    return out


def find(wins):
    dlg = [w for w in wins if any(k in w["title"] for k in KEY)
           and "OpticStudio 20" not in w["title"]]
    main = [w for w in wins if any(k in w["title"] for k in MAIN)
            and w["w"] > 800]  # 主窗口是大窗口
    return (dlg or [None])[0], (sorted(main, key=lambda w: -w["w"]) or [None])[0]


def optic_pids():
    """OpticStudio.exe 的全部进程 PID(含 standalone 实例)。"""
    import subprocess
    out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq OpticStudio.exe", "/FO", "CSV"],
                         capture_output=True).stdout.decode("gbk", errors="replace")
    pids = set()
    for line in out.splitlines():
        if "OpticStudio" not in line:
            continue
        parts = [x.strip('"') for x in line.split('","')]
        try:
            pids.add(int(parts[1]))
        except Exception:
            pass
    return pids


def fix():
    pids = optic_pids()
    wins = all_windows()
    dlg, main = find(wins)
    if not main:
        print("没找到 Zemax 主窗口,先打开 Zemax")
        return

    # 根治:模态小窗会把 OpticStudio 名下所有窗口(主窗/镜头数据编辑器/图形窗...)全部禁用,
    # 逐个解回来 —— 不止主窗口
    unlocked = []
    for w in wins:
        if w["pid"] in pids and not w["enabled"] and w["h"] != (dlg or {}).get("h"):
            u.EnableWindow(w["h"], True)
            unlocked.append(w["title"][:20] or hex(w["h"]))
    if unlocked:
        print("已解锁 %d 个被禁用的窗口: %s" % (len(unlocked), " / ".join(unlocked)))
    else:
        print("没有窗口处于被禁用状态,不用解锁")

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
