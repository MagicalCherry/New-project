# -*- coding: utf-8 -*-
"""
回放校验：用训练样本验证规则式调度器的硬约束（H1~H12）与目标函数。

- 用 loader 灵活加载数据（不再写死路径）
- 用 rules.json 驱动调度器
- 逐条验证硬约束 0 违规；统计延期惩罚与 makespan；写报告到 历史记录/
"""

import json
import math
import os
import sys
from datetime import datetime, timedelta
from collections import defaultdict

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import loader
from scheduler import schedule, load_rules

FROZEN = getattr(sys, "frozen", False)   # 是否打包成 exe（PyInstaller 运行时为 True）
BASE_DIR = os.path.dirname(sys.executable) if FROZEN else os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # 项目根目录（exe 或脚本所在目录）
RULES_PATH = os.path.join(BASE_DIR, "rules.json")
DATA_DIRS = [os.path.join(BASE_DIR, "训练方案数据")]
HISTORY_DIR = os.path.join(BASE_DIR, "历史记录")
BASELINE_PENALTY = 113540.0   # cpsat 基线：延期总惩罚
BASELINE_MAKESPAN = 233.3     # cpsat 基线：makespan 均值（小时）


def parse(s):
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S")


def check_constraints(inp, out):
    """返回违规列表，每项 (规则号, 详情...)。空列表 = 0 违规。"""
    violations = []
    mach = {m["machineId"]: m for m in inp["machines"]}
    jobs = {j["jobId"]: j for j in inp["jobs"]}
    craft_procs = {c["craftId"]: c["procedureIds"] for c in inp["crafts"]}
    transfer = {(t["fromProcedure"], t["toProcedure"]): int(t["aheadTime"])
                for t in inp.get("transTimes", [])}

    preocc = defaultdict(list)
    for b in inp.get("productionScheduleOtherBlocks", inp.get("productionScheduleOtherBlock", [])):
        preocc[b["machineId"]].append((parse(b["startTime"]), parse(b["endTime"])))

    jobproc = defaultdict(list)   # (jobId, procedureId) -> [(machineId, block)]
    mach_blocks = defaultdict(list)
    for mr in out["machineResults"]:
        for b in mr["blockResults"]:
            jobproc[(b["jobId"], b["procedureId"])].append((mr["machineId"], b))
            mach_blocks[mr["machineId"]].append(b)

    # 逐工单逐工序：H2 守恒 / H3 最小分配 / H6 机器工序 / H7 产线 / H10 开始 / H11 覆盖
    for jid, j in jobs.items():
        for p in craft_procs[j["craft"]]:
            blocks = jobproc.get((jid, p), [])
            if not blocks:
                violations.append(("H11-覆盖缺失", jid, p))
                continue
            total = sum(b["plannedQuantity"] for _, b in blocks)
            if total != j["demand"]:
                violations.append(("H2-量守恒", jid, p, total, j["demand"]))
            lines = {mach[mid]["productionLine"] for mid, _ in blocks}
            if len(lines) > 1:
                violations.append(("H7-跨产线", jid, p, lines))
            for mid, b in blocks:
                if b["plannedQuantity"] < j["minDemand"] - 1e-9:
                    violations.append(("H3-低于最小分配", jid, p, mid, b["plannedQuantity"]))
                if mach[mid]["procedure"] != p:
                    violations.append(("H6-机器工序不符", jid, p, mid))
                if parse(b["startTime"]) < parse(j["startProductionTime"]):
                    violations.append(("H10-早于开始时间", jid, p, mid))

    # 逐机器：H4 不重叠 / H5 换单等待 / H12 预占避开
    for mid, blocks in mach_blocks.items():
        blocks.sort(key=lambda b: parse(b["startTime"]))
        for i in range(len(blocks)):
            for k in range(i + 1, len(blocks)):
                if parse(blocks[i]["endTime"]) > parse(blocks[k]["startTime"]):
                    violations.append(("H4-时间重叠", mid, blocks[i]["jobId"], blocks[k]["jobId"]))
            if i + 1 < len(blocks):
                gap = (parse(blocks[i + 1]["startTime"]) - parse(blocks[i]["endTime"])).total_seconds() / 60
                if gap < mach[mid]["setupTime"] - 1e-9:
                    violations.append(("H5-换单不足", mid, blocks[i]["jobId"], blocks[i + 1]["jobId"], round(gap, 1)))
        for b in blocks:
            bs, be = parse(b["startTime"]), parse(b["endTime"])
            for (ws, we) in preocc.get(mid, []):
                if bs < we and be > ws:
                    violations.append(("H12-撞预占", mid, b["jobId"]))

    # H8 联动总量 1:1
    for m in inp["machines"]:
        if m["linkMachineId"] != -1:
            m30, m40 = m["machineId"], m["linkMachineId"]
            for jid in jobs:
                q30 = sum(b["plannedQuantity"] for b in mach_blocks.get(m30, []) if b["jobId"] == jid)
                q40 = sum(b["plannedQuantity"] for b in mach_blocks.get(m40, []) if b["jobId"] == jid)
                if q30 != q40:
                    violations.append(("H8-联动总量不等", jid, m30, m40, q30, q40))

    # H9 转运：累计流动——A_{N+1}(t) <= A_N(t - lag)
    for jid, j in jobs.items():
        for p in craft_procs[j["craft"]]:
            np = p + 10
            if (jid, np) not in jobproc or (jid, p) not in jobproc:
                continue
            lag = transfer.get((p, np))
            if lag is None:
                continue
            A = sorted((parse(b["endTime"]), b["plannedQuantity"]) for _, b in jobproc[(jid, p)])
            B = sorted((parse(b["endTime"]), b["plannedQuantity"]) for _, b in jobproc[(jid, np)])
            cumA, cumB, i = 0, 0, 0
            for (eB, qB) in B:
                cumB += qB
                while i < len(A) and A[i][0] <= eB - timedelta(minutes=lag):
                    cumA += A[i][1]
                    i += 1
                if cumA < cumB - 1e-9:
                    violations.append(("H9-累计流动违反", jid, p, np))

    return violations


