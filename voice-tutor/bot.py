# -*- coding: utf-8 -*-
"""voice-tutor: 麦克风 → FunASR SenseVoice(本地识别) → 智谱 GLM → 本地 Piper → 扬声器

跑之前:
  1. 同目录 .env 里填 GLM_API_KEY=(open.bigmodel.cn 申请)
  2. python bot.py   (模型下载走 HF 镜像,脚本里已设 HF_ENDPOINT)

全双工:说话打断它,它会停嘴听你说(打断由 Pipecat 内置 VAD 处理)。
Ctrl+C 退出。
"""
import asyncio
import io
import os
import re
import shutil
from collections.abc import AsyncGenerator
from pathlib import Path

# 国内直连:whisper 走 HF 镜像
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

# espeak-ng 音素库在 Windows 上打不开含中文的路径(venv 在"学习游戏"下会直接崩),
# 把数据拷到英文目录并强制使用
import piper.espeakbridge as _espeak

_ESPEAK_DIR = Path.home() / ".cache/piper-espeak-ng-data"
if not _ESPEAK_DIR.exists():
    import piper.phonemize_espeak as _pe

    shutil.copytree(Path(_pe.__file__).parent / "espeak-ng-data", _ESPEAK_DIR)
_espeak.initialize_orig = _espeak.initialize
_espeak.initialize = lambda _ignored: _espeak.initialize_orig(str(_ESPEAK_DIR))

# piper 下载音色是 urllib 直连 huggingface.co,不吃 HF_ENDPOINT,手动换成镜像;
# hf-mirror 拦默认 Python UA,要带浏览器 UA
import urllib.request as _ur

_opener = _ur.build_opener()
_opener.addheaders = [("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64)")]
_ur.install_opener(_opener)

import piper.download_voices as _piper_dl

_piper_dl.VOICES_JSON = _piper_dl.VOICES_JSON.replace("huggingface.co", "hf-mirror.com")
_piper_dl.URL_FORMAT = _piper_dl.URL_FORMAT.replace("huggingface.co", "hf-mirror.com")

# g2pW(拼音音素)用的 transformers 启动时会探测 huggingface.co 并重试 5 次,让它离线走本地缓存
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

from pipecat.frames.frames import (
    ErrorFrame,
    Frame,
    InterimTranscriptionFrame,
    LLMFullResponseEndFrame,
    LLMRunFrame,
    StartFrame,
    TextFrame,
    TranscriptionFrame,
)
from loguru import logger

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.observers.base_observer import BaseObserver, FrameProcessed
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.funasr.stt import FunASRSTTService, FunASRSTTSettings
from pipecat.services.minimax.tts import MiniMaxHttpTTSService, MiniMaxTTSSettings
from pipecat.services.openai.base_llm import OpenAILLMSettings
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.services.piper.tts import PiperTTSService
from pipecat.services.settings import TTSSettings
from pipecat.services.tts_service import TTSService
from pipecat.transcriptions.language import Language
from pipecat.transports.local.audio import LocalAudioTransport, LocalAudioTransportParams
from pipecat.turns.user_mute import MuteUntilFirstBotCompleteUserMuteStrategy

from dotenv import load_dotenv

import chat_window
import lessons
import zemax_tools
from lessons import read_lesson_schema, finish_lesson_schema
from zemax_tools import point_at_schema, read_analysis_schema, read_lde_schema

load_dotenv(override=True)

SYSTEM = (
    "你是 Zemax OpticStudio 光学设计助教,学生是初学者,回答都会念出来。"
    "学生说开始上课/下一课/问课程安排:先调用 read_lesson 拿课件,再按课件带学生做,一次只说一步。"
    "要操作步骤:一次只给一个操作指令。"
    "问原理:直接说重点,两句话以内,可以打一个比方。"
    "问参数或检查镜头:先调用 read_lde 读真实数据再回答。"
    "问成像质量、RMS、焦距:先调用 read_analysis 读真实数值再回答。"
    "带课要学生点界面元素时:调用 point_at 指给他看(name 填元素名),再告诉他下一步。"
    "学生报验收数字:用 read_analysis/read_lde 实测对比,当场判定过/不过、差在哪;"
    "判定通过才调用 finish_lesson 记录进度(note 写一句话讲评)。"
    "用纯文本,不用 markdown,不用 emoji(语音里念不出来)。"
    "没把握的才承认不确定,别编。"
)

