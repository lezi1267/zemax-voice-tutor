# -*- coding: utf-8 -*-
"""语音助教的 Zemax 读数工具:经 ZOS-API 读你正在用的那个 Zemax(交互扩展模式)。

前提:Zemax 开着,且点过 编程→交互扩展(弹出的小窗留着别关)。
没开时工具返回中文提示,助教会转告你怎么开。
"""
import asyncio
import atexit
import json
import math
import os
import re
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from loguru import logger

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "zemax"))  # 上级的 zemax\ (API/指引/截屏)

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


_TIMEOUT_MSG = "读取超时:请检查 Zemax 的交互扩展小窗是否还开着,关掉重开一次再说。"
_cache: dict[str, tuple[float, dict]] = {}  # "工具名|参数" -> (时间, 结果):GLM 一轮里连呼同一个工具会刷屏


def _cache_key(name: str, args: dict) -> str:
    """带参数的工具必须按参数分开缓存:15 秒内换一个分析(波前→渐晕)否则会拿到上一个的结果。"""
    return name + "|" + json.dumps(args or {}, sort_keys=True, ensure_ascii=False)


def _cached(key: str, ttl: float):
    hit = _cache.get(key)
    if hit is not None and time.time() - hit[0] < ttl:
        r = dict(hit[1])
        r["缓存"] = f"这是{int(ttl)}秒内的结果,直接用,别再调"
        return r
    return None


def _make_handler(name: str, status: str, work, timeout: float = 30, ttl: float = 15,
                  drop_conn_on_error: bool = True):
    """把阻塞的 work() 包成工具 handler:worker 线程跑、超时兜底、卡死的连接不复用、只缓存成功结果。

    以前每个读数工具把这套流程各抄一遍(5 份、约 100 行),改一处要改五处。"""
    async def handler(params) -> None:
        global _conn
        import chat_window

        args = params.arguments or {}
        key = _cache_key(name, args)
        cached = _cached(key, ttl) if ttl else None
        if cached is not None:
            await params.result_callback(cached)
            return
        chat_window.log("系统", status)
        try:
            # COM 在交互扩展没开时会傻等很久,不能让它冻住语音管线
            result = await asyncio.wait_for(asyncio.to_thread(work, args), timeout=timeout)
        except asyncio.TimeoutError:
            if drop_conn_on_error:
                _conn = None
            result = {"error": _TIMEOUT_MSG}
        except Exception as e:
            if drop_conn_on_error:
                _conn = None
            result = {"error": str(e)}
        if ttl and "error" not in result:  # ttl=0 的工具不缓存,连写都不写
            _cache[key] = (time.time(), result)
        await params.result_callback(result)

    return handler



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


def _read_lde_sync(args=None):
    """阻塞连接+读数,只准在 asyncio.to_thread 的 worker 线程里跑。"""
    with _zemax_busy_timeout():
        return _lde_snapshot(_connect_sync())


_read_lde_handler = _make_handler("read_lde", "正在读你的镜头数据……", _read_lde_sync,
                                  timeout=20, ttl=0)  # 学生随时在改,断点核对必须读实时值


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


def _read_analysis_sync(args=None):
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

_read_analysis_handler = _make_handler("read_analysis", "正在跑点列图分析,几秒钟……",
                                       _read_analysis_sync)


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


def _read_color_sync(args=None):
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


_read_color_handler = _make_handler("read_color", "正在跑色差分析,几秒钟……", _read_color_sync)


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


def _read_mtf_sync(args=None):
    with _zemax_busy_timeout():
        return _mtf_part(_connect_sync())


_read_mtf_handler = _make_handler("read_mtf", "正在跑 MTF 分析,几秒钟……", _read_mtf_sync)


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

_ocr = None  # RapidOCR 单例,模型加载要几秒


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


def _read_screen_sync(args=None):
    """截全屏 → OCR。阻塞,worker 线程专用。"""
    import shot

    img = shot.grab()  # DPI 感知 + 全部显示器 + 分层窗口(shot.py 已解决,别在这儿重抄一遍)
    ox, oy = shot.origin()
    lines = _ocr_lines(img)
    for l in lines:
        l["x"] += ox
        l["y"] += oy  # 换成屏幕绝对坐标,和 point_at / 小幽灵用的是同一套坐标
    out = {
        "屏幕宽高": list(img.size),
        "文字行数": len(lines),
        "文字行": lines[:120],
        "说明": "x/y 是文字左上角的屏幕绝对坐标,按从上到下从左到右排列" if lines else "屏幕上没认出文字",
    }
    if len(lines) < 3:
        # OCR 认不出字(图上的曲线、图标、非文字内容)就换视觉模型看,别只回一句"没认出文字"
        import vision

        vision.queue(img)
        out["视觉描述"] = vision.ask(img, "屏幕上有什么窗口和图?图上的标题、坐标轴、曲线形状、数值分别是什么?")
    return out


