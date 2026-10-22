# 评测协议

## 在线 P1 跨主题种子集

`cross_topic.json` 有30题：中国古代史、中国近现代史、外国史、错误前提、证据不足各6题。它与旧安史之乱三方案基线分开，不能把联网资料增加后的结果归因于 Agent 循环。题目 rubric 是待人工核查的要点，不是金标准；未填固定原文锚点，自动召回率保持 null。少量 `reference_leads` 是核查入口，不作为已读证据传入模型。

```sh
# 完全不连接模型或数据库，只预览范围：
uv run python -m eval.run --questions eval/cross_topic.json --methods agent --mode online --ids Q07 Q13 --dry-run
# 真正运行一题，会消耗模型与工具额度；不应默认运行30题：
uv run python -m eval.run --questions eval/cross_topic.json --methods agent --mode online --ids Q13 --output data/evaluations/my-cross-topic
# 将已有真实worker冒烟记录归档供人工评分，不再调用模型；目标文件已存在时拒绝覆盖：
uv run python -m eval.from_smoke docs/online-p1-boxer.json --id Q07 --output data/evaluations/my-smoke-review
```

实际两道任务见 [在线 P1 验证](../docs/online-p1-verification.md) 和 `docs/online-p1-cases/`。一题partial、一题insufficient_evidence；其余28题未运行，所有人工评分仍为空。线上任务completed只表示执行结束，不等于研究有结论。评测界面 `online-p1` 批次可阅读原文、拒绝表述与失败步骤后手动评分，不能直接抄模型辅助审核当成人工分。

## 原专题基线

当前默认前端为全网研究，原 30 题三方案对比仍固定 `--mode local` 与原始专题语料，避免混入新增网页改变实验条件。单独运行联网 Agent 可用 `--methods agent --mode online --ids Q01`；会真实调用模型及 Search / Fetch，不代表与本地基线公平对比。每次工具请求单独记录状态和可配置的费用估算，超时计费未知；模型金额缺少价格配置仍为 null。跨主题在线 P0 实测及失败案例见 [在线验证](../docs/online-p0-verification.md)。

`questions.json` 包含 30 道种子题，每类 6 道：直接事实、跨篇章综合、原因分析、错误前提、证据不足。原文锚点由 `scripts/build_questions.py` 对已保存的六份真实数字文本逐字校验；rubric 是待人工复核的判分要点，不是预先跑出的 Agent 答案。

先用一题比较三种方案。全量需要明确指定条数上限（会实际调用配置的 DeepSeek，消耗 Token）：

```sh
uv run python eval/run.py --ids Q01
uv run python eval/run.py --max-runs 90 --output data/evaluations/experiment-01
uv run python eval/summarize.py data/evaluations/experiment-01
```

默认比较同一模型、同一语料的 direct、rag、agent：direct 不带检索上下文，RAG 只检索一次并读取 Top 5，Agent 自主决定查询和补查。RAG 与 Agent 共用结构化回答及引用审核，避免只在 Agent 侧增加审核造成指标误读。联网 Agent 应单独运行 `--methods agent --mode hybrid`，不能把增加外部资料后的成绩直接解释为循环本身的收益。

每次模型实例独立，记录可用的 Token usage、工具调用数、耗时和模型/Embedding 标识。模型金额 `cost=null`，不填虚构费用；DeepSeek Search / Fetch 仅按明确配置单独估算工具费。每题按方法分文件保存；已有文件跳过，失败记录保留实际错误类型，重跑应使用新批次并保留原文件。没有执行不会生成分数。

自动指标是已知原文锚点的**已读取证据召回率**、程序引用检查。直接回答无检索证据，锚点召回为 0；没有合适锚点的证据不足题为 null。锚点仅代表部分已知证据，跨片段长摘录或等价表述可能漏计，需人工补核。

模型辅助内容支持审核单独保存在 `model_assisted`；不是史实真实性判定。人工审阅者打开每题 `evidence`、来源页面与 `human_rubric`，填写 `human_review`：

