import asyncio
import time

from langchain.agents import create_agent
from langchain_core.messages import HumanMessage, RemoveMessage
from langgraph.graph.message import REMOVE_ALL_MESSAGES

from middleware.audit import AuditMiddleware
from middleware.budget import BudgetMiddleware
from middleware.chat_events import ChatEventsMiddleware
from middleware.validation_repair import ValidationRepairMiddleware
from runtime.context import build_source_message
from runtime.contracts import (
    AuditSink,
    OutputContract,
    RunInput,
    RunResult,
    ServiceError,
    extractor_thread,
)
from runtime.events import event
from runtime.failures import ModelCallFailure
from runtime.state import RunContext
from streaming.events import Phase
from streaming.phase_sink import PhaseSink


class Harness:
    def __init__(
        self,
        model,
        contract: OutputContract,
        audit: AuditSink,
        *,
        phase_sink: PhaseSink | None = None,
        checkpointer=None,
        call_timeout=30.0,
        run_timeout=90.0,
    ):
        self.contract, self.audit = contract, audit
        self.call_timeout, self.run_timeout = call_timeout, run_timeout
        chat_events = ChatEventsMiddleware(phase_sink)
        self.phases = chat_events.sink
        self.graph = create_agent(
            model=model,
            tools=[],
            middleware=[
                chat_events,
                BudgetMiddleware(),
                AuditMiddleware(),
                ValidationRepairMiddleware(),
            ],
            context_schema=RunContext,
            checkpointer=checkpointer,
        )

    async def run(self, run: RunInput, diagnostics: dict | None = None) -> RunResult:
        source_message = build_source_message(run, self.contract)
        ctx = RunContext(
            run,
            self.contract,
            self.audit,
            time.monotonic() + self.run_timeout,
            self.call_timeout,
            source_message,
            diagnostics=dict(diagnostics or {}),
        )
        try:
            self.audit.run_started(run)
            try:
                async with asyncio.timeout(self.run_timeout):
                    await self.graph.ainvoke(
                        {
                            "messages": [
                                RemoveMessage(id=REMOVE_ALL_MESSAGES),
                                HumanMessage(content=source_message),
                            ],
                            "data": None,
                            "feedback": None,
                            "outcome": None,
                            "model_calls": 0,
                            "repair_count": 0,
                        },
                        config={
                            "configurable": {
                                "thread_id": extractor_thread(run.session_id, run.task_id)
                            },
                            "recursion_limit": 30,
                        },
                        context=ctx,
                    )
            except (ModelCallFailure, TimeoutError):
                ctx.error_code = ctx.error_code or "RUN_TIMEOUT"
            except asyncio.CancelledError:
                ctx.error_code = "INTERRUPTED"
                self._finish(ctx)
                raise
            return self._finish(ctx)
        except asyncio.CancelledError:
            raise
        except OSError:
            raise ServiceError("PERSISTENCE_FAILED", run.request_id) from None
        except Exception as exc:
            if isinstance(exc, ServiceError):
                raise
            raise ServiceError("INTERNAL_ERROR", run.request_id) from None

    def _diagnostics(self, ctx) -> dict:
        return {
            "input_mode": ctx.diagnostics.get("input_mode"),
            "record_id": ctx.diagnostics.get("record_id"),
            "source_revision": ctx.run.source_revision,
            "pii": ctx.diagnostics.get("pii")
            or {"detected": False, "kinds": [], "redactions": 0},
            "tags_neutralized": ctx.diagnostics.get("tags_neutralized", 0),
            "repairs": list(ctx.repairs),
            "model_errors": list(ctx.model_errors),
            "budget": {
                "max_calls": ctx.contract.max_model_calls,
                "model_calls": ctx.model_calls,
                "repair_count": ctx.repair_count,
            },
        }

    def _finish(self, ctx):
        diagnostics = self._diagnostics(ctx)
        if ctx.outcome:
            result = RunResult(
                session_id=ctx.run.session_id,
                task_id=ctx.run.task_id,
                request_id=ctx.run.request_id,
                outcome=ctx.outcome,
                data=ctx.data,
                model_calls=ctx.model_calls,
                repair_count=ctx.repair_count,
                diagnostics=diagnostics,
            )
        else:
            result = RunResult.fallback(
                ctx.run.session_id,
                ctx.run.task_id,
                ctx.run.request_id,
                ctx.error_code or "OUTPUT_INVALID",
                model_calls=ctx.model_calls,
                repair_count=ctx.repair_count,
                diagnostics=diagnostics,
            )
        result.review_id = self.audit.run_finished(result)
        if result.outcome == "FALLBACK" and not result.review_id:
            raise ServiceError("PERSISTENCE_FAILED", result.request_id)
        if result.outcome == "FALLBACK":
            self.phases.emit(Phase.FAILED, "未取得合格结果，已转人工审核")
        else:
            self.phases.emit(Phase.SUCCEEDED, "提取完成")
        event(
            "run_finished",
            **result.model_dump(exclude={"data", "diagnostics"}),
            source_revision=ctx.run.source_revision,
        )
        return result