_read_screen_handler = _make_handler("read_screen", "正在读屏(OCR),几秒钟……", _read_screen_sync,
                                     timeout=90, ttl=10, drop_conn_on_error=False)  # 读屏不碰 Zemax 连接


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


def _look_screen_sync(args=None):
    """截屏(可裁剪) → 视觉模型看图。API 读不到的图形几何(点列图形状、MTF 怎么掉)靠它。"""
    import shot
    import vision

    img = shot.grab()
    ox, oy = shot.origin()
    x, y = int(args.get("x") or 0), int(args.get("y") or 0)
    w, h = int(args.get("w") or 0), int(args.get("h") or 0)
    cropped = w > 0 and h > 0
    if cropped:
        left, top = max(x - ox, 0), max(y - oy, 0)
        img = img.crop((left, top, min(left + w, img.width), min(top + h, img.height)))
    q = str(args.get("question") or "").strip() or (
        "这是 Zemax OpticStudio 的屏幕截图。说清:有哪些窗口和分析图;"
        "每张图的标题、横纵轴和量程、曲线或点的分布形状;图上标出的数值逐个报出来。")
    vision.queue(img)  # MAIN_VISION 开着时:这张图下一轮直接进对话,她看像素而不是转述
    return {"看到了": vision.ask(img, q),
            "说明": "整屏" if not cropped else f"已裁剪到 ({x},{y}) 起 {w}x{h} 这一块",
            "图已挂进对话": vision.MAIN_VISION}


_look_screen_handler = _make_handler("look_screen", "正在看你的屏幕……", _look_screen_sync,
                                     timeout=90, ttl=10, drop_conn_on_error=False)  # 看屏不碰 Zemax 连接


look_screen_schema = FunctionSchema(
    name="look_screen",
    description=(
        "用视觉模型看学生屏幕上的图:点列图/MTF/光扇图的形状和趋势、弹窗长什么样、API 读不到的图形内容。"
        "question 写清要看什么(如'这张 MTF 在 30 线对每毫米掉到多少、子午弧矢差多少')。"
        "只想看某一块就带 x/y/w/h 屏幕坐标(read_screen 或 point_at 给过的坐标),不带就是整屏。"
        "要精确数字优先 read_analysis / run_analysis,看图是用来理解形状的。"
    ),
    properties={
        "question": {"type": "string", "description": "要它看什么、回答什么"},
        "x": {"type": "integer", "description": "裁剪区左上角屏幕 x,不裁剪填 0"},
        "y": {"type": "integer", "description": "裁剪区左上角屏幕 y,不裁剪填 0"},
        "w": {"type": "integer", "description": "裁剪区宽度像素,不裁剪填 0"},
        "h": {"type": "integer", "description": "裁剪区高度像素,不裁剪填 0"},
    },
    required=["question"],
    handler=_look_screen_handler,
)


# ---------- 通用分析:ZOS-API 有 156 个分析,长尾靠这一个兜,不用一个个写解析器 ----------

_ANALYSIS_ALIAS = {
    "点列图": "StandardSpot", "标准点列图": "StandardSpot", "spot": "StandardSpot",
    "光扇图": "RayFan", "光线像差": "RayFan", "rayfan": "RayFan",
    "赛德尔图": "SeidelDiagram", "赛德尔系数": "SeidelCoefficients",
    "mtf": "FftMtf", "调制传递函数": "FftMtf", "几何mtf": "GeometricMtf",
    "轴向像差": "LongitudinalAberration", "纵向像差": "LongitudinalAberration",
    "倍率色差": "LateralColor", "垂轴色差": "LateralColor",
    "场曲畸变": "FieldCurvatureAndDistortion", "场曲": "FieldCurvatureAndDistortion",
    "网格畸变": "GridDistortion",
    "基点": "CardinalPoints", "焦距": "CardinalPoints",
    "波前": "WavefrontMap", "包围能量": "GeometricEncircledEnergy",
    "点扩散": "FftPsf", "psf": "FftPsf",
    "焦点位移": "FocalShiftDiagram", "焦点漂移": "FocalShiftDiagram",
    "光程差": "OpticalPathFan", "渐晕": "VignettingDiagramSettings",
    "相对照度": "RelativeIllumination", "照明": "RelativeIllumination",
    "阴影模型": "ShadedModel", "系统检查": "SystemCheck",
    "公差直方图": "ToleranceHistogram", "公差良率": "ToleranceYield", "良率": "QuickYield",
    "透过率": "Transmission", "光线追迹": "RayTrace", "光线路径": "PathAnalysis",
    "泽尼克": "ZernikeStandardCoefficients", "离焦点列": "ThroughFocusSpot",
    "全视场像差": "FullFieldAberration", "干涉图": "Interferogram",
    "rms弥散": "RMSFocus", "表面数据": "SurfaceDataSettings", "系统数据": "SystemData",
}


