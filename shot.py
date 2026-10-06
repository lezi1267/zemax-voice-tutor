# -*- coding: utf-8 -*-
"""截屏保存为 shot.png(含透明/分层窗口,物理像素),供助手"看"屏幕。

用法: python shot.py [输出文件]
输出会打印图片尺寸和虚拟屏原点(多显示器时,截图坐标 + 原点 = 屏幕绝对坐标)。
"""
import ctypes
import sys

ctypes.windll.shcore.SetProcessDpiAwareness(2)
from PIL import ImageGrab  # noqa: E402  (需先设 DPI 感知)

out = sys.argv[1] if len(sys.argv) > 1 else "shot.png"
img = ImageGrab.grab(include_layered_windows=True, all_screens=True)
img.save(out)
ox = ctypes.windll.user32.GetSystemMetrics(76)  # SM_XVIRTUALSCREEN
oy = ctypes.windll.user32.GetSystemMetrics(77)
print(out, img.size, "origin=(%d,%d)" % (ox, oy))
