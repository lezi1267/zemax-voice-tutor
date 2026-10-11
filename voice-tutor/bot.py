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

# piper 中文音色要 g2pW 拼音模型(152 MB),它的 download_model 也是 urllib 直连 huggingface.co
import piper.phonemize_chinese as _piper_zh

_piper_zh.G2PW_URL = _piper_zh.G2PW_URL.replace("huggingface.co", "hf-mirror.com")

# g2pW(拼音音素)用的 transformers 启动时会探测 huggingface.co 并重试 5 次,让它离线走本地缓存
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

from pipecat.frames.frames import (
    ErrorFrame,
    Frame,
    InterimTranscriptionFrame,
    InterruptionFrame,
    LLMRunFrame,
    StartFrame,
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
import knowledge
import lessons
import vision
import zemax_tools

load_dotenv(override=True)

SYSTEM = (
    "你是 Zemax OpticStudio 光学设计助教,学生是初学者,回答都会念出来。"
    "带课铁律(防循环):一节课的课件只在学生说'开始第N课'时取一次;"
    "之后每轮从对话记录判断学生做到第几步,直接给下一步,"
    "永远不重复已经给过的步骤、不重新念课件、不说'我们开始第N课'。"
    "课件里'改成XX'类的步骤:先用 read_lde 看当前值——已经是目标值就明确说'这步已经是XX,跳过',"
    "并立刻给下一步。学生说'改了/好了':先用 read_lde 实测确认,确认后才说'第X步完成'"
    "并给下一步(报第几步)。"
    "学生说开始上课/问课程安排:调用 read_lesson 拿课件,然后按上面铁律带课。"
    "要操作步骤:一次只给一个操作指令。"
    "问原理:直接说重点,两句话以内,可以打一个比方。"
    "问成像质量、RMS、焦距:先调用 read_analysis 读真实数值再回答。"
    "问 MTF、分辨率、能分辨多少线对:调用 read_mtf 拿实测值,MTF 数值没有单位就是 0~1 的模值。"
    "问色差、消色差、玻璃配对(第4课):调用 read_color 拿轴向色差_mm 和倍率色差_um;只有一个波长时色差必然是 0,先让学生加波长。"
    "课件要设视场/波长或把某个面设为变量:read_lde 返回里有 视场、波长、半径是变量、厚度是变量,用它核对。"
    "带课要学生点界面元素时:调用 point_at 指给他看,再告诉他下一步。"
    "学生报验收数字:用 read_analysis/read_lde 实测对比,当场判定过/不过、差在哪;"
    "判定通过才调用 finish_lesson 记录进度(note 写一句话讲评)。"    "每次下达新的操作步骤后调用 save_step 记断点(step 一句话复述该步),学生重启程序后能接着学;"
    "学生说'继续/接着上次的':调 read_lesson(课号填档案当前课),档案里'断点'还在就从那步的下一步直接带,不重念课件、不重复已给步骤。"
    "EFFL 之类的数:read_analysis 返回里有 EFL_mm(Zemax Cardinal Points 实测,带符号),"
    "用实测值,不要自己估。返回 EFL_error 就说明这个系统现在没有光焦度或分析没跑成,"
    "如实告诉学生,别编一个焦距。"
    "API 读不到的屏幕内容(赛德尔系数图、图上标注、弹窗报错):调用 read_screen 读屏,读到的字直接用。"
    "带课时注意:学生系统的实际状态(孔径/像面位置)可能和课件前提不同,"
    "先用 read_lde 读实际值,按实际状态调整教学(比如课件说 EPD20 而实际是 10,就按 10 讲)。"
    "学生对图形读数报的数字:先对照单位和理论方向检查,明显不合理(如 Ray Fan 边缘 215mm)"
    "就追问单位和读法,别照单全收;实测和理论方向矛盾时,先怀疑读数,陪学生一起排查再下结论。"
    "问球差/彗差/像散/场曲/畸变是什么在起作用:调用 read_aberration 拿赛德尔系数(S1~S5+色差)和场曲畸变实测值。"
    "问评价函数、有几个变量:调用 read_merit。"
    "学生让你动手改镜头(改半径/厚度/材料、设变量、加波长、加视场、改孔径):调用 edit_zemax,"
    "它改之前自动备份,返回的'改完之后'是实际读回的值 —— 照这个报,别只说'改好了'。"
    "课件里要求他自己操作的步骤不要代做,只有他明确让你改才改。"
    "学生说改坏了/撤销/恢复原样:调用 restore_lens 拉回最近备份。"
    "学生让你跑优化:调用 run_optimize(评价函数是空的它会自动建一套),报优化前后的评价函数值。"
    "学生让你找最佳参数(如'扫一遍半径'):调用 scan_param,它扫完会恢复原值,报最好的那个点。"
    "对话小窗底部有输入框:学生不想说话或环境吵就打字提问;按 Esc 打断她念话。"
    "学生要学习总结/报告:调用 write_report 写成文件,告诉他文件在哪,别整份念。"
    "专用读数工具没覆盖的分析(波前、包围能量、焦点位移、渐晕、相对照度、公差直方图、泽尼克…):"
    "调用 run_analysis,name 填分析名,数字在它返回的原始文本里,自己挑,单位看表头。"
    "要看图本身(点列图是什么形状、MTF 曲线怎么往下掉、弹窗长什么样):调用 look_screen,"
    "question 写清要看什么;要细看某一块就带 read_screen/point_at 给过的 x/y/w/h。"
    "要精确数字优先 API 读数,看图是用来理解形状的。"
    "学生拖文件进对话小窗、说'我拖了个资料/我拖进收件箱了'、或给你文件路径:调用 learn_file 学会它,"
    "学完用一句话告诉他你学到了什么(字数、关键词)。只说拖了没给路径:调 list_knowledge,它会顺手扫收件箱。"
    "学生问'你学过哪些资料':调 list_knowledge。要引资料原文讲课、或学生问'那份资料里怎么说':"
    "先调 ask_knowledge 再回答,并说出出自哪份资料;没命中就如实说,别编。"
    "用纯文本,不用 markdown,不用 emoji。没把握的才承认不确定,别编。"
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

# 音色:Piper 中文就两个女声,samples\ 里各有一条试听,选好改 .env 里的 PIPER_VOICE
VOICE = os.getenv("PIPER_VOICE", "zh_CN-xiao_ya-medium")

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

    def __init__(self, *, voice: str, speed: float = 1.0, **kwargs):
        super().__init__(
            settings=TTSSettings(model=None, voice=voice, language=Language.ZH),
            **kwargs,
        )
        self._edge_voice = voice
        # VOICE_SPEED 是 piper 的 length_scale(越大越慢)。微软那边是百分比 rate:
        # 0.7 → +30%(快),1.2 → -20%(慢)。不换算就是设了没效果。
        self._edge_rate = f"{round((1.0 - speed) * 100):+d}%"

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
            comm = edge_tts.Communicate(text, self._edge_voice, rate=self._edge_rate)
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


class _TTSPolish:
    """给 TTS 兜两层底:先擦掉 markdown/emoji 再合成,并把真正念出来的句子记进对话小窗。

    以前只有 edge 这条路做这两件事 —— minimax/piper 会把 emoji 念成"表情"怪声,
    而且小窗里只有学生的话,看不到助教说了什么。"""

    async def run_tts(self, text: str, context_id: str) -> AsyncGenerator[Frame, None]:
        text = _plain(text)
        chat_window.log("助教", text)
        async for f in super().run_tts(text, context_id):
            yield f


class PolishedPiper(_TTSPolish, PiperTTSService):
    pass


class PolishedMiniMax(_TTSPolish, MiniMaxHttpTTSService):
    pass


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


def _greeting_prompt() -> str:
    """开场白指令:有没验收完的断点就提醒续教;隔了几天顺带提议先复习。"""
    prog = lessons.load_progress()
    cp = prog.get("checkpoint") or {}
    if cp.get("lesson") and cp["lesson"] not in prog.get("done", []):
        return (
            f"打招呼,说你记得他上次进行到第{cp['lesson']}课'{cp.get('step', '')}',"
            f"问他要不要从这里继续;他说继续就调 read_lesson 后从这步的下一步直接带,不重念课件。"
        )
    days = lessons._days_since_last(prog)
    if days and days >= 3:
        return (
            f"打招呼,点出他隔了约{int(days)}天没来,提议先花两分钟复习上次第"
            f"{prog.get('current', 0)}课的要点,再问他今天学什么。"
        )
    return "用一句话跟学生打招呼,问准备好上课了吗。"


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
            self._context.add_message({"role": "user", "content": _greeting_prompt()})
            await self.queue_frame(LLMRunFrame())
        await self.push_frame(frame)


async def run_bot(transport) -> None:
    # 启动即连 Zemax 并长驻(后台跑,不挡开场白);ZEMAX_ENABLED=0 时完全跳过
    if os.getenv("ZEMAX_ENABLED", "1") == "1":
        if _zemax_running():
            asyncio.create_task(zemax_tools.warm_connect())
        else:
            chat_window.log("系统", "提示:Zemax 没开——读数功能暂不可用。开 Zemax 并点 编程→交互扩展 后,问参数时会自动连上。")
    # 听:FunASR SenseVoice,中文专用;实测实时率 0.08,比 whisper medium 快 16 倍且更准
    stt = FunASRSTTService(
        device="cpu",
        settings=FunASRSTTSettings(model="iic/SenseVoiceSmall", language=Language.ZH),
    )
    # 念:三引擎。.env 里 TTS_ENGINE=piper(本地快)/edge(微软免费,播音级)/minimax(云端,要 key)
    engine = os.getenv("TTS_ENGINE", "piper").lower()
    if engine == "minimax" and not os.getenv("MINIMAX_API_KEY"):
        # 新克隆默认就是 minimax,没 key 会一路静默没声音 —— 直接退到免费的 edge
        chat_window.log("系统", "TTS_ENGINE=minimax 但 .env 没配 MINIMAX_API_KEY:自动改用免费的 edge。")
        engine = "edge"
    if engine == "edge":
        tts = EdgeTTSService(
            voice=os.getenv("EDGE_VOICE", "zh-CN-XiaoyiNeural"),
            speed=float(os.getenv("VOICE_SPEED", "1.0")),
        )
    elif engine == "minimax":
        import aiohttp

        # 新式 sk-api key 自带分组,GroupId 不填就完全不带参数(带了反而 token not match group)
        group_id = os.getenv("MINIMAX_GROUP_ID", "").strip()
        base_url = os.getenv("MINIMAX_BASE_URL", "https://api.minimaxi.com/v1/t2a_v2")
        session = aiohttp.ClientSession()
        tts = PolishedMiniMax(
            api_key=os.getenv("MINIMAX_API_KEY"),
            group_id=group_id,
            # 国内开放平台是 minimaxi.com(多个 i);pipecat 默认的国际站才是 minimax.io
            base_url=base_url,
            aiohttp_session=session,
            settings=MiniMaxTTSSettings(
                model=os.getenv("MINIMAX_MODEL", "speech-2.8-turbo"),
                voice=os.getenv("MINIMAX_VOICE", "female-shaonv"),
                language=Language.ZH,
                speed=float(os.getenv("MINIMAX_SPEED", "1.0")),
            ),
        )
        if not group_id:
            # pipecat 会无条件拼 ?GroupId=,实测空参数会被服务端拒;新 key 不需要这个参数
            tts._base_url = base_url
    else:
        tts = PolishedPiper(voice_id=VOICE)
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

    context = LLMContext(tools=[*zemax_tools.TOOLS, *lessons.TOOLS, *knowledge.TOOLS])
    vision.attach_context(context)  # 截图要往这里挂
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
            vision.ScreenVision(),  # MAIN_VISION 开着时把截图挂进对话;关着就是个直通
            llm,
            tts,
            transport.output(),
            assistant_agg,
        ]
    )

    task = PipelineTask(pipeline, params=PipelineParams(enable_metrics=True), observers=[ChatLogObserver()])

    # 对话小窗的输入框:打字提问走的是和开场白同一条路(写进上下文 + LLMRunFrame),
    # 不假装有麦克风;Esc 打断 = 直接投 InterruptionFrame,和语音打断同一个机制。
    loop = asyncio.get_running_loop()

    def _type_in(text):
        chat_window.log("你", text)

        def go():
            context.add_message({"role": "user", "content": text})
            asyncio.ensure_future(task.queue_frame(LLMRunFrame()))

        loop.call_soon_threadsafe(go)

    def _interrupt():
        chat_window.log("系统", "(你按 Esc 打断了她)")
        loop.call_soon_threadsafe(lambda: asyncio.ensure_future(task.queue_frame(InterruptionFrame())))

    chat_window.set_handlers(_type_in, _interrupt)

    runner = PipelineRunner()
    await runner.run(task)