def _analysis_names() -> list[str]:
    """ZOS-API 的全部分析名。常量表要 CLR 初始化之后才有(没连过 Zemax 会抛)。"""
    A = zp.constants.Analysis.AnalysisIDM
    return [n for n in dir(A) if not n.startswith("_") and n not in ("count", "index")]


def _resolve_analysis(name: str, names: list[str]) -> tuple:
    """中文/英文分析名 → AnalysisIDM 成员名,返回 (名字, 候选)。纯函数,自检能直接测。"""
    s = str(name).strip().lower().replace(" ", "")
    if not s:
        return None, names
    alias = _ANALYSIS_ALIAS.get(s)
    if alias and alias in names:
        return alias, []
    flat = {n.lower(): n for n in names}
    if s in flat:
        return flat[s], []
    cand = [n for n in names if s in n.lower()]
    if len(cand) == 1:
        return cand[0], []
    return None, cand or names


def _run_analysis_sync(args=None):
    name = str((args or {}).get("name", "")).strip()
    with _zemax_busy_timeout():
        oss = _connect_sync()
        target, cand = _resolve_analysis(name, _analysis_names())
        if not target:
            return {"error": f"没认出分析名「{name}」", "可选分析": cand[:60]}
        text = _analysis_text(oss, getattr(zp.constants.Analysis.AnalysisIDM, target))
    lines = [l.rstrip() for l in text.splitlines() if l.strip()]
    if not lines:
        return {"分析": target, "说明": "这个分析没有文本输出(只有图):改用 look_screen 看图"}
    return {"分析": target, "行数": len(lines), "数据": "\n".join(lines[:140]),
            "说明": "原始分析输出文本,自己从里面挑数字,单位看表头"
                    + ("" if len(lines) <= 140 else f"(共 {len(lines)} 行,只给了前 140 行)")}


_run_analysis_handler = _make_handler("run_analysis", "正在跑分析,几秒钟……",
                                      _run_analysis_sync, timeout=60)

run_analysis_schema = FunctionSchema(
    name="run_analysis",
    description=(
        "跑学生 Zemax 里的任意一个分析,返回它的原始文本数据(数字都在文本里)。"
        "name 填分析名,中文英文都行(如 波前、包围能量、焦点位移、渐晕、相对照度、公差直方图、SeidelDiagram)。"
        "点列图/MTF/色差/赛德尔有专用工具(read_analysis、read_mtf、read_color、read_aberration)的优先用,数字更干净。"
    ),
    properties={"name": {"type": "string", "description": "分析名字,中文或英文"}},
    required=["name"],
    handler=_run_analysis_handler,
)


# ---------- 读数:赛德尔系数 / 场曲畸变(第 2、7 课的分析图,API 直接给数字) ----------

_SEIDEL_KEYS = ("S1球差", "S2彗差", "S3像散", "S4场曲", "S5畸变", "CLA轴向色差", "CT倍率色差")


def _parse_seidel(text: str) -> dict:
    """SeidelCoefficients 文本 → 累计赛德尔系数。
    表头 '表面  SPHA S1  COMA S2 …'(中英文表头一样有 SPHA),取 '累计'/'Cumulative' 那行。"""
    out = {}
    started = False
    for line in text.splitlines():
        s = line.strip()
        if not started:
            started = "SPHA" in s
            continue
        nums = re.findall(r"-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?", s)
        if len(nums) < 7:
            continue
        out = {k: round(float(v), 6) for k, v in zip(_SEIDEL_KEYS, nums[:7])}
        if s.startswith(("累计", "Cumulative")):
            break
    return out or {"赛德尔说明": "没读到赛德尔系数表:分析没跑成,先看视场和波长设了没有"}


