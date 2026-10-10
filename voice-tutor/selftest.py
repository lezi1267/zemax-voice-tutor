# -*- coding: utf-8 -*-
"""全系统自检:逐个子系统实测并打 PASS/FAIL/SKIP。
用法: python selftest.py
需要 Zemax 参与的项,在 Zemax 没开/交互扩展没开时自动 SKIP。"""
import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

# 中文 Windows 上 stdout 一旦被重定向/管道就走 GBK,遇到 ³ ≈ 这类字符直接 UnicodeEncodeError 崩掉。
for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

results = []


def report(name, ok, note=""):
    tag = {True: "PASS", False: "FAIL", None: "SKIP"}[ok]
    results.append((tag, name, note))
    print(f"[{tag}] {name}" + (f" —— {note}" if note else ""), flush=True)


async def main():
    import bot
    import zemax_tools
    import lessons

    # 学习档案沙盒:自检绝不碰真实的 progress.json。
    # (以前它把第 2 课写进"已完成"、往日志里塞自检测试,跑一次就把学生档案弄脏了。)
    real_progress = lessons.PROGRESS
    real_before = real_progress.read_bytes() if real_progress.exists() else None
    sandbox_dir = Path(tempfile.mkdtemp())
    lessons.PROGRESS = sandbox_dir / "progress.json"
    if real_progress.exists():
        shutil.copy(real_progress, lessons.PROGRESS)

    report("模块导入 + 静态名字检查", True, "check_names 由调用方先行执行")

    # 1) 文本清洗
    dirty = "好的😊 **RMS**≈50μm\n- 注意 ×2 ∞ ∝口径³ `code`"
    clean = bot._plain(dirty)
    bad = [ch for ch in clean if ch in "*#`" or ord(ch) >= 0x1F000]
    report("TTS 文本清洗(emoji/markdown/数学符号)", not bad and "微米" in clean and "约等于" in clean, clean[:40])

    # 1b) Cardinal Points 文本解析(不依赖 Zemax:中英文两版样本 + 报错版)
    sample = ("Cardinal Points\n\nStarting Surface:  1\nEnding Surface:  4\nWavelength:  1\n"
              "Orientation:  Y-Z\nLens units:  mm\n\n"
              "                  Object Space   Image Space\n"
              "Focal Length:     -108.031       108.031\n"
              "Principal Planes: -108.031       108.031\n")
    zh = ("基点数据概要\n\n                                \t物空间           \t像空间\n"
          "焦长                  :\t        -192.855704\t         192.855704\n"
          "主平面                :\t           0.000000\t          -2.634140\n")
    efl = zemax_tools._parse_cardinal(sample).get("EFL_mm")
    neg = zemax_tools._parse_cardinal("Focal Length:     50.000       -50.000\n").get("EFL_mm")
    zh_efl = zemax_tools._parse_cardinal(zh).get("EFL_mm")
    none = zemax_tools._parse_cardinal("Cardinal Points\nFocal Lengths:\n")
    err = zemax_tools._parse_cardinal("基点数据概要\n\n基点计算错误。\n")
    report("Cardinal Points 解析(中英/负系统/无数据/基点计算错误)",
           efl == 108.031 and neg == -50.0 and zh_efl == 192.855704
           and "EFL_error" in none and "没有光焦度" in err.get("EFL_error", ""),
           f"EN={efl} 中={zh_efl} 负={neg}")

    # 1c) 分析文本解析(合成样本,不依赖 Zemax)
    # 多视场时每个视场一行 RMS:必须取最差的那行,不是最后一行
    vals = zemax_tools._parse_values(
        "RMS Radius: 0.0123\n没有冒号的行\nRMS光斑半径: 0.5\nRMS光斑半径: 0.9\nRMS Radius: 不是数\n",
        ["RMS Radius", "RMS光斑半径"])
    zh_ray = ("光扇图数据列表\n\n子午数据是y像差作为Py的函数。\n\n"
              "子午光扇，视场号1 = 0.0000 (度)\n\n  光瞳    \t     0.550000\n"
              " -1.000  \t    -0.000000\n  1.000  \t     0.020000\n\n"
              "弧矢光扇，视场号2 = 5.0000 (度)\n -1.000  \t    -0.040000\n  1.000  \t     0.030000\n")
    en_ray = ("Table series: Tangential, Field #1\n   1.0000000     0.0000000\n"
              "   0.5000000    -0.0100000\n  -1.0000000     0.0200000\n")
    rz = zemax_tools._parse_rayfan(zh_ray)
    re_ = zemax_tools._parse_rayfan(en_ray)
    report("分析文本解析(RMS + RayFan 中英文标题 + 上下缘)",
           vals.get("RMS Radius") == 0.0123 and vals.get("RMS光斑半径") == 0.9
           and rz.get("RayFan_子午_视场1_上缘") == 0.02 and rz.get("RayFan_子午_视场1_下缘") == -0.0
           and rz.get("RayFan_弧矢_视场2_上缘") == 0.03 and rz.get("RayFan_弧矢_视场2_下缘") == -0.04
           and re_.get("RayFan_子午_视场1_上缘") == 0.0 and re_.get("RayFan_子午_视场1_下缘") == 0.02,
           f"{vals} 中={rz} 英={re_}")

    # 1d) MTF 文本解析(中英 + 采样档不正好有 30/50 时取最近档 + 截止频率 + 取样太低警告)
    en_mtf = ("Modulation MTF\n\nField: 0.00, 0.00 (deg)\nSpatial Freq   Tangential   Sagittal\n"
              "   0.000000     1.000000     1.000000\n   10.000000     0.600000     0.550000\n"
              "   28.000000     0.300000     0.280000\n   34.000000     0.200000     0.190000\n"
              "   60.000000     0.040000     0.030000\n\nField: 7.00, 0.00 (deg)\n"
              "   0.000000     1.000000     1.000000\n   30.000000     0.450000     0.400000\n")
    zh_mtf = ("复色光衍射MTF\n\n取样太低, 数据不准确!\n\n视场：0.00, 0.00 (度)\n空间频率\t子午\t弧矢\n"
              "         0.000000\t1.000000\t1.000000\n         30.000000\t0.003927\t0.003927\n")
    m_en = zemax_tools._parse_mtf(en_mtf)
    m_zh = zemax_tools._parse_mtf(zh_mtf)
    report("MTF 解析(中英/取最近采样档/截止频率/取样太低警告)",
           m_en.get("MTF@28lp_mm_子午_视场0.00,0.00") == 0.3
           and m_en.get("MTF@60lp_mm_子午_视场0.00,0.00") == 0.04
           and m_en.get("MTF截止频率_lp_mm_视场0.00,0.00") == 60.0
           and m_en.get("MTF@30lp_mm_子午_视场7.00,0.00") == 0.45
           and "取样太低" in m_zh.get("MTF警告", "")
           and m_zh.get("MTF@30lp_mm_弧矢_视场0.00,0.00") == 0.004,
           f"{m_en}")

    # 1e) 色差解析(样本就是真机 FftMtf/LongitudinalAberration/LateralColor 的文本形状)
    la = ("轴向像差数据列表\n单位是毫米.\n\nRel. 光瞳\t      0.5500\t      0.4861\t      0.6563\t\n"
          "    0.0000   1.902E+02   1.888E+02   1.918E+02\t\n"
          "    0.0100   1.902E+02   1.888E+02   1.918E+02\t\n")
    lc = ("垂轴色差数据列表\n单位是µm.\n最大视场    :     0.000000 Deg\n短波长 :       0.4861 μm\n"
          "Rel. 视场垂轴色差\n    0.0000 \t    0.0000E+00\n    0.1000 \t    1.2345E+01\n")
    la_out = zemax_tools._parse_longitudinal(la)
    lc_out = zemax_tools._parse_lateral_color(lc)
    report("色差解析(轴向色差=各波长焦点差/倍率色差取最大)",
           la_out.get("轴向色差_mm") == 3.0 and la_out.get("各波长焦点_mm", {}).get("0.4861") == 188.8
           and lc_out.get("倍率色差_um") == 12.345 and zemax_tools._parse_longitudinal("空") == {},
           f"{la_out} {lc_out}")

    # 2) 课程工具 + 学习档案
    def mk_params(args=None):
        state = {}

        class P:
            arguments = args or {}

            async def result_callback(self, r, properties=None):
                state["r"] = r

        return P(), state

    for n, expect in [(1, "第 1 课"), (2, "第 2 课"), (9, "第 9 课"), (10, "毕业项目")]:
        p, st = mk_params({"lesson": n})
        await lessons._read_lesson_handler(p)
        got = st.get("r", {}).get("本课课件", "")
        report(f"read_lesson({n})", expect in got, got[:30].replace("\n", " "))

    # 2b) 学习档案:finish_lesson 写入 + read_lesson 读回
    p, st = mk_params({"lesson": 2, "note": "自检测试讲评"})
    await lessons._finish_lesson_handler(p)
    ok_w = st.get("r", {}).get("ok") is True
    p, st = mk_params({"lesson": 2})
    await lessons._read_lesson_handler(p)
    prog = st.get("r", {}).get("学习档案", {})
    ok_r = 2 in prog.get("已完成", [])
    report("学习档案(写入+读回)", ok_w and ok_r, f"已完成={prog.get('已完成')}")

    # 2b2) 重过旧课不许把"推进到第几课"往回改
    prog2 = lessons.load_progress()
    prog2["current"] = 3
    lessons.save_progress(prog2)
    p, st = mk_params({"lesson": 2, "note": "重过第 2 课"})
    await lessons._finish_lesson_handler(p)
    cur = lessons.load_progress().get("current")
    report("重过旧课不回退进度", cur == 3, f"current={cur}")

    # 2c) 断点续教:save_step 写入/去重 + read_lesson 读回 + finish_lesson 清断点
    #     (用第 2 课测,它在 done 里,结尾 finish 清掉断点,不留脏状态)
    p, st = mk_params({"lesson": 2, "step": "自检:测试断点"})
    await lessons._save_step_handler(p)
    ok_s = st.get("r", {}).get("ok") is True
    p, st = mk_params({"lesson": 2, "step": "自检:测试断点"})
    await lessons._save_step_handler(p)
    dedup = st.get("r", {}).get("说明") is not None
    p, st = mk_params({"lesson": 2})
    await lessons._read_lesson_handler(p)
    cp = st.get("r", {}).get("学习档案", {}).get("断点") or {}
    ok_c = cp.get("step") == "自检:测试断点"
    p, st = mk_params({"lesson": 2, "note": "自检测试讲评"})
    await lessons._finish_lesson_handler(p)
    n_log = len(lessons.load_progress().get("log", []))
    await lessons._finish_lesson_handler(p)
    dedup_log = len(lessons.load_progress().get("log", [])) == n_log
    p, st = mk_params({"lesson": 2})
    await lessons._read_lesson_handler(p)
    cleared = not st.get("r", {}).get("学习档案", {}).get("断点")
    report("断点续教(写入+去重+读回+过课清除断点+日志去重)",
           ok_s and dedup and ok_c and cleared and dedup_log,
           f"断点={cp.get('step')}" if ok_c else str(cp)[:40])

    # 2d) 开场白分支:有断点→续教话术(测完还原),无断点→默认问候
    def greeting_with(cp, days_ago):
        """时间写死进档案:不然真实档案一隔就是 3 天以上,默认问候分支自己变,自检跟着假 FAIL。"""
        prog = lessons.load_progress()
        if cp:
            prog["checkpoint"] = cp
        else:
            prog.pop("checkpoint", None)
        prog["log"] = [{"time": time.strftime("%Y-%m-%d %H:%M", time.localtime(time.time() - days_ago * 86400)),
                        "lesson": 2, "note": "自检"}]
        lessons.save_progress(prog)
        return bot._greeting_prompt()

    gp = greeting_with({"lesson": 4, "step": "测试续教话术", "time": "x"}, 0)
    report("开场白续教(有断点)", "第4课" in gp and "继续" in gp, gp[:40])
    gp_default = greeting_with(None, 0)
    report("开场白默认(无断点)", "继续" not in gp_default and "复习" not in gp_default, gp_default[:30])
    gp_old = greeting_with(None, 10)
    report("开场白复习(隔10天)", "复习" in gp_old, gp_old[:40])

    # 2e) finish_lesson 课号无效:必须一个字节都不动档案(以前会把断点悄悄清掉)
    p, st = mk_params({"lesson": 2, "step": "自检:不该被清掉"})
    await lessons._save_step_handler(p)
    p, st = mk_params({"lesson": 0, "note": "GLM 填了个 0"})
    await lessons._finish_lesson_handler(p)
    kept = (lessons.load_progress().get("checkpoint") or {}).get("step") == "自检:不该被清掉"
    report("finish_lesson(课号无效不碰档案)", "error" in st.get("r", {}) and kept, str(st.get("r"))[:60])

    # 3) Zemax 系工具(依赖 Zemax 开着+交互扩展)
    def zemax_ready():
        try:
            import zemax_api
            return zemax_api._zemax_running()
        except Exception:
            return False

    async def run_tool(handler, args=None):
        p, st = mk_params(args)
        await asyncio.wait_for(handler(p), timeout=45)
        return st.get("r", {})

    zr = zemax_ready()
    if zr:
        for name, handler, args in [
                ("read_lde", zemax_tools._read_lde_handler, None),
                ("read_analysis", zemax_tools._read_analysis_handler, None),
                ("read_mtf", zemax_tools._read_mtf_handler, None),
                ("read_color", zemax_tools._read_color_handler, None),
                ("point_at", zemax_tools._point_at_handler, {"name": "分析"})]:
            r = await run_tool(handler, args)
            if "error" in r and "交互扩展" in r["error"]:
                report(name, None, "交互扩展没开,跳过(线上会返回友好提示)")
                break
            if name == "read_lde":
                nvar = sum(1 for s in r.get("面参数", []) if s.get("半径是变量") or s.get("厚度是变量"))
                report(name, "error" not in r,
                       f"面数={r.get('面数')} 视场={len(r.get('视场', []))} 波长={len(r.get('波长', []))} 变量面={nvar}"
                       if "error" not in r else r["error"][:60])
            elif name == "read_color":
                got = {k: v for k, v in r.items() if k.startswith(("轴向", "倍率", "波长不够"))}
                report(name, "error" not in r and bool(got), str(got)[:110])
            elif name == "read_mtf":
                got = {k: v for k, v in r.items() if k.startswith("MTF")}
                report(name, "error" not in r and bool(got), str(got)[:110])
            elif name == "read_analysis":
                rms = r.get("RMS光斑半径_um")
                efl = r.get("EFL_mm")
                # 系统没光焦度时 Zemax 自己报"基点计算错误",工具如实返回 EFL_error —— 这也算工具正常
                ok = rms is not None and (efl is not None or "EFL_error" in r)
                # 空系统 RMS=0 必须带"提醒",不然助教会说"像质完美"
                if rms == 0 and "EFL_error" in r:
                    ok = "提醒" in r
                report(name, ok,
                       f"RMS={rms} EFL={efl if efl is not None else r.get('EFL_error')}"
                       + (f" {r.get('提醒', '')}" if rms == 0 else "")
                       if rms is not None else str(r)[:120])
            else:
                report(name, r.get("ok") is True, f"指向={r.get('指向')}" if r.get("ok") else str(r)[:60])
    else:
        for n in ("read_lde", "read_analysis", "read_mtf", "read_color"):
            report(n, None, "Zemax 没开或交互扩展没开,跳过(线上会返回友好提示)")
        # point_at 只依赖窗口存在:Zemax 没开时也必须返回友好错误,不能把对话炸掉
        try:
            r = await run_tool(zemax_tools._point_at_handler, {"name": "点列图"})
            report("point_at(Zemax 没开时不崩)", "error" in r, str(r.get("error", r))[:70])
        except Exception as e:
            report("point_at(Zemax 没开时不崩)", False, type(e).__name__ + ": " + str(e)[:70])

    # 4) Edge TTS 全链路
    try:
        t0 = time.time()
        chunks = 0
        first = None
        total = 0

        async for c in bot.EdgeTTSService(voice="zh-CN-XiaoyiNeural")._edge_pcm_stream(
                "自检开始。R100到R-100,RMS约等于50微米。"):
            if first is None:
                first = time.time() - t0
            chunks += 1
            total += len(c)
        dur = total / 2 / 24000
        report("Edge TTS(实测)", chunks > 0 and dur > 3,
               f"首块{first:.1f}s 全句{time.time()-t0:.1f}s 音频{dur:.1f}s" if first else "无输出")
    except Exception as e:
        report("Edge TTS(实测)", False, str(e)[:60])

    # 4b) VOICE_SPEED 换算:0.7 必须明显比 1.2 短(以前 edge 不换算,语速设了没效果)
    try:
        async def dur_at(speed):
            n = 0
            async for c in bot.EdgeTTSService(voice="zh-CN-XiaoyiNeural", speed=speed)._edge_pcm_stream("语速测试句子。"):
                n += len(c)
            return n / 2 / 24000

        fast, slow = await dur_at(0.7), await dur_at(1.2)
        report("Edge 语速(VOICE_SPEED 真生效)", fast * 1.3 < slow, f"0.7→{fast:.1f}s 1.2→{slow:.1f}s")
    except Exception as e:
        report("Edge 语速(VOICE_SPEED 真生效)", False, str(e)[:60])

    # 5) 对话窗口
    try:
        import chat_window
        chat_window.log("你", "自检:这条是测试")
        chat_window.log("助教", "对话记录小窗工作正常")
        report("对话窗口", True, "已投两条测试消息,窗口应显示(进程退出时随关)")
    except Exception as e:
        report("对话窗口", False, str(e)[:60])

    # 6) OCR 读屏引擎:合成一张带已知数字的图实测识别,不依赖屏幕内容
    try:
        from PIL import Image, ImageDraw

        img = Image.new("RGB", (420, 140), "white")
        d = ImageDraw.Draw(img)
        d.text((20, 20), "RMS 3.14 um", fill="black")
        d.text((20, 70), "EFFL 108.04 mm", fill="black")
        lines = zemax_tools._ocr_lines(img)
        joined = " ".join(l["text"] for l in lines)
        report("OCR 引擎(合成图实测)", "3.14" in joined or "108.04" in joined, joined[:40])
    except Exception as e:
        report("OCR 引擎(合成图实测)", False, str(e)[:60])

    # 7) Minimax TTS:raw HTTP 按生产的同款参数打一发,验 key/端点/音色 + 首包延迟
    api_key = os.getenv("MINIMAX_API_KEY", "").strip()
    group_id = os.getenv("MINIMAX_GROUP_ID", "").strip()
    if not api_key:
        report("Minimax TTS", None, ".env 没配 key,跳过")
    else:
        async def minimax_test():
            import aiohttp

            url = os.getenv("MINIMAX_BASE_URL", "https://api.minimaxi.com/v1/t2a_v2")
            if group_id:
                url += "?GroupId=" + group_id
            payload = {
                "stream": True,
                "model": os.getenv("MINIMAX_MODEL", "speech-2.8-turbo"),
                "text": "自检,赛德尔系数读出来了。",
                "voice_setting": {
                    "voice_id": os.getenv("MINIMAX_VOICE", "female-shaonv"),
                    "speed": 1.0, "vol": 1.0, "pitch": 0,
                },
                "audio_setting": {"bitrate": 128000, "format": "pcm", "channel": 1, "sample_rate": 24000},
            }
            t0 = time.time()
            async with aiohttp.ClientSession() as s:
                async with s.post(url, json=payload,
                                  headers={"Authorization": f"Bearer {api_key}"}) as r:
                    first = None
                    body = b""
                    async for chunk in r.content.iter_any():
                        if first is None:
                            first = time.time() - t0
                        body += chunk
            total = time.time() - t0
            # 响应是 SSE 流:一行行 "data: {json}",audio 是 hex;整包 JSON 报错时走兜底
            status_code, msg, audio_nz = None, "", 0
            for line in body.decode(errors="replace").splitlines():
                line = line.strip()
                if not line.startswith("data:"):
                    continue
                try:
                    d = json.loads(line[5:])
                except Exception:
                    continue
                br = d.get("base_resp") or {}
                if br.get("status_code") is not None:
                    status_code, msg = br["status_code"], br.get("status_msg", "")
                # 数非零字节:服务端异常(如 voice_id 无效)会返 200 + 全零静音,只数字节测不出
                try:
                    audio_nz += sum(1 for b in bytes.fromhex((d.get("data") or {}).get("audio") or "") if b)
                except ValueError:
                    pass
            if status_code is None:
                try:
                    br = json.loads(body).get("base_resp", {})
                    status_code, msg = br.get("status_code"), br.get("status_msg", "")
                except Exception:
                    msg = f"非JSON:{body[:40]!r}"
            return r.status, first, total, status_code, msg, audio_nz

        try:
            status, first, total, code, msg, audio_nz = await asyncio.wait_for(minimax_test(), timeout=30)
            ok = code == 0 and audio_nz > 0
            report("Minimax TTS", ok,
                   f"HTTP {status} 首包{first:.1f}s 全程{total:.1f}s {msg} "
                   f"非静音音频{audio_nz}字节" if audio_nz else f"HTTP {status} {msg} 音频全零或无")
        except Exception as e:
            report("Minimax TTS", False, str(e)[:60])

    # 沙盒收尾:真实学习档案必须一个字节都没变
    real_after = real_progress.read_bytes() if real_progress.exists() else None
    lessons.PROGRESS = real_progress
    shutil.rmtree(sandbox_dir, ignore_errors=True)
    report("学习档案未被自检改动", real_after == real_before,
           "progress.json 字节一致" if real_after == real_before else "档案被改了!检查 lessons.PROGRESS 是否被绕过")

    fails = [r for r in results if r[0] == "FAIL"]
    print(f"\n=== 自检完成: {len(results)-len(fails)}/{len(results)} 通过 ===", flush=True)
    return 1 if fails else 0  # 有 FAIL 就非零退出(以前永远退 0,脚本没法判断)


if __name__ == "__main__":
    code = asyncio.run(main())
    # 全 PASS 时也常退 1:进程退出时 zospy 的 COM 单例清理/后台线程会抛,把退出码改成非零,
    # CI 判断就失效了。和 bot.py 一样,断开 COM 后 os._exit 跳过解释器收尾,退出码才可信。
    try:
        import zemax_tools

        zemax_tools._disconnect_now()
    except Exception:
        pass
    os._exit(code)
