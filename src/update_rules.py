# -*- coding: utf-8 -*-
"""
排产 Agent 训练周期 driver：读数据 → 挖规律(画像) → 改写规则 → 回放校验 → 记录长期记忆。

用法：
  python src/update_rules.py run      # 完整训练周期（先日期自检，到期才跑；--force 强制）
  python src/update_rules.py profile  # 生成画像 + 记录数据进度 + 打印历史教训
  python src/update_rules.py replay   # 回放校验 + 记录规则版本与演进轨迹到长期记忆

长期记忆：
  agent_state.json  机器可读状态（周期数、数据进度、规则演进、规则 diff）
  lessons.md        LLM 写的学习教训（每轮追加）
"""

import json
import os
import sys
from datetime import datetime, timedelta

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import loader
import profiler
import replay
from scheduler import load_rules

FROZEN = getattr(sys, "frozen", False)   # 是否打包成 exe（PyInstaller 运行时为 True）
BASE_DIR = os.path.dirname(sys.executable) if FROZEN else os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # 项目根目录（exe 或脚本所在目录）
AGENT_STATE_PATH = os.path.join(BASE_DIR, "agent_state.json")
LESSONS_PATH = os.path.join(BASE_DIR, "lessons.md")


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def load_state():
    if os.path.exists(AGENT_STATE_PATH):
        with open(AGENT_STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {
        "cycle_count": 0,
        "last_cycle_at": None,
        "current_version": None,
        "current_rules": None,
        "seen_train_paths": [],
        "seen_test_paths": [],
        "data_progress": [],
        "rule_history": [],
    }


def save_state(state):
    with open(AGENT_STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def _diff_rules(prev, cur):
    """对比两版规则，返回变化（软规则字段的 [旧值, 新值]）。"""
    if not prev:
        return {"note": "首版规则"}
    sr_prev = prev.get("softRules", {})
    sr_cur = cur.get("softRules", {})
    diffs = {}
    for k in sorted(set(sr_prev) | set(sr_cur)):
        if sr_prev.get(k) != sr_cur.get(k):
            diffs[k] = [sr_prev.get(k), sr_cur.get(k)]
    return diffs


def _print_lessons():
    if os.path.exists(LESSONS_PATH):
        print("\n===== 历史教训 lessons.md =====")
        with open(LESSONS_PATH, encoding="utf-8") as f:
            print(f.read().strip())


def cmd_profile():
    state = load_state()
    ds = loader.load_dataset(replay.DATA_DIRS)

    train_paths = sorted({os.path.relpath(i["source"], BASE_DIR) for i in ds["train"]})
    test_paths = sorted({os.path.relpath(i["source"], BASE_DIR) for i in ds["test"]})
    seen_train = set(state.get("seen_train_paths", []))
    seen_test = set(state.get("seen_test_paths", []))
    new_train = [p for p in train_paths if p not in seen_train]
    new_test = [p for p in test_paths if p not in seen_test]

    state["seen_train_paths"] = sorted(seen_train | set(train_paths))
    state["seen_test_paths"] = sorted(seen_test | set(test_paths))
    state["data_progress"].append({
        "at": _now(),
        "train_files": len(train_paths),
        "test_files": len(test_paths),
        "new_train_files": len(new_train),
        "new_test_files": len(new_test),
    })
    save_state(state)

    p = profiler.profile(ds)
    profiler.write_profile(p)
    profiler.print_profile(p)

    print("\n[数据进度] 训练样本 %d（新增 %d）｜待排产场景 %d（新增 %d）｜已完成周期 %d" % (
        len(train_paths), len(new_train), len(test_paths), len(new_test), state["cycle_count"]))
    _print_lessons()
    print("\n[下一步] 依据画像 + 教训，改写 rules.json，然后运行：python src/update_rules.py replay")


def cmd_replay():
    state = load_state()
    rules = load_rules(replay.RULES_PATH)
    changes = _diff_rules(state.get("current_rules"), rules)

    report = replay.run_replay(rules)
    ts = replay.write_history(report, rules)
    replay.print_report(report)

    state["cycle_count"] = state.get("cycle_count", 0) + 1
    state["last_cycle_at"] = _now()
    state["current_version"] = rules.get("version")
    state["current_rules"] = rules
    state["rule_history"].append({
        "cycle": state["cycle_count"],
        "at": state["last_cycle_at"],
        "version": rules.get("version"),
        "penalty": report["total_penalty"],
        "makespan_avg_h": report["makespan_avg_h"],
        "hard_constraint_ok": f"{report['hard_constraint_ok']}/{report['sample_count']}",
        "changes": changes,
    })
    save_state(state)

    print(f"\n[已记录] 历史记录/rules_{ts}.json ｜ 长期记忆 agent_state.json 已更新（第 {state['cycle_count']} 轮）")
    if changes and "note" not in changes:
        print("[本轮规则变化] " + json.dumps(changes, ensure_ascii=False))


def _check_due(state):
    """日期自检：距上次周期是否已满一个训练周期（training_period_days）。"""
    period = int(state.get("training_period_days", 7))
    last = state.get("last_cycle_at")
    if not last:
        return True, "首次运行（尚无上次周期记录），判定到期。"
    try:
        last_dt = datetime.strptime(last, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return True, "上次周期时间格式异常，判定到期。"
    due_dt = last_dt + timedelta(days=period)
    now = datetime.now()
    if now >= due_dt:
        return True, f"已到期（上次 {last}，周期 {period} 天）。"
    remain = due_dt - now
    return False, f"未到期：上次 {last}，下次到期 {due_dt.strftime('%Y-%m-%d %H:%M:%S')}（还差 {remain.days} 天 {remain.seconds // 3600} 小时）。"


def _append_lesson(cycle, action, changes, cur_p, cand_p, reason):
    """把本轮结论追加写进 lessons.md（长期记忆，下一轮先读）。"""
    lines = [f"\n## 第 {cycle} 轮（自动挖规律，{_now()}）"]
    if cur_p is not None and cand_p is not None:
        lines.append(f"- 决策：{action}（延期惩罚 {cur_p:,.0f} -> {cand_p:,.0f}）")
    else:
        lines.append(f"- 决策：{action}")
    if changes and "note" not in changes:
        lines.append(f"- 规则变化：{json.dumps(changes, ensure_ascii=False)}")
    if reason:
        lines.append(f"- LLM 理由：{reason}")
    try:
        with open(LESSONS_PATH, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except OSError as e:
        print(f"[警告] 写 lessons.md 失败：{e}")


def _record_no_change(state, rules, reason):
    """本轮 LLM 没提出任何改动：记一轮"无变化"，不动 rules.json、不虚增版本号。"""
    state["cycle_count"] = state.get("cycle_count", 0) + 1
    state["last_cycle_at"] = _now()
    state["current_version"] = rules.get("version")
    state["current_rules"] = rules
    state.setdefault("rule_history", []).append({
        "cycle": state["cycle_count"],
        "at": state["last_cycle_at"],
        "version": rules.get("version"),
        "penalty": None,
        "makespan_avg_h": None,
        "hard_constraint_ok": "-",
        "decision": "无变化",
        "changes": {},
    })
    save_state(state)
    _append_lesson(state["cycle_count"], "无变化", {}, None, None, reason)


def cmd_run(force=False):
    """完整训练周期：日期自检 -> 读数据/画像 -> LLM 挖规律 -> 回放对比 -> 留/回滚 -> 记长期记忆。"""
    state = load_state()

    # 0. 日期自检（--force 可跳过）
    if not force:
        due, msg = _check_due(state)
        print("[日期自检] " + msg)
        if not due:
            print("跳过本轮（可用 --force 强制运行）。")
            return

    # 1. 读数据 + 生成画像 + 记录数据进度
    ds = loader.load_dataset(replay.DATA_DIRS)
    train_paths = sorted({os.path.relpath(i["source"], BASE_DIR) for i in ds["train"]})
    test_paths = sorted({os.path.relpath(i["source"], BASE_DIR) for i in ds["test"]})
    seen_train = set(state.get("seen_train_paths", []))
    seen_test = set(state.get("seen_test_paths", []))
    new_train = [p for p in train_paths if p not in seen_train]
    new_test = [p for p in test_paths if p not in seen_test]
    state["seen_train_paths"] = sorted(seen_train | set(train_paths))
    state["seen_test_paths"] = sorted(seen_test | set(test_paths))
    state.setdefault("data_progress", []).append({
        "at": _now(),
        "train_files": len(train_paths),
        "test_files": len(test_paths),
        "new_train_files": len(new_train),
        "new_test_files": len(new_test),
    })
    save_state(state)

    profile = profiler.profile(ds)
    profiler.write_profile(profile)
    print(f"[数据进度] 训练样本 {len(train_paths)}（新增 {len(new_train)}）｜待排产 {len(test_paths)}（新增 {len(new_test)}）｜第 {state['cycle_count']} 轮")

    # 2. 读当前规则 + 历史教训 + 上一轮表现
    current_rules = load_rules(replay.RULES_PATH)
    lessons_text = ""
    if os.path.exists(LESSONS_PATH):
        with open(LESSONS_PATH, encoding="utf-8") as f:
            lessons_text = f.read()
    last_result = state["rule_history"][-1] if state.get("rule_history") else None

    # 3. LLM 挖规律
    import llm as _llm
    _llm_cfg = _llm.load_config()
    print(f"\n[挖规律] 调用大模型生成候选规则（provider={_llm_cfg.get('provider', 'auto')}，model={_llm_cfg.get('model')}）...")
    import mine_rules
    candidate, reason = mine_rules.mine_rules(profile, current_rules, lessons_text, last_result)
    if candidate is None:
        print("[挖规律] 未得到可用候选，保持当前规则不变，本轮结束。")
        return

    # 无实际变化：候选与当前规则一致，不改规则、不重放，仅记录"保持现状"
    if candidate["softRules"] == current_rules.get("softRules", {}):
        _record_no_change(state, current_rules, reason)
        print("[挖规律] 候选与当前规则一致，无改动，保持现状。")
        return

    # 4. 回放对比（当前 vs 候选）
    cur_rep = replay.run_replay(current_rules)
    cand_rep = replay.run_replay(candidate)
    cur_p, cand_p = cur_rep["total_penalty"], cand_rep["total_penalty"]
    cur_m, cand_m = cur_rep["makespan_avg_h"], cand_rep["makespan_avg_h"]

    # 5. 留/回滚：惩罚下降才留；持平则看 makespan
    keep = cand_p < cur_p or (cand_p == cur_p and cand_m <= cur_m)
    if keep:
        with open(replay.RULES_PATH, "w", encoding="utf-8") as f:
            json.dump(candidate, f, ensure_ascii=False, indent=2)
        chosen, chosen_rep, action = candidate, cand_rep, "留下"
    else:
        chosen, chosen_rep, action = current_rules, cur_rep, "回滚"

    # 6. 记录版本轨迹 + 长期记忆 + 教训
    replay.write_history(cand_rep, candidate)
    changes = _diff_rules(current_rules, candidate)
    state["cycle_count"] = state.get("cycle_count", 0) + 1
    state["last_cycle_at"] = _now()
    state["current_version"] = chosen.get("version")
    state["current_rules"] = chosen
    state.setdefault("rule_history", []).append({
        "cycle": state["cycle_count"],
        "at": state["last_cycle_at"],
        "version": chosen.get("version"),
        "penalty": chosen_rep["total_penalty"],
        "makespan_avg_h": chosen_rep["makespan_avg_h"],
        "hard_constraint_ok": f"{chosen_rep['hard_constraint_ok']}/{chosen_rep['sample_count']}",
        "candidate_penalty": cand_rep["total_penalty"],
        "decision": action,
        "changes": changes,
    })
    save_state(state)
    _append_lesson(state["cycle_count"], action, changes, cur_p, cand_p, reason)

    replay.print_report(chosen_rep)
    print(f"\n[决策] {action}：延期惩罚 {cur_p:,.0f} -> {cand_p:,.0f}；makespan {cur_m:.1f}h -> {cand_m:.1f}h")
    if reason:
        print(f"[LLM 理由] {reason}")
    print(f"[已记录] 第 {state['cycle_count']} 轮，长期记忆 agent_state.json 已更新")


def main():
    args = sys.argv[1:]
    cmd = args[0] if args else "run"
    force = "--force" in args
    if cmd == "run":
        cmd_run(force=force)
    elif cmd == "profile":
        cmd_profile()
    elif cmd == "replay":
        cmd_replay()
    else:
        print("用法：python src/update_rules.py [run|profile|replay] [--force]")


if __name__ == "__main__":
    main()
