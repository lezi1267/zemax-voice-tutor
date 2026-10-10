# -*- coding: utf-8 -*-
"""语音助教的 Zemax 读数工具:经 ZOS-API 读你正在用的那个 Zemax(交互扩展模式)。

前提:Zemax 开着,且点过 编程→交互扩展(弹出的小窗留着别关)。
没开时工具返回中文提示,助教会转告你怎么开。
"""
import asyncio
import atexit
import math
import os
import re
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from loguru import logger

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # 上级目录的 zemax_api

import zemax_api  # CLR 初始化要趁主线程(模块导入时),拖到 worker 线程里 pythonnet 会炸
import zospy as zp

from pipecat.adapters.schemas.function_schema import FunctionSchema

_conn = None  # (oss, zos),启动时建立并长驻;出错/超时清空,下次重连
# ponytail: 一把全局锁。ZOS-API 的 COM 对象不是线程安全的,GLM 一轮里可能同时调
# read_lde 和 read_analysis,两个 worker 线程共用同一个连接会炸。升级路径:每个连接一把锁。
_zemax_lock = threading.Lock()


@contextmanager
def _zemax_busy_timeout(seconds: float = 15):
    """拿不到锁 = 上一个读数卡死了(asyncio 超时杀不掉 worker 线程,锁还它拿着)。
    等它一会儿,超时就如实报错,别把语音管线一起拖死。"""
    if not _zemax_lock.acquire(timeout=seconds):
        raise TimeoutError("上一个 Zemax 读数还卡着:重开一次 编程→交互扩展 再问")
    try:
        yield
    finally:
        _zemax_lock.release()


def _rearm_extension() -> bool:
    """交互扩展断开后自动重开:在 Zemax 功能区点 编程 → 交互扩展。
    实测:「交互扩展」按钮只有在 编程 选项卡被选中后才出现在 UIA 树里 —— 所以先选卡再找按钮。
    invoke 后约 3 秒可重连;找不到入口(比如 Zemax 没开)返回 False。"""
    try:
        from pywinauto import Desktop

        win = Desktop(backend="uia").window(title_re=".*OpticStudio.*")
        win.set_focus()
        for c in win.descendants():
            try:
                if c.element_info.control_type == "TabItem" and (c.window_text() or "").strip() == "编程":
                    c.select()
                    break
            except Exception:
                pass
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


def _connect_locked():
    with _zemax_busy_timeout():
        return _connect_sync()


async def warm_connect(timeout: float = 25):
    """启动时连接并长驻(学生要的);连接会锁主窗口,连上立即用 dialog_fix 驯服解锁。
    学生中途重开交互扩展会再次锁人,所以每次工具调用前也会再驯一次。"""
    try:
        await asyncio.wait_for(asyncio.to_thread(_connect_locked), timeout=timeout)
        logger.info("Zemax 已连接(交互扩展,长驻),主窗口已解锁")
    except Exception as e:
        logger.warning(f"启动时连 Zemax 失败(用到时还会再试): {e}")



def _lde_snapshot(oss):
    """读 LDE 全部面参数 + 孔径,返回 dict。阻塞,worker 线程专用。驯窗由 _connect_sync 负责。"""
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

        def is_var(cell):
            # 实测:IEditorCell.Solve 读回来就是 "Variable"/"Fixed"。
            # 别用 Value == "-" 判 —— 固定格的 Value 也可能是 "-",变量格也可能是数字,两头都错
            try:
                return str(getattr(s, cell).Solve).strip() == "Variable"
            except Exception:
                return None

        rows.append({
            "面序号": i,
            "类型": stype,
            "注释": get("Comment"),
            "半径": get("Radius"),
            "半径是变量": is_var("RadiusCell"),
            "厚度": get("Thickness"),
            "厚度是变量": is_var("ThicknessCell"),
            "材料": get("Material"),
            "半口径": get("SemiDiameter"),
        })
    ap = oss.SystemData.Aperture
    result = {
        "面数": lde.NumberOfSurfaces,
        "面参数": rows,
        "系统孔径": {"类型": str(ap.ApertureType), "值": ap.ApertureValue},
    }
    # 视场/波长:第1课要设对,MTF 按视场报。读不到就整段不给,不编
    try:
        flds = oss.SystemData.Fields
        result["视场"] = [{"视场号": n, "X": flds.GetField(n).X, "Y": flds.GetField(n).Y,
                           "权重": flds.GetField(n).Weight} for n in range(1, flds.NumberOfFields + 1)]
    except Exception:
        pass
    try:
        wls = oss.SystemData.Wavelengths
        result["波长"] = [{"波长_um": wls.GetWavelength(n).Wavelength,
                           "权重": wls.GetWavelength(n).Weight} for n in range(1, wls.NumberOfWavelengths + 1)]
    except Exception:
        pass
    return result


