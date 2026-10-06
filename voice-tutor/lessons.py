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
    prog.setdefault("log", []).append(
        {"time": time.strftime("%Y-%m-%d %H:%M"), "lesson": n, "note": note[:120]}
    )
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
