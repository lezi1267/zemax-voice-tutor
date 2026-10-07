# -*- coding: utf-8 -*-
"""语音助教的 Zemax 读数工具:经 ZOS-API 读你正在用的那个 Zemax(交互扩展模式)。

前提:Zemax 开着,且点过 编程→交互扩展(弹出的小窗留着别关)。
没开时工具返回中文提示,助教会转告你怎么开。
"""
import asyncio
import atexit
import math
import os
import sys
import time
from pathlib import Path

from loguru import logger

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # 上级目录的 zemax_api

import zemax_api  # CLR 初始化要趁主线程(模块导入时),拖到 worker 线程里 pythonnet 会炸
import zospy as zp

from pipecat.adapters.schemas.function_schema import FunctionSchema

_conn = None  # (oss, zos),启动时建立并长驻;出错/超时清空,下次重连


def _enabled():
    return os.getenv("ZEMAX_ENABLED", "1") == "1"


def _rearm_extension() -> bool:
    """交互扩展断开后自动重开:在 Zemax 主窗口点 编程→交互扩展。
    实测 invoke 后约 3 秒可重连;找不到入口(比如 Zemax 没开)返回 False。"""
    try:
        from pywinauto import Desktop

        win = Desktop(backend="uia").window(title_re=".*OpticStudio.*")
        win.set_focus()
        for c in win.descendants():
            try:
                if (c.window_text() or "").strip() == "交互扩展":
                    c.invoke()
                    return True
            except Exception:
                pass
    except Exception:
        pass
    return False


def _connect_sync():
    global _conn
    if _conn is None:
        try:
            _conn = zemax_api.get_system(verbose=False)
        except Exception:
            # 十有八九是交互扩展断会话了,自动点一次重开再试;不行才把错抛给学生
            if not _rearm_extension():
                raise
            time.sleep(2.5)
            _conn = zemax_api.get_system(verbose=False)
    import dialog_fix
    dialog_fix.fix()  # 解锁被扩展小窗锁住的主窗口 + 小窗去置顶挪角落
    return _conn[0]


def _disconnect_now():
    """读完立刻让位:交互扩展一次只认一个连接,占着会挡学生用 Zemax。"""
    global _conn
    if _conn:
        try:
            _conn[1].disconnect()
        except Exception:
            pass
        _conn = None


async def warm_connect(timeout: float = 25):
    """启动时连接并长驻(学生要的);连接会锁主窗口,连上立即用 dialog_fix 驯服解锁。
    学生中途重开交互扩展会再次锁人,所以每次工具调用前也会再驯一次。"""
    try:
        await asyncio.wait_for(asyncio.to_thread(_connect_sync), timeout=timeout)
        logger.info("Zemax 已连接(交互扩展,长驻),主窗口已解锁")
    except Exception as e:
        logger.warning(f"启动时连 Zemax 失败(用到时还会再试): {e}")



def _lde_snapshot(oss):
    """读 LDE 全部面参数 + 孔径,返回 dict。阻塞,worker 线程专用。"""
    import dialog_fix

    dialog_fix.fix()  # 每次读数前驯一次:学生重开小窗会重新锁人
    lde = oss.LDE
    rows = []
    for i in range(lde.NumberOfSurfaces):
        s = lde.GetSurfaceAt(i)

        def get(attr):
            # 各面属性可有可无(材料/口径等),逐个兜底,别让一个属性毁掉整份读数
            try:
                v = getattr(s, attr)
                if isinstance(v, float) and not math.isfinite(v):
                    return "无穷大"
                if attr == "Radius" and float(v) == 0:
                    return "无穷大(0)"
                return v
            except Exception:
                return "?"

        try:
            stype = str(s.Type)  # ZOSPy 的 Type 是封装对象,str() 才是类型名
        except Exception:
            stype = "?"

        rows.append({
            "面序号": i,
            "类型": stype,
            "注释": get("Comment"),
            "半径": get("Radius"),
            "厚度": get("Thickness"),
            "材料": get("Material"),
            "半口径": get("SemiDiameter"),
        })
    ap = oss.SystemData.Aperture
    result = {
        "面数": lde.NumberOfSurfaces,
        "面参数": rows,
        "系统孔径": {"类型": str(ap.ApertureType), "值": ap.ApertureValue},
    }
    return result


