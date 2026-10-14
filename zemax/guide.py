# -*- coding: utf-8 -*-
"""桌面小精灵指引:一只小幽灵滑到目标位置,气泡说话,目标处画脉冲光圈。

用法(命令行兼容旧版):
    python guide.py X Y 提示文字 [显示秒数] [语音wav路径]
程序内调用:
    import guide
    guide.guide_to(x, y, "点「点列图」", secs=8)

说明:窗口跑在 ui.py 的 Tk owner 线程里;整窗穿透点击,不挡 Zemax。
"""
import math
import os
import subprocess
import sys
import threading

RED, DARK = "#FF3B30", "#600000"
BODY, BODY_EDGE = "#CFE8FF", "#3A6EA5"


def speak(text, wav=None):
    if wav:
        def _play():
            try:
                import winsound
                winsound.PlaySound(wav, winsound.SND_FILENAME)
                return
            except Exception:
                pass
            cmd = ("Add-Type -AssemblyName presentationCore;"
                   "$p = New-Object System.Windows.Media.MediaPlayer;"
                   "$p.Open([uri]'%s');"
                   "$p.Play();"
                   "while ($p.NaturalDuration.HasTimeSpan -eq $false) {Start-Sleep -Milliseconds 100};"
                   "Start-Sleep -Seconds ($p.NaturalDuration.TimeSpan.TotalSeconds + 1)"
                   % os.path.abspath(wav).replace("'", "''"))
            subprocess.run(["powershell", "-NoProfile", "-Command", cmd],
                           creationflags=0x08000000)  # CREATE_NO_WINDOW
        threading.Thread(target=_play, daemon=True).start()
        return
    cmd = ("Add-Type -AssemblyName System.Speech;"
           "(New-Object System.Speech.Synthesis.SpeechSynthesizer).Speak('%s')"
           % text.replace("'", "''"))
    subprocess.Popen(["powershell", "-NoProfile", "-Command", cmd],
                     creationflags=0x08000000)  # CREATE_NO_WINDOW


def guide_to(x, y, text, secs=8, wav=None, voice=True):
    """对外接口:在 (x,y) 处指指点点。投给 Tk owner 线程,不阻塞调用方。
    voice=False 只画不念 —— 语音助教自己会说话,别用系统 TTS 抢她的嗓子(还会被麦克风收回去)。"""
    if voice:
        speak(text, wav)
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                    "voice-tutor"))
    import ui

    ui.submit(lambda root: _run_overlay(root, x, y, text, secs))


def _run_overlay(root, x, y, text, secs):
    """只在 Tk owner 线程里执行:建 Toplevel,画幽灵+光圈+气泡,动画完自己销毁。"""
    import tkinter as tk

    W, H = root.winfo_screenwidth(), root.winfo_screenheight()
    top = tk.Toplevel(root)
    top.overrideredirect(True)
    top.attributes("-topmost", True)
    top.attributes("-transparentcolor", "black")
    top.geometry("%dx%d+0+0" % (W, H))
    cv = tk.Canvas(top, bg="black", highlightthickness=0, width=W, height=H)
    cv.pack(fill="both", expand=True)

    state = {"t": 0.0, "arrived": False}
    sx = 60 if x > 300 else W - 60
    sy = 60

    def ghost(cx, cy):
        cv.delete("ghost")
        cv.create_oval(cx - 24, cy - 26, cx + 24, cy + 20, fill=BODY,
                       outline=BODY_EDGE, width=3, tags="ghost")
        cv.create_polygon(cx - 12, cy + 16, cx, cy + 34, cx + 12, cy + 16,
                          fill=BODY, outline="", tags="ghost")
        for ex in (cx - 10, cx + 6):
            cv.create_oval(ex, cy - 12, ex + 8, cy - 2, fill="#22283B",
                           outline="", tags="ghost")
            cv.create_oval(ex + 2, cy - 11, ex + 4, cy - 9, fill="white",
                           outline="", tags="ghost")
        cv.create_oval(cx - 20, cy - 4, cx - 12, cy + 1, fill="#FFB3C1",
                       outline="", tags="ghost")
        cv.create_oval(cx + 9, cy - 4, cx + 17, cy + 1, fill="#FFB3C1",
                       outline="", tags="ghost")

    def tick():
        state["t"] += 0.05
        t = min(state["t"] / 1.1, 1.0)
        ease = 1 - (1 - t) ** 3
        bob = math.sin(state["t"] * 6) * 4
        cx = sx + (x - 46 - sx) * ease
        cy = sy + (y - 46 - sy) * ease + bob
        ghost(cx, cy)

        if t >= 1.0:
            state["arrived"] = True

        if state["arrived"]:
            cv.delete("arm")
            cv.create_line(cx + 18, cy - 6, x - 6, y - 6, arrow="last",
                           width=4, fill=RED, arrowshape=(16, 22, 8), tags="arm")
            r = 20 + (state["t"] * 60 % 16)
            cv.delete("ring")
            cv.create_oval(x - r, y - r, x + r, y + r, outline=RED, width=4, tags="ring")
            bx, by = min(max(x - 130, 4), W - 264), y - 92
            if by < 4:
                by = y + 30
            cv.delete("bubble")
            cv.create_rectangle(bx, by, bx + 260, by + 56, fill="#FFF9DB",
                                outline=DARK, width=2, tags="bubble")
            cv.create_polygon(bx + 120, by + 56, bx + 140, by + 56, bx + 130, by + 72,
                              fill="#FFF9DB", outline=DARK, tags="bubble")
            cv.create_text(bx + 130, by + 28, text=text,
                           font=("Microsoft YaHei", 12, "bold"), fill=DARK,
                           width=244, tags="bubble")

        if state["t"] > 1.1 + secs:
            top.destroy()  # 只销毁自己的 Toplevel,owner 根永生
            return
        top.after(40, tick)

    top.after(50, tick)

    # 整窗穿透点击
    top.update_idletasks()
    try:
        import ctypes

        hwnd = ctypes.windll.user32.GetParent(top.winfo_id()) or top.winfo_id()
        st = ctypes.windll.user32.GetWindowLongW(hwnd, -20)  # GWL_EXSTYLE
        ctypes.windll.user32.SetWindowLongW(hwnd, -20, st | 0x20)  # WS_EX_TRANSPARENT
    except Exception:
        pass


def main():
    x, y = int(sys.argv[1]), int(sys.argv[2])
    text = sys.argv[3] if len(sys.argv) > 3 else "看这里"
    secs = int(sys.argv[4]) if len(sys.argv) > 4 else 8
    wav = sys.argv[5] if len(sys.argv) > 5 else None
    import time

    guide_to(x, y, text, secs, wav)
    time.sleep(secs + 3)  # CLI 模式等动画放完(进程退出会带走后台 Tk 线程)


if __name__ == "__main__":
    main()
