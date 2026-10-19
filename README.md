# Support Extraction Harness

面向 AI 后端工程师招聘评估的项目：把杂乱的客户支持对话提取为六字段结构化记录，并用一个独立的 Harness 给非确定性模型加上确定性控制——严格校验、有限修复、人工审核兜底、输入安全、结构化日志和可恢复的 SQLite 会话。

- [设计规格](docs/specs/2026-10-18-support-extraction-harness-design.md)
- [架构与控制流](docs/DESIGN_REVIEW.md)
- [真实模型评测报告](docs/EVALUATION.md)
- [模型上下文与话术](docs/PROMPT_CONTEXT.md)
- [领域术语表](GLOSSARY.md)
- [ADR 目录](docs/adr/)

> **证据边界（请先读）**：101 个自动化测试证明的是控制流与边界，不是模型语义正确率；真实模型评测只覆盖 7 个样例、单次运行，只代表那些样例在那次配置下的表现。两者都不构成事实认证，也不证明能抵御所有提示词注入。

## 1. 项目说明与状态

### 1.1 原题要求与实现对应

| 原题硬性要求 | 本仓库的实现 | 证据 |
|---|---|---|
| 六字段强类型提取 | `support_extraction/policy.py` 的 Pydantic 模型；严格类型、枚举、拒绝额外字段、摘要最多 200 个 Unicode 码点 | `tests/test_harness.py`、`tests/test_benchmark_records.py` |
| JSON/schema 校验，保存原始失败输出与错误签名 | `harness/validation.py` 校验原始响应；`infrastructure/sqlite.py` 保存每次 attempt 的原始输出、错误码与安全错误签名 | `tests/test_harness.py`、`tests/test_events.py` |
| 有限修复 | `harness/runtime.py` 的 `RecoveryMiddleware`：每次运行最多 3 次模型调用、2 次额外修复；关闭 SDK 重试与自动纠错 | `tests/test_harness.py`、`tests/test_records.py` |
| 修复耗尽后人工审核兜底 | FALLBACK 必须可靠保存 PENDING 审核项并返回 `review_id`；保存失败返回 500，不伪造审核编号 | `tests/test_harness.py`、`tests/test_api.py` |
| 提示词注入缓解 | 来源先检测 PII、脱敏、标签中和，再用随机边界与 JSON 转义包装；模型无业务工具，记录读取由代码白名单控制 | `tests/test_benchmark_records.py`、`tests/test_records.py` |
| 敏感信息保护 | 原文与脱敏来源分开保存；日志、错误、默认页面和修复材料只含脱敏内容 | `tests/test_events.py`、`tests/test_ui.py` |
| 结构化日志 | 每次模型调用、校验失败、来源读取和运行结束输出单行 JSON 事件，关联 session/task/request 与话术版本 | `tests/test_events.py`、`tests/test_serve.py` |
| 自动化测试 | 101 个测试，使用真实 middleware、图、读取策略和临时 SQLite，模型用可编排替身 | `pytest -q` |
| README 说明架构、取舍与生产扩展 | 本文件；架构细节见[设计评审稿](docs/DESIGN_REVIEW.md) | 本文件 |

### 1.2 已确认扩展的实现范围

- LangChain 原生 middleware 作为 Harness 基座，修复由 `RecoveryMiddleware` 唯一控制。
- 一个无业务工具的提取 Agent；首次提取与输出修复共用同一模型配置。
- 直接文本、同任务补充和按记录 ID 读取三种来源，共用同一提取流程。
- 代码授权的只读 fixture 读取；模型不能选择记录、扩大范围或注册工具。
- SQLite 保存会话、任务、来源快照、提取 checkpoint、运行、attempt 和人工审核项。
- FastAPI 提供 HTTP API，Gradio 提供同进程页面，两者共用服务层。
- 受控原文与脱敏来源采用不同保留规则：原文默认七天，脱敏来源随活跃会话保留。

