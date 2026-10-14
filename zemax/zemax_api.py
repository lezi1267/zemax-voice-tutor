# -*- coding: utf-8 -*-
"""Zemax API 连接公共入口,每课的脚本都从这里拿系统句柄。

用法:
    import zemax_api
    oss, zos = zemax_api.get_system()

默认 extension 模式:接入你已打开的 Zemax 界面,API 的改动实时显示在 GUI 里 —— 上课用这个。

standalone 模式(实测同样可用,约 3.6 秒起,不需要开交互扩展):拉一个不可见的 OpticStudio 实例,
跑完 disconnect() 自动关掉,不占你的界面。适合批量跑脚本、无人值守的优化。
但它不可见,改动不进你的 GUI,所以上课不用它。
"""
import subprocess

import zospy as zp


def _zemax_running():
    # 中文 Windows 下 tasklist 输出是 GBK,不按文本解码,直接搜字节
    out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq OpticStudio.exe"],
                         capture_output=True).stdout.lower()
    return b"opticstudio.exe" in out


def get_system(verbose=True, mode="extension"):
    if mode == "extension" and not _zemax_running():
        raise RuntimeError("Zemax 没在运行:请先打开 OpticStudio,等主界面出来后再试。")
    zos = zp.ZOS()
    try:
        # connect() 本身就返回主系统,直接用它的返回值。
        # 别用 zos.get_system():extension 模式下 Application.NumberOfOpticalSystems 报 0,
        # get_system() 内部走 GetSystemAt() 会抛 ArgumentOutOfRangeException。
        oss = zos.connect(mode=mode)
    except ConnectionRefusedError:
        # ZOSPy 这条报错原文是 "OpticStudio Licence is not valid for API",
        # 其实绝大多数情况只是没开交互扩展模式。
        if mode == "standalone":
            raise RuntimeError("standalone 授权校验未通过(LicenseStatus=%s)。"
                               % zos.Application.LicenseStatus)
        raise RuntimeError(
            "OpticStudio 不在交互扩展模式,或连接位已被占用。"
            "请点菜单 编程 → 交互扩展,把弹出的小窗留着别关(建议取消勾选“连接断开时，自动关闭”);"
            "若语音助手正在运行,连接位是它占的,属正常。再试一次。"
        )
    if verbose:
        print("已连接 Zemax(%s),当前系统 %d 个面" % (mode, oss.LDE.NumberOfSurfaces))
    return oss, zos


def close_standalone(zos):
    """关掉 standalone 拉起的隐形实例;扩展模式下不要调这个(那会关你自己的 GUI 的连接)。"""
    try:
        zos.Application.CloseApplication()
    except Exception:
        pass
    try:  # 实例已关时 disconnect 会报 remoting 断连,忽略即可
        zos.disconnect()
    except Exception:
        pass


if __name__ == "__main__":
    import sys

    oss, zos = get_system(verbose=False)
    print("连接成功,开始读自检...")
    n = oss.LDE.NumberOfSurfaces
    s1 = oss.LDE.GetSurfaceAt(1)              # 0=物面 1=光阑 2=像面
    print("LDE 面数: %d(物面/光阑/像面)" % n)
    print("光阑面: R=%s T=%s 材料=%s" % (s1.Radius, s1.Thickness, s1.Material))
    ap = oss.SystemData.Aperture
    print("系统孔径类型: %s, 值: %s" % (ap.ApertureType, ap.ApertureValue))

    if "--new" in sys.argv:
        # 只有显式 --new 才动数据:oss.new() 会清空学生当前镜头,写半径也会改他的设计
        oss.new()
        oss.LDE.GetSurfaceAt(1).Radius = 100.0
        back = oss.LDE.GetSurfaceAt(1).Radius
        assert abs(float(back) - 100.0) < 1e-6, "读写不一致!"
        print("写入 100.0 → 读回 %s" % back)
        oss.LDE.GetSurfaceAt(1).Radius = 0.0   # 恢复成无穷大,不留测试痕迹
    else:
        print("只读自检(没改任何数据)。要测写入:python zemax_api.py --new(会清空当前镜头!)")

    print("自检通过:API 可读,延迟毫秒级。")
    zos.disconnect()                            # 只断开连接,不会关你打开的 Zemax
