# -*- coding: utf-8 -*-
"""视觉:把图片交给智谱的免费视觉模型 glm-4.6v-flash 看,回一段文字给主 LLM。

主模型 glm-4.5-air 没有视觉。把"看"做成工具、返回普通文本,pipecat 管线一行都不用改。
用的是同一个 GLM_API_KEY、同一个端点,只是换个模型名 —— 不新增账号、不新增依赖。
"""
import base64
import io
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env", override=True)  # 单跑自检时也要拿到 key

from pipecat.frames.frames import Frame, LLMRunFrame  # noqa: E402
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor  # noqa: E402

VISION_MODELS = [m.strip() for m in os.getenv(
    "GLM_VISION_MODEL", "glm-4.6v-flash,glm-4v-flash").split(",") if m.strip()]
# 两个都免费:4.6v 更强但免费档常被挤爆(实测 429/1305),退到 4v 一样能读图上的数字。
_URL = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
MAX_SIDE = 1280  # 4K 全屏图按图块计费,又慢又贵;1280 足够读图上的字和曲线形状

# MAIN_VISION=1:主模型换成带视觉的 GLM,截图直接进对话,她看的是像素而不是别人的转述
MAIN_VISION = os.getenv("MAIN_VISION", "0") == "1"
_pending: list = []
_ctx = None


def attach_context(ctx) -> None:
    """bot 把 LLMContext 交进来,截图才有地方挂。"""
    global _ctx
    _ctx = ctx


def queue(img) -> None:
    """把截图挂到下一轮对话(最多攒 2 张,别把上下文塞满)。"""
    if MAIN_VISION and len(_pending) < 2:
        _pending.append(base64.b64encode(_jpeg(img)).decode())


class ScreenVision(FrameProcessor):
    """LLMRunFrame 进 LLM 之前,把攒着的截图作为一条用户消息塞进上下文 —— 挂完就清。"""

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, LLMRunFrame) and _pending and _ctx is not None:
            for b64 in _pending:
                _ctx.add_message({"role": "user", "content": [
                    {"type": "image_url", "image_url": {"url": b64}},
                    {"type": "text", "text": "这是学生屏幕的截图,直接看它。"}]})
            _pending.clear()
        await self.push_frame(frame, direction)


def _jpeg(img) -> bytes:
    from PIL import Image

    img = img.convert("RGB")
    if max(img.size) > MAX_SIDE:
        img.thumbnail((MAX_SIDE, MAX_SIDE), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=88)
    return buf.getvalue()


def ask(img, question: str) -> str:
    """PIL Image → 文字。阻塞,worker 线程专用。"""
    import requests

    key = os.getenv("GLM_API_KEY")
    if not key:
        raise RuntimeError(".env 里没配 GLM_API_KEY")
    b64 = base64.b64encode(_jpeg(img)).decode()
    last = ""
    for model in VISION_MODELS:
        r = requests.post(
            _URL, timeout=90,
            headers={"Authorization": f"Bearer {key}"},
            json={"model": model, "max_tokens": 800,
                  "thinking": {"type": "disabled"},
                  "messages": [{"role": "user", "content": [
                      {"type": "image_url", "image_url": {"url": b64}},
                      {"type": "text", "text": question}]}]})
        if r.status_code == 200:
            return (r.json()["choices"][0]["message"]["content"] or "").strip()
        last = f"{model} {r.status_code}: {r.text[:160]}"
        if r.status_code != 429:
            break  # 不是"免费档被挤爆",没什么好换模型重试的
    raise RuntimeError("视觉模型失败 " + last)


def ask_file(path, question: str) -> str:
    """图片文件 → 文字(学生拖进来的截图、存下来的分析图)。"""
    from PIL import Image

    return ask(Image.open(str(path)), question)


if __name__ == "__main__":
    # 自检:画一张带已知数字的图,看它能不能读出来。没配 key 就 SKIP。
    from PIL import Image, ImageDraw

    im = Image.new("RGB", (640, 200), "white")
    ImageDraw.Draw(im).text((20, 40), "Standard Spot Diagram\nRMS Spot Radius: 0.0123 mm",
                            fill="black")
    try:
        ans = ask(im, "读出图里的标题和所有数字。")
    except Exception as e:
        print("SKIP:", e)
        raise SystemExit(0)
    print("视觉回答:", ans)
    assert "0.012" in ans, "视觉模型没读出图上的数字"
    print("vision 自检通过")
