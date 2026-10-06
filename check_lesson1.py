# -*- coding: utf-8 -*-
"""第1课检查:API 直读当前系统状态,对照理论值逐项报 PASS/FAIL(只读不改)。"""
import zemax_api

oss, zos = zemax_api.get_system(verbose=False)

def cell(s, idx):
    try:
        return s.GetCellAt(idx).Value
    except Exception:
        return None

def show(name, actual, expect, tol=0.01):
    try:
        ok = abs(float(actual) - expect) <= tol
    except (TypeError, ValueError):
        ok = str(actual).strip().upper() == str(expect).strip().upper()
    print(("PASS  " if ok else "FAIL  ") + name + ": " + str(actual) + "  (期望 " + str(expect) + ")")
    return ok

ap = oss.SystemData.Aperture
show("孔径类型", str(ap.ApertureType).split(".")[-1], "EntrancePupilDiameter", tol=1)
show("孔径值(EPD)", ap.ApertureValue, 20)

lde = oss.LDE
print("面数:", lde.NumberOfSurfaces)
if lde.NumberOfSurfaces >= 3:
    s1 = lde.GetSurfaceAt(1)   # 光阑
    s2 = lde.GetSurfaceAt(2)   # 像面
    show("光阑面 曲率半径 R", s1.Radius, 100)
    show("光阑面 厚度", s1.Thickness, 4)
    try:
        mat = s1.Material
    except Exception:
        mat = cell(s1, 3)
    show("光阑面 材料", mat, "BK7", tol=1)
    try:
        print("      半口径(自动):", s1.SemiDiameter, "(EPD=20 应为 10)")
    except Exception:
        pass
    show("像面 厚度", s2.Thickness, 96.07, tol=0.6)

wl = oss.SystemData.Wavelengths
n = wl.NumberOfWavelengths
print("波长数:", n)
for j in range(1, n + 1):
    w = wl.GetWavelength(j)
    print("      波长%d: %s um%s" % (j, w.Value, " (主)" if getattr(w, "IsPrimary", False) else ""))

print()
print("理论对照:R1=100 R2=-100 BK7(n=1.5168) t=4 双凸单透镜 → 焦距 f≈97.4mm,后截距 BFL≈96.07mm")
zos.disconnect()
