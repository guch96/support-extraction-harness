import pytest
from conftest import ScriptedModel

from support_extraction.service import ExtractionService, InputError, Submission


def ticking_clock(now):
    def clock():
        now[0] += 1.0
        return now[0]

    return clock


async def test_history_returns_each_task_sources_and_runs_in_time_order(
    tmp_path, valid_json, valid_data
):
    now = [1000000.0]
    model = ScriptedModel(responses=[valid_json, "bad", "bad", "bad", valid_json])
    app = await ExtractionService.open(tmp_path, lambda: model, clock=ticking_clock(now))
    try:
        first = await app.submit(Submission(text="客户 ID CUST-111，重复扣款。"))
        failed = await app.submit(
            Submission(session_id=first.session_id, text="修正：CUST-222。")
        )
        assert failed.outcome == "FALLBACK"
        second = await app.submit(
            Submission(session_id=first.session_id, new_task=True, text="新问题：登录失败。")
        )
        history = await app.history(first.session_id)
    finally:
        await app.close()

    assert [task["task_id"] for task in history] == [first.task_id, second.task_id]
    assert [task["revision"] for task in history] == [2, 1]
    assert [task["pii_seen"] for task in history] == [False, False]

    first_task, second_task = history
    assert [source["sequence"] for source in first_task["sources"]] == [1, 2]
    assert [source["kind"] for source in first_task["sources"]] == ["transcript", "supplement"]
    assert [source["text"] for source in first_task["sources"]] == [
        "客户 ID CUST-111，重复扣款。",
        "修正：CUST-222。",
    ]
    assert all(
        set(source) == {"sequence", "kind", "text", "created_at"}
        for source in first_task["sources"]
    )
    assert first_task["sources"][0]["created_at"] < first_task["sources"][1]["created_at"]
    assert first_task["sources"][1]["created_at"] < second_task["sources"][0]["created_at"]

    assert [run["request_id"] for run in first_task["runs"]] == [
        first.request_id,
        failed.request_id,
    ]
    assert [run["outcome"] for run in first_task["runs"]] == ["SUCCESS", "FALLBACK"]
    assert [run["data"] for run in first_task["runs"]] == [valid_data, None]
    assert [run["source_revision"] for run in first_task["runs"]] == [1, 2]
    assert [run["model_calls"] for run in first_task["runs"]] == [1, 3]
    assert [run["repair_count"] for run in first_task["runs"]] == [0, 2]
    assert [run["error_code"] for run in first_task["runs"]] == [None, "OUTPUT_INVALID"]
    assert first_task["runs"][0]["review_id"] is None
    assert first_task["runs"][1]["review_id"] == failed.review_id
    assert first_task["runs"][0]["created_at"] < first_task["runs"][1]["created_at"]
    assert all(
        set(run)
        == {
            "request_id",
            "outcome",
            "data",
            "error_code",
            "model_calls",
            "repair_count",
            "source_revision",
            "created_at",
            "review_id",
            "diagnostics",
        }
        for run in first_task["runs"]
    )

    assert [source["text"] for source in second_task["sources"]] == ["新问题：登录失败。"]
    assert [run["outcome"] for run in second_task["runs"]] == ["SUCCESS"]
    assert second_task["runs"][0]["data"] == valid_data
    assert second_task["runs"][0]["request_id"] == second.request_id
    assert second_task["runs"][0]["review_id"] is None
    assert set(first_task["sources"][0]) == {"sequence", "kind", "text", "created_at"}
    assert "raw_text" not in first_task["sources"][0]


async def test_history_hides_other_sessions_tasks(tmp_path, valid_json):
    model = ScriptedModel(responses=[valid_json, valid_json])
    app = await ExtractionService.open(tmp_path, lambda: model)
    try:
        first = await app.submit(Submission(text="第一个会话。"))
        second = await app.submit(Submission(text="第二个会话。"))
        history = await app.history(second.session_id)
    finally:
        await app.close()

    assert first.session_id != second.session_id
    assert [task["task_id"] for task in history] == [second.task_id]
    assert [source["text"] for source in history[0]["sources"]] == ["第二个会话。"]


async def test_history_reports_unknown_cleared_and_expired_sessions(tmp_path, valid_json):
    now = [1000000.0]
    model = ScriptedModel(responses=[valid_json, valid_json])
    app = await ExtractionService.open(
        tmp_path, lambda: model, clock=lambda: now[0], session_ttl_days=1
    )
    try:
        with pytest.raises(InputError, match="SESSION_NOT_FOUND"):
            await app.history("missing")

        cleared = await app.submit(Submission(text="将被清空的会话。"))
        await app.clear(cleared.session_id)
        with pytest.raises(InputError, match="SESSION_EXPIRED"):
            await app.history(cleared.session_id)

        expired = await app.submit(Submission(text="将被过期的会话。"))
        now[0] += 1.5 * 86400
        with pytest.raises(InputError, match="SESSION_EXPIRED"):
            await app.history(expired.session_id)
    finally:
        await app.close()
