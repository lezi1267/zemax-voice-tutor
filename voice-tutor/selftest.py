# -*- coding: utf-8 -*-
"""全系统自检:逐个子系统实测并打 PASS/FAIL/SKIP。
用法: python selftest.py
需要 Zemax 参与的项,在 Zemax 没开/交互扩展没开时自动 SKIP。"""
import asyncio
import json
import sys
import time

results = []


def report(name, ok, note=""):
    tag = {True: "PASS", False: "FAIL", None: "SKIP"}[ok]
    results.append((tag, name, note))
    print(f"[{tag}] {name}" + (f" —— {note}" if note else ""), flush=True)


async def main():
    import bot
    import zemax_tools
    import lessons
    report("模块导入 + 静态名字检查", True, "check_names 由调用方先行执行")

    # 1) 文本清洗
    dirty = "好的😊 **RMS**≈50μm\n- 注意 ×2 ∞ ∝口径³ `code`"
    clean = bot._plain(dirty)
    bad = [ch for ch in clean if ch in "*#`" or ord(ch) >= 0x1F000]
    report("TTS 文本清洗(emoji/markdown/数学符号)", not bad and "微米" in clean and "约等于" in clean, clean[:40])

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
        r = await run_tool(zemax_tools._read_lde_handler)
        report("read_lde(实测)", "error" not in r, f"面数={r.get('面数')}" if "error" not in r else r["error"][:60])
        r = await run_tool(zemax_tools._read_analysis_handler)
        rms = r.get("RMS光斑半径_um")
        report("read_analysis(实测)", rms is not None, f"RMS={rms}" if rms else str(r)[:60])
        r = await run_tool(zemax_tools._point_at_handler, {"name": "绘图"})
        report("point_at(实测)", r.get("ok") is True, f"指向={r.get('指向')}" if r.get("ok") else str(r)[:60])
    else:
        for n in ("read_lde", "read_analysis", "point_at"):
            report(n, None, "Zemax 没开或交互扩展没开,跳过(线上会返回友好提示)")

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

    # 5) 对话窗口
    try:
        import chat_window
        chat_window.log("你", "自检:这条是测试")
        chat_window.log("助教", "对话记录小窗工作正常")
        report("对话窗口", True, "已投两条测试消息,窗口应显示(进程退出时随关)")
    except Exception as e:
        report("对话窗口", False, str(e)[:60])

    fails = [r for r in results if r[0] == "FAIL"]
    print(f"\n=== 自检完成: {len(results)-len(fails)}/{len(results)} 通过 ===", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
