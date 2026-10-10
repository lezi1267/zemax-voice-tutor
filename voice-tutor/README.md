# voice-tutor

全双工语音助教:你开口说话 → 它听懂 → 智谱 GLM 回 → 念出来。可打断。

## 把它当软件用(三步)

1. 桌面双击 **光学设计助教** 快捷方式(控制台自动最小化,对话窗弹出)
2. 要用读数/指路功能时:先开 Zemax,点 **编程 → 交互扩展**(小窗弹出后 bot 会自动连上并解锁界面;没开 Zemax 也能纯聊天)
3. 对它说话:"开始第 N 课" / "我在 LDE 里填了什么" / "RMS 多少"

配置都在 `voice-tutor\.env`(音色/语速/引擎),改完重启生效。

## 链路

```
麦克风 → FunASR SenseVoice(本地识别) → 智谱 GLM(LLM,云端) → 本地 Piper(TTS) → 扬声器
                你的录音不出机器           ↑只发转好的文字
```

打断(barge-in)、断句 —— Pipecat 内置,不用手搓。但**本地外放没有回声消除**:建议戴耳机,否则它说出来的话会被麦克风收回去,像"自己打断自己"(开场白做了闭麦保护,后面几轮仍会这样)。

## 跑之前

1. 复制 `.env.example` 为 `.env`,填 `GLM_API_KEY`([open.bigmodel.cn](https://open.bigmodel.cn) 注册后在"API Keys"里创建,`glm-4-flash` 免费档不用充值)
2. 麦克风插好(笔记本自带即可),最好戴耳机
3. `.venv` 已经建好;万一要重建:`python -m venv .venv` 然后 `.venv\Scripts\pip install -r requirements.txt`

## 跑

```cmd
cd D:\ASDF\aas\zemax\学习游戏\voice-tutor
.venv\Scripts\python bot.py
```

第一次跑会下载 SenseVoice 识别模型和 piper 中文音色(走 HF 镜像),等几分钟。
之后秒起。

## 听到什么算通

启动后 bot 会先开口说一句(开场问候)。听到它说话 = 全链路通。
然后你对它说话,它回答。

## 常见故障

| 症状 | 原因 / 怎么办 |
|---|---|
| 她不开口 | `.env` 没配 `GLM_API_KEY`(启动会警告)/ 输出设备不对(填 `AUDIO_OUT_DEVICE`)/ 耳机没插 |
| 她听不见你 | 麦克风没被识别(启动会警告)/ 说话太轻(VAD 响度线已降到 0.35)/ 外放时她会被自己的声音打断 → 戴耳机 |
| 读数报"读取超时" | 交互扩展小窗被关了:重开 编程→交互扩展,bot 会自己点一次重连(实测 3 秒内) |
| 报"连接位已被占用" | 别的 API 客户端或残留 bot 进程占着:关掉再试;bot 启动时会自动清掉本目录的残留实例 |
| 第一次跑卡在下载 | SenseVoice 模型和 piper 音色走 HF 镜像,慢但会成,别断网 |

## 要调的

| 项 | 现值 | 换成 |
|---|---|---|
| LLM 模型 | `GLM_MODEL=glm-4.5-air`(实测 1.3 秒,已关思考) | 免费 `glm-4-flash` 知识浅;更强如 `glm-4.7` 思考慢,语音慎用 |
| TTS 引擎 | `TTS_ENGINE=minimax`(实测首包 0.7s,音色 `MINIMAX_VOICE`) | 免费本地用 `piper`;微软免费播音级用 `edge`(首包 2~3s);minimax 新式 sk-api key 不用 GroupId,端点 `MINIMAX_BASE_URL=https://api.minimax.cn/v1/t2a_v2` |
| STT 精度 | FunASR SenseVoice(实时率 0.08,中文识别全对) | whisper 在本机 CPU 实时率 1.29 不可用;要更快/更准再议 |
| TTS 音色 | `.env` 里 `PIPER_VOICE = "zh_CN-xiao_ya-medium"`(只对 piper) | 换回 `zh_CN-huayan-medium` 即温柔女声;试听在 `samples\`;minimax 用 `MINIMAX_VOICE` |
| 语速 | `VOICE_SPEED=0.85`(只对 piper,越大越慢) | minimax 用 `MINIMAX_SPEED` |
| 人设 | `SYSTEM` | 按课改 |

试听音频不在仓库里(各家 TTS 的输出,有版权):`.venv\Scripts\python make_samples.py` 生成 edge 版到 `samples\`。

中文可选音色(Piper 就这么多,都是本地免费):
- `zh_CN-xiao_ya-medium` — 年轻女声,更活泼(**现用**);需要 g2pW 拼音模型(已下到本目录),重建环境时装 `piper-tts[zh]`
- `zh_CN-huayan-medium` — 温柔女声
- 想要真正标准/甜的普通话:`TTS_ENGINE=edge`(微软免费,晓晓,试听 samples\edge_xiaoxiao.wav)/ `minimax`(云端付费,音色用 `MINIMAX_VOICE` 挑)

## 下一步(接 Zemax)

- **已完成:read_lde** —— 问"我填了什么/帮我检查参数/设成变量了吗",实时读 LDE(含每面半径/厚度是否为变量)、
  孔径、视场表、波长表(毫秒级)
- **已完成:read_color** —— 问"色差多少/消掉了吗/这对玻璃行不行"(第 4 课):轴向色差_mm(各波长焦点差)+ 倍率色差_um;
  只有一个波长时会如实说"色差测不出来",不会把 0 当成消掉了
- **已完成:read_mtf** —— 问"MTF 多少/能分辨多少线对"(第 7 课):跑 FftMtf,报每个视场最接近 30 和 50 lp/mm 的
  子午/弧矢 MTF 和 MTF 掉到 0.05 以下的截止频率;Zemax 提示"取样太低"会如实带出来
- **已完成:read_analysis** —— 问"图怎么样/RMS 多少",跑点列图报 RMS(µm)、RayFan 边缘读数,
  焦距用 Zemax 自己的 Cardinal Points 分析取实测 EFL(反射面、任意玻璃都对)
- **已完成:point_at** —— 带课要你点界面时,小幽灵滑过去指位置(pywinauto UIA,精确坐标)
- **已完成:read_screen(OCR 读屏)** —— API 和 UIA 读不到的内容(赛德尔系数图、图上标注、
  报错弹窗)用 RapidOCR 离线读屏;问她"屏幕上写的什么"就行。
  赛德尔系数的两条 API 路(SeidelCoefficients/SeidelDiagram 的 ToFile)2026-10-07 实测均返回空,
  别再试 —— 学生开图 + 读屏就是现行路径
- **已完成:交互扩展断开自动恢复** —— 连接被拒时自动点一次 编程→交互扩展 再重连(实测 3 秒内恢复),
  不用再手动救;中途重开小窗会再次锁人,下一次工具调用前会自动再解锁
- **已完成:read_lesson + finish_lesson + 学习档案(progress.json)** —— 全部 10 课课件已录入,
  说"开始第N课"按课件带课;验收数字对上了才记进度,跨次学习不失忆
- **已完成:断点续教** —— 每个操作步骤实时记入学习档案,重启后她会接着上次的步骤教,
  还会主动问要不要继续;隔了几天会先提议复习
- 前提:Zemax 开着 + 点过 编程→交互扩展(连接后小窗自动隐藏,主窗口自动解锁;
  中途重开小窗会再次锁人,下一次工具调用前会自动再解锁)
- 待做:"真看图"(把图给视觉模型理解几何形状;现在 read_screen 的 OCR 只认字,不识图)
