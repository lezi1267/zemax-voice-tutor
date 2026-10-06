# -*- coding: utf-8 -*-
"""静态体检:扫描 bot.py 里所有函数用到的全局名字,核对这个模块是否真的定义/导入了它们。
专防 NameError(比如漏 import 模块)——import bot 是查不出这类错的。
用法: python check_names.py  (无输出 = 通过)"""
import builtins
import symtable
import sys
from pathlib import Path

src = Path(__file__).with_name("bot.py").read_text(encoding="utf-8")
table = symtable.symtable(src, "bot.py", "exec")

import bot  # noqa: E402  拿模块真实命名空间

missing = []
for child in table.get_children():
    if child.get_type() != "function":
        continue
    for sym in child.get_symbols():
        if sym.is_global() or (sym.is_free() and sym.get_name() not in dir(builtins)):
            name = sym.get_name()
            if name in dir(builtins):
                continue
            if not hasattr(bot, name):
                missing.append(f"{child.get_name()}() 用了未定义的 {name!r}")

if missing:
    print("\n".join(missing))
    sys.exit(1)
print("check_names: 全部通过")
