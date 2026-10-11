# -*- coding: utf-8 -*-
"""全进程唯一的 Tk 主线程(owner):所有 tkinter 窗口(小幽灵/对话记录)都在这个线程里
创建和销毁,别的线程只往队列里投任务。跨线程创建/销毁 Tk 会触发
"Tcl_AsyncDelete: async handler deleted by the wrong thread",这里的单 owner 模式根治它。"""
import queue
import threading

_q: queue.Queue = queue.Queue()
_started = False
_root = None


def _owner():
    global _root
    import tkinter as tk

    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass
    _root = None

    try:
        from tkinterdnd2 import TkinterDnD

        _root = TkinterDnD.Tk()  # 对话小窗要接得住拖进来的文件;没装就退回普通 Tk
    except Exception:
        import tkinter as tk

        _root = tk.Tk()
    _root.withdraw()  # 主根只当宿主,自己不显示

    def poll():
        try:
            while True:
                job = _q.get_nowait()
                try:
                    job(_root)
                except Exception as e:
                    print("UI 任务出错:", e)
        except queue.Empty:
            pass
        _root.after(50, poll)

    poll()
    _root.mainloop()


def start():
    """懒启动 owner 线程;重复调用无害。"""
    global _started
    if not _started:
        _started = True
        threading.Thread(target=_owner, daemon=True).start()


def submit(job):
    """把一个 job(root) 投给 Tk 线程执行;job 里只能操作这个线程的 Tk 控件。"""
    start()
    _q.put(job)