_MD_PATTERNS = [
    (re.compile(r"\*\*"), ""),
    (re.compile(r"\*"), ""),
    (re.compile(r"`"), ""),
    (re.compile(r"^#{1,6}\s*", re.M), ""),
    (re.compile(r"^\s*[-•]\s+", re.M), ""),
    # emoji 和变体选择符:TTS 会把它们念成"表情"怪声
    (re.compile(r"[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F\u200D]"), ""),
    # 数学符号换成念得出来的词
    (re.compile(r"[µμ]m"), "微米"),
    (re.compile(r"→"), "到"),
    (re.compile(r"≈"), "约等于"),
    (re.compile(r"±"), "正负"),
    (re.compile(r"×"), "乘以"),
    (re.compile(r"∞"), "无穷大"),
    (re.compile(r"∝"), "正比于"),
]


def _plain(text: str) -> str:
    """剥掉 markdown 符号,给 TTS 一份干净的朗读文本。"""
    for pat, rep in _MD_PATTERNS:
        text = pat.sub(rep, text)
    return text

# 音色:Piper 中文就两个女声,samples\ 里各有一条试听,选好改这里
VOICE = "zh_CN-xiao_ya-medium"

# 语速:.env 里 VOICE_SPEED 调,length_scale 越大越慢(0.85 快 / 1.0 正常 / 1.2 慢)
import dataclasses
import piper.voice as _piper_voice

_speed = float(os.getenv("VOICE_SPEED", "1.0"))
if _speed != 1.0:
    _piper_voice._DEFAULT_SYNTHESIS_CONFIG = dataclasses.replace(
        _piper_voice._DEFAULT_SYNTHESIS_CONFIG, length_scale=_speed
    )
    logger.info("语速 length_scale={}", _speed)


def _pick_output_index() -> int | None:
    """按名字片段固定输出设备(.env 里 AUDIO_OUT_DEVICE=耳机名)。
    多套蓝牙耳机来回切时,系统默认输出不可靠,这里锁死。找不到就返回 None 用系统默认。"""
    want = os.getenv("AUDIO_OUT_DEVICE", "").strip()
    if not want:
        return None
    import pyaudio

    p = pyaudio.PyAudio()
    try:
        for i in range(p.get_device_count()):
            d = p.get_device_info_by_index(i)
            if d["maxOutputChannels"] > 0 and want.lower() in d["name"].lower():
                logger.info("输出设备: [{}] {}", i, d["name"])
                return i
        logger.warning("找不到名字含 '{}' 的输出设备,用系统默认", want)
        return None
    finally:
        p.terminate()