明确未实现：双 Agent 演示、业务 CLI（评测命令除外）、人工编辑或关闭审核、多用户账户、分布式部署、自动重放中断请求。这些是范围取舍，不是遗漏，详见[第 6 节](#6-取舍)。

### 1.3 哪些已由自动化测试证明，哪些只做了小样本评测

| 证据 | 覆盖 | 能证明 | 不能证明 |
|---|---|---|---|
| 101 个 `pytest` 测试 | 真实 middleware、图跳转、工具配置、校验、预算、SQLite、HTTP/UI 话术 | 控制流、失败出口、持久化、安全边界按设计执行 | 真实模型的业务准确率与注入鲁棒性 |
| 真实模型评测（[报告](docs/EVALUATION.md)） | 7 个样例：TX-001、TX-002、TX-002 补充、TX-003、TX-004、TX-INJECT、TX-003-CLEAN；单次运行、单一配置 | 这些样例当次全部交付、攻击前后分类一致、未发现敏感原值外发 | 普遍准确率、其他模型/供应商、其他攻击方式、统计显著性 |

本仓库对“已实现并测试”“已实现但只做过小样本评测”“明确未实现”分别陈述，不把替身测试当作真实模型结论。

## 2. 快速开始

### 2.1 前置条件

- Python 3.12（`pyproject.toml` 要求 `>=3.12`）。
- 依赖见 `pyproject.toml`：LangChain、LangGraph SQLite checkpoint、FastAPI、Uvicorn、Gradio。
- 一个 OpenAI 兼容的聊天补全端点与模型名；不需要与本仓库的任何评测配置相同。
- `uv`（推荐）。**说明**：本机 WSL 环境未安装 `uv`，因此实际验证的是 2.3 的 `.venv` 命令；`uv run` 路径按项目配置编写，未在本机执行。

### 2.2 环境变量

| 变量 | 必填 | 默认 | 用途 |
|---|---|---|---|
| `OPENAI_API_KEY` | 是 | — | 模型调用凭据；只从进程环境读取 |
| `OPENAI_BASE_URL` | 否 | SDK 默认 | 任何 OpenAI 兼容端点；不设置时用 SDK 默认地址 |
| `OPENAI_MODEL` | 是 | — | 模型名 |
| `HARNESS_DATA_DIR` | 否 | `runtime` | SQLite 审计库与 checkpoint 目录 |
| `HARNESS_PORT` | 否 | `8000` | 服务端口；主机固定为 `127.0.0.1` |

仓库提供[`.env.example`](.env.example)，只含占位值，不含任何真实凭据。应用**不会自动加载** `.env`；请显式导出变量。例如在 bash 中：

```bash
cp .env.example .env
# 编辑 .env，填入真实值
set -a; . ./.env; set +a
```

PowerShell 等价写法：

```powershell
$env:OPENAI_API_KEY = "replace-with-your-key"
$env:OPENAI_MODEL = "your-model-name"
```

缺少 `OPENAI_API_KEY` 或 `OPENAI_MODEL` 时，提取请求返回 500 `MODEL_NOT_CONFIGURED`，不会静默切换到模型替身。

### 2.3 安装与启动

推荐使用 `uv`：

```bash
uv sync
uv run python -m entrypoints.serve
```

若已在仓库根创建 `.venv` 并安装依赖，可使用本机验证过的路径：

```bash
PYTHONPATH=src .venv/bin/python -m entrypoints.serve
```

启动后可访问：

- API 基址：`http://127.0.0.1:8000`
- 交互式 API 文档：`http://127.0.0.1:8000/docs`
- 演示页面：`http://127.0.0.1:8000/ui`（会 307 跳转到 `/ui/`）

服务只监听回环地址；改变端口用 `HARNESS_PORT`，改变数据目录用 `HARNESS_DATA_DIR`。

### 2.4 一条 curl 示例

```bash
curl -sS -X POST http://127.0.0.1:8000/extractions \
  -H 'Content-Type: application/json' \
  -d '{"text": "支付账单时应用崩溃，账户明天会被锁定。"}'
```

配置齐全时返回体是运行结果：`outcome`（`SUCCESS` / `REPAIRED` / `FALLBACK`）、六字段 `data`、`session_id`、`task_id`、`request_id`、`model_calls`、`repair_count`、`review_id` 和 `error_code`。审核列表与详情：

```bash
curl -sS http://127.0.0.1:8000/reviews
curl -sS http://127.0.0.1:8000/reviews/<review_id>
curl -sS 'http://127.0.0.1:8000/reviews/<review_id>?include_raw=true'
```

## 3. 架构

### 3.1 模块职责

| 模块 | 职责 |
|---|---|
| `src/harness/contracts.py` | 领域契约：`OutputContract`、`RunInput`、`RunResult`、`AuditSink`，以及 `SourceUnavailable` / `ServiceError` |
| `src/harness/runtime.py` | `Harness` 与 `RecoveryMiddleware`：调用预算、超时、上下文投影、真实响应校验、唯一修复跳转和终止 |
| `src/harness/validation.py` | 严格 JSON/schema 校验；错误路径、错误码与静态约束投影；后处理后再校验一次 |
| `src/harness/context.py` | 构造安全来源包与修复反馈包；随机 nonce、JSON 转义、失败正文脱敏 |
| `src/harness/failures.py` | 把供应商异常归类为 `MODEL_TIMEOUT`、`MODEL_CALL_FAILED`、`MODEL_CONFIG_ERROR` |
| `src/harness/events.py` / `prompts.py` | 单行 JSON 事件与关联字段；`extraction.v1` / `repair.v1` 话术版本 |
| `src/support_extraction/policy.py` | 六字段 Pydantic schema、业务字段规则、交付前 `finalize` |
| `src/support_extraction/safety.py` | 信用卡、身份证/NRIC、密码语境检测；脱敏与 `<...>` 标签中和 |
| `src/support_extraction/service.py` | 应用服务：来源授权/读取、先保存来源、安全检查、运行 Harness、审核与维护 |
| `src/support_extraction/benchmark.py` / `fixtures/` | 只读基准记录与允许读取的 ID 集合 |
| `src/support_extraction/evaluation.py` | 独立真实模型评测命令与 Markdown 报告生成 |
| `src/support_extraction/system.txt` / `repair.txt` | 固定 system 话术与修复话术模板 |
| `src/infrastructure/model.py` | `ChatOpenAI` 配置：`max_retries=0`、`timeout=30`、`temperature=0`、`max_tokens=2048`、关闭流式 |
| `src/infrastructure/records.py` | 只读 fixture 适配器；记录缺失/内容非法时抛 `SourceUnavailable` |
| `src/infrastructure/sqlite.py` | 审计库、会话/来源/运行/attempt/审核存储、checkpoint 清理与七天维护 |
| `src/entrypoints/api.py` | FastAPI 路由、请求校验、领域错误到 HTTP 的映射 |
| `src/entrypoints/serve.py` | 回环 Uvicorn 启动与逐行 JSON 日志格式 |
| `src/entrypoints/ui.py` | Gradio 页面与纯协程处理函数；默认只展示脱敏内容 |
| `tests/` | 101 个测试；真实 middleware + 模型替身 + 临时 SQLite |

依赖方向是：`entrypoints` → `support_extraction` → `harness`；`infrastructure` 实现接口并由外层注入。`harness` 不依赖 FastAPI、Gradio、HTTP 对象或页面状态。

### 3.2 请求到结果

1. **接收与校验**：`POST /extractions` 严格解析请求体，拒绝未知字段；校验文本/记录二选一以及已提供 ID 的组合。
2. **预留会话与任务**：检查会话是否存在/过期、任务是否属于该会话、同任务是否有活动请求；检查来源总长（默认 20,000 字符）与回合数（默认 20）。
3. **来源读取（记录模式）**：只允许代码白名单中的 ID；适配器只读、最多一次、10 秒超时。授权拒绝返回 403，实际读取与模型都不执行。
4. **先保存来源**：检测 PII、脱敏、中和标签，把受控原文与脱敏来源在同一事务中保存；保存失败不调用模型。模型失败不撤销已保存的补充或修正。
5. **进入 Harness**：把当前任务的完整脱敏来源快照、版本号和累计 PII 标记投影为来源包；每次请求重新构造 system + 来源包。
6. **模型调用**：提取 Agent 的 `tools=[]`；每次调用记录开始/结束、原始响应、错误码和耗时；单次调用 30 秒、整次运行 90 秒。
7. **严格校验与修复**：只接受纯 JSON 对象；六字段齐全、类型严格、拒绝额外字段、摘要不超过 200 码点。失败保存原始证据和错误签名，仅把最近一次失败的安全反馈包加入下一次请求；最多额外修复 2 次。
8. **终止与审核**：通过则 `SUCCESS`（首次）或 `REPAIRED`（修复后）；预算耗尽、输出无效、模型超时/调用失败、运行超时、来源不可用则保存 PENDING 审核项并返回 `FALLBACK`；中断同样保存审核项，但客户端可能已断开、收不到响应；配置、安全、策略或持久化故障返回 500。

### 3.3 六字段 schema

| 字段 | 类型与约束 |
|---|---|
| `customer_id` | `string` 或 `null`；字段必须出现，无明确标识时为 `null` |
| `issue_category` | `BILLING` / `TECHNICAL` / `ACCOUNT` / `OTHER` |
| `urgency` | `LOW` / `MEDIUM` / `HIGH` / `CRITICAL` |
| `summary` | `string`，最多 200 个 Unicode 码点 |
| `actionable_steps` | `list[string]`；没有明确后续事项时为空列表 |
| `detected_pii` | `boolean`；宿主检测、合格模型判断与任务历史标记取 OR |

校验器不会通过剥离 Markdown、补字段、隐式类型转换、截断摘要或替换枚举来“修好”模型输出；这些情况都进入同一修复循环。字段与 schema 合法只表示结构合格，不表示业务事实已被证实。

### 3.4 修复预算与超时

| 单次运行上限 | 值 | 说明 |
|---|---:|---|
| 模型调用 | 3 | 直接文本、补充、记录读取统一 |
| 额外输出修复 | 2 | 第 3 次调用仍失败即 FALLBACK |
| 指定记录读取 | 1 | 修复不重新读取；直接文本/补充为 0 |
| 单次模型调用超时 | 30 秒 | 关闭 SDK 自动网络重试 |
| 整次运行超时 | 90 秒 | 统一截止时间 |
| 来源读取超时 | 10 秒 | 仅记录读取模式 |

JSON/schema 错误共享同一修复预算。模型调用异常、超时和来源读取异常不进入格式修复；失败调用仍计入调用次数并记录耗时。

### 3.5 上下文投影

- 首次请求固定为 **system + 本任务完整安全来源包**；system 只含提取规则和 schema，不含来源原文。
- 修复请求为 **同一 system + 同一来源包 + 最近一次失败的反馈包**；不追加更早的失败消息串，也不重新读取来源。
- 来源按 `sequence` 排序，补充和明确修正保留来源身份；最新明确业务修正优先。
- 来源包使用宿主生成的随机 nonce 作为边界，JSON 序列化时转义 `<` 和 `>`；客户文字不能拼出消息角色或外层字段。
- 历史模型答案、旧修复对话、其他任务材料不进入下一次运行的模型输入；checkpoint 只保存安全状态与必要引用，原始证据保存在审计库。

### 3.6 安全处理

- **PII 检测与脱敏**：覆盖 13–19 位银行卡号、18 位身份证号、NRIC（如 `S9123456A`）和显式密码语境；仅卡号尾号不单独触发。模型可见内容中的已识别原值替换为 `[REDACTED_*]` 占位符。
- **标签中和**：`<...>` 形式的伪造标签替换为 `[UNTRUSTED_TAG]`；字符串中的 `<`、`>` 再做 JSON Unicode 转义，避免闭合来源边界。
- **无工具**：提取 Agent 的 `tools=[]`；即使模型返回工具调用，也不执行，只作为输出协议错误进入修复预算。
- **白名单取数**：记录读取只接受可信请求中的允许 ID，由代码执行；模型不能指定新记录、扩大范围或改变次数。
- **失败不外发**：PII/安全处理异常时停止模型调用，返回 500 `SAFETY_PROCESSING_FAILED`；不用原文替代脱敏失败的材料继续调用。

### 3.7 SQLite 生命周期与保留

- `HARNESS_DATA_DIR` 下有两个文件：`audit.sqlite3`（来源、运行、attempt、审核）和 `checkpoints.sqlite3`（LangGraph checkpoint）。
- 文件权限为 `0600`，目录为 `0700`；启用 WAL 和 `secure_delete`。这**不等于加密**。
- 会话默认连续七天未使用后过期；显式清空立即使旧 `sessionId` 无法恢复，并删除对应 checkpoint。
- 受控原文与每次模型原始输出各自默认保留七天，按保存时间计算；活跃会话、新增补充或新审核项**不延长**旧原文保留期。
- 原文到期后仍用保留的脱敏来源继续提取；审核详情把对应证据标为 `EXPIRED`，不把脱敏损失补齐。
- 重启会恢复会话与 checkpoint，但**不自动续跑**中断的模型调用；已发生的中断运行被标记为 FALLBACK 并生成 PENDING 审核项。

## 4. 失败与可见性

### 4.1 执行状态

| 状态 | 含义 |
|---|---|
| `SUCCESS` | 首次提取通过全部已声明检查 |
| `REPAIRED` | 至少一次输出修复后通过相同检查 |
| `FALLBACK` | 未取得可接受结果，且 PENDING 审核项已可靠保存；本次 `data` 为空 |

FALLBACK 的 `review_id` 只在审核项保存成功后返回；保存失败是服务错误，不能返回虚假审核编号。成功只代表通过检查，不代表事实真实或获得操作授权。

### 4.2 错误码

| 错误码 | 触发 | 进入 `/reviews` | HTTP |
|---|---|---|---|
| `MODEL_TIMEOUT` | 模型调用超过 30 秒（或供应商超时异常） | 是，FALLBACK | 200 |
| `MODEL_CALL_FAILED` | 鉴权/权限/连接/限流/供应商 API 等调用失败 | 是，FALLBACK | 200 |
| `MODEL_CONFIG_ERROR` | 模型未找到、请求参数被供应商拒绝等配置错误 | 否 | 500 |
| `MODEL_NOT_CONFIGURED` | 缺少 `OPENAI_API_KEY` 或 `OPENAI_MODEL` | 否 | 500 |
| `SOURCE_UNAVAILABLE` | 来源适配器声明不可用（通用原因码） | 是，FALLBACK | 200 |
| `RECORD_NOT_FOUND` / `INVALID_RECORD` | 记录不存在 / 记录内容为空或非文本 | 是，FALLBACK | 200 |
| `INVALID_SOURCE_RESPONSE` | 适配器返回空或非文本内容 | 是，FALLBACK | 200 |
| `SOURCE_POLICY_FAILED` | 读取策略自身异常，停止读取 | 否 | 500 |
| `ACCESS_DENIED` | 记录不在白名单；读取与模型均未执行 | 否 | 403 |
| `SAFETY_PROCESSING_FAILED` | PII/脱敏/安全序列化异常，停止模型外发 | 否 | 500 |
| `PERSISTENCE_FAILED` | 来源、审计、checkpoint 或审核保存失败 | 否（不能伪造审核） | 500 |
| `OUTPUT_INVALID` | 第 3 次输出仍不符合 JSON/schema，预算耗尽 | 是，FALLBACK | 200 |
| `BUDGET_EXHAUSTED` | 调用前发现模型调用预算或统一截止时间已耗尽 | 是，FALLBACK | 200 |
| `RUN_TIMEOUT` | 整次运行超过 90 秒 | 是，FALLBACK | 200 |
| `INTERRUPTED` | 请求取消，或重启时恢复出中断的运行 | 是，FALLBACK | 无响应 / 启动时恢复 |
| `INTERNAL_ERROR` | 未预期内部异常 | 否 | 500 |

`MODEL_CONFIG_ERROR` 与 `MODEL_CALL_FAILED` 的区别是：前者是部署配置问题，不应由人工审核弥补；后者是预期内的外部调用失败，转人工可见。

### 4.3 HTTP 映射

| 情况 | HTTP |
|---|---:|
| `SUCCESS`、`REPAIRED`、已保存审核的 `FALLBACK` | 200 |
| 来源读取授权拒绝 | 403 |
| 请求字段不合法、文本/记录参数组合错误 | 422 |
| 输入或累计来源材料超限 | 413 |
| 同任务已有活动请求 | 409 |
| 会话过期或已清空 | 410 |
| 会话/任务/审核项不存在 | 404 |
| 安全、策略、配置、持久化或内部错误 | 500 |

500 响应包含 `error_code`、`request_id`，以及适用时的 `source_saved`。入口和页面不能仅凭 HTTP 500 推断补充是否已保存：`source_saved=true` 表示来源已入库但后续失败，`false` 表示来源未保存，`null` 表示无法确认。

### 4.4 如何查看人工审核

1. `GET /reviews`：按创建时间倒序列出所有 `review_id`、`request_id`、状态（当前固定为 `PENDING`）、错误码、调用/修复次数。
2. `GET /reviews/{review_id}`：默认只返回脱敏来源、每次 attempt 的错误码与 `raw_status`；不包含原文。
3. `GET /reviews/{review_id}?include_raw=true`：显式请求受控原文。`raw_status` 为 `AVAILABLE`、`EXPIRED` 或 `UNAVAILABLE`；到期证据明确标记，不复制旧原文续期。

页面 `/ui` 的“人工审核” tab 可以刷新列表并查看详情，但**详情需要手工粘贴 `review_id`**；列表项没有可点击的详情链接。这是已知短板，不是隐藏行为。审核项当前只支持查看，不支持编辑、关闭或重新处理。

### 4.5 结构化日志

`entrypoints.serve` 把 `support_harness.events` 的日志渲染为**每行一个 JSON 对象**，写到 stderr。主要事件：

- `model_call`：`session_id`、`task_id`、`request_id`、`source_revision`、`attempt`、`prompt_version`（`extraction.v1` / `repair.v1`）、`sdk_latency_ms`、`wrapper_latency_ms`、`model_calls`、`repair_count`、`error_code`。
- `validation_failed`：关联字段、`attempt`、`validation_stage`、`error_signature`。
- `run_finished`：关联字段、`outcome`、调用/修复次数、`review_id`、`error_code`；不包含六字段数据。
- `source_read` / `source_denied`：读取允许或拒绝原因、耗时、结果与错误码。

日志、错误响应和默认页面只含脱敏内容或安全引用，不嵌入已识别敏感原值；原文只通过受控审核接口显式查看。

## 5. 测试与评测

### 5.1 自动化测试

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q
PYTHONPATH=src .venv/bin/python -m ruff check .
```

当前结果：`101 passed`，`ruff` 通过。测试使用 `tests/conftest.py::ScriptedModel` 模型替身，但运行**真实**的 LangChain middleware、图、来源读取策略、服务层和临时 SQLite，不使用网络或付费 API。覆盖范围包括：

- 正常提取、修复后恢复、预算耗尽、空响应、非法 JSON、错误枚举/类型、缺字段、额外字段、超长摘要、意外工具调用。
- 模型超时/调用失败/配置错误/未配置；来源不存在、内容非法、授权拒绝、策略故障。
- 先保存来源、补充在 FALLBACK 后仍生效、同任务并发拒绝、会话隔离、恢复、过期、清空、重启恢复与中断转审核。
- PII 检测与脱敏、卡号尾号不单独触发、注入文本中和、模型实际输入不含敏感原值。
- 上下文投影只含 system + 来源包（修复时加最近失败）、无旧答案/旧修复/其他任务材料、工具配置为空。
- HTTP 状态映射、审核列表/详情/原文显式开关、页面话术、JSON 日志字段与关联、评测 NOT RUN 路径。
- 分层约束：`harness` 不依赖入口或领域词汇；应用层注入话术与预算。

模型替身证明的是控制流、边界和失败出口，**不是**真实模型的分类准确率、摘要质量或注入鲁棒性。

### 5.2 真实模型评测

评测命令（会产生真实供应商调用）：

```bash
PYTHONPATH=src .venv/bin/python -m support_extraction.evaluation \
  --output docs/EVALUATION.md
```

行为：

- 缺少 `OPENAI_API_KEY` 或 `OPENAI_MODEL`：输出 NOT RUN（未执行），不发起模型调用，退出码 **2**。
- 配置齐全并完成：退出码 **0**，即使出现 FALLBACK 也如实写入报告。
- 意外错误：退出码 **1**。
- `--output` 的默认值就是 `docs/EVALUATION.md`，**不带 `--output` 或传入同一路径会覆盖仓库中已提交的评测报告**。只想做 NOT RUN 冒烟或试跑时，请显式写到临时路径，例如 `--output /tmp/evaluation.md`。

[`docs/EVALUATION.md`](docs/EVALUATION.md) 记录了一次真实运行：7 个样例（4 个原题样例、1 个同任务补充、1 个独立注入样例 TX-INJECT、1 个去攻击对照 TX-003-CLEAN），7/7 交付，0 次修复，0 次 FALLBACK，0 次 ERROR，无未通过检查，未发现敏感原值外发；TX-003 与移除攻击文字后的 TX-003-CLEAN 都是 `TECHNICAL` / `MEDIUM`，攻击前后一致。该报告同时记录了供应商、模型、base URL、话术版本和模型参数；本 README 不复述供应端点。

该结果的边界：单次运行、每个样例只跑一次、只覆盖 7 个样例和一个模型配置；它不能证明所有业务事实正确，也不能证明对所有注入都稳健。

## 6. 取舍

| 取舍 | 为什么这样选 | 付出了什么 |
|---|---|---|
| 单个提取 Agent，无业务工具 | 读取目标由代码确定，不需要模型自主规划；减少调用、消息配对与跨角色状态；权限边界更小 | 模型不能自行取数或调用工具；复杂业务流程需要另行设计 |
| 没有 CLI（评测命令除外） | 原题要求 Web/API 演示与审核，CLI 不增加评估价值 | 自动化或运维场景需要直接调用 HTTP 或服务层 |
| SQLite 本地持久化 | 单机演示、零外部依赖、事务简单、便于测试重启与保留策略 | 单进程、写串行；不适合多实例或高吞吐；文件未加密 |
| 不实现人工编辑、关闭或重新处理审核 | 本次只证明“失败可见、证据可查、审核可靠保存” | 审核是 PENDING 证据队列，不是完整工单系统 |
| 启发式 PII 检测 | 可解释、可测试、无需额外模型调用 | 正则存在误报与漏报；格式变化、上下文语义可能漏检 |
| 提示层注入缓解 | 随机边界、标签中和、JSON 转义、固定 system 都是低成本纵深防御 | 不能保证模型语义上一定忽略攻击；真实评测只覆盖少量样例 |
| 单次小样本真实评测 | 控制成本、如实报告、不隐藏失败 | 无统计显著性；不能外推到其他模型、语言或攻击 |

## 7. 生产就绪性：10,000+ 操作/分钟

10,000 次业务操作/分钟约为 **167 次/秒**。每次操作至少 1 次模型生成，最坏 3 次；若平均修复次数为 `r`，模型请求量约为 `167 × (1 + r)` 次/秒，最坏约 500 次/秒。这还没有计入审核、日志和原文存储的放大。

要达到这个量级，本地 SQLite 单进程方案必须替换或扩展，至少需要以下能力：

- **异步并发与有界工作池**：请求接收与模型调用解耦，限制同时进行中的运行数；避免无限创建协程或连接。
- **供应商配额与限流**：按模型/租户统计 RPM、TPM 和并发；处理 429、配额耗尽和区域性故障；配置明确的退避与熔断策略，而不是隐式无限重试。
- **背压**：队列满时快速拒绝或降级，返回可重试信号；不能让请求在内存中无限堆积。
- **统一截止时间**：把端到端 deadline 传到每次模型调用和来源读取，拒绝已经不可能在时限内完成的请求；当前代码已有单次调用/整次运行超时，但生产环境还需要跨服务传播。
- **共享持久化**：用网络数据库（如 PostgreSQL）保存运行、来源、审核和幂等键，用共享 checkpoint 存储支持多实例；对象存储保存受控原文；事务边界要覆盖“来源已保存”和“审核已保存”两个事实。
- **幂等键与未知调用结果**：客户端提供幂等键，服务端对重复提交去重；模型调用可能“已发出但结果未知”，不能当作可安全重试——需要按供应商请求 ID/审计记录对账，明确哪些情况可重试、哪些必须转人工。SQLite 不保证供应商调用 exactly-once。
- **原文访问与保留**：加密存储与密钥管理、细粒度访问审计、按区域/租户的保留策略、到期删除任务；原文访问必须显式授权并可追溯。
- **日志采样**：高吞吐下对成功运行采样，对 FALLBACK、安全事件和 5xx 全量保留；采样不能丢失关联 ID 和错误签名。

**明确声明**：当前本地 SQLite 方案不承诺达到 10,000+ 操作/分钟的吞吐量；它是单机演示与正确性验证实现，不是生产集群方案。

## 8. 已知限制

- SQLite 文件未加密；只靠文件权限（`0600`/`0700`）、本地回环和 Git 排除保护，不能抵御拥有本机文件访问权的攻击者。
- 服务只监听 `127.0.0.1`，没有公网多用户账户、认证、租户隔离或配额系统。
- PII 检测是启发式正则，存在误报（例如长数字串被识别为卡号）与漏报（例如换一种写法、图片或上下文隐含的敏感信息）。
- 提示层注入缓解不保证模型语义正确；真实评测只覆盖 TX-003、TX-INJECT 等少量攻击样例。
- 未做浏览器端真实点击链路的端到端测试；Gradio 处理函数和 `/ui` 挂载有测试，但“打开浏览器、点击、观察页面”仍靠人工冒烟。
- `/ui` 的人工审核详情需要手工填写 `review_id`；列表项不可点击。
- 未实现人工编辑、关闭审核、重新处理、自动重放中断请求、长期记忆和双 Agent 演示。
- `uv` 未在本机实测；已验证的是已有 `.venv` 下的 `PYTHONPATH=src .venv/bin/python ...` 命令。
- 真实评测是单次小样本，供应商和模型参数见[评测报告](docs/EVALUATION.md)；更换模型或提示词后需要重新评测。
- 六字段结构合法不等于事实正确、完整或具有操作授权；`actionable_steps` 只用于展示和记录。

## 9. 文档导航

- [设计规格](docs/specs/2026-10-18-support-extraction-harness-design.md)：范围、契约、失败出口、验收标准、生产扩展要求。
- [架构与控制流](docs/DESIGN_REVIEW.md)：模块图、middleware 控制点、上下文投影与观测。
- [模型上下文与话术](docs/PROMPT_CONTEXT.md)：来源包、`extraction.v1`、`repair.v1` 与页面话术。
- [真实模型评测](docs/EVALUATION.md)：7 个样例的实际输出、攻击前后一致性与限制。
- [术语表](GLOSSARY.md)：会话、任务、运行、来源、修复预算、执行结果等术语。
- [ADR-0002](docs/adr/0002-own-repair-loop-in-native-middleware.md)：修复循环的唯一所有权。
- [ADR-0003](docs/adr/0003-keep-harness-independent-of-entrypoints.md)：Harness 独立于入口。
- [ADR-0006](docs/adr/0006-separate-source-and-raw-retention.md)：脱敏来源与受控原文分开保留。
- [ADR-0008](docs/adr/0008-use-one-tool-free-extraction-agent.md)：单提取 Agent 与代码授权取数。
- [PRD 草案](docs/PRD_DESIGN_DRAFT.md)：原题要求与逐轮决策摘要。

文档中的“设计规格”是行为契约；本 README 描述当前实现与证据边界。若文档状态行与实现进度冲突，以代码、测试和[评测报告](docs/EVALUATION.md)为准，并在后续文档任务中修正。
