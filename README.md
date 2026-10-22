# 史鉴

一个面向求职展示的 Python 历史资料研究 Agent。默认通过 DeepSeek Search / Fetch 研究古今中外的历史问题：先检查前提，再搜索、阅读、根据缺口补查，最后输出可追溯到实际读取正文的回答。联网范围扩大不代表所有历史问题都能可靠回答；读不到证据时明确说明缺口。

例如“为什么安禄山和史思明会翻脸？”不能直接生成原因。系统需要核查人物与先后关系，对照安禄山、安庆绪与史思明相关记载，判断问题是否混淆了人物；证据不足时保留不确定性。仓库不提供冒充真实模型结果的预制答案。

## 功能与当前验证边界

- 默认「全网历史研究」，不搜索预装资料；可明确选择「限定资料研究」，仅检索当前资料库。旧 hybrid 任务仍兼容。
- DeepSeek Search 提供候选链接，Fetch 读取正文；这两项是独立工具 API，普通聊天调用不会自动联网。Tavily、中文维基文库作为可选搜索适配保留。
- TXT、Markdown、结构化 JSON、网页正文导入，保存版本、来源、原文、哈希与片段位置。
- jieba + 中文二元字组倒排 BM25，与 Milvus BGE 向量检索按 RRF 融合；支持来源、人物和主题元数据过滤。
- LangGraph 真实条件分支与有界循环，模型根据证据缺口选择四个 LangChain 工具；不是固定顺序的 RAG 包装。
- 引用程序检查 + 模型辅助内容检查。候选表述没有通过检查时从正式回答移除，保留审核记录；内容审核不保证史实真实。
- Redis 队列独立 worker，MySQL 持久化任务、事件、已读证据和最终引用；WebSocket 重连回放，浏览器断开不影响任务。
- 原生 HTML/CSS/JavaScript 工作台：独立欢迎页、带固定地址的研究详情、阶段进度、失败重试、证据阅读及来源对照；资料库支持筛选、元数据编辑、原文查找与后台索引。
- 界面已整体重做：深墨绿导航、暖白阅读区、古籍书目入口及手机抽屉，保持研究与资料功能；截图与实际检查见 [界面重做](docs/ui-redesign-verification.md)。
- 评测页面可逐题查看直接回答 / RAG / Agent 的实际记录和引用，独立保存人工评分；按需文献入口支持维基文库官方 API 目录与固定修订快照。
- P2 增加原文先释读、逐项支持核查、安全失败诊断、逐次用量与 Markdown 导出；已部署并完成单题真实回归，完整三方案复评与人工评分尚未完成，见 [P2 验证](docs/p2-verification.md)。
- 本次在线优先 P0 增加 Search / Fetch 协议、按任务的工具费估算与独立上限、无需等待向量索引的网页阅读；真实运行和失败案例见 [在线 P0 验证](docs/online-p0-verification.md)。原有安史之乱文献保留为专题演示库与回归资料。
- 在线 P1 增加来源与语种线索筛选、长网页目录及全文内重新定位、中英文查询计划、原文与中文译述对照、可能转载提示，以及独立的 30 题跨主题种子集。117 项测试通过；实际只运行两题，一题部分核查、一题证据不足，人工评分为空，见 [在线 P1 验证](docs/online-p1-verification.md)。
- 在线 P2 增加主动收藏、按需异步索引、按人物 / 事件 / 地区整理专题，以及固定专题资料范围的研究。已有资料保留，新网页先作为证据快照，收藏后再参与本地检索；验证范围见 [在线 P2 验证](docs/online-p2-verification.md)。

2026-10-19 已完成 P0 修复与五题真实运行，60 项测试通过；当前五题中 2 题有据回答、3 题部分核查，独立人工语义评分仍待完成，见 [P0 验证](docs/p0-verification.md)。

