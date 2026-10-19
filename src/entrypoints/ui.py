"""Review page handlers and the Gradio assembly over the extraction service.

Every handler is a plain coroutine over :class:`ExtractionService`, so the page
logic can be tested without starting Gradio, a network server or a model.
Gradio is imported lazily inside :func:`mount_ui` only.
"""

import json
from typing import Any

from harness.contracts import RunResult, ServiceError
from support_extraction.benchmark import BENCHMARK_RECORD_IDS, SAMPLE_RECORD_IDS
from support_extraction.policy import SIX_FIELDS
from support_extraction.service import ExtractionService, InputError, Submission

FIELD_LABELS = {
    "customer_id": "客户 ID",
    "issue_category": "问题类别",
    "urgency": "紧急程度",
    "summary": "摘要",
    "actionable_steps": "可执行步骤",
    "detected_pii": "检测到 PII",
}
RECORD_CHOICES = tuple(sorted(BENCHMARK_RECORD_IDS))

NEW_TASK_NOTICE = "新建一份支持对话任务；此前任务的客户事实不会自动带入。"
SUPPLEMENT_NOTICE = (
    "补充或修正当前对话的信息。补充可靠保存后会重新提取，提取失败不会撤销已保存内容。"
)
RECORD_MODE_NOTICE = "读取指定的预置对话记录并提取信息。系统会先检查是否允许读取该记录。"
SUCCESS_NOTICE = "本次提取已通过格式与字段检查。"
REPAIRED_NOTICE = "本次提取经过 {repair_count} 次自动纠正后通过检查。"
FALLBACK_NOTICE = "本次未取得合格结果，已保存为待人工审核。审核编号：{review_id}。"
SAVED_BUT_ERROR_NOTICE = (
    "补充已保存，本次提取未完成。请求编号：{request_id}。已有补充会用于后续提取。"
)
SOURCE_SAVE_FAILED_NOTICE = "本次补充未保存，未开始模型提取。请求编号：{request_id}。"
REVIEW_SAVE_FAILED_NOTICE = "本次提取失败，审核记录也未能保存。请求编号：{request_id}。"
EXPIRED_NOTICE = "原文已超过保留期限，当前仅可查看保留的脱敏材料与处理记录。"
EXPIRED_MARKER = "（原文已过期）"
HISTORICAL_NOTICE = "这是请求 {request_id} 的历史结果，未必反映最新补充。"
UNKNOWN_NOTICE = "暂无法确认补充是否已保存，请按请求编号查询。"
CLEARED_NOTICE = "会话已清空；该会话编号将无法恢复。"
SESSION_NOTICE = "已恢复会话；以下为保留的脱敏来源与历史运行。"

INPUT_NOTICES = {
    "INVALID_INPUT": "请提供对话文本或选择一条记录，并检查提交参数。",
    "SESSION_EXPIRED": "该会话已过期或已清空，请新建任务。",
    "SESSION_NOT_FOUND": "未找到该会话，请检查会话编号。",
    "TASK_NOT_FOUND": "未找到该任务，请恢复会话或新建任务。",
    "TASK_BUSY": "该任务正在处理中，请稍后再试。",
    "SOURCE_TOO_LARGE": "来源材料超过上限，本次提交未保存。",
    "ACCESS_DENIED": "该记录不在允许读取的范围内。",
    "REVIEW_NOT_FOUND": "未找到该审核项。",
}


def _empty_panel() -> dict[str, Any]:
    return {
        "kind": "error",
        "status": "REJECTED",
        "error_code": None,
        "request_id": None,
        "session_id": None,
        "task_id": None,
        "fields": {},
        "model_calls": None,
        "repair_count": None,
        "review_id": None,
        "notice": "",
    }