def _read_lde_sync():
    """阻塞连接+读数,只准在 asyncio.to_thread 的 worker 线程里跑。"""
    with _zemax_busy_timeout():
        return _lde_snapshot(_connect_sync())


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
                    num = float(num)
                except ValueError:
                    continue
                # 多视场分析每个视场一行:取最差的那个,别默默只留最后一行
                if label not in out or num > out[label]:
                    out[label] = num
    return out


def _parse_rayfan(text: str) -> dict:
    """RayFan 文本 → 每个系列在光瞳 ±1 处的像差(µm),这就是'边缘光线读数'。

    系列标题两种语言都认(文本语言跟着 OpticStudio 界面语言走):
    'Table series: Tangential, Field #1' / '子午光扇，视场号1 = 0.0000 (度)'"""
    out = {}
    key_head = None
    for line in text.splitlines():
        s = line.strip()
        if re.search(r"Table series|视场号|Field\s*#", s) and re.search(r"子午|弧矢|Tangential|Sagittal", s):
            tag = ("弧矢" if ("弧矢" in s or "Sagittal" in s)
                   else "子午" if ("子午" in s or "Tangential" in s) else s[:6])
            m = re.search(r"(?:视场号|Field\s*#?)\s*=?\s*(\d+)", s)
            key_head = f"RayFan_{tag}_{f'视场{m.group(1)}' if m else '视场?'}"
            continue
        if key_head is None:
            continue
        parts = s.split()
        if len(parts) == 2:
            try:
                py, ab = float(parts[0]), float(parts[1])
            except ValueError:
                continue
            if abs(abs(py) - 1.0) < 1e-6:
                out[key_head + ("_下缘" if py < 0 else "_上缘")] = ab
    return out


def _txt_encoding() -> str:
    """OpticStudio 自己说它写文本文件用什么编码(偏好 Unicode=UTF-16LE,ANSI=本地码页)。
    猜错就是乱码,解析全部失效 —— 所以问它本人,别猜。"""
    try:
        pref = str(_conn[1].Application.Preferences.General.TXTFileEncoding)
        if "Unicode" in pref:
            return "utf-16-le"
        if "ANSI" in pref:
            import locale

            return locale.getpreferredencoding(False)
    except Exception:
        pass
    return "utf-8"


def _analysis_text(oss, analysis_id) -> str:
    """跑一个分析 → 它的文本输出。临时文件读完就删。

    取文本必须用 GetResults().GetTextFile:实测 IA_.ToFile 对 Cardinal Points 这类报告
    只写出 "0 message(s) in output" 的空壳。"""
    import tempfile

    a = oss.Analyses.New_Analysis(analysis_id)
    fd, tmp = tempfile.mkstemp(suffix=".txt")
    os.close(fd)
    try:
        a.ApplyAndWaitForCompletion()
        a.GetResults().GetTextFile(tmp)
        raw = Path(tmp).read_bytes()
        if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
            return raw.decode("utf-16", errors="replace")
        return raw.decode(_txt_encoding(), errors="replace").lstrip("\ufeff")
    finally:
        try:
            a.Close()
        except Exception:
            pass
        try:
            os.remove(tmp)
        except OSError:
            pass