2026-10-20 已完成 P1 交互与资料索引闭环，69 项单元测试及另行执行的 2 项真实基础设施测试通过；30题三方案共90次真实运行，Agent有1次执行失败，解释错误案例也原样保留。独立人工评分仍为空，不能据此宣称回答质量领先，见 [P1验证](docs/p1-verification.md)。

已实际完成的验证、测试替身范围、尚未验证的条件见 [验证记录](docs/verification.md)。**代码实现、协议测试通过，不等于真实模型和完整 Compose 部署通过。**

P1 的交互、异步索引与评测进度见 [P1 验证](docs/p1-verification.md)，史料扩展方案见 [专题基础库与按需卷章](docs/corpus-strategy.md)。

## 架构

```mermaid
flowchart LR
    UI[浏览器] --> API[FastAPI]
    API --> DB[(MySQL：资料 / 任务 / 事件 / 引用)]
    API --> R[(Redis：队列 / 通知)]
    R --> W[独立 worker]
    W --> G[LangGraph 单个研究 Agent]
    G --> L[DeepSeek / LangChain]
    G --> C[本地检索与阅读]
    C --> DB
    C --> V[(Milvus：BGE 向量)]
    G --> S[独立搜索服务 / 受限网页读取]
    W --> DB
    W --> R
    R --> WS[WebSocket：提示 + 数据库回放]
    DB --> WS
    WS --> UI
```

工作流：`analyze → choose → execute → assess → choose / answer → check → revise / END`。引用审核导致前提或核心答案缺失时最多修订一次，再次审核。`choose` 由 DeepSeek 工具调用决定检索和阅读行为；`assess` 判断证据是否足够、前提是否支持、还缺什么并改写查询。满足条件、没有选择工具或达到轮数/调用/时间预算时进入回答。达到总超时会返回已读来源及明确的“未完成核查”，不会补写结论。