def _parse_field_curvature(text: str) -> dict:
    """FieldCurvatureAndDistortion 文本 → 最大畸变%、畸变焦距、最大视场处的场曲(Tan 位移 − 矢高位移,mm)。
    数据行 6 列:视场角 Tan位移 矢高位移 实部高度 参考高度 畸变%。"""
    out = {}
    m = re.search(r"(?:最大畸变|Maximum Distortion)\s*[:=]\s*(-?[\d.]+)\s*%", text)
    if m:
        out["最大畸变_pct"] = float(m.group(1))
    m = re.search(r"(?:畸变焦距|Distortion Focal Length)\s*[:=]\s*(-?[\d.]+)", text)
    if m:
        out["畸变焦距_mm"] = float(m.group(1))
    rows = []
    for line in text.splitlines():
        p = line.replace("%", "").split()
        if len(p) != 6:
            continue
        try:
            rows.append([float(x) for x in p])
        except ValueError:
            continue
    if rows:
        worst = max(rows, key=lambda r: abs(r[0]))
        out["最大视场_度"] = round(abs(worst[0]), 4)
        out["场曲_mm_最大视场"] = round(worst[1] - worst[2], 6)  # 子午像面 - 弧矢像面
    return out or {"场曲说明": "场曲/畸变分析没给出数据行:先对焦再跑一次"}


def _read_aberration_sync(args=None):
    """赛德尔系数 + 场曲/畸变,一次给全(第 2 课看图、第 7 课用指标说话)。"""
    with _zemax_busy_timeout():
        oss = _connect_sync()
        out = {}
        for name, aid, parse in (
                ("赛德尔", zp.constants.Analysis.AnalysisIDM.SeidelCoefficients, _parse_seidel),
                ("场曲畸变", zp.constants.Analysis.AnalysisIDM.FieldCurvatureAndDistortion,
                 _parse_field_curvature)):
            try:
                out.update(parse(_analysis_text(oss, aid)))
            except Exception as e:
                out[name + "读取失败"] = str(e)[:120]
        return out


_read_aberration_handler = _make_handler("read_aberration", "正在读赛德尔系数和场曲畸变……",
                                         _read_aberration_sync)

read_aberration_schema = FunctionSchema(
    name="read_aberration",
    description=(
        "读赛德尔像差系数(S1球差 S2彗差 S3像散 S4场曲 S5畸变 + CLA/CT 色差,累计值)和场曲/畸变"
        "(最大畸变%、畸变焦距、最大视场处的子午-弧矢场曲 mm)。学生问'球差多大''什么像差最狠'"
        "'场曲/畸变多少''像面弯不弯'(第 2、7 课)时调用,拿实测系数讲,别凭点列图猜。"
    ),
    properties={},
    required=[],
    handler=_read_aberration_handler,
)


# ---------- 助教动手:改镜头 / 备份回滚 / 跑优化 / 扫参数(改之前一律先备份) ----------

BACKUP_DIR = Path(__file__).resolve().parent / "data" / "backups"

_SURFACE_ATTR = {"半径": "Radius", "厚度": "Thickness", "材料": "Material",
                 "半口径": "SemiDiameter", "注释": "Comment"}
_SURFACE_CELL = {"半径是变量": "RadiusCell", "厚度是变量": "ThicknessCell"}
# 改镜头后要作废的读数缓存:凡是"读当前镜头/当前屏幕"的都要在 —— 漏一个,
# 助教就会拿改前的数说"改好了"(read_merit / run_analysis / 看屏 以前就漏在外面)
_READ_TOOLS = ("read_lde", "read_analysis", "read_mtf", "read_color", "read_aberration",
               "read_merit", "run_analysis", "read_screen", "look_screen")


def _drop_cache(*names):
    """改过镜头就把旧读数缓存全作废 —— 不然助教会拿改前的数字说'改好了'。
    缓存键是 "工具名|参数",所以按前缀匹配着删。"""
    targets = names or _READ_TOOLS
    for k in [k for k in _cache if k.split("|")[0] in targets]:
        _cache.pop(k, None)


def _backup_lens(oss) -> str:
    """动手改之前先存一份到 voice-tutor\\data\\backups\\:改坏了 restore_lens 拉得回来。"""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    # 名字带到毫秒:同一秒里连改两次(优化+扫描)不会互相覆盖
    p = BACKUP_DIR / ("lens_" + time.strftime("%Y%m%d_%H%M%S") + f"_{int(time.time() * 1000 % 1000):03d}.zmx")
    oss.save_as(str(p))
    keep = int(os.getenv("BACKUP_KEEP", "20"))
    old = sorted(BACKUP_DIR.glob("lens_*.zmx"))[:-keep]  # 只留最近 20 份:一轮课改几十次不该攒成一堆
    for f in old:
        try:
            f.unlink()
        except OSError:
            pass
    return p.name