def _spot_part(oss):
    """StandardSpot 分析 → RMS 光斑半径(µm)。"""
    vals = _parse_values(_analysis_text(oss, zp.constants.Analysis.AnalysisIDM.StandardSpot),
                         ["RMS光斑半径", "RMS Spot Radius", "RMS Radius"])
    return {"RMS光斑半径_um": vals.get("RMS光斑半径", vals.get("RMS Spot Radius", vals.get("RMS Radius")))}


def _rayfan_part(oss):
    """RayFan 分析 → 光瞳 ±1 处的横向像差(µm),即'边缘光线读数'。"""
    return _parse_rayfan(_analysis_text(oss, zp.constants.Analysis.AnalysisIDM.RayFan))


def _parse_cardinal(text: str) -> dict:
    """Cardinal Points 文本 → EFL_mm。数据行形如(语言跟着 OpticStudio 界面走):
    'Focal Length:  -108.031  108.031' / '焦长 :  -192.855704  192.855704' —— 物方 像方,取像方。"""
    if "基点计算错误" in text or "Cardinal point calculation error" in text:
        return {"EFL_error": "Zemax 报“基点计算错误”:当前系统没有光焦度,焦距是无穷大"}
    for line in text.splitlines():
        s = line.lstrip("\ufeff ")
        if s.startswith("Focal Length") or s.startswith("焦长"):
            nums = re.findall(r"-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?", line.split(":", 1)[-1])
            if len(nums) >= 2:
                return {"EFL_mm": float(nums[1])}
            if nums:
                return {"EFL_mm": float(nums[0])}
    return {"EFL_error": "Cardinal Points 输出里没找到焦距行,分析可能没跑成"}


def _cardinal_part(oss):
    """Cardinal Points 分析 → 实测有效焦距 EFL(mm,带符号)。

    让 Zemax 自己算:反射面、任意玻璃、负焦距系统都对。
    这里以前是手工近轴追迹 + 硬编码 5 种玻璃的折射率,遇到反射面和别的玻璃会静默算错。"""
    return _parse_cardinal(_analysis_text(oss, zp.constants.Analysis.AnalysisIDM.CardinalPoints))


def _read_analysis_sync():
    """点列图 RMS + RayFan 边缘 + Cardinal Points 焦距 + LDE 结构快照,一次全给。阻塞,worker 线程专用。"""
    with _zemax_busy_timeout():
        oss = _connect_sync()  # 里面已经驯过一次窗
        # 一个分析跑挂不该把整份读数一起带走(问焦距时点列图数据仍要能用)
        out = {}
        for name, part in (("点列图", _spot_part), ("光扇图", _rayfan_part), ("基点", _cardinal_part)):
            try:
                out.update(part(oss))
            except Exception as e:
                out[name + "读取失败"] = str(e)[:120]
        out["当前结构"] = _lde_snapshot(oss)
        # 空系统/没光焦度时点列图 RMS 会是 0.0 —— 看着像"完美",其实是根本没成像
        if out.get("EFL_error") and out.get("RMS光斑半径_um") == 0:
            out["提醒"] = "焦距算不出来 + RMS=0:这不是像质好,是系统里没有有效镜头,先看镜头数据"
        return out

_last_analysis = (0.0, None)  # (时间, 结果),15 秒内复用,防 GLM 连呼刷屏


async def _read_analysis_handler(params) -> None:
    global _conn, _last_analysis
    import chat_window

    if _last_analysis[1] is not None and time.time() - _last_analysis[0] < 15:
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
    if "error" not in result:
        _last_analysis = (time.time(), result)  # 只缓存成功结果
    await params.result_callback(result)


read_analysis_schema = FunctionSchema(
    name="read_analysis",
    description=(
        "读取学生当前 Zemax 的分析数据:点列图 RMS 光斑半径(单位 µm)、RayFan 边缘光线读数、"
        "有效焦距 EFL_mm(单位 mm,Zemax Cardinal Points 实测,带符号);多视场时 RMS 取最差视场。"
        "学生问'图怎么样''RMS 多少''焦距多少''成像好不好'时调用,拿真实数值回答。"
    ),
    properties={},
    required=[],
    handler=_read_analysis_handler,
)