def _read_lde_sync():
    """阻塞连接+读数,只准在 asyncio.to_thread 的 worker 线程里跑。"""
    oss = _connect_sync()
    return _lde_snapshot(oss)


async def _read_lde_handler(params) -> None:
    global _conn
    import chat_window

    chat_window.log("系统", "正在读你的镜头数据……")
    try:
        # 20 秒兜底:COM 连接在扩展模式没开时会傻等很久,不能让它冻住语音管线
        result = await asyncio.wait_for(asyncio.to_thread(_read_lde_sync), timeout=20)
    except asyncio.TimeoutError:
        _conn = None  # 卡住的连接不复用
        result = {"error": "读取超时:请检查 Zemax 的交互扩展小窗是否还开着,关掉重开一次再说。"}
    except Exception as e:
        _conn = None
        result = {"error": str(e)}
    await params.result_callback(result)


@atexit.register
def _disconnect():
    """bot 退出时干净断开,否则交互扩展的连接位会被占死,下次谁也连不上。"""
    _disconnect_now()


def _parse_values(text: str, labels: list[str]) -> dict:
    """从分析文本里抠数值,兼容中英文标签。"""
    out = {}
    for line in text.splitlines():
        if ":" not in line and "：" not in line:
            continue
        for label in labels:
            if label in line:
                tail = line.replace("：", ":").split(":")[-1].strip()
                num = tail.split()[0] if tail else ""
                try:
                    out[label] = float(num)
                except ValueError:
                    pass
    return out


def _parse_rayfan(text: str) -> dict:
    """RayFan 文本 → 每个系列在光瞳 ±1 处的像差(µm),这就是'边缘光线读数'。"""
    out = {}
    series = None
    for line in text.splitlines():
        if "Table series" in line:
            series = line.split(":", 1)[-1].strip()
            tag = ("子午" if "子午" in series else "弧矢" if "弧矢" in series else series[:6])
            field = "视场?" if "视场号" not in series else "视场" + series.split("视场号")[1].split()[0].lstrip("=")
            key_head = f"RayFan_{tag}_{field}"
            continue
        if series is None:
            continue
        parts = line.split()
        if len(parts) == 2:
            try:
                py, ab = float(parts[0]), float(parts[1])
            except ValueError:
                continue
            if abs(abs(py) - 1.0) < 1e-6:
                key = key_head + ("_下缘" if py < 0 else "_上缘")
                out[key] = ab
    return out


def _spot_part(oss):
    """StandardSpot 分析 → RMS 光斑半径(µm)。"""
    import tempfile

    a = oss.Analyses.New_Analysis(zp.constants.Analysis.AnalysisIDM.StandardSpot)
    try:
        a.ApplyAndWaitForCompletion()
        tmp = tempfile.mktemp(suffix=".txt")
        a.ToFile(tmp)
        vals = _parse_values(open(tmp, encoding="utf-8", errors="replace").read(),
                             ["RMS光斑半径", "RMS Radius"])
        return {"RMS光斑半径_um": vals.get("RMS光斑半径", vals.get("RMS Radius"))}
    finally:
        try:
            a.Close()
        except Exception:
            pass


def _rayfan_part(oss):
    """RayFan 分析 → 光瞳 ±1 处的横向像差(µm),即'边缘光线读数'。"""
    import tempfile

    a = oss.Analyses.New_Analysis(zp.constants.Analysis.AnalysisIDM.RayFan)
    try:
        a.ApplyAndWaitForCompletion()
        tmp = tempfile.mktemp(suffix=".txt")
        a.ToFile(tmp)
        return _parse_rayfan(open(tmp, encoding="utf-8", errors="replace").read())
    finally:
        try:
            a.Close()
        except Exception:
            pass