class EdgeTTSService(TTSService):
    """微软 Edge 朗读接口(免费,无 key):边收边解码,非官方接口,失效就 .env 切回 piper。"""

    def __init__(self, *, voice: str, **kwargs):
        super().__init__(
            settings=TTSSettings(model=None, voice=voice, language=Language.ZH),
            **kwargs,
        )
        self._edge_voice = voice

    async def _edge_pcm_stream(self, text: str) -> AsyncGenerator[bytes, None]:
        """从微软接口流式取 mp3,后台线程边收边解码,逐块吐 24k s16 PCM。"""
        import queue
        import threading

        import edge_tts

        text = _plain(text)

        mp3_q: queue.Queue = queue.Queue()
        pcm_q: queue.Queue = queue.Queue()

        class _Stream(io.RawIOBase):
            """av 从这个文件对象读 mp3;没数据就阻塞,直到下一块到达或结束。"""

            def __init__(self, q):
                super().__init__()
                self._q = q
                self._buf = bytearray()
                self._eof = False

            def readable(self):
                return True

            def readinto(self, b):
                # 有多少先给多少(短读),不攒满 —— 攒满会吃掉流式延迟
                while len(self._buf) == 0 and not self._eof:
                    item = self._q.get()
                    if item is None:
                        self._eof = True
                        break
                    self._buf.extend(item)
                n = min(len(b), len(self._buf))
                b[:n] = self._buf[:n]
                del self._buf[:n]
                return n

        def decode():
            try:
                import av

                rs = av.AudioResampler(format="s16", rate=24000, layout="mono")
                for frame in av.open(_Stream(mp3_q), format="mp3").decode(audio=0):
                    for r in rs.resample(frame):
                        pcm_q.put(r.to_ndarray().tobytes())
            except Exception as e:
                pcm_q.put(e)
            finally:
                pcm_q.put(None)

        threading.Thread(target=decode, daemon=True).start()

        try:
            comm = edge_tts.Communicate(text, self._edge_voice)
            async for chunk in comm.stream():
                if chunk["type"] == "audio":
                    mp3_q.put(chunk["data"])
            mp3_q.put(None)
        except Exception as e:
            logger.error(f"Edge TTS fetch failed: {e}")
            mp3_q.put(None)

        loop = asyncio.get_running_loop()
        while True:
            item = await loop.run_in_executor(None, pcm_q.get)
            if item is None:
                return
            if isinstance(item, Exception):
                raise item
            yield item

    async def run_tts(self, text: str, context_id: str | None = None) -> AsyncGenerator[Frame, None]:
        # 她念出来的原句,在这里记录最准(管线里文本帧会被重复投递,不可靠)
        text = _plain(text)
        chat_window.log("助教", text)
        try:
            async for f in self._stream_audio_frames_from_iterator(
                self._edge_pcm_stream(text), in_sample_rate=24000, context_id=context_id
            ):
                yield f
        except Exception as e:
            logger.error(f"Edge TTS exception: {e}")
            yield ErrorFrame(error=str(e))


class ChatLogObserver(BaseObserver):
    """抓学生的语音转写(最终句)投进对话记录小窗。帧会流过多个处理器,按 id 去重。
    助教的话不在这里抓 —— 由 TTS 合成入口记录,那才是她真正念出来的内容。"""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._seen = set()

    async def on_process_frame(self, data: FrameProcessed):
        frame = data.frame
        if frame.id in self._seen:
            return
        self._seen.add(frame.id)
        if len(self._seen) > 500:
            self._seen = set(list(self._seen)[-200:])

        if isinstance(frame, InterimTranscriptionFrame):
            return
        if isinstance(frame, TranscriptionFrame):
            chat_window.log("你", frame.text)


class Greeting(FrameProcessor):
    """看到 StartFrame 触发一次 LLM,让 bot 先开口 —— 第一次跑就能验证全链路通没通。
    GLM 不接受只有 system 的请求,先往上下文写一句用户消息。"""

    def __init__(self, context: LLMContext):
        super().__init__()
        self._context = context
        self._done = False

    async def process_frame(self, frame: object, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, StartFrame) and not self._done:
            self._done = True
            self._context.add_message(
                {"role": "user", "content": "用一句话跟学生打招呼,问准备好上课了吗。"}
            )
            await self.queue_frame(LLMRunFrame())
        await self.push_frame(frame)


