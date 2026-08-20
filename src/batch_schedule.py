# -*- coding: utf-8 -*-
"""
一键批量排产：扫 待排工单/ 里的工单 json，用当前 rules.json 逐个排产，
把方案写到 排产结果/，并为每个工单生成甘特图 html。

用法：
  双击 / python batch_schedule.py            # 处理完等回车退出（交互）
  python batch_schedule.py --force           # 忽略已有结果，全部重排
  python batch_schedule.py <任意参数>         # 处理完直接退出（供自动化/任务计划）

输出（排产结果/，与工单文件同名）：
  <工单名>_方案.json    完整方案（machineResults）+ 汇总（硬约束/惩罚/makespan/延期）
  <工单名>_甘特图.html  甘特图（可浏览器打开直观看）

依赖：只用标准库 + 项目内模块（scheduler/replay/loader/visualize），不调 LLM。
"""

import json
import os
import sys
from collections import defaultdict
from datetime import datetime

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import scheduler
import replay
from loader import _is_scenario
from visualize import build_html, parse

FROZEN = getattr(sys, "frozen", False)   # 是否打包成 exe（PyInstaller 运行时为 True）
BASE_DIR = os.path.dirname(sys.executable) if FROZEN else os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INBOX_DIR = os.path.join(BASE_DIR, "待排工单")
OUT_DIR = os.path.join(BASE_DIR, "排产结果")
RULES_PATH = os.path.join(BASE_DIR, "rules.json")


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def load_scenario(path):
    """读文件，兼容裸场景 / {"input": 场景}；返回场景 dict，失败抛异常。"""
    with open(path, encoding="utf-8") as f:
        obj = json.load(f)
    if isinstance(obj, dict) and _is_scenario(obj.get("input")):
        return obj["input"]
    if _is_scenario(obj):
        return obj
    raise ValueError("不是排产场景（缺 jobs/machines/crafts/procedures）")


def process_one(path, force):
    """处理单个工单文件。返回 ('ok', info) 或 ('skipped', None)；异常向上抛。"""
    scenario = load_scenario(path)
    stem = os.path.splitext(os.path.basename(path))[0]
    sol_path = os.path.join(OUT_DIR, stem + "_方案.json")
    gantt_path = os.path.join(OUT_DIR, stem + "_甘特图.html")

    if not force and os.path.exists(sol_path):
        return "skipped", None

    rules = scheduler.load_rules(RULES_PATH)
    result = scheduler.schedule(scenario, rules=rules)

    # 硬约束校验 + 指标
    viols = replay.check_constraints(scenario, result)
    by_rule = defaultdict(int)
    for v in viols:
        by_rule[v[0]] += 1
    penalty, makespan_h = replay.penalty_and_makespan(scenario, result)

    jobs = {j["jobId"]: j for j in scenario["jobs"]}
    comp = {}
    for mr in result["machineResults"]:
        for b in mr["blockResults"]:
            e = parse(b["endTime"])
            comp[b["jobId"]] = max(comp.get(b["jobId"], e), e)
    delayed = sum(
        1 for jid, j in jobs.items()
        if jid in comp and (comp[jid] - parse(j["deadline"])).total_seconds() > 0
    )

    # 方案文件：完整方案 + 汇总
    out_doc = {
        "processedAt": _now(),
        "rules_version": rules.get("version"),
        "input_unicode": result.get("unicode", ""),
        "summary": {
            "硬约束违规数": len(viols),
            "违规明细": {k: v for k, v in sorted(by_rule.items())},
            "延期惩罚": round(penalty, 1),
            "总工期_小时": round(makespan_h, 1),
            "延期工单数": delayed,
            "工单总数": len(jobs),
        },
        "result": result,
    }
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(sol_path, "w", encoding="utf-8") as f:
        json.dump(out_doc, f, ensure_ascii=False, indent=2)

    # 甘特图
    all_dt = []
    for mr in result["machineResults"]:
        for b in mr["blockResults"]:
            all_dt += [parse(b["startTime"]), parse(b["endTime"])]
    for b in scenario.get("productionScheduleOtherBlocks", []):
        all_dt += [parse(b["startTime"]), parse(b["endTime"])]
    t0, t1 = min(all_dt), max(all_dt)
    html = build_html(scenario, result, t0, t1,
                      rules.get("version", "?"), penalty, makespan_h, delayed)
    with open(gantt_path, "w", encoding="utf-8") as f:
        f.write(html)

    return "ok", {
        "path": os.path.basename(path),
        "jobs": len(jobs),
        "viols": len(viols),
        "penalty": penalty,
        "makespan": makespan_h,
        "delayed": delayed,
    }


def process(force):
    if not os.path.isdir(INBOX_DIR):
        os.makedirs(INBOX_DIR, exist_ok=True)
        print(f"[提示] 待排工单文件夹不存在，已创建：{INBOX_DIR}")
        print("把工单 json 放进去后重新运行。")
        return

    files = sorted(f for f in os.listdir(INBOX_DIR) if f.lower().endswith(".json"))
    if not files:
        print(f"[提示] {INBOX_DIR} 里没有 json 工单。")
        return

    rules = scheduler.load_rules(RULES_PATH)
    print(f"找到 {len(files)} 个工单文件，开始排产（规则 v{rules.get('version')}）...\n")

    done, skipped, failed = 0, 0, 0
    total_penalty = 0.0
    for fn in files:
        path = os.path.join(INBOX_DIR, fn)
        try:
            status, info = process_one(path, force)
            if status == "skipped":
                skipped += 1
                print(f"  [跳过] {fn}（已有结果；--force 强排）")
            else:
                done += 1
                total_penalty += info["penalty"]
                print(f"  [完成] {info['path']}｜工单 {info['jobs']}｜硬约束违规 {info['viols']}"
                      f"｜延期惩罚 {info['penalty']:,.0f}｜总工期 {info['makespan']:.1f}h"
                      f"｜延期 {info['delayed']}/{info['jobs']}")
        except Exception as e:
            failed += 1
            print(f"  [失败] {fn}：{e}")

    print("\n===== 汇总 =====")
    print(f"处理 {done} ｜跳过 {skipped} ｜失败 {failed} ｜总延期惩罚 {total_penalty:,.0f}")
    print(f"方案输出目录：{OUT_DIR}")
    if failed:
        print("（有失败文件，请修正后重跑；已成功的不会重复排）")


def main():
    args = sys.argv[1:]
    interactive = not args   # 无参数 = 双击启动 = 处理完停留等回车
    force = "--force" in args
    try:
        process(force)
    finally:
        if interactive:
            try:
                input("\n运行完毕，按回车退出...")
            except EOFError:
                pass


if __name__ == "__main__":
    main()
