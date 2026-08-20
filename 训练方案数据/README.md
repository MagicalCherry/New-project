# 训练方案数据

agent 学习的输入目录。把「工单场景 → 该场景的排产方案（优解）」成对的 json 丢进这个目录，
下一个训练周期跑 `python src/update_rules.py profile` 时会被自动识别。

- 来源可以是求解器（cpsat）跑出的好结果，也可以是老师傅人工调配的实际结果。
- 每个样本 = `{input: 场景, output: 排产方案}`；`loader.py` 会按结构自动识别「成对」和「只有场景」两类。
- 非 json 格式（csv / excel 等）以后拿到样例再扩展 `loader.py`。
