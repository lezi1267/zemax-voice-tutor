# -*- coding: utf-8 -*-
"""静态体检:扫描每个模块里所有函数用到的全局名字,核对模块是否真的定义/导入了它们。
专防 NameError(比如漏 import 模块)—— import 一个模块是查不出这类错的。
用法: python check_names.py  (只报问题,全过就一行"全部通过")"""
import builtins
import importlib
import symtable
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SKIP = {Path(__file__).name, "selftest.py"}  # selftest 自己不需要体检

missing = []
for f in sorted(HERE.glob("*.py")):
    # 下划线开头的是临时探针:import 它们会把副作用(连 Zemax、写文件)真跑一遍
    if f.name in SKIP or f.name.startswith("_"):
        continue
    try:
        mod = importlib.import_module(f.stem)
    except Exception as e:
        missing.append(f"{f.name}: 导入失败 {type(e).__name__}: {e}")
        continue

    table = symtable.symtable(f.read_text(encoding="utf-8"), f.name, "exec")
    for child in table.get_children():
        if child.get_type() != "function":
            continue
        for sym in child.get_symbols():
            name = sym.get_name()
            if name in dir(builtins):
                continue
            if (sym.is_global() or (sym.is_free() and name not in dir(builtins))) and not hasattr(mod, name):
                missing.append(f"{f.name}:{child.get_name()}() 用了未定义的 {name!r}")

if missing:
    print("\n".join(missing))
    sys.exit(1)
print("check_names: 全部通过")
