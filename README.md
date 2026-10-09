# 光学设计助教 · voice-tutor

会说话、能听懂、还能直接读你 Zemax 界面的**语音助教**,配 10 课 Zemax OpticStudio 光学设计课件。
你对它开口,它听懂、回答、念出来,可以打断;带课时它实时读你的镜头数据、跑分析拿真实数值、
在屏幕上指给你点哪里。

An English-speaking Zemax OpticStudio voice tutor with a 10-lesson curriculum. It listens, answers,
reads your live lens data through ZOS-API, runs analyses for real numbers, and points at the UI for you.

## 30 秒上手

```cmd
cd voice-tutor
copy .env.example .env      :: 填 GLM_API_KEY(open.bigmodel.cn 注册,免费档够用)
.venv\Scripts\python bot.py
```

听到它开口说话 = 全链路通。之后你对它说"开始第 1 课"。
要读数/指路功能:先开 Zemax,点 **编程 → 交互扩展**(bot 会自动连上并驯服那个锁人的小窗)。
没开 Zemax 也能纯聊天。

完整说明、音色选择、常见坑:[voice-tutor/README.md](voice-tutor/README.md)。
想桌面双击启动:`pwsh voice-tutor/install_shortcut.ps1` 建桌面快捷方式(控制台自动最小化)。
想挑音色:`voice-tutor\.venv\Scripts\python voice-tutor\make_samples.py` 生成试听音频到 `voice-tutor\samples\`。

## 它能做什么

| 能力 | 怎么说 |
|---|---|
| 按课件带课 | "开始第 N 课" |
| 读你正在编辑的镜头 | "我填了什么""帮我检查参数" |
| 跑分析拿真实数值 | "RMS 多少""焦距多少""图怎么样" |
| 屏幕上指给你点哪里 | 带课时自动触发(小幽灵滑过去) |
| OCR 读 API 读不到的内容 | "屏幕上/图上写的什么" |
| 验收 + 记进度 + 断点续教 | 数字对上才记;重启后接着上次那步教 |

链路:麦克风 → FunASR SenseVoice(本地识别) → 智谱 GLM(LLM) → TTS(piper / edge / minimax) → 扬声器。
你的录音不出本机,只有转好的文字上云。

## 目录

| 路径 | 是什么 |
|---|---|
| [voice-tutor/](voice-tutor/) | 主程序:语音管线 + 7 个 Zemax/课程工具 |
| [学习路线.md](学习路线.md) | 10 课总纲 |
| 第1课…第10课_*.md | 课件(bot 直接读这些文件带课) |
| [zemax_api.py](zemax_api.py) | ZOS-API 连接入口(extension / standalone) |
| [guide.py](guide.py) | 屏幕小幽灵指引(画光圈 + 气泡) |
| [dialog_fix.py](dialog_fix.py) | 驯服交互扩展小窗:解锁被它锁住的所有窗口 |
| [shot.py](shot.py) | DPI 感知全屏截屏 |
| [selftest.py](voice-tutor/selftest.py) | 全系统自检(Zemax 没开的项自动 SKIP) |

## 跑自检

```cmd
cd voice-tutor
.venv\Scripts\python check_names.py   :: 静态查 NameError(漏 import 这类)
.venv\Scripts\python selftest.py      :: 逐项 PASS/FAIL/SKIP,不碰你的学习档案
```

## 环境

Windows + Python 3.13。依赖见 [voice-tutor/requirements.txt](voice-tutor/requirements.txt)
(已实测锁版本)。Zemax OpticStudio 2023 R1 实测可用,ZOS-API 走 [ZOSPy](https://github.com/MREYE-LUMC/ZOSPy)。

STT/TTS 都在本地或你自己的云端账号上跑;LLM 和 minimax TTS 需要你自己的 API key,仓库里不含任何 key。

## 已知边界

- 本地外放没有回声消除:建议戴耳机,否则它会被自己的声音打断。
- OCR 只认字,不识图形几何(点列图形状它"看不懂")。
- 交互扩展一次只认一个连接:bot 读完会立刻让位,但同一时刻别再开第二个 API 客户端。

## License

MIT — 见 [LICENSE](LICENSE)。Zemax / OpticStudio 是 Ansys 的商标,本项目与 Ansys 无关,
需要你自己持有合法的 OpticStudio 授权。