def _apply_surface_edit(oss, e: dict) -> dict:
    try:
        n = int(e.get("面"))
    except (TypeError, ValueError):
        return {"错误": "面序号不是数字"}
    s = oss.LDE.GetSurfaceAt(n)
    out = {"面": n, "改动": {}}
    for zh, attr in _SURFACE_ATTR.items():
        if e.get(zh) is not None:
            before = getattr(s, attr)
            setattr(s, attr, e[zh])
            out["改动"][zh] = f"{before} → {getattr(s, attr)}"
    for zh, cell in _SURFACE_CELL.items():
        if e.get(zh) is not None:
            c = getattr(s, cell)
            # IEditorCell 没有 RemoveSolve,Solve 又是只读属性 —— 只有 MakeSolveVariable / MakeSolveFixed
            (c.MakeSolveVariable if e[zh] else c.MakeSolveFixed)()
            out["改动"][zh] = "变量" if e[zh] else "固定"
    return out


def _edit_zemax_sync(args=None):
    """插/删面、改 LDE 某面、系统孔径、加波长、加视场。改前自动备份,改完立刻回读实际值给学生核对。"""
    args = args or {}
    with _zemax_busy_timeout():
        oss = _connect_sync()
        backup = _backup_lens(oss)
        sysd = {}
        # 第 1 课要插面:先插/删,再按新序号改参数,不然索引全错位
        for n in args.get("插入面") or []:
            oss.LDE.InsertNewSurfaceAt(int(n))
            sysd.setdefault("插入面", []).append(int(n))
        for n in args.get("删除面") or []:
            oss.LDE.RemoveSurfaceAt(int(n))
            sysd.setdefault("删除面", []).append(int(n))
        out = {"已备份": backup, "改动": [_apply_surface_edit(oss, e) for e in args.get("面改动") or []]}
        if args.get("孔径值") is not None:
            before = oss.SystemData.Aperture.ApertureValue
            oss.SystemData.Aperture.ApertureValue = float(args["孔径值"])
            sysd["孔径值"] = f"{before} → {oss.SystemData.Aperture.ApertureValue}"
        for wl in args.get("加波长") or []:
            oss.SystemData.Wavelengths.AddWavelength(float(wl), 1.0)
            sysd.setdefault("加波长", []).append(wl)
        for y in args.get("加视场") or []:
            oss.SystemData.Fields.AddField(0.0, float(y), 1.0)
            sysd.setdefault("加视场", []).append(y)
        if sysd:
            out["系统设置改动"] = sysd
        _drop_cache()
        out["改完之后"] = _lde_snapshot(oss)
        return out


_edit_zemax_handler = _make_handler("edit_zemax", "正在改你的镜头(改之前先存了一份备份)……",
                                    _edit_zemax_sync, timeout=40, ttl=0)

edit_zemax_schema = FunctionSchema(
    name="edit_zemax",
    description=(
        "替学生直接改 Zemax:插入/删除面、某面的半径/厚度/材料/半口径/注释、把半径或厚度设为变量/取消变量、"
        "系统孔径值、加波长、加视场。学生说'你帮我改''直接改成XX''插入一个面''帮我加个波长 0.4861''加 7 度视场'时调用。"
        "改之前自动备份到 data\\backups\\,返回的'改完之后'是实际读回的值 —— 必须照这个报给学生,别只说'改好了'。"
        "课件里要求学生自己练的步骤不要代做,除非学生明确让你改。"
    ),
    properties={
        "插入面": {"type": "array", "items": {"type": "integer"}, "description": "在这个序号处插入新面,如 [2]"},
        "删除面": {"type": "array", "items": {"type": "integer"}},
        "面改动": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "面": {"type": "integer", "description": "面序号(0=物面,1=光阑,2 起是透镜面)"},
                    "半径": {"type": "number"}, "厚度": {"type": "number"},
                    "材料": {"type": "string", "description": "玻璃牌号,如 BK7 / F2 / SK16"},
                    "半口径": {"type": "number"}, "注释": {"type": "string"},
                    "半径是变量": {"type": "boolean"}, "厚度是变量": {"type": "boolean"},
                },
                "required": ["面"],
            },
        },
        "孔径值": {"type": "number"},
        "加波长": {"type": "array", "items": {"type": "number"}, "description": "波长 µm,如 [0.4861, 0.6563]"},
        "加视场": {"type": "array", "items": {"type": "number"}, "description": "离轴视场角度,如 [7]"},
    },
    required=[],
    handler=_edit_zemax_handler,
)


