# -*- coding: utf-8 -*-
"""对话记录小窗:跑在 ui.py 的 Tk owner 线程里(单 Tk 线程,根治跨线程销毁崩溃)。
log() 任何线程可调,内部转投 owner 队列。"""
import ui

_txt = None


def log(speaker: str, text: str):
    text = text.strip()
    if text:
        ui.submit(lambda root: _append(root, speaker, text))


def start():
    ui.start()  # 兼容旧调用:让窗口线程随 bot 启动


def _append(root, speaker, text):
    global _txt
    import tkinter as tk

    if _txt is None or not _txt.winfo_exists():
        top = tk.Toplevel(root)
        top.title("voice-tutor 对话记录")
        top.geometry("440x600+20+80")
        top.attributes("-topmost", True)
        _txt = tk.Text(top, font=("Microsoft YaHei", 11), wrap="word",
                       state="disabled", bg="#14161B", fg="#E8E8E8",
                       padx=12, pady=12, spacing2=4)
        _txt.pack(fill="both", expand=True)
        _txt.tag_config("you", foreground="#7FB4FF")
        _txt.tag_config("bot", foreground="#8CE99A")
        _txt.tag_config("sys", foreground="#9A9AA5", font=("Microsoft YaHei", 10))

    tag = {"你": "you", "助教": "bot"}.get(speaker, "sys")
    _txt.config(state="normal")
    _txt.insert("end", f"{speaker}:{text}\n\n", tag)
    _txt.see("end")
    _txt.config(state="disabled")
