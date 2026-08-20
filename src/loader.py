# -*- coding: utf-8 -*-
"""
数据加载层：灵活识别历史排产数据文件。

递归扫描目录，按结构自动区分两类文件并归一化：
- 训练样本：{input, output} 成对（场景 + 优解），如 task/*.json
- 待排产场景：只有场景（jobs/machines/crafts/...），如 small.json

json 优先，留扩展点给 csv/excel 等后续格式（老师傅人工方案等）。
"""

import json
import os

# 判断"这是一个排产场景"所需的键
_SCENARIO_KEYS = ("jobs", "machines", "crafts", "procedures")


def _is_scenario(obj):
    return isinstance(obj, dict) and all(k in obj for k in _SCENARIO_KEYS)


def load_file(fp):
    """加载单个文件，归一化为 {'train': {...}} 或 {'test': {...}}，不认识则返回 None。"""
    try:
        with open(fp, encoding="utf-8") as f:
            obj = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None

    # 训练样本：{input, output} 成对
    if isinstance(obj, dict) and "input" in obj and "output" in obj:
        inp = obj["input"]
        if _is_scenario(inp):
            out = obj["output"].get("data", {}) if isinstance(obj.get("output"), dict) else {}
            return {"train": {"input": inp, "output": out, "source": fp}}

    # 待排产场景：只有场景，没有优解
    if _is_scenario(obj):
        return {"test": {"input": obj, "source": fp}}

    return None


def load_dataset(dirs):
    """递归扫描若干目录/文件，返回 {'train': [...], 'test': [...]}。

    dirs: 目录或文件路径的列表；不存在的目录会被安全跳过。
    """
    train, test = [], []
    paths = []
    for d in dirs:
        if os.path.isfile(d):
            paths.append(d)
        elif os.path.isdir(d):
            for root, dirs_, files in os.walk(d):
                dirs_[:] = [x for x in dirs_ if not x.startswith(".")]
                for fn in sorted(files):
                    if fn.lower().endswith(".json"):
                        paths.append(os.path.join(root, fn))

    seen = set()
    for fp in paths:
        rp = os.path.abspath(fp)
        if rp in seen:
            continue
        seen.add(rp)
        item = load_file(fp)
        if item:
            if "train" in item:
                train.append(item["train"])
            else:
                test.append(item["test"])
    return {"train": train, "test": test}
