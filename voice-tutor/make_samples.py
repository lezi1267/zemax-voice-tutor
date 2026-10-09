# -*- coding: utf-8 -*-
"""生成试听音频到 samples\(试听音频有版权,不入库,自己生成)。

用法:
    .venv\Scripts\python make_samples.py        # 微软 edge 免费音色,无需 key
生成完去 samples\ 里听,选好改 .env 的 EDGE_VOICE / PIPER_VOICE / MINIMAX_VOICE。
"""
import asyncio
from pathlib import Path

VOICES = ["zh-CN-XiaoxiaoNeural", "zh-CN-XiaoyiNeural", "zh-CN-XiaoyunNeural"]
TEXT = "第 3 课:把后表面半径改成负一百,然后跑优化。RMS 大约五十微米。"


async def main():
    import edge_tts

    out = Path(__file__).parent / "samples"
    out.mkdir(exist_ok=True)
    for v in VOICES:
        f = out / f"edge_{v.split('-')[-1].lower()}.mp3"  # edge 出来是 mp3,别改名成 wav
        await edge_tts.Communicate(TEXT, v).save(str(f))
        print("已生成", f.name)


if __name__ == "__main__":
    asyncio.run(main())
