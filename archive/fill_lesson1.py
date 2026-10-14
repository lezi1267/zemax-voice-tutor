# -*- coding: utf-8 -*-
"""B 方案:API 直接填第 1 课单透镜参数,填完立即读回验证。"""
import zemax_api

oss, zos = zemax_api.get_system(verbose=False)

# 1) 孔径值 EPD=20
oss.SystemData.Aperture.ApertureValue = 20.0

# 2) 光阑面 = 透镜前表面:R100 / 厚4 / BK7
s1 = oss.LDE.GetSurfaceAt(1)
s1.Radius = 100.0
s1.Thickness = 4.0
how = None
try:
    s1.Material = "BK7"
    how = "Material 属性"
except Exception:
    try:
        s1.MaterialCell.Value = "BK7"
        how = "MaterialCell"
    except Exception:
        try:
            s1.GetCellAt(3).Value = "BK7"
            how = "单元格3"
        except Exception as e:
            how = "失败: %s" % e
print("材料写入方式:", how)

# 3) 像面厚度 96(先占位)
oss.LDE.GetSurfaceAt(2).Thickness = 96.0

# 4) 波长 d 光 0.5876um
w = oss.SystemData.Wavelengths.GetWavelength(1)
w.Wavelength = 0.5876

print("--- 读回验证 ---")
print("孔径类型:", oss.SystemData.Aperture.ApertureType)
print("孔径值:", oss.SystemData.Aperture.ApertureValue)
print("光阑面: R=%s T=%s 材料=%s" % (s1.Radius, s1.Thickness, s1.Material))
print("像面厚度:", oss.LDE.GetSurfaceAt(2).Thickness)
print("波长: %s um" % w.Wavelength)
zos.disconnect()
print("完成:以上数字应该已经同时出现在你的 Zemax 界面里。")
