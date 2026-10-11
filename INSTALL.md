# 装起来（Windows）

只支持 **Windows + Python 3.13**。代码用了 pywin32 / pywinauto / comtypes / tkinterdnd2，Linux 和 macOS 跑不了。

## 1. Python 3.13

```cmd
winget install -e --id Python.Python.3.13
python --version      :: 必须是 3.13.x,3.12 和 3.14 都没锁版本
```

## 2. 建环境 + 装依赖

```cmd
cd voice-tutor
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
:: 国内慢就换清华镜像
.venv\Scripts\pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
```

约 1.4 GB。装失败常见两个：

| 卡在哪 | 怎么办 |
|---|---|
| `PyAudio` 要编译 | 说明 pip 没找到 3.13 的 wheel:先 `pip install --upgrade pip`,再试;还不行退回默认源(不带 `-i`) |
| `tkinterdnd2` 报错 | 确认用的是官方 python(不是 Microsoft Store 版 python) |

## 3. 填 key

```cmd
copy .env.example .env
```

`.env` 里必填 `GLM_API_KEY`(去 [open.bigmodel.cn](https://open.bigmodel.cn) 注册,API Keys 里创建,免费档够用)。
不填 minimax 也能跑:`TTS_ENGINE` 换成 `edge`(微软免费,无需 key)或 `piper`(本地免费)。
`.env` 已被 gitignore,别提交上去。

## 4. 跑

```cmd
.venv\Scripts\python bot.py
```

第一次会下载模型,别断网:

| 东西 | 大小 | 存哪 |
|---|---|---|
| SenseVoice 识别模型 | 896 MB | `~\.cache\modelscope\` |
| piper 中文音色(用 piper 时) | 60 MB | `~\.cache\pipecat\piper\` |
| g2pW 拼音模型(用 piper 时) | 108 MB | 音色目录下的 `g2pW\` |

模型默认走 hf-mirror(`bot.py` 第 20 行写死)。你在国外反而下不动,把那行的域名换成 `https://huggingface.co` 即可。

听到它开口说一句 = 全链路通。

## 5. 要它读你的 Zemax

- 你自己得有合法的 **Zemax OpticStudio** 授权(本项目与 Ansys 无关,不提供任何授权)
- 打开 OpticStudio,点 **编程 → 交互扩展**,弹出的小窗留着别关
- 实测版本 2023 R1
- 没有 Zemax 也能用:`.env` 里 `ZEMAX_ENABLED=0`,只剩语音聊天 + 看屏幕,其余功能照常

## 6. 验一遍

```cmd
.venv\Scripts\python check_names.py     :: 静态检查,应该"全部通过"
.venv\Scripts\python selftest.py        :: 逐项 PASS/FAIL/SKIP;Zemax 没开的项自动 SKIP
```

## 7. 最常卡的 4 条

| 症状 | 原因 |
|---|---|
| 她不开口 | `.env` 没填 `GLM_API_KEY`,或输出设备不对(填 `AUDIO_OUT_DEVICE=你耳机名字片段`) |
| 她听不见你 | 麦克风没被系统识别,或说话太轻 |
| 她说到一半自己打断自己 | 外放没有回声消除 —— **戴耳机** |
| 读数报"读取超时" | 交互扩展小窗被关了:重开 编程 → 交互扩展 |

更多见 [voice-tutor/README.md](voice-tutor/README.md)。