def _restore_lens_sync(args=None):
    """把镜头拉回备份状态(默认最近一次备份)。"""
    args = args or {}
    with _zemax_busy_timeout():
        oss = _connect_sync()
        files = sorted(BACKUP_DIR.glob("lens_*.zmx")) if BACKUP_DIR.exists() else []
        if not files:
            return {"error": "没有备份可回滚:data\\backups\\ 是空的"}
        name = str(args.get("备份", "")).strip()
        p = files[-1] if not name else BACKUP_DIR / name
        if not p.exists():
            return {"error": "找不到备份 " + name + ";最近的有: " + ", ".join(f.name for f in files[-5:])}
        oss.load(str(p))
        _drop_cache()
        return {"已恢复": p.name, "现在": _lde_snapshot(oss),
                "还有这些备份": [f.name for f in files[-5:]]}


_restore_lens_handler = _make_handler("restore_lens", "正在把镜头拉回备份……", _restore_lens_sync,
                                      timeout=40, ttl=0)

restore_lens_schema = FunctionSchema(
    name="restore_lens",
    description=(
        "把学生的镜头恢复成某一份备份(每次 edit_zemax/run_optimize 前都自动存过)。"
        "学生说'改坏了''撤销''恢复原样'时调用;备份 是可选文件名,不填就用最近的一份(只退一步)。"
        "注意:一次只退一步 —— 学生说'还是不对''再往前退''退到最初'时,把返回的'还有这些备份'里更早的"
        "那份文件名填进 备份 再调一次。"
    ),
    properties={"备份": {"type": "string", "description": "备份文件名,如 lens_20261010_2015.zmx;留空=最近一次"}},
    required=[],
    handler=_restore_lens_handler,
)


def _build_default_mfe(oss):
    """学生没建评价函数时,替他建最小的两个:RSCE(RMS 光斑半径,就是第 3 课的"参照:光斑半径")+ EFFL 钉住当前焦距。

    实测对比(同一个 f/5 单透镜):GUI 的优化向导默认生成 41 个 DMFS/OPDX(畸变)操作数,
    优化把焦距从 97mm 压到 74mm、RMS 6618µm;这两个操作数只有 3 行,焦距稳在 97.27mm、RMS 3927µm。"""
    row = oss.MFE.InsertNewOperandAt(oss.MFE.NumberOfOperands + 1)
    row.ChangeType(zp.constants.Editors.MFE.MeritOperandType.RSCE)
    row.Weight = 1.0
    return _pin_efl(oss)


def _pin_efl(oss):
    """往 MFE 里补一个 EFFL 操作数,目标 = 当前焦距,权重 1 —— 优化时焦距不会漂走。返回目标值。"""
    efl = _cardinal_part(oss).get("EFL_mm")
    if not efl:
        return None
    row = oss.MFE.InsertNewOperandAt(oss.MFE.NumberOfOperands + 1)
    row.ChangeType(zp.constants.Editors.MFE.MeritOperandType.EFFL)
    row.Target = float(efl)
    row.Weight = 1.0
    return round(float(efl), 4)


