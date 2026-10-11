# -*- coding: utf-8 -*-
"""对话记录小窗:跑在 ui.py 的 Tk owner 线程里(单 Tk 线程,根治跨线程销毁崩溃)。
log() 任何线程可调,内部转投 owner 队列。
窗口底部有输入框:不想说话或环境吵的时候打字提问;按 Esc 打断她说话。"""
import ui

_txt = None
_entry = None
_on_text = None  # bot 注册的回调:学生打字提问
_on_interrupt = None  # bot 注册的回调:按 Esc 打断


def set_handlers(on_text=None, on_interrupt=None):
    """bot 启动后注册:打字提问 / 打断说话。不注册就只是个只读记录窗。"""
    global _on_text, _on_interrupt
    _on_text, _on_interrupt = on_text, on_interrupt


def log(speaker: str, text: str):
    text = text.strip()
    if text:
        ui.submit(lambda root: _append(root, speaker, text))


def start():
    ui.start()  # 兼容旧调用:让窗口线程随 bot 启动


def _send():
    if _entry is None or not _on_text:
        return
    text = _entry.get("1.0", "end").strip()
    _entry.delete("1.0", "end")
    if not text:
        return
    from pathlib import Path

    if Path(text.strip('"')).is_file():  # 输入框里打/贴一个文件路径 = 让它学这份资料
        _learn(text.strip('"'))
        return
    _on_text(text)


def _learn(path):
    """抽文本 + 分词是阻塞的(读 PDF 可能几秒),丢后台线程,别冻住 Tk 线程。"""
    import threading

    def work():
        import knowledge

        r = knowledge.learn(path)
        if r.get("ok"):
            name = r.get("学了") or r.get("已学过")
            log("系统", f"已学习《{name}》:{r.get('字数', '')}字 / {r.get('知识块', '同一份')}块,"
                        "现在可以问我里面写了什么")
        else:
            log("系统", f"这份资料没学进去:{r.get('error')}")

    threading.Thread(target=work, daemon=True).start()


def _on_drop(event):
    """把文件从资源管理器拖进对话小窗 = 让它学。tkdnd 把带空格的路径用花括号包着。"""
    import re

    for raw in re.findall(r"\{[^}]*\}|[^\s]+", event.data or ""):
        path = raw.strip("{}").strip()
        if path:
            _learn(path)


def _append(root, speaker, text):
    global _txt, _entry
    import tkinter as tk

    if _txt is None or not _txt.winfo_exists():
        top = tk.Toplevel(root)
        top.title("voice-tutor 对话记录")
        top.geometry("440x600+20+80")
        top.attributes("-topmost", True)
        row = tk.Frame(top)
        row.pack(side="bottom", fill="x")
        _entry = tk.Text(row, height=2, font=("Microsoft YaHei", 11), wrap="word")
        _entry.pack(side="left", fill="x", expand=True)
        tk.Button(row, text="发送", command=_send).pack(side="right", padx=4)
        _entry.bind("<Return>", lambda e: (_send(), "break"))
        _entry.bind("<KP_Enter>", lambda e: (_send(), "break"))
        top.bind("<Escape>", lambda e: _on_interrupt and _on_interrupt())
        _txt = tk.Text(top, font=("Microsoft YaHei", 11), wrap="word",
                       state="disabled", bg="#14161B", fg="#E8E8E8",
                       padx=12, pady=12, spacing2=4)
        _txt.pack(fill="both", expand=True)
        try:  # 拖文件进来学习(根窗口是 TkinterDnD.Tk 时才可用)
            from tkinterdnd2 import DND_FILES

            _txt.drop_target_register(DND_FILES)
            _txt.dnd_bind("<<Drop>>", _on_drop)
        except Exception:
            pass
        _txt.tag_config("you", foreground="#7FB4FF")
        _txt.tag_config("bot", foreground="#8CE99A")
        _txt.tag_config("sys", foreground="#9A9AA5", font=("Microsoft YaHei", 10))

    tag = {"你": "you", "助教": "bot"}.get(speaker, "sys")
    _txt.config(state="normal")
    _txt.insert("end", f"{speaker}:{text}\n\n", tag)
    # 一整晚的课会把小窗撑到几万行:只留最近 400 行
    n_lines = int(_txt.index("end-1c").split(".")[0])
    if n_lines > 400:
        _txt.delete("1.0", f"{n_lines - 400}.0")
    _txt.see("end")
    _txt.config(state="disabled")