def _read_analysis_sync():
    """点列图 RMS + RayFan 边缘 + LDE 结构快照,一次全给。阻塞,worker 线程专用。"""
    oss = _connect_sync()
    import dialog_fix

    dialog_fix.fix()
    out = _spot_part(oss)
    out.update(_rayfan_part(oss))
    snap = _lde_snapshot(oss)
    out["当前结构"] = snap
    try:
        rows = snap["面参数"]
        epd = float(snap["系统孔径"]["值"]) / 2.0
        n_map = {"BK7": 1.5168, "F2": 1.6200, "SF2": 1.6475, "SK16": 1.6204, "F15": 1.6055}
        y, u, n = epd, 0.0, 1.0  # 近轴 yu 追迹:物在无穷远,y=入瞳半高
        for m in rows:
            if str(m["面序号"]) == "0":
                continue
            glass = str(m["材料"]).strip()
            n_after = n_map.get(glass, 1.0)  # 材料列 = 该面之后的介质;空 = 空气
            Rv = m["半径"]
            try:
                R = float(Rv)
                R = R if math.isfinite(R) else 0.0
            except (TypeError, ValueError):
                R = 0.0  # "无穷大" = 平面,光焦度 0
            phi = (n_after - n) / R if R else 0.0
            nu = n * u - y * phi          # 折射(每个面都做,包括玻璃→空气)
            u = nu / n_after
            n = n_after
            try:
                Tv = float(m["厚度"])
                y += (Tv if math.isfinite(Tv) else 0.0) * u
            except (TypeError, ValueError):
                pass  # 像面厚度无穷大,不再转移
        if abs(u) > 1e-12:
            out["近轴EFL_mm"] = round(epd / abs(u), 2)
    except Exception:
        pass
    return out

_last_analysis = (0.0, None)  # (时间, 结果),15 秒内复用,防 GLM 连呼刷屏


async def _read_analysis_handler(params) -> None:
    global _conn, _last_analysis
    import chat_window
    import time

    if time.time() - _last_analysis[0] < 15:
        r = dict(_last_analysis[1])
        r["缓存"] = "这是15秒内的结果,直接用,别再调"
        await params.result_callback(r)
        return
    chat_window.log("系统", "正在跑点列图分析,几秒钟……")
    try:
        result = await asyncio.wait_for(asyncio.to_thread(_read_analysis_sync), timeout=30)
    except asyncio.TimeoutError:
        _conn = None
        result = {"error": "读取超时:请检查 Zemax 的交互扩展小窗是否还开着,关掉重开一次再说。"}
    except Exception as e:
        _conn = None
        result = {"error": str(e)}
    await params.result_callback(result)


read_analysis_schema = FunctionSchema(
    name="read_analysis",
    description=(
        "读取学生当前 Zemax 的分析数据:点列图 RMS 光斑半径(单位 µm)和有效焦距(单位 mm)。"
        "学生问'图怎么样''RMS 多少''焦距多少''成像好不好'时调用,拿真实数值回答。"
    ),
    properties={},
    required=[],
    handler=_read_analysis_handler,
)


read_lde_schema = FunctionSchema(
    name="read_lde",
    description=(
        "读取学生当前 Zemax 的镜头数据编辑器(LDE):每个面的类型/半径/厚度/材料/半口径,"
        "以及系统孔径。学生问'我填了什么''帮我检查参数''对不对',或讨论当前镜头结构时,"
        "先调用它拿真实数据再回答,不要凭空猜。"
    ),
    properties={},
    required=[],
    handler=_read_lde_handler,
)


_last_point = ("", 0.0)  # (目标名, 时间),5 分钟内同目标不重复指


