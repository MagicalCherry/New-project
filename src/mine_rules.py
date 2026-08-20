# -*- coding: utf-8 -*-
"""
自动挖规律：读画像 + 历史教训 + 当前规则 -> 让大模型提出新的软规则。

LLM 只输出 softRules（可调参数）+ 一句 reason；硬规则锁死在 scheduler.py，LLM 碰不到。
输出经过严格校验 + 规范化：非法字段回退到当前值，保证 rules.json 永远合法、不会发出坏规则。
"""

import json
import re
import sys
from datetime import datetime

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import llm

# softRules 各字段的合法取值（与 scheduler.py 的 DEFAULT_RULES / _job_sort_key / _pick_line 对齐）
JOB_ORDERS = ["deadline_first", "slack_first", "priority_then_start", "priority_then_deadline"]
LINE_ASSIGNS = ["load_balance", "round_robin"]
KNOWN_FIELDS = ("splitMaxMachines", "allocProportional", "preferHighCapacity",
                "jobOrder", "lineAssign", "loosenessRiskLine")

_SYSTEM_PROMPT = """你是排产规则优化助手。你负责为一个"规则式贪心排产引擎"调整软规则参数，目标：先降低延期惩罚，其次降低 makespan。

关键前提（务必理解）：
- 硬约束（工序顺序、量守恒、机器唯一性、预占窗口避开等共 12 条）全部由引擎代码保证，无论你怎么改软规则都绝不会违反。你只需要调软规则参数来改善质量。
- 引擎是贪心/列表调度，不是最优求解器。历史上发现"照抄 cpsat 最优解的统计"反而更差，必须针对这个贪心引擎调参，不能想当然。
- 你只能改动 softRules 里的 6 个参数，不能新增硬约束、不能改引擎。

各参数含义与合法取值：
- splitMaxMachines（整数 1~20）：同一工单同一工序最多拆几台机器并行。经验值 3 较好。
- allocProportional（布尔）：true=按机器容量比例分配量；false=均分。经验 false 更好。
- preferHighCapacity（布尔）：true=优先选高容量机器；false=按机器列表顺序选。
- jobOrder（枚举）：工单排序。deadline_first=交期早优先；slack_first=宽松度小优先；priority_then_start=优先级高优先(同级按开工早)；priority_then_deadline=优先级高优先(同级按交期早)。
- lineAssign（枚举）：产线分配。load_balance=负载均衡；round_robin=轮流。
- loosenessRiskLine（浮点）：仅作延期风险告警阈值，不参与排产计算，可保持原值不动。

输出格式：只输出一个 JSON 对象，不要任何其他文字、注释或围栏，形如：
{"softRules": {"splitMaxMachines": 3, "allocProportional": false, "preferHighCapacity": true, "jobOrder": "deadline_first", "lineAssign": "load_balance", "loosenessRiskLine": 12.0}, "reason": "一句话说明为什么这么改"}
"""


def _extract_json(text):
    """从 LLM 返回里抠出 JSON 对象。先整串解析，失败则去掉围栏、取首个 { ... } 块。"""
    text = (text or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    return None


def _validate_field(name, value):
    """校验单个 softRules 字段，返回 (是否合法, 规范化后的值)。"""
    if name == "splitMaxMachines":
        if isinstance(value, bool) or not isinstance(value, int):
            return False, None
        return (1 <= value <= 20), value
    if name in ("allocProportional", "preferHighCapacity"):
        return isinstance(value, bool), value
    if name == "jobOrder":
        return value in JOB_ORDERS, value
    if name == "lineAssign":
        return value in LINE_ASSIGNS, value
    if name == "loosenessRiskLine":
        if isinstance(value, bool):
            return False, None
        if isinstance(value, (int, float)):
            return True, float(value)
        return False, None
    return False, None


def _next_version(cur):
    try:
        return str(int(str(cur)) + 1)
    except (TypeError, ValueError):
        return f"{cur}.1"


def canonicalize(obj, current_rules):
    """把 LLM 输出规范化为一个完整、合法的 rules 对象（与 rules.json 同构）。

    - 只采纳合法的已知字段；非法/缺失字段回退到当前值。
    - version 自动 +1，updatedAt 用今天，learnedRules 保留当前。
    """
    sr_cur = current_rules.get("softRules", {})
    new_sr = dict(sr_cur)
    reason = ""
    if isinstance(obj, dict):
        candidate_sr = obj.get("softRules")
        if not isinstance(candidate_sr, dict):
            candidate_sr = obj
        for name in KNOWN_FIELDS:
            if name in candidate_sr:
                ok, val = _validate_field(name, candidate_sr[name])
                if ok:
                    new_sr[name] = val
        if isinstance(obj.get("reason"), str):
            reason = obj["reason"].strip()

    new_rules = {
        "version": _next_version(current_rules.get("version")),
        "updatedAt": datetime.now().strftime("%Y-%m-%d"),
        "softRules": new_sr,
        "learnedRules": current_rules.get("learnedRules", []),
    }
    return new_rules, reason


def build_prompt(profile, current_rules, lessons_text, last_result):
    """构造 system + user 消息。"""
    user_parts = [
        "## 当前软规则（rules.json）",
        json.dumps(current_rules, ensure_ascii=False, indent=2),
        "",
        "## 历史数据画像（100 个「场景->cpsat 优解」的统计）",
        json.dumps(profile, ensure_ascii=False, indent=2),
    ]
    if last_result:
        user_parts += [
            "",
            "## 当前引擎表现（上一轮回放）",
            f"延期惩罚 {last_result.get('penalty')}，makespan 均值 {last_result.get('makespan_avg_h')}h，"
            f"硬约束 {last_result.get('hard_constraint_ok')}。",
        ]
    if lessons_text:
        user_parts += [
            "",
            "## 历史教训（务必先读，避免重复踩坑）",
            lessons_text.strip(),
        ]
    user_parts += [
        "",
        "请提出一组新的 softRules，目标是降低延期惩罚（其次 makespan）。"
        "只能微调有把握的 1~3 个参数，没把握的保持原值；不要照抄 cpsat 的统计，要针对这个贪心引擎。",
    ]
    return [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": "\n".join(user_parts)},
    ]


def mine_rules(profile, current_rules, lessons_text="", last_result=None, config=None, attempts=3):
    """调用 LLM 提出候选规则，返回 (candidate_rules_dict, reason)；失败返回 (None, None)。"""
    messages = build_prompt(profile, current_rules, lessons_text, last_result)
    last_err = "输出不是合法 JSON"
    for i in range(attempts):
        msgs = list(messages)
        if i > 0:
            msgs.append({
                "role": "user",
                "content": f"上一次输出解析失败：{last_err}。请重新只输出一个合法 JSON 对象，不要任何多余文字。",
            })
        try:
            content = llm.chat(msgs, config=config)
        except RuntimeError as e:
            print(f"[挖规律] LLM 调用失败：{e}")
            return None, None
        obj = _extract_json(content)
        if obj is None:
            last_err = "输出不是合法 JSON"
            continue
        if not isinstance(obj, dict):
            last_err = "JSON 顶层不是对象"
            continue
        return canonicalize(obj, current_rules)
    print(f"[挖规律] 连续 {attempts} 次输出都无法解析，放弃。")
    return None, None
