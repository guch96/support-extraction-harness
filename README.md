# Support Extraction Harness

客户对话结构化提取与弹性 Harness 的面试项目。

## 当前状态

已实现核心 Harness、SQLite 会话生命周期与 FastAPI 入口；Gradio 页面、真实模型评测与交付说明尚待完成。正式要求见 [设计规格](docs/specs/2026-10-18-support-extraction-harness-design.md)，架构图见 [设计评审稿](docs/DESIGN_REVIEW.md)，需求来源与逐轮决策见 [PRD 草案](docs/PRD_DESIGN_DRAFT.md)。

**当前采用单提取 Agent 设计。** 正式规格区分招聘题目的硬性要求和用户确认的扩展方向；模型输入与提取、修复、页面及交接话术见[上下文契约](docs/PROMPT_CONTEXT.md)。

文档按 grill-with-docs 的方式维护：规格记录已确认决策及验收要求，[术语表](GLOSSARY.md)定义领域词汇，ADR 记录架构取舍；不重新访谈已确认需求。

## 本机运行

需要 Python 3.12 与 [uv](https://docs.astral.sh/uv/)。在仓库根目录配置供应商后启动：

```bash
export OPENAI_API_KEY=...
export OPENAI_MODEL=...
uv run python -m entrypoints.serve
```

PowerShell 用 `$env:OPENAI_API_KEY = "..."` 与 `$env:OPENAI_MODEL = "..."` 设置环境变量。服务只监听 `127.0.0.1:8000`；`HARNESS_PORT` 修改端口，`HARNESS_DATA_DIR` 修改 SQLite 目录（默认 `runtime/`）。运行事件由标准 logging 以逐行 JSON 输出。

冒烟检查（第一条不调用模型；第二条调用已配置的模型）：

```bash
curl -sS http://127.0.0.1:8000/reviews
curl -sS -X POST http://127.0.0.1:8000/extractions \
  -H 'Content-Type: application/json' \
  -d '{"text": "支付账单时应用崩溃。"}'
```

缺少 `OPENAI_API_KEY` 或 `OPENAI_MODEL` 时提取返回 500 `MODEL_NOT_CONFIGURED`，不会静默切换模型替身。

## 开发方向

- LangChain 原生 middleware 作为 Harness 基座。
- 六字段强类型提取、有限自我纠正、人工审核兜底。
- 一个无业务工具的提取 Agent 承担首次提取与修复；授权取数、校验、预算和审核由代码控制。
- 直接文本与记录读取模式共用流程，每次运行最多三次模型调用、额外修复两次；修复不重新取数。
- 客户输入、记录内容及失败输出统一作为不可信数据，覆盖提示词注入缓解和敏感信息保护。
- 会话记忆持久化到 SQLite，重启后使用原 sessionId 恢复，隔离不同会话与任务的上下文。
- 重启不自动续跑中断请求；会话默认连续七天未使用后过期，可配置，审核原文独立保留七天。
- 脱敏来源随会话保留，原文到期后仍可继续提取；审核明确显示原文已过期。
- 接收补充后先可靠保存再调用模型，提取失败不撤销已保存的补充或修正。
- 自动化测试使用模型替身，运行真实 middleware、工具策略与 SQLite；真实模型的业务及注入样例评测单独报告。
- 结构化日志与可重复演示。
- FastAPI 提供 HTTP API，Gradio 提供演示页面，共用服务层，不实现 CLI。
- Harness 作为独立模块提供原生执行能力，不依赖 FastAPI、Gradio 或 HTTP 请求与响应对象。

## 后续会话入口

先阅读[设计规格](docs/specs/2026-10-18-support-extraction-harness-design.md)与[上下文契约](docs/PROMPT_CONTEXT.md)，按需查看架构图、术语表与 [ADR-0008](docs/adr/0008-use-one-tool-free-extraction-agent.md)；根据用户后续指示进入规划或实施。双角色草案仅作历史记录。