def result_panel(result: RunResult) -> dict[str, Any]:
    if result.outcome == "FALLBACK":
        if result.review_id:
            notice = FALLBACK_NOTICE.format(review_id=result.review_id)
        else:
            notice = REVIEW_SAVE_FAILED_NOTICE.format(request_id=result.request_id)
    elif result.outcome == "REPAIRED":
        notice = REPAIRED_NOTICE.format(repair_count=result.repair_count)
    else:
        notice = SUCCESS_NOTICE
    return {
        "kind": "result",
        "status": result.outcome,
        "error_code": result.error_code,
        "request_id": result.request_id,
        "session_id": result.session_id,
        "task_id": result.task_id,
        "fields": dict(result.data) if result.data else {},
        "model_calls": result.model_calls,
        "repair_count": result.repair_count,
        "review_id": result.review_id,
        "notice": notice,
    }


def error_panel(exc: InputError | ServiceError) -> dict[str, Any]:
    panel = _empty_panel()
    if isinstance(exc, InputError):
        panel["notice"] = INPUT_NOTICES.get(exc.code, "请求未被接受，请检查输入。")
        panel["error_code"] = exc.code
        panel["request_id"] = exc.request_id
        return panel
    panel["status"] = "SERVICE_ERROR"
    panel["error_code"] = exc.code
    panel["request_id"] = exc.request_id
    if exc.code == "REVIEW_SAVE_FAILED" or (
        exc.code == "PERSISTENCE_FAILED" and exc.source_saved is True
    ):
        panel["notice"] = REVIEW_SAVE_FAILED_NOTICE.format(request_id=exc.request_id)
    elif exc.source_saved is True:
        panel["notice"] = SAVED_BUT_ERROR_NOTICE.format(request_id=exc.request_id)
    elif exc.source_saved is False:
        panel["notice"] = SOURCE_SAVE_FAILED_NOTICE.format(request_id=exc.request_id)
    else:
        panel["notice"] = UNKNOWN_NOTICE
    return panel


async def extract(
    service: ExtractionService,
    *,
    text: str | None = None,
    record_id: str | None = None,
    session_id: str | None = None,
    task_id: str | None = None,
    new_task: bool = False,
) -> dict[str, Any]:
    try:
        result = await service.submit(
            Submission(
                text=text,
                record_id=record_id,
                session_id=session_id or None,
                task_id=None if new_task else (task_id or None),
                new_task=new_task,
            )
        )
    except (InputError, ServiceError) as exc:
        return error_panel(exc)
    return result_panel(result)


async def supplement(
    service: ExtractionService,
    *,
    text: str | None,
    session_id: str | None,
    task_id: str | None = None,
) -> dict[str, Any]:
    if not session_id:
        return error_panel(InputError("INVALID_INPUT"))
    return await extract(service, text=text, session_id=session_id, task_id=task_id)


def historical_entry(run: dict[str, Any]) -> dict[str, Any]:
    return {
        "request_id": run["id"],
        "status": run["status"],
        "source_revision": run["source_revision"],
        "fields": json.loads(run["data"]) if run["data"] else None,
        "notice": HISTORICAL_NOTICE.format(request_id=run["id"]),
    }


async def restore_session(
    service: ExtractionService, session_id: str | None
) -> dict[str, Any]:
    if not session_id:
        return error_panel(InputError("INVALID_INPUT"))
    try:
        session = await service.restore(session_id)
    except (InputError, ServiceError) as exc:
        return error_panel(exc)
    panel = _empty_panel()
    panel.update(
        {
            "kind": "session",
            "status": "RESTORED",
            "session_id": session["id"],
            "task_id": session["current_task_id"],
            "sources": [
                {"sequence": source["sequence"], "kind": source["kind"], "text": source["text"]}
                for source in session["sources"]
            ],
            "history": [historical_entry(run) for run in session["runs"]],
            "notice": SESSION_NOTICE,
        }
    )
    return panel


async def clear_session(
    service: ExtractionService, session_id: str | None
) -> dict[str, Any]:
    if not session_id:
        return error_panel(InputError("INVALID_INPUT"))
    try:
        await service.clear(session_id)
    except (InputError, ServiceError) as exc:
        return error_panel(exc)
    panel = _empty_panel()
    panel.update(
        {
            "kind": "cleared",
            "status": "CLEARED",
            "session_id": session_id,
            "notice": CLEARED_NOTICE,
        }
    )
    return panel