def _parse_mtf(text: str) -> dict:
    """FftMtf 文本 → 每个视场的 MTF。文本块(语言跟着 OpticStudio 界面走):
    '视场：0.00, 0.00 (度)' / 'Field: 0.00, 0.00 (deg)',后跟 '空间频率 子午 弧矢' 数据行。
    取最接近 30 和 50 线对/mm 的那一档(采样格不一定正好有 30)。"""
    out = {}
    if "取样太低" in text or "Sampling too low" in text:
        out["MTF警告"] = "Zemax 提示取样太低、数据不准:让学生提高采样(或先别拿这个数下结论)"
    blocks = {}
    key = None
    for line in text.splitlines():
        s = line.strip()
        m = re.search(r"(?:视场|Field)\s*[:：]\s*(-?[\d.]+)\s*,\s*(-?[\d.]+)", s)
        if m:
            key = f"{m.group(1)},{m.group(2)}"
            blocks[key] = []
            continue
        if not key:
            continue
        p = s.split()
        if len(p) == 3:
            try:
                blocks[key].append((float(p[0]), float(p[1]), float(p[2])))
            except ValueError:
                pass
    if not any(blocks.values()):
        out["MTF说明"] = "MTF 分析没给出数据行:先让学生对焦(Quick Focus)或提高采样,再跑一次"
    for key, rows in blocks.items():
        if not rows:
            continue
        for target in (30, 50):
            f, tan, sag = min(rows, key=lambda r: abs(r[0] - target))
            out[f"MTF@{f:.0f}lp_mm_子午_视场{key}"] = round(tan, 3)
            out[f"MTF@{f:.0f}lp_mm_弧矢_视场{key}"] = round(sag, 3)
        cut = next((r[0] for r in rows if r[0] > 0 and r[1] < 0.05), None)
        if cut is not None:
            out[f"MTF截止频率_lp_mm_视场{key}"] = cut
    return out


def _mtf_part(oss):
    """FftMtf(复色光衍射 MTF)分析 → 每视场 MTF + 截止频率。"""
    return _parse_mtf(_analysis_text(oss, zp.constants.Analysis.AnalysisIDM.FftMtf))


def _parse_longitudinal(text: str) -> dict:
    """轴向像差(LongitudinalAberration)→ 轴向色差。文本(语言跟着界面走):
    'Rel. 光瞳  0.5500  0.4861  0.6563' 表头是波长,数据行 '光瞳比 值 值 值',单位 mm。
    取光瞳比 0 那一行(近轴):各波长焦点位置的最大差就是轴向色差。"""
    wl = None
    rows = []
    for line in text.splitlines():
        p = line.split()
        if not p:
            continue
        if p[0].startswith("Rel."):
            cand = []
            for x in p[1:]:
                try:
                    cand.append(float(x))
                except ValueError:
                    pass  # 'Rel. 光瞳' 里的中文表头
            wl = cand or None
            continue
        if not wl or len(p) != len(wl) + 1:
            continue
        try:
            rows.append((float(p[0]), [float(v) for v in p[1:]]))
        except ValueError:
            pass
    if not rows:
        return {}
    axis = rows[0][1]
    return {"轴向色差_mm": round(max(axis) - min(axis), 4),
            "各波长焦点_mm": {f"{w:.4f}": v for w, v in zip(wl, axis)}}


def _parse_lateral_color(text: str) -> dict:
    """垂轴色差(LateralColor)→ 全视场最大倍率色差,单位 µm。数据行就两列:视场比 + 色差。"""
    vals = []
    for line in text.splitlines():
        p = line.split()
        if len(p) != 2:
            continue
        try:
            f, v = float(p[0]), float(p[1])
        except ValueError:
            continue
        if 0 <= f <= 1.5:
            vals.append(abs(v))
    return {"倍率色差_um": round(max(vals), 4)} if vals else {}


