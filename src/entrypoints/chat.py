"""Chat page handlers and the Gradio assembly over the extraction service.

Every handler is a plain coroutine or async generator over
:class:`ExtractionService`, so the page logic can be tested without starting
Gradio, a network server or a model. Gradio is imported lazily inside
:func:`create_ui` and :func:`mount_ui` only.
"""

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import suppress
from typing import Any

from runtime.contracts import RunResult, ServiceError
from streaming.events import ChatEvent
from streaming.phase_sink import PhaseSink
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

CHAT_TAB_ID = "chat"
REVIEW_TAB_ID = "review"

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
FALLBACK_ACTION_NOTICE = "本次 FALLBACK · 审核编号 `{review_id}`"
PLACEHOLDER_NOTICE = "⏳ 正在处理…"
SOURCE_PLACEHOLDER = "（记录读取）"
EMPTY_MESSAGE = "（空消息）"

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

PHASE_ICONS = {
    "reading": "📖",
    "calling_model": "🤖",
    "repairing": "🛠️",
    "validating": "🔎",
    "succeeded": "✅",
    "failed": "⚠️",
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


def _status_bar(
    session_id: str | None,
    task_id: str | None,
    source_count: int | None,
    armed: bool,
) -> str:
    parts = []
    if task_id:
        count = "未知" if source_count is None else str(source_count)
        parts.extend([f"**当前任务**：`{task_id}`", f"来源 **{count}** 条"])
    else:
        parts.append("**当前任务**：尚未开始")
    if session_id:
        parts.append(f"会话：`{session_id}`")
    if armed:
        parts.append("**下一条自由文本将新建任务**")
    return " ｜ ".join(parts)


def _page_state(
    messages: list[dict[str, Any]],
    *,
    session_id: str | None = None,
    task_id: str | None = None,
    armed: bool = False,
    source_count: int | None = None,
    fallback_review_id: str | None = None,
) -> dict[str, Any]:
    return {
        "messages": messages,
        "status": _status_bar(session_id, task_id, source_count, armed),
        "session_id": session_id or "",
        "task_id": task_id,
        "armed": bool(armed),
        "fallback_review_id": fallback_review_id,
        "fallback_notice": (
            FALLBACK_ACTION_NOTICE.format(review_id=fallback_review_id)
            if fallback_review_id
            else ""
        ),
    }


def _empty_state() -> dict[str, Any]:
    return _page_state([])


def _error_state(
    exc: InputError | ServiceError,
    *,
    messages: list[dict[str, Any]],
    session_id: str | None,
    task_id: str | None,
    source_count: int | None = None,
) -> dict[str, Any]:
    card = format_error_card(error_panel(exc))
    return _page_state(
        [*messages, {"role": "assistant", "content": card}],
        session_id=session_id,
        task_id=task_id,
        armed=False,
        source_count=source_count,
    )


def _with_placeholder(
    messages: list[dict[str, Any]] | None, user_text: str
) -> list[dict[str, Any]]:
    history = list(messages or [])
    history.append({"role": "user", "content": user_text})
    history.append({"role": "assistant", "content": PLACEHOLDER_NOTICE})
    return history


def _replace_last_placeholder(
    history: list[dict[str, Any]], content: str
) -> list[dict[str, Any]]:
    return [*history[:-1], {"role": "assistant", "content": content}]


def _placeholder_text(chat_event: ChatEvent) -> str:
    icon = PHASE_ICONS.get(chat_event.phase.value, "⏳")
    return f"{icon} {chat_event.label}…"


async def _source_count(
    service: ExtractionService, session_id: str | None, task_id: str | None
) -> int | None:
    if not session_id or not task_id:
        return None
    try:
        session = await service.restore(session_id)
    except (InputError, ServiceError):
        return None
    if session["current_task_id"] != task_id:
        return None
    return len(session["sources"])


async def _cancel(event_task: asyncio.Task) -> None:
    event_task.cancel()
    with suppress(asyncio.CancelledError):
        await event_task


async def _stream(
    service: ExtractionService,
    submission: Submission,
    messages: list[dict[str, Any]] | None,
    *,
    user_text: str,
    session_id: str | None,
    task_id: str | None,
    armed: bool,
) -> AsyncIterator[dict[str, Any]]:
    history = _with_placeholder(messages, user_text)
    previous_count = await _source_count(service, session_id, task_id)
    yield _page_state(
        history,
        session_id=session_id,
        task_id=task_id,
        armed=armed,
        source_count=previous_count,
    )

    sink = PhaseSink()
    task = asyncio.create_task(service.submit(submission, phases=sink))
    event_task: asyncio.Task | None = None
    try:
        while True:
            event_task = asyncio.create_task(sink.next_event())
            done, _ = await asyncio.wait(
                {event_task, task}, return_when=asyncio.FIRST_COMPLETED
            )
            if event_task in done:
                history = _replace_last_placeholder(
                    history, _placeholder_text(event_task.result())
                )
                yield _page_state(
                    history,
                    session_id=session_id,
                    task_id=task_id,
                    armed=armed,
                    source_count=previous_count,
                )
            else:
                await _cancel(event_task)
            if task.done() and sink.empty:
                break

        try:
            result = await task
        except (InputError, ServiceError) as exc:
            yield _error_state(
                exc,
                messages=history[:-1],
                session_id=session_id,
                task_id=task_id,
                source_count=previous_count,
            )
            return
    finally:
        if event_task is not None and not event_task.done():
            await _cancel(event_task)
        if not task.done():
            await _cancel(task)

    history = _replace_last_placeholder(history, format_result_card(result_panel(result)))
    fallback_review_id = (
        result.review_id if result.outcome == "FALLBACK" else None
    )
    yield _page_state(
        history,
        session_id=result.session_id,
        task_id=result.task_id,
        armed=False,
        source_count=await _source_count(service, result.session_id, result.task_id),
        fallback_review_id=fallback_review_id,
    )


async def send_text(
    service: ExtractionService,
    text: str | None,
    session_id: str | None = None,
    task_id: str | None = None,
    new_task_armed: bool = False,
    messages: list[dict[str, Any]] | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Send free text: supplement the current task unless a new task is armed."""
    submission = Submission(
        text=text,
        session_id=session_id or None,
        task_id=None if new_task_armed else (task_id or None),
        new_task=bool(new_task_armed),
    )
    async for state in _stream(
        service,
        submission,
        messages,
        user_text=(text or "").strip() or EMPTY_MESSAGE,
        session_id=session_id,
        task_id=task_id,
        armed=bool(new_task_armed),
    ):
        yield state


async def send_record(
    service: ExtractionService,
    record_id: str | None,
    session_id: str | None = None,
    messages: list[dict[str, Any]] | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Send a sample or preset record; a record always starts a new task."""
    submission = Submission(
        record_id=record_id,
        session_id=session_id or None,
        new_task=True,
    )
    async for state in _stream(
        service,
        submission,
        messages,
        user_text=f"读取预置记录：`{record_id}`",
        session_id=session_id,
        task_id=None,
        armed=False,
    ):
        yield state


def _history_card(task: dict[str, Any], run: dict[str, Any], session_id: str) -> str:
    if run["outcome"] == "ERROR":
        panel = _empty_panel()
        panel.update(
            {
                "status": "SERVICE_ERROR",
                "error_code": run["error_code"],
                "request_id": run["request_id"],
                "session_id": session_id,
                "task_id": task["task_id"],
                "notice": HISTORICAL_NOTICE.format(request_id=run["request_id"]),
            }
        )
        return format_error_card(panel)
    result = RunResult(
        session_id=session_id,
        task_id=task["task_id"],
        request_id=run["request_id"],
        outcome=run["outcome"],
        data=run["data"],
        model_calls=run["model_calls"],
        repair_count=run["repair_count"],
        review_id=run["review_id"],
        error_code=run["error_code"],
    )
    return format_result_card(result_panel(result), historical=True)


def history_messages(
    tasks: list[dict[str, Any]], session_id: str
) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    for task in tasks:
        messages.append(
            {
                "role": "assistant",
                "content": (
                    f"---\n**任务** `{task['task_id']}` ｜ 来源 **{len(task['sources'])}** 条"
                ),
            }
        )
        source_texts = {
            source["sequence"]: source["text"] for source in task["sources"]
        }
        for run in task["runs"]:
            text = source_texts.get(run["source_revision"]) or SOURCE_PLACEHOLDER
            messages.append({"role": "user", "content": text})
            messages.append(
                {"role": "assistant", "content": _history_card(task, run, session_id)}
            )
    return messages


async def restore(
    service: ExtractionService, session_id: str | None
) -> dict[str, Any]:
    if not session_id:
        return _error_state(
            InputError("INVALID_INPUT"),
            messages=[],
            session_id="",
            task_id=None,
        )
    try:
        session = await service.restore(session_id)
        tasks = await service.history(session_id)
    except (InputError, ServiceError) as exc:
        return _error_state(
            exc,
            messages=[],
            session_id=session_id,
            task_id=None,
        )
    return _page_state(
        history_messages(tasks, session["id"]),
        session_id=session["id"],
        task_id=session["current_task_id"],
        armed=False,
        source_count=len(session["sources"]),
    )


async def clear(service: ExtractionService, session_id: str | None) -> dict[str, Any]:
    if not session_id:
        return _empty_state()
    try:
        await service.clear(session_id)
    except (InputError, ServiceError) as exc:
        return _error_state(
            exc,
            messages=[],
            session_id=session_id,
            task_id=None,
        )
    return _empty_state()


def new_session() -> dict[str, Any]:
    return _empty_state()


def arm_new_task() -> bool:
    return True


def open_review(review_id: str | None) -> dict[str, Any]:
    return {"tab": REVIEW_TAB_ID, "review_id": review_id or ""}


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
        text = "；".join(str(item) for item in value) if value else "（无）"
    elif value is None:
        text = "（无）"
    elif isinstance(value, bool):
        text = "是" if value else "否"
    else:
        text = str(value)
    return text.replace("|", "\\|").replace("\n", "<br>")


def _fields_table(fields: dict[str, Any]) -> str:
    lines = ["| 字段 | 值 |", "|---|---|"]
    for name in SIX_FIELDS:
        lines.append(f"| {FIELD_LABELS[name]} | {_field_value(name, fields.get(name))} |")
    return "\n".join(lines)


def format_result_card(panel: dict[str, Any], *, historical: bool = False) -> str:
    badge = {
        "SUCCESS": "🟢 SUCCESS",
        "REPAIRED": "🟠 REPAIRED",
        "FALLBACK": "🟡 FALLBACK",
    }.get(panel["status"], f"🔴 {panel['status']}")
    lines = [f"### {badge}"]
    if panel.get("notice"):
        lines.append(panel["notice"])
    if historical and panel.get("request_id"):
        lines.append(HISTORICAL_NOTICE.format(request_id=panel["request_id"]))
    lines.append("")
    lines.append(f"模型调用次数：{panel['model_calls']} ｜ 修复次数：{panel['repair_count']}")
    if panel.get("request_id"):
        lines.append(f"请求编号：`{panel['request_id']}`")
    if panel.get("error_code"):
        lines.append(f"错误代码：`{panel['error_code']}`")
    if panel.get("fields"):
        lines.extend(["", _fields_table(panel["fields"])])
        payload = json.dumps(panel["fields"], ensure_ascii=False, indent=2)
        lines.extend(
            [
                "",
                "<details>",
                "<summary>结构化结果（已校验）</summary>",
                "",
                "```json",
                payload,
                "```",
                "",
                "</details>",
            ]
        )
    else:
        lines.extend(["", "本次没有可展示的已校验字段。"])
    return "\n".join(lines)


def format_error_card(panel: dict[str, Any]) -> str:
    lines = ["### 🔴 ERROR"]
    if panel.get("notice"):
        lines.append(panel["notice"])
    if panel.get("error_code"):
        lines.append(f"错误代码：`{panel['error_code']}`")
    if panel.get("request_id"):
        lines.append(f"请求编号：`{panel['request_id']}`")
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
    if kind == "review":
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
        lines.append(format_error_card(panel))
    return "\n".join(lines)


def create_ui(service_provider):
    import gradio as gr

    def render(state):
        fallback_review_id = state.get("fallback_review_id")
        return (
            state["messages"],
            state["status"],
            state["session_id"],
            state["task_id"],
            state["armed"],
            gr.update(visible=bool(fallback_review_id)),
            state.get("fallback_notice") or "",
            fallback_review_id,
        )

    async def on_send(text, messages, session_id, task_id, armed):
        async for state in send_text(
            service_provider(),
            text,
            session_id,
            task_id,
            bool(armed),
            messages=messages,
        ):
            yield render(state)

    async def on_record(record_id, messages, session_id):
        async for state in send_record(
            service_provider(), record_id, session_id, messages=messages
        ):
            yield render(state)

    async def on_restore(session_id):
        return render(await restore(service_provider(), session_id))

    async def on_clear(session_id):
        return render(await clear(service_provider(), session_id))

    def on_new_session():
        return render(new_session())

    def on_open_review(review_id):
        state = open_review(review_id)
        return gr.update(selected=state["tab"]), state["review_id"]

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
        with gr.Tabs(selected=CHAT_TAB_ID) as tabs:
            with gr.Tab("聊天", id=CHAT_TAB_ID):
                status_view = gr.Markdown(_status_bar(None, None, None, False))
                with gr.Row():
                    session_box = gr.Textbox(
                        label="会话编号",
                        placeholder="可复制；填入后点“恢复会话”",
                        buttons=["copy"],
                    )
                    restore_button = gr.Button("恢复会话")
                    clear_button = gr.Button("清空会话")
                    new_session_button = gr.Button("新建会话")
                with gr.Row():
                    new_task_button = gr.Button("新建任务")
                    new_task_box = gr.Checkbox(
                        label="下一条自由文本新建任务",
                        value=False,
                        interactive=False,
                        info=NEW_TASK_NOTICE,
                    )
                    task_state = gr.State(None)
                chatbot = gr.Chatbot(label="对话", height=460, layout="bubble")
                with gr.Group(visible=False) as fallback_row:
                    with gr.Row():
                        fallback_notice = gr.Markdown()
                        fallback_button = gr.Button("查看审核")
                fallback_state = gr.State(None)
                with gr.Row():
                    text_box = gr.Textbox(
                        label="补充或修正当前任务",
                        lines=3,
                        scale=4,
                        placeholder="直接粘贴客户对话；默认补充当前任务",
                    )
                    send_button = gr.Button("发送", variant="primary", scale=1)
                gr.Markdown(SUPPLEMENT_NOTICE)
                with gr.Accordion("读取预置记录（发送时新建任务）", open=False):
                    gr.Markdown(RECORD_MODE_NOTICE)
                    with gr.Row():
                        sample_box = gr.Dropdown(
                            label="原题样例",
                            choices=list(SAMPLE_RECORD_IDS),
                            value=SAMPLE_RECORD_IDS[0],
                        )
                        sample_button = gr.Button("用样例发送")
                    with gr.Row():
                        record_box = gr.Dropdown(
                            label="读取预置记录",
                            choices=list(RECORD_CHOICES),
                            value="TX-INJECT",
                        )
                        record_button = gr.Button("读取记录并发送")
            with gr.Tab("人工审核", id=REVIEW_TAB_ID):
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

        outputs = [
            chatbot,
            status_view,
            session_box,
            task_state,
            new_task_box,
            fallback_row,
            fallback_notice,
            fallback_state,
        ]
        send_button.click(
            on_send,
            inputs=[text_box, chatbot, session_box, task_state, new_task_box],
            outputs=outputs,
        )
        text_box.submit(
            on_send,
            inputs=[text_box, chatbot, session_box, task_state, new_task_box],
            outputs=outputs,
        )
        sample_button.click(
            on_record,
            inputs=[sample_box, chatbot, session_box],
            outputs=outputs,
        )
        record_button.click(
            on_record,
            inputs=[record_box, chatbot, session_box],
            outputs=outputs,
        )
        restore_button.click(on_restore, inputs=[session_box], outputs=outputs)
        clear_button.click(on_clear, inputs=[session_box], outputs=outputs)
        new_session_button.click(on_new_session, outputs=outputs)
        new_task_button.click(arm_new_task, outputs=[new_task_box])
        fallback_button.click(
            on_open_review, inputs=[fallback_state], outputs=[tabs, review_box]
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