async def list_reviews(service: ExtractionService) -> dict[str, Any]:
    try:
        rows = await service.reviews()
    except (InputError, ServiceError) as exc:
        return error_panel(exc)
    reviews = [
        {
            "review_id": row["review_id"],
            "request_id": row["request_id"],
            "status": row["status"],
            "error_code": row["error_code"],
            "model_calls": row["model_calls"],
            "repair_count": row["repair_count"],
        }
        for row in rows
    ]
    panel = _empty_panel()
    panel.update(
        {
            "kind": "reviews",
            "status": "LOADED",
            "reviews": reviews,
            "notice": f"共 {len(reviews)} 条待人工审核记录。",
        }
    )
    return panel


async def review_detail(
    service: ExtractionService, review_id: str | None, *, include_raw: bool = False
) -> dict[str, Any]:
    if not review_id:
        return error_panel(InputError("REVIEW_NOT_FOUND"))
    try:
        review = await service.review(review_id, raw=include_raw)
    except (InputError, ServiceError) as exc:
        return error_panel(exc)
    sources = []
    for source in review["sources"]:
        item = {
            "sequence": source["sequence"],
            "kind": source["kind"],
            "text": source["safe_text"],
            "raw_status": source["raw_status"],
        }
        if include_raw:
            item["raw_text"] = source.get("raw_text")
        sources.append(item)
    attempts = []
    for attempt in review["attempts"]:
        item = {
            "attempt": attempt["attempt"],
            "latency_ms": attempt["latency_ms"],
            "error_code": attempt["error_code"],
            "raw_status": attempt["raw_status"],
        }
        if include_raw:
            item["raw"] = attempt.get("raw")
        attempts.append(item)
    expired = any(item["raw_status"] == "EXPIRED" for item in sources + attempts)
    panel = _empty_panel()
    panel.update(
        {
            "kind": "review",
            "status": review["status"],
            "review_id": review["review_id"],
            "request_id": review["id"],
            "session_id": review["session_id"],
            "task_id": review["task_id"],
            "error_code": review["error_code"],
            "model_calls": review["model_calls"],
            "repair_count": review["repair_count"],
            "sources": sources,
            "attempts": attempts,
            "raw_visible": include_raw,
            "expired": expired,
            "notice": EXPIRED_NOTICE if expired else "",
        }
    )
    return panel


def _field_value(name: str, value: Any) -> str:
    if name == "actionable_steps":
        return "；".join(value) if value else "（无）"
    if value is None:
        return "（无）"
    if isinstance(value, bool):
        return "是" if value else "否"
    return str(value)


def _fields_table(fields: dict[str, Any]) -> str:
    lines = ["| 字段 | 值 |", "|---|---|"]
    for name in SIX_FIELDS:
        lines.append(f"| {FIELD_LABELS[name]} | {_field_value(name, fields.get(name))} |")
    return "\n".join(lines)


def _format_sources(sources: list[dict[str, Any]], *, include_raw: bool) -> list[str]:
    lines = ["**保留的脱敏来源**"]
    for source in sources:
        lines.append(f"- [{source['kind']}] {source['text']}")
        if source.get("raw_status") == "EXPIRED":
            lines.append(f"  {EXPIRED_MARKER}")
        elif include_raw and source.get("raw_text") is not None:
            lines.append(f"  - 原文：{source['raw_text']}")
    return lines


def _format_attempts(attempts: list[dict[str, Any]], *, include_raw: bool) -> list[str]:
    lines = ["**处理记录**"]
    if not attempts:
        lines.append("- 无模型调用记录")
    for attempt in attempts:
        detail = f"- 第 {attempt['attempt']} 次：错误 {attempt['error_code'] or '无'}"
        if attempt["raw_status"] == "EXPIRED":
            detail += f" {EXPIRED_MARKER}"
        lines.append(detail)
        if attempt["raw_status"] == "EXPIRED":
            continue
        if include_raw and attempt.get("raw") is not None:
            lines.append(f"  - 原文：{attempt['raw']}")
    return lines


