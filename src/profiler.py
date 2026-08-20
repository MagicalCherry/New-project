# -*- coding: utf-8 -*-
"""
规律挖掘（确定性统计层）：把 (场景 → 优解) 训练样本压成一份紧凑画像。

画像是给 LLM 读的——LLM 据此写/调 rules.json，而不是逐文件读原始数据。
所有统计都直接从 input 与 output 对照算出，不涉及复杂建模。
"""

import json
import math
import os
import sys
from collections import defaultdict
from datetime import datetime

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

FROZEN = getattr(sys, "frozen", False)   # 是否打包成 exe（PyInstaller 运行时为 True）
BASE_DIR = os.path.dirname(sys.executable) if FROZEN else os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # 项目根目录（exe 或脚本所在目录）
PROFILE_PATH = os.path.join(BASE_DIR, "历史记录", "profile_latest.json")


def _parse(s):
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S")


def _minutes(a, b):
    return (b - a).total_seconds() / 60.0


def _avg(lst):
    return sum(lst) / len(lst) if lst else 0.0


def profile(dataset):
    """dataset: loader.load_dataset 的返回值。返回紧凑画像 dict。"""
    train = dataset["train"]

    split_hist = defaultdict(int)               # 每(工单,工序)拆台数 -> 次数
    per_proc_split = defaultdict(list)          # procedure -> [拆台数, ...]
    mach_agg = defaultdict(lambda: [0.0, 0])    # (procedure, machineId) -> [totalQty, useCount]
    alloc_ratio = []                            # (量占比/容量占比) 样本，衡量 S3 贴合度
    priority_rank = defaultdict(list)           # priority -> [开工排名]
    changeover_gaps = []                        # 相邻块 gap - setup（分钟）
    changeover_idle = []                        # 相邻对是否留了换单等待(1/0)
    delayed_by_priority = defaultdict(int)
    lateness_all = []
    total_penalty = 0.0
    link_ok = link_total = 0
    multi_line = 0

    for item in train:
        inp = item["input"]
        out = item["output"]
        mach = {m["machineId"]: m for m in inp["machines"]}
        jobs = {j["jobId"]: j for j in inp["jobs"]}

        jobproc = defaultdict(list)     # (jobId, procedureId) -> [(machineId, block)]
        mach_blocks = defaultdict(list)
        for mr in out.get("machineResults", []):
            for b in mr["blockResults"]:
                jobproc[(b["jobId"], b["procedureId"])].append((mr["machineId"], b))
                mach_blocks[mr["machineId"]].append(b)

        # S2 拆台数 / S3 分配vs容量 / S4 机器使用 / H7 产线
        for (jid, p), blocks in jobproc.items():
            split_hist[len(blocks)] += 1
            per_proc_split[p].append(len(blocks))
            lines = {mach[mid]["productionLine"] for mid, _ in blocks}
            if len(lines) > 1:
                multi_line += 1
            caps = {mid: mach[mid]["capacity"] for mid, _ in blocks}
            qtys = {mid: b["plannedQuantity"] for mid, b in blocks}
            tot_cap, tot_qty = sum(caps.values()), sum(qtys.values())
            if tot_cap > 0 and tot_qty > 0:
                for mid in caps:
                    if caps[mid] > 0:
                        alloc_ratio.append((qtys[mid] / tot_qty) / (caps[mid] / tot_cap))
            for mid, b in blocks:
                mach_agg[(p, mid)][0] += b["plannedQuantity"]
                mach_agg[(p, mid)][1] += 1

        # S5 优先级开工顺序
        first_start = {}
        for (jid, _p), blocks in jobproc.items():
            first_start[jid] = min(_parse(b["startTime"]) for _, b in blocks)
        for rank, (jid, _t) in enumerate(sorted(first_start.items(), key=lambda kv: kv[1]), 1):
            priority_rank[jobs[jid]["priority"]].append(rank)

        # S6 换单间隔
        for mid, blocks in mach_blocks.items():
            blocks.sort(key=lambda b: _parse(b["startTime"]))
            setup = mach[mid]["setupTime"]
            for a, b in zip(blocks, blocks[1:]):
                gap = _minutes(_parse(a["endTime"]), _parse(b["startTime"]))
                changeover_gaps.append(gap - setup)
                changeover_idle.append(1.0 if gap >= setup - 1e-9 else 0.0)

        # S7 延期
        comp = {}
        for blocks in mach_blocks.values():
            for b in blocks:
                e = _parse(b["endTime"])
                if b["jobId"] not in comp or e > comp[b["jobId"]]:
                    comp[b["jobId"]] = e
        for jid, j in jobs.items():
            if jid not in comp:
                continue
            lateness = max(0.0, _minutes(_parse(j["deadline"]), comp[jid]))
            if lateness > 0:
                delayed_by_priority[j["priority"]] += 1
                lateness_all.append(lateness)
                total_penalty += math.exp(j["priority"]) * lateness

        # 联动 1:1 验证
        for m in inp["machines"]:
            if m["linkMachineId"] != -1:
                m30, m40 = m["machineId"], m["linkMachineId"]
                for jid in jobs:
                    link_total += 1
                    q30 = sum(b["plannedQuantity"] for b in mach_blocks.get(m30, []) if b["jobId"] == jid)
                    q40 = sum(b["plannedQuantity"] for b in mach_blocks.get(m40, []) if b["jobId"] == jid)
                    if q30 == q40:
                        link_ok += 1

    proc_machine_usage = {}
    for (p, mid), (qty, cnt) in mach_agg.items():
        proc_machine_usage.setdefault(p, []).append(
            {"machineId": mid, "totalQty": round(qty, 1), "useCount": cnt}
        )
    for p in proc_machine_usage:
        proc_machine_usage[p].sort(key=lambda x: -x["totalQty"])

    return {
        "sample_count": len(train),
        "S2_split_machine_histogram": {str(k): v for k, v in sorted(split_hist.items())},
        "S2_per_procedure_avg_split": {str(p): round(_avg(v), 2) for p, v in sorted(per_proc_split.items())},
        "S3_alloc_vs_capacity_ratio_avg": round(_avg(alloc_ratio), 3),
        "S4_machine_usage_by_procedure": {str(p): v for p, v in sorted(proc_machine_usage.items())},
        "S5_priority_avg_start_rank": {str(pr): round(_avg(ranks), 2) for pr, ranks in sorted(priority_rank.items())},
        "S6_changeover_idle_fraction": round(_avg(changeover_idle), 3),
        "S6_changeover_avg_gap_minus_setup": round(_avg(changeover_gaps), 2),
        "S7_delayed_jobs_by_priority": {str(pr): c for pr, c in sorted(delayed_by_priority.items())},
        "S7_total_penalty": round(total_penalty, 1),
        "S7_avg_lateness_minutes": round(_avg(lateness_all), 2),
        "emergent_single_line_violations": multi_line,
        "emergent_link_1to1_ok": f"{link_ok}/{link_total}",
    }


def write_profile(p, path=PROFILE_PATH):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(p, f, ensure_ascii=False, indent=2)


def print_profile(p):
    print(json.dumps(p, ensure_ascii=False, indent=2))


def main():
    import loader
    ds = loader.load_dataset([os.path.join(BASE_DIR, "训练方案数据")])
    p = profile(ds)
    write_profile(p)
    print_profile(p)


if __name__ == "__main__":
    main()