def penalty_and_makespan(inp, out):
    """计算延期总惩罚（e^priority × 分钟）与 makespan（小时）。"""
    jobs = {j["jobId"]: j for j in inp["jobs"]}
    comp = {}
    for mr in out["machineResults"]:
        for b in mr["blockResults"]:
            e = parse(b["endTime"])
            comp[b["jobId"]] = max(comp.get(b["jobId"], e), e)

    penalty = 0.0
    for jid, j in jobs.items():
        if jid not in comp:
            continue
        lateness = max(0.0, (comp[jid] - parse(j["deadline"])).total_seconds() / 60)
        penalty += math.exp(j["priority"]) * lateness

    t0 = min(parse(j["startProductionTime"]) for j in jobs.values())
    makespan_h = (max(comp.values()) - t0).total_seconds() / 3600
    return penalty, makespan_h


def run_replay(rules, data_dirs=DATA_DIRS):
    """用给定规则跑全部训练样本，返回报告 dict。"""
    ds = loader.load_dataset(data_dirs)
    files = ds["train"]
    total_violations = defaultdict(int)
    total_penalty = 0.0
    makespans = []
    n_ok = 0

    for item in files:
        inp = item["input"]
        out = schedule(inp, rules=rules)
        viols = check_constraints(inp, out)
        if not viols:
            n_ok += 1
        for v in viols:
            total_violations[v[0]] += 1
        pen, mks = penalty_and_makespan(inp, out)
        total_penalty += pen
        makespans.append(mks)

    report = {
        "rules_version": rules.get("version"),
        "sample_count": len(files),
        "hard_constraint_ok": n_ok,
        "violations_by_rule": {k: v for k, v in sorted(total_violations.items())},
        "total_penalty": round(total_penalty, 1),
        "baseline_penalty": BASELINE_PENALTY,
        "makespan_avg_h": round(sum(makespans) / len(makespans), 1) if makespans else 0.0,
        "baseline_makespan_h": BASELINE_MAKESPAN,
    }
    return report


def write_history(report, rules):
    """写规则快照 + 报告 + 演进轨迹，返回时间戳。"""
    os.makedirs(HISTORY_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    with open(os.path.join(HISTORY_DIR, f"rules_{ts}.json"), "w", encoding="utf-8") as f:
        json.dump(rules, f, ensure_ascii=False, indent=2)
    with open(os.path.join(HISTORY_DIR, f"replay_{ts}.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    line = {
        "timestamp": ts,
        "rules_version": report["rules_version"],
        "total_penalty": report["total_penalty"],
        "makespan_avg_h": report["makespan_avg_h"],
        "hard_constraint_ok": report["hard_constraint_ok"],
    }
    with open(os.path.join(HISTORY_DIR, "trajectory.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(line, ensure_ascii=False) + "\n")
    return ts


def print_report(report):
    print("=" * 60)
    print("回放校验结果")
    print("=" * 60)
    print(f"规则版本：{report['rules_version']}")
    print(f"训练样本数：{report['sample_count']}")
    print(f"硬约束 0 违规文件数：{report['hard_constraint_ok']} / {report['sample_count']}")
    print()
    print("违规统计（按规则）：")
    if report["violations_by_rule"]:
        for rule, cnt in report["violations_by_rule"].items():
            print(f"  {rule}: {cnt}")
    else:
        print("  （无）—— 全部硬约束 0 违规")
    print()
    print(f"延期总惩罚：{report['total_penalty']:,.1f}  （cpsat 基线 {report['baseline_penalty']:,.0f}）")
    print(f"makespan 均值：{report['makespan_avg_h']:.1f} h  （cpsat 基线 {report['baseline_makespan_h']} h）")


def main():
    rules = load_rules(RULES_PATH)
    report = run_replay(rules)
    ts = write_history(report, rules)
    print_report(report)
    print(f"\n[已记录版本] 历史记录/rules_{ts}.json , 历史记录/replay_{ts}.json")


if __name__ == "__main__":
    main()