def _num(v):
    """nan / inf 不往 LLM 里塞(它会把 inf 当数据念出来)。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return v
    return round(f, 6) if math.isfinite(f) else None


def _real_operands(oss) -> list:
    """MFE 里真正的操作数。DeleteAllRows 之后还会剩一行 BLNK(空行),别把它当'已建评价函数'。"""
    return [oss.MFE.GetOperandAt(i) for i in range(1, oss.MFE.NumberOfOperands + 1)
            if str(oss.MFE.GetOperandAt(i).Type) != "BLNK"]


def _read_merit_sync(args=None):
    """评价函数编辑器(MFE)里有什么操作数 + 当前评价函数值。"""
    with _zemax_busy_timeout():
        oss = _connect_sync()
        rows = []
        for r in _real_operands(oss):
            # IMFERow 只有 Type/Target/Weight/Value/Contribution 这些,没有 Surface1 之类的列名
            rows.append({"行": r.RowIndex, "操作数": str(r.Type), "目标": _num(r.Target), "权重": _num(r.Weight),
                         "当前值": _num(r.Value), "对评价函数的贡献": _num(r.Contribution)})
        nvar = sum(1 for i in range(oss.LDE.NumberOfSurfaces)
                   if str(oss.LDE.GetSurfaceAt(i).RadiusCell.Solve).strip() == "Variable"
                   or str(oss.LDE.GetSurfaceAt(i).ThicknessCell.Solve).strip() == "Variable")
        return {"评价函数值": oss.MFE.CalculateMeritFunction(), "操作数": rows, "变量格数": nvar,
                "说明": "评价函数值越小越好;操作数是空的说明还没建评价函数(优化向导)"
                if not rows else ""}


_read_merit_handler = _make_handler("read_merit", "正在读评价函数……", _read_merit_sync, ttl=10)

read_merit_schema = FunctionSchema(
    name="read_merit",
    description=(
        "读评价函数编辑器(MFE):里面每个操作数(类型/面/参数/目标/权重)和当前评价函数值,以及有多少个变量格。"
        "第 3 课优化前后对比、学生问'评价函数多少''我建对操作数了吗''有几个变量'时调用。"
    ),
    properties={},
    required=[],
    handler=_read_merit_handler,
)


def _run_optimize_sync(args=None):
    """把指定面设为变量 → 需要时自动跑优化向导建 MFE → 跑本地优化,报评价函数前后 + 实际改动。"""
    args = args or {}
    with _zemax_busy_timeout():
        oss = _connect_sync()
        backup = _backup_lens(oss)
        made = []
        for v in args.get("变量") or []:
            try:
                n, item = int(v[0]), str(v[1])
            except (TypeError, ValueError, IndexError):
                made.append({"错误": "变量要填 [面, 半径|厚度]"})
                continue
            cell = oss.LDE.GetSurfaceAt(n).RadiusCell if "半径" in item else oss.LDE.GetSurfaceAt(n).ThicknessCell
            cell.MakeSolveVariable()
            made.append(f"{n}面{item}")
        if not _real_operands(oss):
            pinned = _build_default_mfe(oss)
        else:
            pinned = None
        if not _real_operands(oss):
            return {"error": "评价函数建不起来:让学生在 MFE 里右键>优化向导", "已备份": backup}
        before = oss.MFE.CalculateMeritFunction()
        try:
            efl_before = _cardinal_part(oss).get("EFL_mm")
        except Exception:
            efl_before = None
        tool = oss.Tools.OpenLocalOptimization()
        if tool is None:
            return {"error": "优化器打不开:Zemax 里可能已经有别的工具窗口在跑", "已备份": backup}
        try:
            tool.RunAndWaitForCompletion()
            ok, err = bool(tool.Succeeded), str(tool.ErrorMessage or "")
        finally:
            tool.Close()
        _drop_cache()
        out = {"已备份": backup, "设为变量": made,
               "评价函数": {"优化前": before, "优化后": oss.MFE.CalculateMeritFunction()},
               "成功": ok, "报错": err, "改完之后": _lde_snapshot(oss)}
        if pinned:
            out["自动建的评价函数"] = f"RSCE(RMS 光斑半径)+ EFFL 目标 {pinned} mm —— 焦距不会漂"
        elif not _real_operands(oss):
            out["提醒"] = "评价函数里没有约束焦距的操作数,焦距可能漂走"
        # 优化只朝"评价函数最小"跑:没有 EFFL 之类的操作数,焦距会一路漂走(第 3 课的坑)
        try:
            efl_after = _cardinal_part(oss).get("EFL_mm")
        except Exception:
            efl_after = None
        if efl_before and efl_after and abs(efl_after - efl_before) > 0.02 * abs(efl_before):
            out["提醒"] = (f"焦距从 {efl_before:.2f} 漂到 {efl_after:.2f} mm:"
                           "评价函数里没有约束焦距的操作数(加个 EFFL 目标 100 再优化)")
        return out


_run_optimize_handler = _make_handler("run_optimize", "正在跑优化,可能要十几秒……", _run_optimize_sync,
                                      timeout=120, ttl=0)

run_optimize_schema = FunctionSchema(
    name="run_optimize",
    description=(
        "替学生跑 Zemax 本地优化:先把 变量 里的面(如 [[2,'半径'],[3,'半径']])设为变量,"
        "评价函数是空的就自动建最小的两个操作数(RSCE 光斑半径 + EFFL 钉住当前焦距),然后执行优化。"
        "返回优化前后评价函数值和改完的实际镜头数据。学生说'你帮我优化''跑一下优化'时调用;"
        "第 3 课要学生自己体验优化,别代做,除非他要求。改之前自动备份,可 restore_lens 回滚。"
    ),
    properties={
        "变量": {"type": "array", "items": {"type": "array", "items": {"type": ["integer", "string"]}},
                 "description": "要设为变量的面,如 [[2,'半径'],[3,'半径']]"},
    },
    required=[],
    handler=_run_optimize_handler,
)


def _scan_param_sync(args=None):
    """把某个面参数在区间里扫一遍,每个值跑一次点列图 RMS,扫完恢复原值(第 9 课的批量扫描)。"""
    args = args or {}
    with _zemax_busy_timeout():
        oss = _connect_sync()
        n = int(args.get("面", 2))
        attr = _SURFACE_ATTR.get(str(args.get("项", "半径")), "Radius")
        s = oss.LDE.GetSurfaceAt(n)
        original = getattr(s, attr)
        start = float(args.get("起", original - 10))
        stop = float(args.get("止", original + 10))
        step = float(args.get("步", 0)) or max((stop - start) / 12, 1e-6)
        step = max(step, (stop - start) / 12)  # 最多 13 个点:每次点列图都要几秒,别把语音管线挂死
        rows = []
        v = start
        while v <= stop + 1e-9:
            setattr(s, attr, v)
            try:
                rms = _spot_part(oss).get("RMS光斑半径_um")
            except Exception as e:
                rms = None
                rows.append({"值": round(v, 4), "错误": str(e)[:80]})
                v += step
                continue
            rows.append({"值": round(v, 4), "RMS_um": rms})
            v += step
        setattr(s, attr, original)
        _drop_cache()
        good = [r for r in rows if r.get("RMS_um") is not None]
        return {"扫描": f"{n}面{args.get('项', '半径')} {start} → {stop} 步长 {step:.4g}",
                "结果": rows, "最好": min(good, key=lambda r: r["RMS_um"]) if good else None,
                "已恢复原值": original}


_scan_param_handler = _make_handler("scan_param", "正在扫描参数(每个值跑一次点列图)……",
                                    _scan_param_sync, timeout=180, ttl=0)

scan_param_schema = FunctionSchema(
    name="scan_param",
    description=(
        "批量扫描:把某面的半径/厚度/材料/半口径从 起 扫到 止(步长 步),每个值跑一次点列图 RMS,"
        "返回表格和最好的那个值,扫完自动恢复原值。第 9 课'机器替你试遍参数'的活,学生说"
        "'扫一遍''帮我找最好的半径''换个玻璃试试'时调用。"
    ),
    properties={
        "面": {"type": "integer"},
        "项": {"type": "string", "enum": ["半径", "厚度", "材料", "半口径"]},
        "起": {"type": "number"}, "止": {"type": "number"}, "步": {"type": "number"},
    },
    required=["面"],
    handler=_scan_param_handler,
)


def _quick_focus_sync(args=None):
    """跑 Quick Focus 自动对焦(第 1 课那一下),报改完的像面位置。"""
    with _zemax_busy_timeout():
        oss = _connect_sync()
        tool = oss.Tools.OpenQuickFocus()
        if tool is None:
            return {"error": "Quick Focus 打不开:先关掉 Zemax 里开着的其它工具窗口"}
        try:
            tool.RunAndWaitForCompletion()
            ok, err = bool(tool.Succeeded), str(tool.ErrorMessage or "")
        finally:
            tool.Close()
        _drop_cache()
        return {"成功": ok, "报错": err,
                "像面位置": oss.LDE.GetSurfaceAt(oss.LDE.NumberOfSurfaces - 1).Thickness}


_quick_focus_handler = _make_handler("quick_focus", "正在自动对焦……", _quick_focus_sync, timeout=60, ttl=0)

quick_focus_schema = FunctionSchema(
    name="quick_focus",
    description=(
        "跑 Quick Focus 自动对焦:Zemax 自己把像面位置调到最清晰。学生说'帮我对焦''图像糊,调一下像面'时调用。"
    ),
    properties={},
    required=[],
    handler=_quick_focus_handler,
)


# 助教的全部 Zemax 工具。bot.py 只 import 这一个清单 —— 以前这里加一个工具要在
# bot.py 改两处(import 一行 + tools 列表一行),漏一处就是"工具写了但没挂上"。
TOOLS = [
    read_lde_schema,
    read_analysis_schema,
    read_mtf_schema,
    read_color_schema,
    read_aberration_schema,
    read_merit_schema,
    read_screen_schema,
    look_screen_schema,
    run_analysis_schema,
    point_at_schema,
    edit_zemax_schema,
    restore_lens_schema,
    run_optimize_schema,
    scan_param_schema,
    quick_focus_schema,
]