def _read_color_sync():
    """轴向色差 + 倍率色差(第 4 课消色差的核心验收数字)。阻塞,worker 线程专用。"""
    with _zemax_busy_timeout():
        oss = _connect_sync()
        out = {}
        for name, part in (("轴向像差", lambda: _parse_longitudinal(
                _analysis_text(oss, zp.constants.Analysis.AnalysisIDM.LongitudinalAberration))),
            ("垂轴色差", lambda: _parse_lateral_color(
                _analysis_text(oss, zp.constants.Analysis.AnalysisIDM.LateralColor)))):
            try:
                out.update(part())
            except Exception as e:
                out[name + "读取失败"] = str(e)[:120]
        try:
            n = oss.SystemData.Wavelengths.NumberOfWavelengths
        except Exception:
            n = 0
        if n and n < 2:
            out["波长不够"] = (f"系统里只有 {n} 个波长,色差根本测不出来(现在测到的 0 不算数):"
                              "至少加两个,比如 0.4861 和 0.6563")
        try:
            flds = oss.SystemData.Fields
            on_axis_only = all(abs(flds.GetField(i).X) < 1e-9 and abs(flds.GetField(i).Y) < 1e-9
                               for i in range(1, flds.NumberOfFields + 1))
        except Exception:
            on_axis_only = False
        if on_axis_only:
            out["视场不够"] = "视场全是 0°,倍率色差必然是 0(不是消掉了):设一个离轴视场,比如 7°"
        return out


_last_color = (0.0, None)  # (时间, 结果),15 秒内复用,防 GLM 连呼刷屏


async def _read_color_handler(params) -> None:
    global _conn, _last_color
    import chat_window

    if _last_color[1] is not None and time.time() - _last_color[0] < 15:
        r = dict(_last_color[1])
        r["缓存"] = "这是15秒内的结果,直接用,别再调"
        await params.result_callback(r)
        return
    chat_window.log("系统", "正在跑色差分析,几秒钟……")
    try:
        result = await asyncio.wait_for(asyncio.to_thread(_read_color_sync), timeout=30)
    except asyncio.TimeoutError:
        _conn = None
        result = {"error": "读取超时:请检查 Zemax 的交互扩展小窗是否还开着,关掉重开一次再说。"}
    except Exception as e:
        _conn = None
        result = {"error": str(e)}
    if "error" not in result:
        _last_color = (time.time(), result)
    await params.result_callback(result)


read_color_schema = FunctionSchema(
    name="read_color",
    description=(
        "读取学生当前镜头的色差(第 4 课消色差双胶合的核心验收):轴向色差_mm(不同波长焦点位置差,越大色差越严重)"
        "和倍率色差_um(全视场最大垂轴色差)。学生问'色差多少''消掉了吗''这对玻璃行不行'时调用。"
        "只有一个波长时会提示先加波长,此时测到的 0 不是消掉了色差。"
    ),
    properties={},
    required=[],
    handler=_read_color_handler,
)


_last_mtf = (0.0, None)  # (时间, 结果),15 秒内复用,防 GLM 连呼刷屏


def _read_mtf_sync():
    with _zemax_busy_timeout():
        return _mtf_part(_connect_sync())


async def _read_mtf_handler(params) -> None:
    global _conn, _last_mtf
    import chat_window

    if _last_mtf[1] is not None and time.time() - _last_mtf[0] < 15:
        r = dict(_last_mtf[1])
        r["缓存"] = "这是15秒内的结果,直接用,别再调"
        await params.result_callback(r)
        return
    chat_window.log("系统", "正在跑 MTF 分析,几秒钟……")
    try:
        result = await asyncio.wait_for(asyncio.to_thread(_read_mtf_sync), timeout=30)
    except asyncio.TimeoutError:
        _conn = None
        result = {"error": "读取超时:请检查 Zemax 的交互扩展小窗是否还开着,关掉重开一次再说。"}
    except Exception as e:
        _conn = None
        result = {"error": str(e)}
    if "error" not in result:
        _last_mtf = (time.time(), result)
    await params.result_callback(result)


