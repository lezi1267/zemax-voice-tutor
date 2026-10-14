# -*- coding: utf-8 -*-
"""截屏(物理像素,含透明/分层窗口,覆盖全部显示器),供助手"看"屏幕。

程序内调用:
    import shot
    img = shot.grab()
命令行:
    python shot.py [输出文件]
"""
import ctypes
import sys


def grab():
    """全屏 PIL 图像。必须先设 DPI 感知,否则高分屏上截到缩放后的糊图、坐标也对不上。

    图像原点是虚拟屏幕原点(多显示器时可能为负),要屏幕绝对坐标就加 origin()。"""
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
    from PIL import ImageGrab  # noqa: E402  (需先设 DPI 感知)

    return ImageGrab.grab(include_layered_windows=True, all_screens=True)


def origin():
    """虚拟屏幕原点 (x, y):截图内坐标 + 原点 = 屏幕绝对坐标。"""
    return (ctypes.windll.user32.GetSystemMetrics(76),  # SM_XVIRTUALSCREEN
            ctypes.windll.user32.GetSystemMetrics(77))  # SM_YVIRTUALSCREEN


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "shot.png"
    img = grab()
    img.save(out)
    print(out, img.size, "origin=(%d,%d)" % origin())
