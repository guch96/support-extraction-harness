---
status: accepted
---

# 会话上下文持久化到 SQLite

2026-10-18，用户明确要求短期记忆保存到 SQLite，服务重启后使用原 sessionId 恢复。本项目保存会话、当前任务及提取上下文的稳定关联，新任务不继承上一任务业务事实。后续 [ADR-0008](0008-use-one-tool-free-extraction-agent.md) 将原双角色拓扑改为单个提取 Agent，当前仅维护该提取 Agent 的 checkpoint；session/task 隔离和每次运行预算重置继续适用。

内存 checkpoint 更简单，但无法满足重启恢复要求，因此不作为本项目的会话存储方案。会话上下文与原文审核记录可以使用同一种存储技术，却有不同的内容边界及保留策略；默认连续七天未使用的会话过期，可配置。中断运行的行为见 ADR-0005：恢复会话但不自动重放模型调用。

脱敏来源与原文的独立保留规则见 [ADR-0006](0006-separate-source-and-raw-retention.md)，来源补充先于提取保存的边界见 [ADR-0007](0007-persist-source-before-extraction.md)。