- 证据支持 0/1/2：无可靠支持 / 部分支持或过度推断 / 主要表述均有恰当原文支持。
- 回答完整度 0/1/2：未回应 / 漏掉主要阶段或局限 / 核心问题、时序、局限均覆盖。
- 前提识别 0/1/2：迎合错误前提 / 提及疑点未核查 / 对照人物和时序恰当纠正或保留不确定性。

未人工审核的字段保持 null。没有人工独立复核前，不将 rubric 称为金标准，不声称 Agent 优于普通 RAG，不加入重排器。

## 网页核查与持久化

主机运行时使用 `--output data/evaluations/<批次名>`，本机 API 按 `EVALUATION_DIR` 读取。Compose 将该目录映射到独立 `evaluation-data` 命名卷，可以直接在已启动的 API 容器内运行：

```sh
docker compose exec -T api python eval/run.py --max-runs 90 --output data/evaluations/experiment-01
docker compose exec -T api python eval/summarize.py data/evaluations/experiment-01
```

这里的 exec 只是维护者运行评测脚本，不通过 WebSocket 执行研究。默认只比较 local，固定每种方法的模型、Embedding、原文哈希、预算与核心代码哈希。中断后用同一个命令跳过已保存结果；配置或语料变化会拒绝复用该批次。失败文件保持原样，重跑时使用新批次或先备份该失败文件，不能用后一次成功覆盖失败率。

`--max-runs` 默认 3，按所选题目数 × 方法数在连接基础设施前检查，超限直接停止。恢复批次也按完整计划检查，不会因为已有文件而隐式放大授权范围。401/402 停止该批次；单任务还有 `MAX_MODEL_REQUESTS=16` 与 `MAX_MODEL_TOKENS=60000`。后者依据已返回的 usage 在下一次调用前检查，正在执行的请求可能越过阈值；未返回 usage 的请求无法纳入实际 Token 汇总。它们不构成金额或账号总用量上限，也不覆盖其他进程。

P0 五题专用脚本仍是五题运行，不能把普通 runner 的默认三条限制套用到它。多批回归会累积费用。已保存数据可使用 `python -m eval.audit_usage` 离线审计，不发起任何 API 调用，见 [用量核对](../docs/usage-audit.md)。

用 `--source-manifest docs/p1-manifest.json` 在 local 模式限制为既有基线的资料 ID 与正文哈希；不满足条件直接停止。`python -m eval.p2_replay` 则会对固定 Q19 旧答案执行真实模型审核，默认两次已知错误回放加一次生日字句纠正对照，**同样收费**。纠正生日后，其余子句仍可能缺少对应引文，不是整句有效的金标准正例；旧报告中的 positive_control 标签不能解释为人工确认有效。`--premise-only --repeats 1` 缩小到前提条目的两次回放，仍非盲测。本轮真实用量见 [P2 验证](../docs/p2-verification.md)。

所有真实请求新增单次 input/output/total、序号、已观测累计及缺失用量次数。worker 会将 model_usage 事件保存到 MySQL 并实时推送，断线后可以回放；普通评测、P0 与 P2 CLI 也逐次打印。returned 仅表示响应返回，不代表结构化或内容校验通过。金额仍为 null，不能用 Token 合计当作扣款金额。

“效果评测”页面显示真实批次和逐题结果；点击“核查”阅读回答、已读原文及独立模型审核，再由实际审阅者填写评分。没有人工核查者时不要提交评分。该界面用于单人本地研究，不支持多用户并发合并评分；最后一次提交生效。回答有引用不代表每条引用有足够内容支持。

`summarize.py --output <文件>` 导出真实汇总，包括锚点读取、耗时、Token、工具调用、执行失败和需关注的证据不足案例。直接回答没有可定位的已读证据，其锚点读取为零是协议定义，不是历史知识错误率。单次运行的耗时受网络、缓存和本机负载影响，不作吞吐或稳定性基准。

## P0 五题子集

`python -m eval.p0 --output eval/runs/p0-new` 只运行 Q01/Q07/Q13/Q19/Q25 的 Agent，实际调用模型。`--report-only` 只读现有文件，`--baseline <directory>` 生成同题 Token 对比。引用定位检查不代表内容支持，人工评分仍单独留空。已运行结果与失败边界见 [P0 验证](../docs/p0-verification.md)。