模型适配器显式发送 `thinking.type=disabled`：本项目的结构化输出指定函数选择，与 DeepSeek 默认开启的思考模式不兼容。该选择保留环境配置的模型名称，也避免内部推理内容进入研究协议。依据 [DeepSeek 官方思考模式说明](https://api-docs.deepseek.com/guides/thinking_mode/)，并已用真实请求验证。

主要文件：`corpus.py`（原文/关键词）、`vectors.py`（Embedding/Milvus）、`web.py`（安全抓取）、`deepseek_web.py`（搜索/读取协议与计量）、`source_reading.py`（选源线索/目录/转载提示）、`agent.py`（图/工具）、`citations.py`（引用）、`tasks.py`（状态/租约）、`worker.py`、`api.py`。没有额外微服务、角色群、知识图谱和复杂权限系统。

## 完整本地启动

需要 Docker Desktop 的 Linux 引擎（Windows 用 WSL2）、Docker Compose v2。为 Docker 分配至少 4 核 / 8GB 内存作为起点，开发机建议 16GB 总内存并留 10–15GB 磁盘；这是容量规划建议，整套部署峰值尚未实测。[Milvus 官方资源要求](https://milvus.io/docs/v2.6.x/prerequisite-docker.md) 还涉及 SIMD 支持和磁盘延迟。

```sh
cp .env.example .env
# Windows PowerShell：Copy-Item .env.example .env
```

编辑 `.env`：填写 `DEEPSEEK_API_KEY`、`DEEPSEEK_BASE_URL` 和 `DEEPSEEK_MODEL`，模型须支持工具调用及 function-calling 结构化输出。三项都由环境变量读取，代码没有聊天模型名称或 Key 的默认值。默认 `SEARCH_PROVIDER=deepseek`，`DEEPSEEK_TOOLS_API_KEY` 留空时复用聊天 Key；工具地址独立由 `DEEPSEEK_TOOLS_BASE_URL` 配置，不能假定聊天中转地址也支持工具 API。

默认工具地址 `https://api.deepseek.com`，实际分别调用 `POST /v1/tools/search` 与 `/v1/tools/fetch`。Search 只取 5 条候选且 `need_content=false`，摘要不能引用，必须 Fetch 取得正文。依据 [DeepSeek Search 指南](https://api-docs.deepseek.com/zh-cn/guides/search/)、[Search 协议](https://api-docs.deepseek.com/api/search/) 与 [Fetch 协议](https://api-docs.deepseek.com/api/fetch/)。2026-10-20 官方中文价格为成功搜索/读取各 5 元/千次，示例配置每次估算 0.005 元；请按账户实际价格维护配置。

`SEARCH_PRICE_CNY_PER_1000`、`FETCH_PRICE_CNY_PER_1000` 只影响工具费估算，删掉对应环境变量即不估算。界面逐次显示模型输入/输出 Token 与工具返回状态，分别计量；工具费不含聊天模型费用，也不是账户账单。超时或响应无法解析的调用标为计费未知，不当作免费。默认每任务最多 3 次搜索、4 次抓取、4 轮、16 次工具选择、16 次模型请求、60,000 个已观测 Token、240 秒总时间；在途请求可能超过已观测 Token 阈值，取消也不保证服务端不计费。

选择 `tavily` 时另需 `SEARCH_API_KEY`，`SEARCH_API_URL` 必须符合 [Tavily 请求格式](https://docs.tavily.com/documentation/api-reference/endpoint/search)；选择 `wikisource` 无需搜索 Key，但范围仅限中文维基文库，不是全网。界面显示当前服务范围。缺少联网配置会明确阻止全网模式，仍可手动选择限定资料模式；不会静默降级或伪造搜索结果。

`.env.example` 的 MySQL / MinIO 密码是本机开发示例，启动前可更换。Compose 的数据库 URL 直接使用 `MYSQL_PASSWORD`，请使用 URL 安全字符，复杂密码需要自行填写已编码的连接串并调整 Compose 环境覆盖。服务只绑定 127.0.0.1，本版没有登录和多租户隔离，不适合直接开放公网。

```sh
docker compose up -d --build --wait --wait-timeout 180
docker compose ps
# migrate 服务在 MySQL 健康后执行 alembic upgrade head，成功后启动 API / worker
docker compose run --rm worker shijian embedding-check
# 以下演示文献导入可选：全网研究无需预先导入或等待向量索引。
docker compose run --rm worker shijian import data/demo/corpus.json
docker compose run --rm worker shijian import data/demo/web_pages.json
```

打开 [史鉴前端](http://localhost:8000) 和 [接口文档](http://localhost:8000/docs)。CLI 导入同步建索引；API/网页上传先保存原文，再自动交给 `index-worker` 异步索引，无需手工执行命令。资料详情显示排队、已完成片段数、失败原因及重试入口。失败时原文保留，修复 Embedding/Milvus 后可重试；CLI 的 `reindex` 仍可用于维护。

MinIO 的历史 Hub 镜像及 Quay 仓库在本环境已无法匿名取得，因此 `deploy/minio/Dockerfile` 从官方固定源码提交 `f92beb79b555ca0762e01d5aca11e531353e9eae` 构建原定 2024-05-28 版本，未换用其他对象存储或不明镜像。首次会额外下载 Go 构建工具与模块并编译，可能需要数分钟；后续使用 Docker 构建缓存。镜像保留 AGPL 许可及对应源码压缩包，位置为 `/usr/share/minio`。源码构建依据 [MinIO 官方版本](https://github.com/minio/minio/tree/RELEASE.2024-05-28T17-19-04Z)。

首次构建会下载 Python 依赖和 CPU 版 PyTorch；本机 Windows CPU wheel 约 591MiB，Linux wheel 大小不同。首次索引另需下载 BAAI/bge-small-zh-v1.5 权重及 tokenizer，FP32 权重约 100MB，缓存和依赖需额外空间。Embedding 使用 CPU、归一化 512 维向量，模型 revision 固定为 `7999e1d3359715c523056ef9478215996d62a620`；加载方式是 SentenceTransformer，`trust_remote_code=False`。实际资源验证见验证记录，不承诺未测的并发吞吐。

构建镜像时不下载模型权重；启动 API 不要求聊天 Key 已配置，可先查看页面和导入。创建研究任务时检查模型配置。导入与索引不需要 DeepSeek Key，公开 BGE 模型不要求 Hugging Face Token。下载失败请修复网络/证书，不要关闭 TLS 校验。

模型缓存完整后可在 `.env` 设置 `HF_HUB_OFFLINE=1` 并重新 `docker compose up -d api worker`，让本地 Embedding 直接加载固定缓存，避免远程元数据检查耗尽单次工具预算。这只影响 Hugging Face 模型加载，DeepSeek 和独立搜索 API 仍正常联网。首次下载或更换模型时应先设回 0；未下载完整时开启离线会明确失败。

## 在主机上开发

固定 Python 3.11，直接依赖固定版本，`uv.lock` 锁定传递依赖和分平台哈希，PyTorch 使用独立 CPU 索引。

```sh
uv python install 3.11
uv sync --locked --extra embedding
docker compose up -d mysql redis etcd minio milvus
uv run shijian migrate
uv run shijian import data/demo/corpus.json
uv run uvicorn shijian.api:app --reload --host 127.0.0.1 --port 8000
# 第二个终端
uv run shijian worker
# 第三个终端：上传资料的索引 worker
uv run python -m shijian.indexing
```

本次工作区已经有 `.python/` 下的 Python 3.11.15 和 `.venv/`；它们属于本地工具环境并被忽略，不是应提交的源码。

如果只运行无服务测试，可以 `uv sync --locked`，无需安装 Embedding 额外依赖。测试用 SQLite、fakeredis、明确标注的 ScriptedModel/FakeVectors 隔离外部依赖；**生产没有这些替代后端或假模型开关**。

## 导入自己的资料

结构化 JSON 是一个 DocumentInput 对象或对象数组（单次最多 50 份）：

```json
{
  "title": "实际资料标题",
  "author": null,
  "source_type": "历史文献",
  "chapter": null,
  "edition": "实际底本或数字修订信息；未知部分写明未知",
  "source_url": null,
  "raw_text": "此处填写真实取得且有使用权的完整原文，不能用本示例占位文字冒充史料。",
  "people": ["史思明"],
  "topics": ["安史之乱"],
  "provenance_note": "说明来源、授权、转录方式和可靠性限制"
}
```

TXT/Markdown 需要另附元数据 JSON（同上但不含 raw_text）：

```sh
uv run shijian import path/to/source.txt --metadata path/to/metadata.json
uv run shijian import path/to/source.md --metadata path/to/metadata.json --metadata-only
uv run shijian reindex
uv run shijian search "史思明 安庆绪"
```

HTTP 接口 `POST /api/sources` 接收结构化对象，`POST /api/sources/upload?metadata=...` 接收文件，`POST /api/sources/page` 接收 `{ "url": "https://..." }`。单文件上限 2MB，单资料文本上限 50 万字符。前端已经提供入口。

原文不作繁简转换。卷章由资料元数据划定，每份卷章内按段落、句末边界切分至最多 420 字符；位置是该份已保存 `raw_text` 的 Python Unicode code point `[start,end)`，不是影印页码或原网站字节偏移。片段之间保存 `prev_id/next_id`，引用可查看前后各一个片段。

辅助检索字段使用 OpenCC 繁转简、jieba 自定义人名词典及二元字组；倒排表存 MySQL 中的 token/词频，BM25 在过滤后的片段集合上计算，**不依赖 MySQL 默认 FULLTEXT 中文行为**。向量和关键词各取 Top 30，用 `1/(60+rank)` 的 RRF 融合，默认返回 8 条。人物/主题过滤基于导入元数据，未标注的资料不会自动被算入该过滤项。第一版不接重排器，等待评测证明有收益。

## 证据、去重与一致性

检索摘要不能直接引用。`read_passage` 或 `fetch_page` 实际读取后，将不可变证据快照登记到任务，回答用结构化 `evidence_id + quote` 引用。程序核对 ID 属于本任务、摘录是原文连续子串；标题、卷章、URL 和位置从服务端快照生成，不接受模型提供的来源位置。核查后的结构化引用保存在任务最终 JSON，`task_evidence` 保存片段关联和快照。

全网模式只在本次刚抓取的正文内用关键词定位片段及邻文，不检索预装安史之乱资料，也不依赖 Embedding/Milvus 成功。全文保存至 MySQL，未收藏的新快照状态为 `unrequested`，不会进入向量队列。主动收藏或加入专题后后台建立索引，供以后的限定资料研究使用；手动导入属于主动收藏。DeepSeek Fetch 返回的是提取后的 Markdown，位置基于保存的快照；读取时间、请求 URL、版本未知说明随证据保存，不冒称取得原始 HTML、固定修订或实际重定向终点。摘录点击后可核对原文与上下文。

### 收藏与研究专题

在已读网页、引用原文或文献详情中点击「收藏资料」，原文和片段 ID 不变，后台复用现有索引 worker；已就绪的相同正文不重复索引。资料库可切换「已收藏资料」「未收藏的研究快照」「所有资料与快照」。取消收藏只排除未来的本地检索，并撤下尚未开始的索引任务；已经运行的索引可完成，历史任务和证据仍可阅读，不会删除原文、向量或引用。快照暂不设自动清理期限，因此仍需关注 MySQL 磁盘空间。

「研究专题」支持名称、说明、人物 / 事件 / 地区标签，以及关联已有文献和研究记录。加入文献会同时收藏；移出专题、移除专题均只删除分类关联。取消收藏不会自动移出专题，详情会提示未就绪。已有资料迁移时全部保留为已收藏，旧任务的检索范围为空值，保持原语义。

从专题点击「使用专题资料研究」会进入限定资料模式。创建任务时固定该专题当时的资料 ID（1–30份），并要求全部已收藏、已完成当前 Embedding 配置的索引；空专题或未就绪不会悄悄回退到全库。随后编辑专题不改变进行中任务的范围。worker 启动前再检查就绪情况，失效时失败并说明，尚未调用模型；重试使用原固定范围。全网模式保持独立，需要归档时在研究详情加入专题。

相关 API：`PUT /api/sources/{id}/bookmark`（`{"saved":true}`）、`GET/POST /api/collections`、`GET/PUT/DELETE /api/collections/{id}`、`PUT/DELETE /api/collections/{id}/sources/{source_id}` 与 `/tasks/{task_id}`。列表支持 `query/person/event/region`，专题详情每页30条，通过 `source_offset/task_offset` 翻页。创建任务可传 `collection_id`，或传 `source_ids` 重试固定范围，两者互斥且仅限 local。API 无复杂权限系统，仍仅面向个人本机环境。

实际读取的相邻上下文也会登记独立片段 ID。若模型用中心 ID 引用了已登记相邻段落中的逐字原文，程序仅在这两个相邻片段内定位并返回正确片段 ID 和位置；不跨来源或任意已读段落寻找替代，不做模糊匹配、繁简转换或修补原文。引用候选句从原文直接切出，未通过的候选引用保留在审核记录中。

正式回答区分文献记载、后人解释、推断和无法判断，提供前提判断、核心结论、关键事件、不同来源分歧及局限。模型对内容支持不足的条目会被移除；模型也可能误判，所以前端始终保留原文入口和审核说明。模型自由生成的证据局限仍应人工阅读，不能自动当成史实结论。

URL 规范化去 fragment、默认端口与常见跟踪参数，并排序查询参数。同正文去空白后的 SHA-256 全局唯一，重复导入不再建立片段，新增地址记录到 `source_locations`，首份元数据保留。来源的 `content_hash` 是**去空白正文哈希**；采集清单的 `raw_sha256` 是原文精确哈希。近似转载、广告改变正文和共享史源未做可靠自动识别，不用来源数量代替独立证据数量。

MySQL 与 Milvus 不做跨库事务：先提交原文、片段及倒排，状态 pending；索引 worker 通过 MySQL 条件更新领取任务，向 Milvus 按稳定 passage_id 每批最多 32 段 upsert，全部成功后改 ready 并记录 Embedding 标识。检索只使用 ready 且模型标识匹配的 MySQL 资料，Milvus 孤立行不会成为证据。中途异常标为 failed，保留原文、已处理片段数及脱敏错误，通过详情页重试或 `reindex` 幂等修复。

索引队列同样以 MySQL 为准，Redis 丢通知会补投；一次一个资料，可独立增加索引 worker。120 秒租约、15 秒心跳，worker 异常退出后过期任务标为 failed 并提示重试，不自动无限重跑。重复消息无法重复领取，过期 owner 无权提交进度或覆盖新任务。进度表示已提交的 Milvus 批次，不表示模型下载百分比；资料只有全部完成才参与检索。

更换 Embedding 模型、revision、维度或编码策略时，设置**新 `MILVUS_COLLECTION`**，重启 API/worker 后 `shijian reindex --all`。集合 description 校验模型标识，拒绝把相同维度但不同模型混用。重建索引会暂时缩小已匹配资料范围；本版没有无停机双索引切换。

## 任务执行与恢复

`pending → running → completed / failed / cancelled`。创建请求先落 MySQL，再投 Redis；Redis 暂不可用会返回已保存待补投的提示。worker 扫描 pending 任务补投，Redis 的 60 秒短期入队标记减少重复消息；SET 与 LPUSH 间崩溃最多等该标记过期后补投。最终执行资格来自 MySQL 条件更新，重复消费拿不到 running 所有权，不重复生成答案。

worker 默认一次运行一个任务，可扩容多个 worker。默认每 15 秒更新 120 秒租约，工具及模型节点边界检查取消与所有权。失去 worker 的 running 任务在另一 worker 或重启后的清理中标为 failed（若已请求取消则 cancelled），保留已读证据和事件，不自动付费重试。一个任务不跨进程从中间图节点恢复，需用户新建研究。长期无 worker 时需要先恢复 worker 才会执行清理。

取消 pending 任务立即生效；running 任务记录 cancel_requested，在模型/工具返回后边界停止，最终提交也会复核取消状态。已发出的外部 HTTP/模型请求不保证能在服务端撤销或停止计费；客户端有请求超时。DNS/同步线程返回可能晚于协程取消，但网页网络线程没有持久化副作用；已完成导入的资料不会因取消删除。

WebSocket 使用 `task_id` 和全局单调 `event_id`，事件含类型、UTC 时间和 data。前端记录游标，重连 `?after=...` 从 MySQL 回放。Redis Pub/Sub 只用于唤醒，即使丢通知也会轮询持久化事件。事件仅含步骤、工具摘要、新来源与最终状态，不传内部模型消息和思维链。第一版不做答案 Token 流式推送，完成后读取结构化结果。

## HTTP / WebSocket

| 接口 | 用途 |
|---|---|
| `POST /api/tasks` | `{question, mode: online/local/hybrid}` 新建任务，默认 online |
| `GET /api/tasks?limit=30&offset=0` | 历史研究 |
| `GET /api/tasks/{id}`、`.../result` | 状态、最终结果和引用 |
| `POST /api/tasks/{id}/cancel` | 请求取消 |
| `GET /api/tasks/{id}/events?after=0` | 分页回放事件 |
| `GET /api/tasks/{id}/export` | 从保存结果导出 Markdown 与已定位引文，不调用模型 |
| `GET /api/tasks/{id}/evidence`、`.../evidence/{evidence_id}` | 已读证据原文及上下文 |
| `GET /api/passages/{id}` | 资料片段原文与相邻片段 |
| `GET /api/sources`、`.../sources/{id}` | 筛选资料元数据 / 阅读完整原文、版本及索引进度 |
| `PATCH /api/sources/{id}` | 编辑元数据；原文与历史任务证据快照不可变 |
| `POST /api/sources/{id}/index` | 重试 / 重建索引，202 异步排队 |
| `POST /api/sources`、`.../upload`、`.../page` | 保存资料并自动排队索引 |
| `GET /api/books/search?query=...` | 维基文库官方目录候选，不等于已读证据 |
| `GET /api/evaluations`、`.../{run}`、`.../{run}/{Qxx}/{method}` | 实际评测批次、指标及逐题证据 |
| `PUT /api/evaluations/{run}/{Qxx}/{method}/review` | 实际核查者提交人工评分 |
| `POST /api/corpus/search?query=...` | body 为可选 `{source_ids,people,topics}` 过滤 |
| `WS /ws/tasks/{id}?after=0` | 历史进度 + 实时通知，终态附 snapshot |
| `GET /api/config`、`/health` | 无密钥配置状态、MySQL 基础健康检查 |

## 网络与密钥边界

网页内容始终作为不可信资料传入带明确边界的研究提示，不会执行页面中的指令或代码。抓取只允许 HTTP/HTTPS、80/443，禁止 URL 内嵌凭据，解析初始目的域名并拒绝任何非公网地址。手动导入与旧服务读取的直接客户端还会固定已验证 IP、保留正确 SNI 和 TLS 校验、逐跳校验重定向（最多五次）、限制响应类型及体积，并拒绝压缩响应绕过体积限制。

DeepSeek Fetch 的实际下游连接由提供商执行，本地无法固定其 IP、逐跳验证或取得实际最终 URL；安全依赖其服务端限制，不能等同于直接抓取客户端的保证。工具 API 的响应另有结构、大小和超时上限。观察到的 Incapsula 拦截页会被拒绝作为正文；其他未识别错误页仍需模型及人工阅读核查，200 状态不能保证取得有效史料。

企业代理/登录墙/JavaScript 渲染网页可能不可用，失败作为证据缺口，不会当作已联网成功。公网检索只读候选站点；本地模式禁用两个联网工具。HTTP 网页导入仍允许用户显式指定公网 URL。

Key 只从后端环境读取；`.env` 在 Git 和 Docker 构建上下文中均被忽略，不向前端、WebSocket、任务或评测文件输出。任务边界仅记录异常类型，避免 SDK 异常带入请求凭据。默认不启用 LangSmith 等外部追踪；若运行环境自行开启追踪，需自行审视额外的数据去向。

## 持久化、停止与重建

Compose 命名卷：`shijian_mysql-data`（业务及原文）、`shijian_redis-data`（AOF）、`shijian_etcd-data`（Milvus 元数据）、`shijian_minio-data`（对象）、`shijian_milvus-data`（本地存储/WAL）、`shijian_model-cache`（模型缓存）、`shijian_evaluation-data`（实际评测与人工评分）。主机 CLI 的模型缓存位于 `data/cache/models`，与容器命名卷不同。备份时 MySQL、Milvus/etcd/MinIO 应作为一致的数据集合对待，不单独复制某个运行中的目录。

```sh
docker compose stop              # 保留容器与卷
docker compose down              # 移除容器与网络，保留命名卷
docker compose up -d             # 再次启动
docker compose run --rm worker shijian reindex       # 修复 pending
docker compose run --rm worker shijian reindex --all # 重写已收藏资料的索引，不处理未收藏快照
```

`docker compose down -v` 会删除持久化资料，本项目启动和修复流程不会自动执行它。

## 测试与评测

```sh
uv run ruff check shijian tests scripts eval migrations
uv run pytest -q
uv run python scripts/build_questions.py
# 独立的真实 Embedding 资源检查（需要已下载模型，不连接 Milvus）：
uv run --extra embedding python scripts/embedding_probe.py
# 仅生成 MySQL 方言迁移 SQL，不连接数据库：
uv run alembic upgrade head --sql
```

真实基础设施集成测试显式要求 `TEST_DATABASE_URL`，数据库名必须以 `_test` 结尾，事先创建并授予权限，Redis/Milvus 可连接且已下载 BGE：

```sh
# PowerShell：$env:TEST_DATABASE_URL='mysql+pymysql://shijian:...@localhost:3306/shijian_test?charset=utf8mb4'
uv run --extra embedding pytest tests/test_live_integration.py -q
```

测试对该独立库执行迁移，写入真实语料、测试任务和独立 `shijian_integration_bge_zh_v1` 集合，不自动清库。这里仍使用明确标注的模型替身验证基础设施契约，**真实 DeepSeek 推理质量需运行另外的评测脚本**。

30 题与三种方法的可复现实验、Token/耗时记录、人工评分规则见 [评测说明](eval/README.md)。没有实际模型调用时没有分数，没有价格配置时费用为 null。模型辅助审核和人工审核单独记录。

普通评测默认最多计划 3 条，完整 30 × 3 需显式 `--max-runs 90`。单任务默认最多 16 次模型请求，并在已观测 Token 达到 60000 后阻止下一次调用，格式修正也计入调用次数。没有已核查结果时，达到阈值会失败并保留已读证据；已有结果的修订中断则保留已核查部分并说明局限。正在执行的请求、无 usage 的失败和其他进程不受该 Token 阈值精确计费控制。尚未实现按人民币/账号/日累计的硬预算。历史批次的离线核对与边界见 [用量说明](docs/usage-audit.md)。

研究页实时显示已请求次数、累计Token与缺失用量；展开步骤可查看每次输入/输出/合计，断线重连可回放。CLI逐次打印相同用量字段。若修订前已有通过审核的部分结果，后续修订格式错误或超限时保留它并明确修订未完成，不用错误草稿替换。

## 已知限制与下一步

演示原文与来源说明见 [语料清单](data/demo/README.md)。古籍转录未经影印逐字校勘；现代网页仅为通俗样本，尚无有许可的现代学术研究语料。系统不证明所有史料已经穷尽，不以模型记忆补证据。没有 OCR、自动史源谱系判断、近似转载可靠识别或多用户隔离。

已完成真实服务部署、索引和协议验证；最新进度见验证记录。当前模型请求已恢复，已完成五题真实运行与实际 worker 断线回放；详细结果和高 Token 案例见 [P0 验证](docs/p0-verification.md)。P1 已跑完 30 题三方案真实调用，人工评分尚待完成，详见 [P1 验证](docs/p1-verification.md)。下一步人工审阅失败案例，决定是否增加现代研究资料或重排。不要以空白评测表宣传准确率提升。

历史专题 P1 基线保留原有8份资料，并在评测结束后增加2卷真实史料和1份按需文献快照；该批补查使用维基文库，不能当作当前全网模式。当前在线研究默认使用 DeepSeek 工具，旧资料与历史任务继续保留；最新部署与质量限制见 [在线 P1 验证](docs/online-p1-verification.md)。

![史鉴 P1 研究详情与引用阅读](docs/p1-result.jpg)

实现中核对的官方资料还包括 [LangGraph 图与条件边](https://docs.langchain.com/oss/python/langgraph/quickstart)、[LangChain DeepSeek 集成](https://docs.langchain.com/oss/python/integrations/chat/deepseek)、[Milvus 官方 2.6.15 Compose 文件](https://github.com/milvus-io/milvus/blob/v2.6.15/deployments/docker/standalone/docker-compose.yml)（文件自身固定 Milvus 镜像 v2.6.14，本项目沿用这一配置组合）、[BGE 官方模型说明](https://huggingface.co/BAAI/bge-small-zh-v1.5)。LangChain 文档中的模型能力描述可能滞后于 DeepSeek 最新文档，因此聊天模型由配置选择，实际工具调用仍需用自己的账号验证。
