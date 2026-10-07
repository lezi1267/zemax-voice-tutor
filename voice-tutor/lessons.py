# -*- coding: utf-8 -*-
"""课程工具:课程表/课件读取 + 学习档案(progress.json)+ 验收进度记录。"""
import json
import time
from pathlib import Path

from pipecat.adapters.schemas.function_schema import FunctionSchema

BASE = Path(__file__).resolve().parent.parent  # 学习游戏根目录(课件在这)
PROGRESS = Path(__file__).resolve().parent / "progress.json"  # 学习档案在 bot 目录


def load_progress() -> dict:
    """读学习档案;没有就返回初始档案。"""
    try:
        return json.loads(PROGRESS.read_text(encoding="utf-8"))
    except Exception:
        return {"done": [], "current": 0, "log": []}


def save_progress(p: dict):
    PROGRESS.write_text(json.dumps(p, ensure_ascii=False, indent=2), encoding="utf-8")


def _days_since_last(p: dict) -> float | None:
    """距上次学习过了几天(按 log 最后一条),没有记录返回 None。"""
    log = p.get("log") or []
    if not log:
        return None
    try:
        t = time.mktime(time.strptime(log[-1]["time"], "%Y-%m-%d %H:%M"))
        return round((time.time() - t) / 86400, 1)
    except Exception:
        return None


async def _save_step_handler(params) -> None:
    args = params.arguments or {}
    try:
        n = int(args.get("lesson", 0))
    except (TypeError, ValueError):
        n = 0
    step = str(args.get("step", "")).strip()[:120]

    prog = load_progress()
    old = prog.get("checkpoint") or {}
    if n and old.get("lesson") == n and old.get("step") == step:
        await params.result_callback({"ok": True, "说明": "断点没变,不用重复记"})
        return
    if n:
        prog["checkpoint"] = {"lesson": n, "step": step, "time": time.strftime("%Y-%m-%d %H:%M")}
        save_progress(prog)
    await params.result_callback({"ok": True, "已记断点": f"第{n}课:{step}" if n else "课号无效,没记"})


async def _read_lesson_handler(params) -> None:
    args = params.arguments or {}
    try:
        n = int(args.get("lesson", 0))
    except (TypeError, ValueError):
        n = 0

    overview = ""
    op = BASE / "学习路线.md"
    if op.exists():
        overview = op.read_text(encoding="utf-8")

    content = None
    if n:
        for f in sorted(BASE.glob(f"*第{n}课*.md")):
            content = f.read_text(encoding="utf-8")
            break

    prog = load_progress()
    result = {
        "课程表": overview or "没找到 学习路线.md",
        "本课课件": content or (f"还没有第{n}课的课件(学习游戏目录下 第{n}课*.md)。" if n else "学生没说课号,把课程表念给学生,问他想上哪课。"),
        "学习档案": {
            "已完成": prog.get("done", []),
            "当前推进到第几课": prog.get("current", 0),
            "断点": prog.get("checkpoint"),
            "距上次学习天数": _days_since_last(prog),
            "最近记录": prog.get("log", [])[-3:],
        },
    }
    await params.result_callback(result)


async def _finish_lesson_handler(params) -> None:
    args = params.arguments or {}
    try:
        n = int(args.get("lesson", 0))
    except (TypeError, ValueError):
        n = 0
    note = str(args.get("note", "")).strip()

    prog = load_progress()
    if n and n not in prog.get("done", []):
        prog.setdefault("done", []).append(n)
    if n:
        prog["current"] = n  # 记录推进到哪课
    prog.pop("checkpoint", None)  # 过课清断点,下次从新一课开头教
    entry = {"time": time.strftime("%Y-%m-%d %H:%M"), "lesson": n, "note": note[:120]}
    log = prog.setdefault("log", [])
    if not (log and log[-1].get("lesson") == n and log[-1].get("note") == entry["note"]):
        log.append(entry)  # GLM 常连环调用同一验收,相邻重复只记一条
    save_progress(prog)

    has_next = any(BASE.glob(f"*第{n + 1}课*.md"))
    await params.result_callback({
        "ok": True,
        "已记录": f"第{n}课完成" + (f"({note})" if note else ""),
        "累计完成": prog["done"],
        "下一课": f"第{n + 1}课课件{'已就绪,可以继续' if has_next else '还没写,如实告诉学生'}",
    })


finish_lesson_schema = FunctionSchema(
    name="finish_lesson",
    description=(
        "学生通过某课的验收(报的数字与实测一致)时调用,把这一课记入学习档案。"
        "note 填一句话讲评(如'RMS偏大,建议复习球差')。没通过就别调用,直接告诉他差在哪。"
    ),
    properties={
        "lesson": {"type": "integer", "description": "通过的课号"},
        "note": {"type": "string", "description": "一句话讲评,可为空"},
    },
    required=["lesson"],
    handler=_finish_lesson_handler,
)


save_step_schema = FunctionSchema(
    name="save_step",
    description=(
        "给学生下达一个新的操作步骤后调用,记录教学断点——程序重启后能从这里接着教,学生不丢进度。"
        "step 用一句话复述刚下达的步骤(如'第2步:后表面半径改成-100')。答疑、评价、闲聊不用调。"
    ),
    properties={
        "lesson": {"type": "integer", "description": "当前课号"},
        "step": {"type": "string", "description": "一句话描述刚下达的步骤"},
    },
    required=["lesson", "step"],
    handler=_save_step_handler,
)


read_lesson_schema = FunctionSchema(
    name="read_lesson",
    description=(
        "读取课程表、某一课的课件和学习档案。学生说'开始第N课''下一课''今天学什么''课程安排'"
        "'我上次学到哪'时调用(lesson 填课号,没说填 0),然后按课件带学生做:一次只说一步。"
    ),
    properties={
        "lesson": {
            "type": "integer",
            "description": "课号,如 1、2。学生没说具体课号时填 0,只看课程表和档案",
        },
    },
    required=["lesson"],
    handler=_read_lesson_handler,
)
