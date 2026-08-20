# -*- coding: utf-8 -*-
"""
规则式排产调度器 v1 —— 排产 Agent 生成 / 维护的"规则式代码"产物。

定位：
- 确定性、低复杂度、秒级出结果，针对本 FJSP 场景（2 产线 × 4 工序）定制。
- 硬规则 H1~H12 编码为排产时必须满足的硬约束逻辑，保证 0 违规。
- 软规则 S1~S7 编码为可调参数 / 启发式，由 Agent 从历史数据挖掘后调整。

时间模型：整数分钟。加工时长 dur = ceil(quantity / capacity)。
转运模型：累计流动（前序工序完成"对应量" + 转运时长后，后序对应量才可完成），
          即任意时刻 t 有 A_{工序N+1}(t) <= A_{工序N}(t - 转运时长)。
"""

import json
import math
import os
from datetime import datetime, timedelta


# 默认软规则（S1~S7 的可调参数）。与 rules.json 同构，load_rules() 用它兜底。
DEFAULT_RULES = {
    "version": "1",
    "softRules": {
        "splitMaxMachines": 3,                  # S2：同工序并行拆台数上限
        "allocProportional": True,              # S3：分配量近似与容量成正比
        "preferHighCapacity": True,             # S4：机器选择偏好高容量
        "jobOrder": "priority_then_deadline",   # S5：工单排序策略
        "lineAssign": "load_balance",           # 产线分配策略（软性）
        "loosenessRiskLine": 12.0,              # S7：延期风险线（仅告警，不参与排产）
    },
    "learnedRules": [],                         # 后续挖出的新软规则
}


def load_rules(path=None):
    """读取 rules.json，合并到默认值上（文件缺失/字段缺失都兜底）。"""
    rules = json.loads(json.dumps(DEFAULT_RULES))
    if path and os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                _deep_merge(rules, json.load(f))
        except (json.JSONDecodeError, OSError):
            pass
    return rules


def _deep_merge(base, extra):
    for k, v in extra.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v


_REF = datetime(2000, 1, 1)