def format_panel(panel: dict[str, Any]) -> str:
    kind = panel.get("kind")
    lines: list[str] = []
    if kind == "result":
        lines.append(f"**执行状态**：{panel['status']}")
        if panel.get("notice"):
            lines.append(panel["notice"])
        lines.append(
            f"模型调用次数：{panel['model_calls']}；修复次数：{panel['repair_count']}"
        )
        if panel.get("request_id"):
            lines.append(f"请求编号：{panel['request_id']}")
        if panel.get("error_code"):
            lines.append(f"错误代码：{panel['error_code']}")
        if panel.get("fields"):
            lines.extend(["", _fields_table(panel["fields"])])
        else:
            lines.append("本次无可展示的提取结果。")
        if panel.get("session_id"):
            lines.append(f"会话编号：{panel['session_id']}")
    elif kind == "session":
        lines.append("**会话已恢复**")
        if panel.get("notice"):
            lines.append(panel["notice"])
        lines.append(f"会话编号：{panel['session_id']}")
        if panel.get("task_id"):
            lines.append(f"当前任务：{panel['task_id']}")
        lines.extend(["", *_format_sources(panel["sources"], include_raw=False)])
        lines.extend(["", "**历史有效结果**"])
        for run in panel["history"]:
            lines.append(f"- 请求 {run['request_id']}：{run['status']}。{run['notice']}")
            if run["fields"]:
                lines.append(f"  - 摘要：{run['fields'].get('summary', '（无）')}")
        if not panel["history"]:
            lines.append("- 暂无历史有效结果")
    elif kind == "review":
        lines.append(f"**审核项**：{panel['review_id']}")
        lines.append(
            f"状态：{panel['status']}；模型调用次数：{panel['model_calls']}；"
            f"修复次数：{panel['repair_count']}"
        )
        if panel.get("error_code"):
            lines.append(f"错误代码：{panel['error_code']}")
        if panel.get("notice"):
            lines.append(panel["notice"])
        lines.extend(["", *_format_sources(panel["sources"], include_raw=panel["raw_visible"])])
        lines.extend(["", *_format_attempts(panel["attempts"], include_raw=panel["raw_visible"])])
    elif kind == "reviews":
        lines.append(f"**待人工审核**：{panel['notice']}")
        if not panel["reviews"]:
            lines.append("当前没有待人工审核的记录。")
        for row in panel["reviews"]:
            lines.append(
                f"- {row['review_id']}｜请求 {row['request_id']}｜{row['status']}｜"
                f"模型调用 {row['model_calls']}｜修复 {row['repair_count']}"
            )
    else:
        if panel.get("notice"):
            lines.append(panel["notice"])
        if panel.get("error_code"):
            lines.append(f"错误代码：{panel['error_code']}")
        if panel.get("request_id"):
            lines.append(f"请求编号：{panel['request_id']}")
    return "\n".join(lines)