async def run_bot(transport) -> None:
    # 启动即连 Zemax 并长驻(后台跑,不挡开场白);ZEMAX_ENABLED=0 时完全跳过
    if os.getenv("ZEMAX_ENABLED", "1") == "1":
        asyncio.create_task(zemax_tools.warm_connect())
    # 听:FunASR SenseVoice,中文专用;实测实时率 0.08,比 whisper medium 快 16 倍且更准
    stt = FunASRSTTService(
        device="cpu",
        settings=FunASRSTTSettings(model="iic/SenseVoiceSmall", language=Language.ZH),
    )
    # 念:三引擎。.env 里 TTS_ENGINE=piper(本地快)/edge(微软免费,播音级)/minimax(云端,要 key)
    engine = os.getenv("TTS_ENGINE", "piper").lower()
    if engine == "edge":
        tts = EdgeTTSService(voice=os.getenv("EDGE_VOICE", "zh-CN-XiaoxiaoNeural"))
    elif engine == "minimax":
        import aiohttp

        session = aiohttp.ClientSession()
        tts = MiniMaxHttpTTSService(
            api_key=os.getenv("MINIMAX_API_KEY"),
            group_id=os.getenv("MINIMAX_GROUP_ID"),
            aiohttp_session=session,
            settings=MiniMaxTTSSettings(
                model="speech-2.8-hd",
                voice=os.getenv("MINIMAX_VOICE", "female-shaonv"),
                language=Language.ZH,
                speed=float(os.getenv("MINIMAX_SPEED", "1.0")),
            ),
        )
    else:
        tts = PiperTTSService(voice_id=VOICE)
    # 脑:智谱 GLM。glm-4.5-air 实测 1.3 秒回答,知识比 glm-4-flash 深;关思考模式(语音等不起长思考)
    llm = OpenAILLMService(
        api_key=os.getenv("GLM_API_KEY"),
        base_url="https://open.bigmodel.cn/api/paas/v4",
        settings=OpenAILLMSettings(
            model=os.getenv("GLM_MODEL", "glm-4.5-air"),
            system_instruction=SYSTEM,
            extra={"extra_body": {"thinking": {"type": "disabled"}}},
        ),
    )

    context = LLMContext(tools=[read_lesson_schema, read_lde_schema, read_analysis_schema, point_at_schema, finish_lesson_schema])
    user_agg, assistant_agg = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            # min_volume 默认 0.6,麦克风阵列增益低,正常说话过不了这条响度线,VAD 一直不触发;
            # 降到 0.35,真正的语音判断仍由 Silero confidence=0.7 把关
            vad_analyzer=SileroVADAnalyzer(params=VADParams(min_volume=0.35)),
            # 开场白期间闭麦:外放时麦克风会收到它自己的声音,不开这个它会被自己打断
            user_mute_strategies=[MuteUntilFirstBotCompleteUserMuteStrategy()],
        ),
    )

    pipeline = Pipeline(
        [
            transport.input(),
            Greeting(context),
            stt,
            user_agg,
            llm,
            tts,
            transport.output(),
            assistant_agg,
        ]
    )

    task = PipelineTask(pipeline, params=PipelineParams(enable_metrics=True), observers=[ChatLogObserver()])
    runner = PipelineRunner()
    await runner.run(task)


def _kill_stale_bots():
    """启动前清掉之前没退干净的 bot 实例 —— 多个实例会抢麦克风和 Zemax 连接位,整机卡死。"""
    import subprocess

    me = os.getpid()
    subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         f"Get-CimInstance Win32_Process -Filter \"Name='python.exe' or Name='python3.13.exe'\" | "
         f"Where-Object {{$_.CommandLine -like '*bot.py*' -and $_.ProcessId -ne {me}}} | "
         f"ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force }}"],
        capture_output=True,
    )


async def main():
    _kill_stale_bots()
    chat_window.start()
    transport = LocalAudioTransport(
        LocalAudioTransportParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            output_device_index=_pick_output_index(),
        )
    )
    await run_bot(transport)


if __name__ == "__main__":
    logger.info("voice-tutor 启动:Ctrl+C 退出")
    asyncio.run(main())
    # 先让位交互扩展,再硬退出 —— 跳过解释器收尾,避开 Tk/COM 的跨线程销毁崩溃
    zemax_tools._disconnect_now()
    os._exit(0)