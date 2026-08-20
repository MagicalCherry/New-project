# -*- coding: utf-8 -*-
"""
排产甘特图可视化：读场景 json → 规则式排产 → 生成自包含 HTML 甘特图。

用法：
  python visualize.py [场景.json] [输出.html]
  默认：python visualize.py test_scenario.json test_gantt.html
"""

import json
import math
import sys
from datetime import datetime

from scheduler import schedule, load_rules

# Tableau 10 分类色板（区分度好，兼顾深浅背景）
PALETTE = ["#4E79A7", "#F28E2B", "#E15759", "#76B7B2", "#59A14F",
           "#EDC948", "#B07AA1", "#FF9DA7", "#9C755F", "#BAB0AC"]


def parse(s):
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S")


def job_color(job_id):
    return PALETTE[(job_id - 1) % len(PALETTE)]


def build_html(scenario, result, t0, t1, rules_version, penalty, makespan_h, delayed):
    mach = {m["machineId"]: m for m in scenario["machines"]}
    jobs = {j["jobId"]: j for j in scenario["jobs"]}
    procs = {p["procedureId"]: p["procedureName"] for p in scenario["procedures"]}

    span = (t1 - t0).total_seconds() / 60
    if span <= 0:
        span = 1

    def pct(dt):
        return (dt - t0).total_seconds() / 60 / span * 100

    def dur_pct(a, b):
        return (b - a).total_seconds() / 60 / span * 100

    # 收集块与预占窗口
    blocks = {}
    for mr in result["machineResults"]:
        mid = mr["machineId"]
        blocks.setdefault(mid, [])
        for b in mr["blockResults"]:
            blocks[mid].append(b)

    preocc = {}
    for b in scenario.get("productionScheduleOtherBlocks", []):
        preocc.setdefault(b["machineId"], []).append(b)

    # 图例行
    legend = "".join(
        f'<span class="lg"><i style="background:{job_color(j["jobId"])}"></i>{j["jobName"]}</span>'
        for j in sorted(jobs.values(), key=lambda x: x["jobId"])
    )

    rows = []
    # 按产线分组输出机器行
    for line in (1, 2):
        line_machines = [m for m in sorted(mach.values(), key=lambda x: x["machineId"])
                         if m["productionLine"] == line]
        rows.append(f'<div class="line-head">产线 {line}</div>')
        for m in line_machines:
            mid = m["machineId"]
            track = []
            for b in preocc.get(mid, []):
                s, e = parse(b["startTime"]), parse(b["endTime"])
                track.append(
                    f'<div class="preocc" style="left:{pct(s):.3f}%;width:{dur_pct(s, e):.3f}%" '
                    f'title="预占：{b.get("description", "")}"></div>'
                )
            for b in blocks.get(mid, []):
                s, e = parse(b["startTime"]), parse(b["endTime"])
                jid = b["jobId"]
                w = dur_pct(s, e)
                label = f"J{jid}" if w >= 1.5 else ""
                qty_label = f"J{jid} {b['plannedQuantity']}kg" if w >= 4.0 else f"J{jid}"
                track.append(
                    f'<div class="block" style="left:{pct(s):.3f}%;width:{w:.3f}%;'
                    f'background:{job_color(jid)}" title="{jobs[jid]["jobName"]}｜'
                    f'{procs[b["procedureId"]]}｜{m["machineName"]}｜{b["plannedQuantity"]}kg｜'
                    f'{b["startTime"]} ~ {b["endTime"]}">{qty_label}</div>'
                )
            rows.append(
                f'<div class="row"><div class="label">{m["machineName"]}</div>'
                f'<div class="track">{"".join(track)}</div></div>'
            )

    # 时间刻度（每 ~20% 一个刻度）
    ticks = []
    for i in range(0, 6):
        frac = i / 5
        d = t0 + (t1 - t0) * frac
        ticks.append(
            f'<div class="tick" style="left:{frac * 100:.1f}%">{d.strftime("%m-%d")}</div>'
        )

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>排产计划甘特图</title>
<style>
  * {{ box-sizing: border-box; }}
  body {{ font-family: -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif; margin: 0; background: #f6f7f9; color: #1f2328; }}
  .wrap {{ max-width: 1280px; margin: 24px auto; padding: 0 20px; }}
  h1 {{ font-size: 20px; margin: 0 0 6px; }}
  .sub {{ color: #59636e; font-size: 13px; margin-bottom: 16px; }}
  .stats {{ display: flex; gap: 18px; flex-wrap: wrap; margin-bottom: 12px; }}
  .stat {{ background: #fff; border: 1px solid #e1e4e8; border-radius: 8px; padding: 10px 16px; }}
  .stat .k {{ font-size: 12px; color: #59636e; }}
  .stat .v {{ font-size: 18px; font-weight: 600; }}
  .legend {{ display: flex; gap: 14px; flex-wrap: wrap; margin-bottom: 16px; font-size: 13px; }}
  .lg {{ display: inline-flex; align-items: center; gap: 5px; }}
  .lg i {{ width: 12px; height: 12px; border-radius: 3px; display: inline-block; }}
  .gantt {{ background: #fff; border: 1px solid #e1e4e8; border-radius: 8px; overflow: hidden; }}
  .line-head {{ padding: 6px 12px; background: #eef1f4; font-weight: 600; font-size: 13px; color: #424a53; border-top: 1px solid #e1e4e8; }}
  .row {{ display: flex; border-top: 1px solid #f0f1f3; height: 34px; }}
  .label {{ width: 150px; min-width: 150px; padding: 8px 10px; font-size: 12px; color: #424a53; border-right: 1px solid #e1e4e8; background: #fafbfc; white-space: nowrap; }}
  .track {{ position: relative; flex: 1; }}
  .block {{ position: absolute; top: 6px; height: 22px; border-radius: 3px; color: #fff; font-size: 10px; line-height: 22px; text-align: center; overflow: hidden; white-space: nowrap; padding: 0 2px; box-shadow: 0 1px 1px rgba(0,0,0,.15); }}
  .preocc {{ position: absolute; top: 7px; height: 20px; border-radius: 3px; background: repeating-linear-gradient(45deg, #d0d7de 0 6px, #e8ebee 6px 12px); }}
  .axis {{ display: flex; border-top: 1px solid #e1e4e8; position: relative; height: 26px; background: #fafbfc; margin-left: 150px; }}
  .tick {{ position: absolute; top: 4px; font-size: 11px; color: #59636e; transform: translateX(-50%); }}
  .tip {{ font-size: 12px; color: #59636e; margin-top: 12px; }}
</style>
</head>
<body>
<div class="wrap">
  <h1>排产计划甘特图</h1>
  <div class="sub">规则式排产引擎（rules.json v{rules_version}）｜{len(jobs)} 个工单｜{len(mach)} 台机器</div>
  <div class="stats">
    <div class="stat"><div class="k">延期惩罚</div><div class="v">{penalty:,.0f}</div></div>
    <div class="stat"><div class="k">总工期（批次完工）</div><div class="v">{makespan_h:.1f} h</div></div>
    <div class="stat"><div class="k">延期工单</div><div class="v">{delayed} / {len(jobs)}</div></div>
    <div class="stat"><div class="k">时间范围</div><div class="v" style="font-size:13px">{t0.strftime("%m-%d")} ~ {t1.strftime("%m-%d")}</div></div>
  </div>
  <div class="legend">{legend}</div>
  <div class="gantt">
    <div class="axis">{''.join(ticks)}</div>
    {''.join(rows)}
  </div>
  <div class="tip">颜色 = 工单；灰色斜纹 = 预占窗口（机器不可用）；鼠标悬停查看详情。</div>
</div>
</body>
</html>"""

    return html


def main():
    scenario_path = sys.argv[1] if len(sys.argv) > 1 else "test_scenario.json"
    out_path = sys.argv[2] if len(sys.argv) > 2 else "test_gantt.html"

    scenario = json.load(open(scenario_path, encoding="utf-8"))
    rules = load_rules("rules.json")
    result = schedule(scenario, rules=rules)

    mach = {m["machineId"]: m for m in scenario["machines"]}
    jobs = {j["jobId"]: j for j in scenario["jobs"]}

    # 时间范围
    all_dt = []
    for mr in result["machineResults"]:
        for b in mr["blockResults"]:
            all_dt += [parse(b["startTime"]), parse(b["endTime"])]
    for b in scenario.get("productionScheduleOtherBlocks", []):
        all_dt += [parse(b["startTime"]), parse(b["endTime"])]
    t0, t1 = min(all_dt), max(all_dt)

    # 指标
    comp = {}
    for mr in result["machineResults"]:
        for b in mr["blockResults"]:
            comp[b["jobId"]] = max(comp.get(b["jobId"], parse(b["startTime"])), parse(b["endTime"]))
    penalty = 0.0
    delayed = 0
    for jid, j in jobs.items():
        if jid not in comp:
            continue
        late = max(0.0, (comp[jid] - parse(j["deadline"])).total_seconds() / 60)
        if late > 0:
            delayed += 1
        penalty += math.exp(j["priority"]) * late
    t_start = min(parse(j["startProductionTime"]) for j in jobs.values())
    makespan_h = (max(comp.values()) - t_start).total_seconds() / 3600

    html = build_html(scenario, result, t0, t1,
                      rules.get("version", "?"), penalty, makespan_h, delayed)

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)

    # 同时把排产结果存成 json 方便查看
    result_path = out_path.rsplit(".", 1)[0] + "_result.json"
    with open(result_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(f"排产完成：{len(scenario['jobs'])} 工单，{sum(1 for mr in result['machineResults'] for _ in mr['blockResults'])} 个排产块")
    print(f"延期惩罚 {penalty:,.0f}｜makespan {makespan_h:.1f} h｜延期工单 {delayed}/{len(jobs)}")
    print(f"甘特图：{out_path}")
    print(f"排产结果：{result_path}")


if __name__ == "__main__":
    main()
