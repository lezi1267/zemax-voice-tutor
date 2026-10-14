# 第 9 课 讲义:ZOS-API + Python —— 把 Zemax 变成你的提线木偶

> 用法:按顺序做。预计 40 分钟。
> 前提:Zemax 开着,交互扩展小窗挂着(你天天用的那套)。

## 一、核心概念(5 分钟)

- **ZOS-API**:Zemax 官方的编程接口,.NET 封装。Python 通过 ZOSPy 库调用,**能做 GUI 里能做的一切,还能批量做**
- **两种模式**:交互扩展(接你正开着的 GUI,实时可见)/ standalone(隐形实例,跑批处理)。日常上课用前者
- **你已经用了一整个课程**:每次问我"RMS 多少""焦距多少""屏幕上写的什么",我调的就是这套 API——read_lde、read_analysis、point_at、read_screen 这些工具全靠它
- 这课的目标:**从"会用我封装好的"变成"会自己写"**

## 二、动手(25 分钟)

1. 建 `my_first_api.py`(就用学习游戏目录,和 zemax_api.py 放一起):

```python
import zemax_api
oss, zos = zemax_api.get_system()

# 读出全部面的半径
for i in range(oss.LDE.NumberOfSurfaces):
    s = oss.LDE.GetSurfaceAt(i)
    print(i, s.Type.Name if hasattr(s.Type, 'Name') else str(s.Type), float(s.Radius))
```

   跑它,对照你的 LDE 界面——数字应该一模一样
2. 写一个**批量扫描**:把第 1 面半径从 90 扫到 110(步长 5),每个值跑一次点列图、记录 RMS:

```python
import ZOSAPI, zospy as zp
for r in [90, 95, 100, 105, 110]:
    oss.LDE.GetSurfaceAt(1).Radius = r
    a = oss.Analyses.New_Analysis(zp.constants.Analysis.AnalysisIDM.StandardSpot)
    a.ApplyAndWaitForCompletion()
    # ToFile 后读 RMS(参考 zemax_tools 里 _read_analysis_sync 的写法)
```

3. 把结果画成折线图(matplotlib),找 RMS 最小的半径——**这就是第 3 课优化干的事,你手工复现了一遍**

> 要顺手把焦距也读出来:用 `AnalysisIDM.CardinalPoints` 分析(写法参考 `zemax_tools._cardinal_part`)。
> 别自己写近轴追迹算焦距——反射面和玻璃库里的牌号会把你自己算的那版算错。

## 三、实验(10 分钟)

**实验 A|API 优化的原子操作**:用 API 做第 3 课的事——把半径设为变量(`s1.CellFlag = ...` 或直接手工循环),调用 `oss.MFE.ApplyAndWaitForCompletion()` + 优化器 `oss.Optimize`——体会"优化器也是 API 的一部分"。

**实验 B|真正的生产力**:写一个循环,把第 6 课库克三片式的**第 1 片玻璃从 BK7 换成所有你猜得到的牌号**(SK16/F2/F15/SF2…),每个跑 RMS——**机器替你试遍玻璃库**,这就是光学设计自动化的雏形。

## 四、自测(能答上来才算过)

1. 交互扩展和 standalone 模式,各自的适用场景?
2. API 读到的数和 GUI 显示的不一致,你应该先怀疑什么?(提示:最后保存状态/刷新)
3. 批量扫描时为什么每次都要 `ApplyAndWaitForCompletion()`?

## 【验收】交两样东西

1. 你的批量扫描折线图(截图或文件)
2. 折线图最低点对应的半径值和 RMS

(交给我核。过了进第 10 课:毕业设计——没有讲义,只有任务书。)