def create_ui(service_provider):
    import gradio as gr

    def outputs(panel, session_id="", task_id=None):
        return (
            format_panel(panel),
            panel.get("session_id") or session_id,
            panel.get("task_id") or task_id,
        )

    async def on_extract(text, session_id, task_id, new_task):
        panel = await extract(
            service_provider(),
            text=text,
            session_id=session_id,
            task_id=task_id,
            new_task=bool(new_task),
        )
        return outputs(panel, session_id, task_id)

    async def on_supplement(text, session_id, task_id):
        panel = await supplement(
            service_provider(), text=text, session_id=session_id, task_id=task_id
        )
        return outputs(panel, session_id, task_id)

    async def on_record(record_id):
        return outputs(await extract(service_provider(), record_id=record_id))

    async def on_restore(session_id):
        panel = await restore_session(service_provider(), session_id)
        return outputs(panel, session_id)

    async def on_clear(session_id):
        panel = await clear_session(service_provider(), session_id)
        return format_panel(panel), "", None

    async def on_reviews():
        return format_panel(await list_reviews(service_provider()))

    async def on_review(review_id, include_raw):
        panel = await review_detail(
            service_provider(), review_id, include_raw=bool(include_raw)
        )
        return format_panel(panel)

    with gr.Blocks(title="Support Extraction Harness") as demo:
        gr.Markdown(
            "# 支持对话提取演示\n"
            "页面与 API 共用同一个应用服务；提取仍由 Harness 执行，页面不直接调用模型或文件。"
        )
        with gr.Tabs():
            with gr.Tab("提取"):
                with gr.Row():
                    with gr.Column():
                        session_box = gr.Textbox(
                            label="会话编号",
                            placeholder="恢复或补充时填写；提取成功后自动填入",
                        )
                        task_state = gr.State(None)
                        new_task_box = gr.Checkbox(
                            label="新建任务", value=False, info=NEW_TASK_NOTICE
                        )
                        text_box = gr.Textbox(
                            label="支持对话文本", lines=6, placeholder="直接粘贴客户对话"
                        )
                        with gr.Row():
                            extract_button = gr.Button("提取", variant="primary")
                            supplement_button = gr.Button("补充当前任务")
                        gr.Markdown(SUPPLEMENT_NOTICE)
                        gr.Markdown(RECORD_MODE_NOTICE)
                        sample_box = gr.Dropdown(
                            label="原题样例",
                            choices=list(SAMPLE_RECORD_IDS),
                            value=SAMPLE_RECORD_IDS[0],
                        )
                        sample_button = gr.Button("用样例提取")
                        record_box = gr.Dropdown(
                            label="读取预置记录",
                            choices=list(RECORD_CHOICES),
                            value="TX-INJECT",
                        )
                        record_button = gr.Button("读取记录并提取")
                        with gr.Row():
                            restore_button = gr.Button("恢复会话")
                            clear_button = gr.Button("清空会话")
                    with gr.Column():
                        result_view = gr.Markdown("尚无结果。")
            with gr.Tab("人工审核"):
                gr.Markdown(
                    "默认只显示脱敏来源与处理记录；勾选后才展示受控原文。"
                    "超过保留期的原文会明确标记为已过期。"
                )
                refresh_button = gr.Button("刷新审核列表")
                reviews_view = gr.Markdown("尚未加载审核列表。")
                review_box = gr.Textbox(label="审核编号")
                raw_box = gr.Checkbox(label="显式展示受控原文", value=False)
                detail_button = gr.Button("查看审核详情")
                detail_view = gr.Markdown("尚未选择审核项。")

        extract_button.click(
            on_extract,
            inputs=[text_box, session_box, task_state, new_task_box],
            outputs=[result_view, session_box, task_state],
        )
        supplement_button.click(
            on_supplement,
            inputs=[text_box, session_box, task_state],
            outputs=[result_view, session_box, task_state],
        )
        sample_button.click(
            on_record,
            inputs=[sample_box],
            outputs=[result_view, session_box, task_state],
        )
        record_button.click(
            on_record,
            inputs=[record_box],
            outputs=[result_view, session_box, task_state],
        )
        restore_button.click(
            on_restore,
            inputs=[session_box],
            outputs=[result_view, session_box, task_state],
        )
        clear_button.click(
            on_clear,
            inputs=[session_box],
            outputs=[result_view, session_box, task_state],
        )
        refresh_button.click(on_reviews, outputs=[reviews_view])
        detail_button.click(
            on_review, inputs=[review_box, raw_box], outputs=[detail_view]
        )
        demo.load(on_reviews, outputs=[reviews_view])
    return demo


def mount_ui(app, service_provider):
    import gradio as gr

    gr.mount_gradio_app(app, create_ui(service_provider), path="/ui")