read_mtf_schema = FunctionSchema(
    name="read_mtf",
    description=(
        "读取学生当前镜头的衍射 MTF(FftMtf 分析):每个视场在 30 和 50 线对/mm 的子午/弧矢 MTF 值,"
        "以及 MTF 掉到 0.05 以下的截止频率(lp/mm)。学生问'MTF 多少''分辨率怎么样''能分辨多少线对'"
        "(第 7 课)时调用,拿真实数值回答,别凭点列图猜。"
    ),
    properties={},
    required=[],
    handler=_read_mtf_handler,
)


read_lde_schema = FunctionSchema(
    name="read_lde",
    description=(
        "读取学生当前 Zemax 的镜头数据编辑器(LDE):每个面的类型/半径/厚度/材料/半口径、这一面的半径或"
        "厚度是不是变量,以及系统孔径、视场表、波长表。学生问'我填了什么''帮我检查参数''对不对''设成变量了吗',或讨论当前镜头结构时,"
        "先调用它拿真实数据再回答,不要凭空猜。"
    ),
    properties={},
    required=[],
    handler=_read_lde_handler,
)


_last_point = ("", 0.0)  # (目标名, 时间),5 分钟内同一个目标不重复指


async def _point_at_handler(params) -> None:
    global _last_point
    import chat_window

    name = str((params.arguments or {}).get("name", "")).strip()
    if not name:
        await params.result_callback({"error": "没说要点哪里,请给界面元素的名字"})
        return
    if name == _last_point[0] and time.time() - _last_point[1] < 300:
        await params.result_callback({"ok": True, "already": True,
                                      "提示": "5分钟内已经指过这里了,直接告诉学生下一步"})
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
    except Exception as e:
        # Zemax 没开时 pywinauto 抛 ElementNotFoundError。以前只接 TimeoutError,
        # 这个异常会把整轮对话炸掉(自检在 Zemax 没开时跳过 point_at,所以一直没暴露)。
        await params.result_callback({"error": "找不到 OpticStudio 窗口(" + type(e).__name__ + "):先打开 Zemax 再让我指路"})
        return
    if not best:
        await params.result_callback({
            "error": f'在 Zemax 界面上没找到"{name}"。换个叫法(如 点列图/光线像差图/系统选项),或让学生自己找'
        })
        return
    t, x, y = best
    _last_point = (name, time.time())
    import guide

    # voice=False:助教自己会念这一句。再让 guide 用系统 TTS 念一遍 = 两个声音,
    # 而且外放会被麦克风收回去,变成她自己打断自己。
    guide.guide_to(x, y, f"点「{t}」", secs=8, voice=False)
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
    import shot

    img = shot.grab()  # DPI 感知 + 全部显示器 + 分层窗口(shot.py 已解决,别在这儿重抄一遍)
    ox, oy = shot.origin()
    lines = _ocr_lines(img)
    for l in lines:
        l["x"] += ox
        l["y"] += oy  # 换成屏幕绝对坐标,和 point_at / 小幽灵用的是同一套坐标
    return {
        "屏幕宽高": list(img.size),
        "文字行数": len(lines),
        "文字行": lines[:120],
        "说明": "x/y 是文字左上角的屏幕绝对坐标,按从上到下从左到右排列" if lines else "屏幕上没认出文字",
    }


async def _read_screen_handler(params) -> None:
    global _last_screen
    import time

    import chat_window

    if _last_screen[1] is not None and time.time() - _last_screen[0] < 10:
        await params.result_callback(_last_screen[1])
        return
    chat_window.log("系统", "正在读屏(OCR),几秒钟……")
    try:
        result = await asyncio.wait_for(asyncio.to_thread(_read_screen_sync), timeout=45)
    except Exception as e:
        result = {"error": f"读屏失败:{e}"}
    if "error" not in result:
        _last_screen = (time.time(), result)  # 只缓存成功结果,失败下次真重试
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