async def _point_at_handler(params) -> None:
    global _last_point
    import chat_window
    import time

    name = str((params.arguments or {}).get("name", "")).strip()
    if not name:
        await params.result_callback({"error": "没说要点哪里,请给界面元素的名字"})
        return
    if name in _last_point[0] and time.time() - _last_point[1] < 300:
        await params.result_callback({"ok": True, "already": True,
                                     "提示": "30秒内已经指过这里了,直接告诉学生下一步"})
        return

    chat_window.log("系统", f"正在找「{name}」的位置……")
    def find():
        from pywinauto import Desktop

        win = Desktop(backend="uia").window(title_re=".*OpticStudio.*")
        try:
            win.set_focus()
        except Exception:
            pass
        best = None
        for c in win.descendants():
            try:
                t = (c.window_text() or "").strip()
                if t and name in t:
                    r = c.rectangle()
                    cand = (t, (r.left + r.right) // 2, (r.top + r.bottom) // 2)
                    if t == name:  # 精确命中,直接用
                        return cand
                    if best is None or len(cand[0]) < len(best[0]):
                        best = cand
            except Exception:
                pass
        return best

    try:
        best = await asyncio.wait_for(asyncio.to_thread(find), timeout=20)
    except asyncio.TimeoutError:
        best = None
    if not best:
        await params.result_callback({
            "error": f'在 Zemax 界面上没找到"{name}"。换个叫法(如 点列图/光线像差图/系统选项),或让学生自己找'
        })
        return
    t, x, y = best
    _last_point = (name, time.time())
    import guide

    guide.guide_to(x, y, f"点「{t}」", secs=8)
    await params.result_callback({
        "ok": True,
        "指向": t,
        "屏幕坐标": [x, y],
        "提示": "小幽灵已经在屏幕上指给它看了",
    })


point_at_schema = FunctionSchema(
    name="point_at",
    description=(
        "在学生屏幕上指引一个 Zemax 界面元素的位置:小幽灵会滑过去指向它。"
        "课件要求学生点某个菜单/按钮/选项时调用,name 填界面元素的名字(如 点列图、系统选项)。"
    ),
    properties={
        "name": {
            "type": "string",
            "description": "界面元素的名字,如 点列图、Quick Focus、系统选项",
        },
    },
    required=["name"],
    handler=_point_at_handler,
)


# ---------- OCR 读屏:补 API/UIA 的盲区(图上的标注、赛德尔系数表、报错弹窗等) ----------

_ocr = None
_last_screen = (0.0, None)  # (时间, 结果),10 秒内复用,防 GLM 连呼刷屏


def _get_ocr():
    """RapidOCR 单例:模型加载要几秒,只做一次。首次调用在 worker 线程里发生,不挡语音。"""
    global _ocr
    if _ocr is None:
        from rapidocr_onnxruntime import RapidOCR

        _ocr = RapidOCR()
    return _ocr


def _ocr_lines(img) -> list:
    """PIL Image → 按阅读顺序的文字行 [{text,x,y}],低置信度的不要。"""
    if hasattr(img, "save"):  # PIL 只认 ndarray:RGB 转 cv2 的 BGR
        import numpy as np

        img = np.asarray(img)[:, :, ::-1]
    result, _ = _get_ocr()(img)
    lines = []
    for box, text, score in result or []:
        try:
            if float(score) < 0.55:  # 这版 score 是字符串
                continue
        except (TypeError, ValueError):
            pass  # 没给置信度就保留
        lines.append({
            "text": text,
            "x": int(min(p[0] for p in box)),
            "y": int(min(p[1] for p in box)),
        })
    lines.sort(key=lambda l: (l["y"] // 24, l["x"]))  # 同一水平带算一行,行内从左到右
    return lines


def _read_screen_sync():
    """截全屏 → OCR。阻塞,worker 线程专用。"""
    from PIL import ImageGrab

    img = ImageGrab.grab()
    lines = _ocr_lines(img)
    return {
        "屏幕宽高": list(img.size),
        "文字行数": len(lines),
        "文字行": lines[:120],
        "说明": "x/y 是文字左上角的屏幕坐标,按从上到下从左到右排列" if lines else "屏幕上没认出文字",
    }


async def _read_screen_handler(params) -> None:
    global _last_screen
    import time

    import chat_window

    if time.time() - _last_screen[0] < 10:
        await params.result_callback(_last_screen[1])
        return
    chat_window.log("系统", "正在读屏(OCR),几秒钟……")
    try:
        result = await asyncio.wait_for(asyncio.to_thread(_read_screen_sync), timeout=45)
    except Exception as e:
        result = {"error": f"读屏失败:{e}"}
    _last_screen = (time.time(), result)
    await params.result_callback(result)


read_screen_schema = FunctionSchema(
    name="read_screen",
    description=(
        "OCR 读屏:把学生屏幕上的文字读出来(带位置坐标)。Zemax 的 API 和界面自动化读不到的"
        "内容用它——赛德尔系数图/分析图上的标注和数值、弹出的小窗、报错提示等。"
        "学生问'屏幕上/图上写的什么'时也调用它。"
    ),
    properties={},
    required=[],
    handler=_read_screen_handler,
)