def _to_min(dt):
    return int((dt - _REF).total_seconds() // 60)


def _to_dt(m):
    return _REF + timedelta(minutes=m)


def _parse(s):
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S")


def _fmt(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _job_sort_key(job, rules):
    """软规则 S5：工单排序键。"""
    pri = -job["priority"]
    dl = _to_min(_parse(job["deadline"]))
    st = _to_min(_parse(job["startProductionTime"]))
    order = rules["softRules"].get("jobOrder", "priority_then_deadline")
    if order == "deadline_first":
        return (dl, pri, st)
    if order == "slack_first":
        return (dl - st, pri, dl)
    if order == "priority_then_start":
        return (pri, st, dl)
    return (pri, dl, st)


def _pick_line(line_load, seq, rules):
    """软规则：产线分配策略。"""
    if rules["softRules"].get("lineAssign", "load_balance") == "round_robin":
        return 1 if seq % 2 == 1 else 2
    return 1 if line_load[1] <= line_load[2] else 2


def process_duration(quantity, capacity):
    """加工时长（整数分钟）= ceil(quantity / capacity)。"""
    return int(math.ceil(quantity / capacity - 1e-9))


def split_demand(demand, capacities, min_demand, rules, proc=None):
    """把 demand 拆到若干机器（H2 守恒 + H3 最小分配 + S2/S3/S4 软规则）。

    capacities: [(machine_id, capacity), ...]；proc 用于按工序定制拆台数上限。
    返回 [(machine_id, qty), ...]，qty > 0，sum(qty) == demand，每个 qty >= min_demand。
    """
    sr = rules["softRules"]
    by_proc = sr.get("splitMaxMachinesByProcedure", {})
    split_max = by_proc.get(str(proc), sr.get("splitMaxMachines", 3))
    max_n = min(len(capacities), int(demand // min_demand), split_max)
    max_n = max(max_n, 1)
    if sr.get("preferHighCapacity", True):
        cand = sorted(capacities, key=lambda t: -t[1])[:max_n]
    else:
        cand = list(capacities)[:max_n]
    n = len(cand)
    total_cap = sum(c for _, c in cand)

    if sr.get("allocProportional", True):
        shares = [[mid, int(demand * c / total_cap)] for mid, c in cand]
    else:
        base = demand // n
        shares = [[mid, base] for mid, _ in cand]
    for s in shares:
        if s[1] < min_demand:
            s[1] = int(min_demand)

    diff = demand - sum(s[1] for s in shares)
    i = 0
    while diff != 0:
        s = shares[i % n]
        if diff > 0:
            s[1] += 1
            diff -= 1
        else:
            if s[1] > min_demand:
                s[1] -= 1
                diff += 1
        i += 1

    return [(mid, q) for mid, q in shares if q > 0]


class _Timeline:
    """一台机器的可用时间轴：记录已排块与预占窗口，求最早可行开工时间。"""

    def __init__(self, preocc):
        self.preocc = sorted(preocc)   # [(ws, we), ...] 固定预占
        self.blocks = []               # [(start, end, setup), ...] 已排块

    def earliest_start(self, duration, lower_bound):
        """返回 >= lower_bound 的最早开工，满足：不撞预占窗口（H12）、
        与已排块在时间上不重叠且双向换单等待 >= setup（H4/H5）。"""
        start = lower_bound
        while True:
            end = start + duration
            next_start = None
            for (ws, we) in self.preocc:
                if not (end <= ws or start >= we):      # 与预占窗口重叠
                    if next_start is None or we < next_start:
                        next_start = we
            for (bs, be, setup) in self.blocks:
                # 新块要么在其后（start >= be + setup），要么在其前（end <= bs - setup）
                if not (end <= bs - setup or start >= be + setup):
                    if next_start is None or be + setup < next_start:
                        next_start = be + setup
            if next_start is None:
                return start
            start = next_start


def build_flow(end_qty):
    """由一道工序各块的 (结束时间, 数量) 构造累计完成函数。

    返回 flow(need) -> 前序工序完成 need 量的最早结束时间（分钟）。
    """
    steps = []
    cum = 0
    for (e, q) in sorted(end_qty):
        cum += q
        steps.append((cum, e))

    def flow(need):
        for (c, e) in steps:
            if c >= need - 1e-9:
                return e
        return steps[-1][1] if steps else 0

    return flow


def schedule(scenario, rules=None, line_map=None):
    """规则式排产主入口。scenario = input dict，返回与 output.data 同构的结果。

    line_map: 可选，{jobId: line} 固定产线分配（用于实验/外部指定）；None 则按规则自动分。
    """
    if rules is None:
        rules = DEFAULT_RULES
    inp = scenario
    jobs = inp["jobs"]
    machines = inp["machines"]
    crafts = inp["crafts"]
    trans = inp.get("transTimes", [])
    preocc_raw = inp.get("productionScheduleOtherBlocks", inp.get("productionScheduleOtherBlock", []))

    mach = {m["machineId"]: m for m in machines}
    by_line_proc = {}
    for m in machines:
        by_line_proc.setdefault((m["productionLine"], m["procedure"]), []).append(m)

    craft_procs = {c["craftId"]: c["procedureIds"] for c in crafts}

    transfer = {}
    for t in trans:
        transfer[(t["fromProcedure"], t["toProcedure"])] = int(t["aheadTime"])

    # 联动配对：工序 30 机器 -> 工序 40 机器，按产线分组（H8）
    link_pairs = {}
    for m in machines:
        if m["procedure"] == 30 and m["linkMachineId"] != -1:
            link_pairs.setdefault(m["productionLine"], []).append(
                (m["machineId"], m["linkMachineId"])
            )

    preocc_by_mach = {}
    for b in preocc_raw:
        preocc_by_mach.setdefault(b["machineId"], []).append(
            (_to_min(_parse(b["startTime"])), _to_min(_parse(b["endTime"])))
        )

    timelines = {m["machineId"]: _Timeline(preocc_by_mach.get(m["machineId"], [])) for m in machines}
    results = {}          # machineId -> [block dict]
    job_completion = {}   # jobId -> 完成时间(分钟)

    # 软规则 S5：按 jobOrder 策略排序（默认优先级高先排，同优先级按交期早）
    job_order = sorted(jobs, key=lambda j: _job_sort_key(j, rules))

    # 软规则 H7/负载均衡：产线按已分配 demand 均衡
    line_load = {1: 0.0, 2: 0.0}

    def place(mid, job, proc, qty, lower_bound):
        """把一块放到机器 mid 的最早可行时间，返回 (start, end)（分钟）。"""
        m = mach[mid]
        dur = process_duration(qty, m["capacity"])
        s = timelines[mid].earliest_start(dur, lower_bound)
        e = s + dur
        timelines[mid].blocks.append((s, e, m["setupTime"]))
        results.setdefault(mid, []).append({
            "jobId": job["jobId"],
            "procedureId": proc,
            "craftId": job["craft"],
            "startTime": _fmt(_to_dt(s)),
            "endTime": _fmt(_to_dt(e)),
            "plannedQuantity": qty,
        })
        return s, e

    def schedule_blocks(line, job, proc, demand, min_demand, flow=None, lower_bound=0):
        """排一道工序的所有块。flow=(flow_func, lag) 表示前序累计流动约束。
        返回 (blocks_end_qty, max_end)，blocks_end_qty=[(end, qty), ...]。"""
        cand = by_line_proc.get((line, proc), [])
        caps = [(m["machineId"], m["capacity"]) for m in cand]
        alloc = split_demand(demand, caps, min_demand, rules, proc)
        cum = 0
        out = []
        for mid, q in alloc:
            dur = process_duration(q, mach[mid]["capacity"])
            lb = lower_bound
            if flow is not None:
                flow_func, lag = flow
                cum += q
                # 该块结束时间 >= 前序完成累计量 cum 的时间 + 转运（H9 累计流动）
                lb = max(lb, flow_func(cum) + lag - dur)
            s, e = place(mid, job, proc, q, lb)
            out.append((e, q))
        max_end = max(e for e, _ in out) if out else lower_bound
        return out, max_end

    def schedule_linked(line, job, demand, min_demand, flow30, lower_bound=0):
        """工序 30/40 联动：总量 1:1（H8），时间各排各的（H9 累计流动）。"""
        pairs = link_pairs.get(line, [])
        pair_map = dict(pairs)
        # 以工序 30 机器容量为权重拆量，拆出的每份同时给 30 和 40（保证 1:1）
        caps = [(m30, mach[m30]["capacity"]) for (m30, _m40) in pairs]
        alloc = split_demand(demand, caps, min_demand, rules, 30)

        # 工序 30：累计流动约束来自工序 20
        flow_func, lag30 = flow30
        cum = 0
        out30 = []
        for mid30, q in alloc:
            dur = process_duration(q, mach[mid30]["capacity"])
            cum += q
            lb = max(lower_bound, flow_func(cum) + lag30 - dur)
            s, e = place(mid30, job, 30, q, lb)
            out30.append((e, q))
        pe30 = max(e for e, _ in out30) if out30 else 0

        # 工序 40：累计流动约束来自工序 30，数量与 30 一一对应（联动 1:1）
        flow30_func = build_flow(out30)
        lag40 = transfer.get((30, 40), 0)
        cum = 0
        out40 = []
        for mid30, q in alloc:
            mid40 = pair_map[mid30]
            dur = process_duration(q, mach[mid40]["capacity"])
            cum += q
            lb = max(lower_bound, flow30_func(cum) + lag40 - dur)
            s, e = place(mid40, job, 40, q, lb)
            out40.append((e, q))
        pe40 = max(e for e, _ in out40) if out40 else pe30
        return pe30, pe40

    for seq, job in enumerate(job_order, 1):
        jid = job["jobId"]
        demand = job["demand"]
        min_demand = job["minDemand"]
        if line_map is not None:
            line = line_map[jid]
        else:
            line = _pick_line(line_load, seq, rules)
        line_load[line] += demand

        start_min = _to_min(_parse(job["startProductionTime"]))
        procs = craft_procs[job["craft"]]

        # 工序 10（无前序）
        out10, pe10 = schedule_blocks(line, job, 10, demand, min_demand, lower_bound=start_min)
        flow10 = build_flow(out10)

        # 工序 20（前序 10）
        out20, pe20 = schedule_blocks(
            line, job, 20, demand, min_demand,
            flow=(flow10, transfer.get((10, 20), 0)),
            lower_bound=start_min,
        )

        # 工序 30/40（前序 20；30 与 40 联动）
        _pe30, pe40 = schedule_linked(
            line, job, demand, min_demand,
            flow30=(build_flow(out20), transfer.get((20, 30), 0)),
            lower_bound=start_min,
        )

        job_completion[jid] = pe40

    # 组装输出（与历史数据 output.data 同构，仅输出有块的机器，块按开始时间排序）
    machine_results = []
    for mid in sorted(results):
        blocks = sorted(results[mid], key=lambda b: b["startTime"])
        machine_results.append({"machineId": mid, "blockResults": blocks})

    return {
        "machineResults": machine_results,
        "unicode": inp.get("unicode", ""),
    }
