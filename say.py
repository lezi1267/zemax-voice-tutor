# -*- coding: utf-8 -*-
"""语音播放:播放助手预先生成的自然语音 wav(豆包 TTS)。

用法:
    python say.py voice.wav          # 播放指定 wav
    python say.py "要朗读的文字"      # 没有 wav 时,回退 Windows 自带朗读
"""
import os
import subprocess
import sys


def play_wav(path):
    try:
        import winsound
        winsound.PlaySound(path, winsound.SND_FILENAME)
        return
    except Exception:
        pass
    # 回退:WPF MediaPlayer(兼容更多 wav 编码)
    cmd = ("Add-Type -AssemblyName presentationCore;"
           "$p = New-Object System.Windows.Media.MediaPlayer;"
           "$p.Open([uri]'%s');"
           "$p.Play();"
           "while ($p.NaturalDuration.HasTimeSpan -eq $false) {Start-Sleep -Milliseconds 100};"
           "Start-Sleep -Seconds ($p.NaturalDuration.TimeSpan.TotalSeconds + 1)"
           % os.path.abspath(path).replace("'", "''"))
    subprocess.run(["powershell", "-NoProfile", "-Command", cmd],
                   creationflags=0x08000000)


def speak_text(text):
    cmd = ("Add-Type -AssemblyName System.Speech;"
           "(New-Object System.Speech.Synthesis.SpeechSynthesizer).Speak('%s')"
           % text.replace("'", "''"))
    subprocess.run(["powershell", "-NoProfile", "-Command", cmd],
                   creationflags=0x08000000)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
    elif os.path.exists(sys.argv[1]):
        play_wav(sys.argv[1])
    else:
        speak_text(sys.argv[1])
