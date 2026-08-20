# 排产 Agent 每个训练周期的固定流程

> 这是 agent 的核心工作流。周期由 **agent 自己看日期** 触发：跑 `run` 命令时先看距上次周期是否已满一个周期，到期才真正重训；没到期就跳过。

## 一、一个完整训练周期（自动）

`python src/update_rules.py run` 依次做：

1. **日期自检**：读 `agent_state.json` 的 `last_cycle_at` + `training_period_days`（默认 7 天），没到期就打印"未到期"并退出；到期（或加 `--force`）才继续。
2. **读数据 + 画像**：`loader` 递归扫描 `训练方案数据/`，自动识别「场景→优解」训练样本与只含场景的待排产文件；`profiler` 把历史数据压成 `历史记录/profile_latest.json`；同时把新增文件数记进长期记忆。
3. **挖规律（调大模型）**：`mine_rules.py` 把画像 + 当前规则 + `lessons.md` 历史教训 + 上一轮表现拼成 prompt，调 DeepSeek 生成一组新的 `softRules`（只动软规则，硬规则锁死在 `scheduler.py`）。输出经严格校验，非法字段回退到当前值，保证 `rules.json` 永远合法。
4. **回放对比 + 留/回滚**：用新规则跑全部训练样本，验证硬约束 H1~H12、算惩罚/makespan；与当前规则对比，**惩罚下降（或持平且 makespan 不升）才留下，否则回滚**。
5. **记长期记忆**：更新 `agent_state.json`（周期数、规则演进、每轮 diff）、追加 `lessons.md` 一条本轮结论、写 `历史记录/` 快照与时间线。

## 二、手动子命令

```bash
python src/update_rules.py profile      # 只生成画像 + 记数据进度
python src/update_rules.py replay       # 只用当前 rules.json 回放校验
python src/update_rules.py run --force  # 无视日期，立刻跑一个完整周期
```

## 三、硬约束底线

任何情况下硬约束 0 违规是第一前提。`scheduler.py` 把 H1~H12 锁死在排产逻辑里，软规则（无论 LLM 怎么改）都不可能破坏硬约束；软规则只影响惩罚/makespan 的质量。

## 四、长期记忆

| 文件 | 内容 |
|---|---|
| `agent_state.json` | 机器可读状态：周期数、训练周期天数、数据进度、规则演进轨迹、每轮 diff |
| `lessons.md` | 每轮学习结论（"为什么这么改"），下一轮先读 |
| `历史记录/trajectory.jsonl` | 每次回放的惩罚/makespan 时间线 |
| `历史记录/rules_*.json` | 每版规则快照（含被回滚的候选，可回滚） |
| `历史记录/profile_latest.json` | 最新数据画像 |

## 五、周期触发（agent 自己看日期）

- **不再用外部定时任务**。`python src/update_rules.py run` 会自己读 `agent_state.json` 的 `last_cycle_at` 和 `training_period_days`（默认 7 天）判断到没到期。
- 想在别人电脑上**全自动**：让那台机器自己挂一个系统级定时器（Windows 任务计划 / cron），每天调一次 `python src/update_rules.py run` 即可——因为日期判断在 agent 内部，没到期它自己会跳过，不会重复重训。
- 想改周期：编辑 `agent_state.json` 里的 `training_period_days`（单位：天）。

## 六、API 配置（交给别人时）

挖规律默认调 **DeepSeek**；`config.json` 的 `provider: auto` 会自动识别协议，也支持任意 OpenAI 兼容服务以及 Anthropic / Gemini 原生。新主人拿到项目后，只需：

1. 装 Python 3（只用标准库，无第三方依赖）。
2. 编辑 `config.json`，填自己的 `api_key`（也可设环境变量 `LLM_API_KEY`）；换服务商就改 `base_url` 和 `model`，可用 `POST /llm/test` 验证连通后再用。
3. 跑 `python src/update_rules.py run`（或挂系统定时器每天跑一次）。

`config.json` 里 `model` 默认 `deepseek-v4-flash`（快、便宜）；要更强的推理可改 `deepseek-v4-pro`。

## 七、训练数据放哪

把「场景→优解」文件（json，来源 cpsat 或老师傅）丢进 `训练方案数据/`，下个周期跑 `run` 时会被自动识别并计入数据进度。非 json 格式以后按样例扩展 `loader.py`。
