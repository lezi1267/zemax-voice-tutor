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

## 延迟预算(2026-10 实测,别再凭感觉猜)

从"学生说完"到"听到第一个字"的各段实测值:

| 段 | 实测 | 备注 |
|---|---|---|
| VAD 判定说完 | 0.2s | pipecat `VADParams.stop_secs` 默认就是 0.2(不是 0.8,别再往这儿优化) |
| 语音识别 | 0.50s | 本地直测:4.01s 语音 → 0.50s 出文本(实时率 0.125) |
| LLM 首字 | 0.81s / 1.42s | 不带工具 / 带 22 个工具 schema(3475 tokens,描述占 2535) |
| TTS 首音 | 0.33s | minimax 3 次中位数(piper 0.47s) |
| 文本聚合 | 0.00s | 已改 `TTS_AGGREGATION=token`;原来 sentence 模式要攒够一整句,实测首音 2.89s |

合计约 **1.9s**(纯聊天);要调工具的回答每多一次工具往返 +1.4s 左右(第二次 LLM 调用)+ 工具本身的耗时(Zemax 读数 1s 上下,优化/扫描十几秒)。

## 跑之前

1. 复制 `.env.example` 为 `.env`,填 `GLM_API_KEY`([open.bigmodel.cn](https://open.bigmodel.cn) 注册后在"API Keys"里创建,`glm-4-flash` 免费档不用充值)
2. 麦克风插好(笔记本自带即可),最好戴耳机
3. 仓库不含 `.venv`(1.4 GB 依赖不上传)。先建:`python -m venv .venv`,再装依赖:
   ```cmd
   .venv\Scripts\pip install -r requirements.txt
   :: 国内慢就换清华镜像:.venv\Scripts\pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
   ```
4. 要读数/指路:你自己得有 **Zemax OpticStudio**(2023 R1 实测过),启动前点 **编程 → 交互扩展**。没有 Zemax 也能跑,只是只剩聊天 + 看屏幕
5. 首次要下的东西:依赖 1.4 GB、SenseVoice 识别模型 896 MB、piper 音色 60 MB(用 piper 时再加 g2pW 拼音模型,下载 108 MB,解压后占 152 MB)。都走 hf-mirror,别断网

## 跑

```cmd
cd voice-tutor
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
| 看图报"视觉模型失败 429" | 免费视觉档被挤爆:过一会儿再问(代码已自动换模型);要换主选就改 `.env` 的 `GLM_VISION_MODEL` |
| 拖文件没反应 | 拖到的是对话记录小窗本体(不是输入框);或 PDF 是扫描件 —— 抽不出字时她会喊你截图 |

## 要调的

| 项 | 现值 | 换成 |
|---|---|---|
| LLM 模型 | `GLM_MODEL=glm-4.5-air`(实测 1.3 秒,已关思考) | 免费 `glm-4-flash` 知识浅;更强如 `glm-4.7` 思考慢,语音慎用 |
| TTS 文本聚合 | `TTS_AGGREGATION=token`(边生成边合成,**实测首音 1.36s**) | 改成 `sentence` = pipecat 原生行为,攒够一整句才开口,语调更连贯但**首音 2.89s** |
| TTS 引擎 | `TTS_ENGINE=minimax`(同一句话 3 次首音 0.33s 中位数,音色 `MINIMAX_VOICE`) | 免费本地 `piper` 实测首音 **0.47s**,比 minimax 还慢一点(别再以为本地一定快);`edge` 服务实测 403 挂了;minimax 新式 sk-api key 不用 GroupId,端点 `MINIMAX_BASE_URL=https://api.minimax.cn/v1/t2a_v2` |
| STT 精度 | FunASR SenseVoice(本地直测:4.01s 语音 → 0.50s 出文本,实时率 0.125) | whisper 在本机 CPU 实时率 1.29 不可用;要更快/更准再议 |
| TTS 音色 | `.env` 里 `PIPER_VOICE = "zh_CN-xiao_ya-medium"`(只对 piper) | 换回 `zh_CN-huayan-medium` 即温柔女声;试听在 `data\samples\`;minimax 用 `MINIMAX_VOICE` |
| 语速 | `VOICE_SPEED=0.85`(只对 piper,越大越慢) | minimax 用 `MINIMAX_SPEED` |
| 人设 | `SYSTEM` | 按课改 |

试听音频不在仓库里(各家 TTS 的输出,有版权):`.venv\Scripts\python make_samples.py` 生成 edge 版到 `data\samples\`。

中文可选音色(Piper 就这么多,都是本地免费):
- `zh_CN-xiao_ya-medium` — 年轻女声,更活泼;首次用会自动下 g2pW 拼音模型(108 MB,已改走 hf-mirror),需要 `[zh]` extra 里的 `g2pw` 查表
- `zh_CN-huayan-medium` — 温柔女声
- 想要真正标准/甜的普通话:`TTS_ENGINE=edge`(微软免费,晓晓,试听 data\samples\edge_xiaoxiao.mp3)/ `minimax`(云端付费,音色用 `MINIMAX_VOICE` 挑)


## 它能替你做什么

Zemax 那半边一共 22 个工具。你正常说话就行,不用记工具名。

| 能力 | 你可以说 | 备注 |
|---|---|---|
| 读你正在编辑的镜头 | "我填了什么""帮我检查参数""R2 是变量吗" | 实时读 LDE:每面类型/半径/厚度/材料/半口径、孔径、视场表、波长表,毫秒级 |
| 跑分析拿真实数值 | "RMS 多少""焦距多少""图怎么样" | 点列图 RMS(µm) + RayFan 边缘读数;焦距走 Zemax 自己的 Cardinal Points(反射面、任意玻璃都对) |
| 色差 | "色差多少""消掉了吗""这对玻璃行不行" | 轴向色差_mm + 倍率色差_um;只有一个波长时如实说"测不出来",不把 0 当消掉了 |
| MTF | "MTF 多少""能分辨多少线对" | FftMtf 每个视场最接近 30 / 50 lp/mm 的子午弧矢值 + 截止频率;取样太低会带出 Zemax 的警告 |
| 像差系数 / 场曲畸变 | "球差多大""什么像差最狠""场曲多少" | 赛德尔系数、场曲畸变。注意:赛德尔的两条 API 路(SeidelCoefficients / SeidelDiagram 的 ToFile)实测都返回空,别再试 —— 学生开图 + 读屏是现行路径 |
| 读评价函数 | "MF 多少""我有几个变量" | 含真实操作数列表 |
| 跑任意一个分析 | "跑一下波前""看渐晕""公差直方图怎么样" | ZOS-API 156 个分析里没写专用解析器的长尾(波前、包围能量、焦点位移、相对照度、泽尼克……),按中文名跑,原始文本直接给模型 |
| **替你动手改镜头** | "帮我把 R2 改成 63.7""加个波长 0.4861""把半径设成变量" | 插/删面、半径、厚度、材料、半口径、注释、设变量 |
| **撤销** | "改坏了""恢复原样" | 每次动手前自动备份到 `data\backups\`,说一句就拉回来 |
| **替你跑优化 / 扫参数 / 对焦** | "你帮我优化""扫一遍半径找最好的""图像糊,调一下像面" | 优化先建默认评价函数;没有 EFFL 之类约束时焦距会漂,她会主动提醒 |
| 屏幕上指给你点哪里 | (带课时自动触发) | 小幽灵滑过去指位置,pywinauto UIA 精确坐标 |
| OCR 读屏 | "屏幕上写的什么" | RapidOCR 离线读,补 API 和 UIA 的盲区(图上标注、报错弹窗) |
| 看分析图的形状和趋势 | "这个点列图是什么形状""MTF 曲线怎么往下掉" | 截屏(可裁剪)交给智谱视觉模型 `glm-4.6v-flash`(挤爆自动退 `glm-4v-flash`),同一个 GLM key,不新增账号 |
| 让她直接看像素 | `.env` 里 `MAIN_VISION=1` + `GLM_MODEL=glm-4.6v-flash` | 截图作为带图消息挂进对话,她看的是图本身,不是视觉模型的转述。不开就是默认(glm-4.5-air 没视觉,启动会警告) |
| 拖文件让她学 | 把课件/手册 PDF/教材/截图拖进对话窗,或丢进 `data\knowledge\收件箱\` | 抽文本 → 切块 → jieba 词重叠×IDF + `embedding-2` 余弦两路检索;向量量化成 int8(文件 3 倍小、检索内存 7 倍小)。引原文会说出处,没命中就如实说"没讲到"。支持 文本/md/docx/pdf/图片;扫描件 PDF 抽不出字时会喊你截图 |
| 带课 + 记进度 + 断点续教 | "开始第 1 课""今天学什么""给我一份学习报告" | 10 课课件已录入;验收数字对上了才记进度,重启接着上次那步教,隔几天先提议复习 |

**前提**:Zemax 开着 + 点过 **编程 → 交互扩展**(连接后小窗自动隐藏、主窗口自动解锁)。连接被拒时她会自己再点一次重连(实测 3 秒内)。中途重开小窗会再次锁人,下一次工具调用前会自动再解锁。