def _kill_stale_bots():
    """启动前清掉之前没退干净的 bot 实例 —— 多个实例会抢麦克风和 Zemax 连接位,整机卡死。
    注意:商店版 Python 的 venv 是"启动器+真身"两个进程,os.getpid() 是真身;
    排除时必须把启动器(os.getppid())也排除,否则清理脚本会把自己杀了。"""
    import subprocess

    me, parent = os.getpid(), os.getppid()
    # 只杀"这个目录里的 bot.py":匹配 '*bot.py*' 会顺手干掉别人项目里的同名脚本
    here = str(Path(__file__).resolve())
    ps = (
        f"Get-CimInstance Win32_Process -Filter \"Name='python.exe' or Name='python3.13.exe'\" | "
        f"Where-Object {{$_.CommandLine -like '*{here}*' -and $_.ProcessId -ne {me} "
        f"-and $_.ProcessId -ne {parent}}} | "
        f"ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force; 'killed ' + $_.ProcessId }}"
    )
    out = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True)
    killed = out.stdout.decode(errors="replace").strip()
    if killed:
        logger.info("已清理残留实例: {}", killed)


def _zemax_running() -> bool:
    try:
        import zemax_api

        return zemax_api._zemax_running()
    except Exception:
        return False


def _startup_checks():
    """启动自检:硬伤在开场前就喊出来,别等学生开口才发现。"""
    if not os.getenv("GLM_API_KEY"):
        chat_window.log("系统", "警告:.env 里没配 GLM_API_KEY,她答不了话。去 open.bigmodel.cn 申请,填进 voice-tutor\\.env 后重启。")
    if vision.MAIN_VISION and "v" not in os.getenv("GLM_MODEL", "").replace("glm-", ""):
        chat_window.log("系统", "警告:MAIN_VISION=1 但 GLM_MODEL 不带视觉。换成 GLM_MODEL=glm-4.6v-flash(免费)或 glm-4.6v,否则屏幕图会被 API 拒收。")
    try:
        import pyaudio

        p = pyaudio.PyAudio()
        mics = sum(1 for i in range(p.get_device_count())
                   if p.get_device_info_by_index(i)["maxInputChannels"] > 0)
        p.terminate()
        if not mics:
            chat_window.log("系统", "警告:没找到麦克风,她听不见你说话。插上麦克风/耳机后重启。")
    except Exception as e:
        chat_window.log("系统", f"警告:麦克风检测失败({e}),语音输入可能不可用。")


async def main():
    _kill_stale_bots()
    chat_window.start()
    _startup_checks()
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