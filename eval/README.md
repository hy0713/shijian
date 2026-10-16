# 评测协议

`questions.json` 包含 30 道种子题，每类 6 道：直接事实、跨篇章综合、原因分析、错误前提、证据不足。原文锚点由 `scripts/build_questions.py` 对已保存的六份真实数字文本逐字校验；rubric 是待人工复核的判分要点，不是预先跑出的 Agent 答案。

先用两题检查连接，再运行全量（会实际调用配置的 DeepSeek，消耗 Token）：

```sh
uv run python eval/run.py --ids Q01 Q19
uv run python eval/run.py --output eval/runs/experiment-01
uv run python eval/summarize.py eval/runs/experiment-01
```

默认比较同一模型、同一语料的 direct、rag、agent：direct 不带检索上下文，RAG 只检索一次并读取 Top 5，Agent 自主决定查询和补查。RAG 与 Agent 共用结构化回答及引用审核，避免只在 Agent 侧增加审核造成指标误读。联网 Agent 应单独运行 `--methods agent --mode hybrid`，不能把增加外部资料后的成绩直接解释为循环本身的收益。

每次模型实例独立，记录可用的 Token usage、工具调用数、耗时和模型/Embedding 标识。缺少价格配置时 `cost=null`；本版不估价。每题按方法分文件保存；已有文件跳过，失败记录只有实际错误类型，修复后移走该失败文件再重跑。没有执行不会生成分数。

自动指标是已知原文锚点的**已读取证据召回率**、程序引用检查。直接回答无检索证据，锚点召回为 0；没有合适锚点的证据不足题为 null。锚点仅代表部分已知证据，跨片段长摘录或等价表述可能漏计，需人工补核。

模型辅助内容支持审核单独保存在 `model_assisted`；不是史实真实性判定。人工审阅者打开每题 `evidence`、来源页面与 `human_rubric`，填写 `human_review`：

- 证据支持 0/1/2：无可靠支持 / 部分支持或过度推断 / 主要表述均有恰当原文支持。
- 回答完整度 0/1/2：未回应 / 漏掉主要阶段或局限 / 核心问题、时序、局限均覆盖。
- 前提识别 0/1/2：迎合错误前提 / 提及疑点未核查 / 对照人物和时序恰当纠正或保留不确定性。

未人工审核的字段保持 null。没有人工独立复核前，不将 rubric 称为金标准，不声称 Agent 优于普通 RAG，不加入重排器。
