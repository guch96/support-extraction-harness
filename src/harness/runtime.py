import asyncio
import time
from dataclasses import dataclass, field
from typing import NotRequired

from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware, AgentState, ModelResponse, hook_config
from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage, SystemMessage
from langgraph.graph.message import REMOVE_ALL_MESSAGES

from harness.context import build_repair_message, build_source_message, safe_json
from harness.contracts import AuditSink, OutputContract, RunInput, RunResult, ServiceError
from harness.events import correlation, event
from harness.failures import ModelCallFailure, model_error_code
from harness.prompts import EXTRACTION_PROMPT_VERSION, REPAIR_PROMPT_VERSION
from harness.validation import Accepted, validate_response


class ExtractionState(AgentState):
    outcome: NotRequired[str | None]
    data: NotRequired[dict | None]
    feedback: NotRequired[dict | None]
    model_calls: NotRequired[int]
    repair_count: NotRequired[int]


@dataclass
class RunContext:
    run: RunInput
    contract: OutputContract
    audit: AuditSink
    deadline: float
    call_timeout: float
    source_message: str
    model_calls: int = 0
    repair_count: int = 0
    last_response: AIMessage | None = field(default=None, repr=False)
    feedback: dict | None = None
    data: dict | None = None
    outcome: str | None = None
    error_code: str | None = None

    def sanitize(self, text: str) -> str:
        return self.contract.sanitize(text, self.run.secret_values)

    def messages(self):
        messages = [HumanMessage(content=self.source_message)]
        if self.feedback:
            messages.append(
                HumanMessage(
                    content=build_repair_message(self.contract.repair_prompt, self.feedback)
                )
            )
        return messages


class RecoveryMiddleware(AgentMiddleware):
    state_schema = ExtractionState

    @hook_config(can_jump_to=["end"])
    async def abefore_model(self, state, runtime):
        ctx = runtime.context
        if ctx.model_calls >= ctx.contract.max_model_calls or time.monotonic() >= ctx.deadline:
            ctx.error_code = "BUDGET_EXHAUSTED"
            return {"jump_to": "end"}
        return None

    async def awrap_model_call(self, request, handler):
        ctx = request.runtime.context
        wrapper_start = time.monotonic()
        remaining = ctx.deadline - wrapper_start
        if remaining <= 0 or ctx.model_calls >= ctx.contract.max_model_calls:
            ctx.error_code = "BUDGET_EXHAUSTED"
            raise ModelCallFailure()
        messages = ctx.messages()
        prompt_version = REPAIR_PROMPT_VERSION if ctx.feedback else EXTRACTION_PROMPT_VERSION
        ctx.model_calls += 1
        ctx.repair_count = ctx.model_calls - 1
        attempt = ctx.model_calls
        ctx.audit.attempt_started(ctx.run.request_id, attempt)
        sdk_latency_ms = 0.0
        raw = None
        code = None
        try:
            async with asyncio.timeout(min(ctx.call_timeout, remaining)):
                sdk_start = time.monotonic()
                try:
                    response = await handler(
                        request.override(
                            messages=messages,
                            tools=[],
                            system_message=SystemMessage(content=ctx.contract.system_prompt),
                        )
                    )
                finally:
                    sdk_latency_ms = (time.monotonic() - sdk_start) * 1000
            original = response.result[-1]
            ctx.last_response = original
            raw = original.model_dump_json()
            # Raw evidence stays in the sink/context, never in the graph checkpoint.
            content = original.content if isinstance(original.content, str) else ""
            safe = AIMessage(content=ctx.sanitize(content))
            return ModelResponse(result=[safe])
        except asyncio.CancelledError:
            code = "INTERRUPTED"
            raise
        except Exception as exc:
            code = model_error_code(exc)
            if code is None:
                raise
            ctx.error_code = code
            if code == "MODEL_CONFIG_ERROR":
                # A configuration fault is not a fallback candidate: let it reach
                # the service layer as a stable service error, not INTERNAL_ERROR.
                raise ServiceError(code, ctx.run.request_id) from None
            raise ModelCallFailure() from None
        finally:
            wrapper_latency_ms = (time.monotonic() - wrapper_start) * 1000
            sdk_latency_ms = round(sdk_latency_ms, 3)
            wrapper_latency_ms = round(wrapper_latency_ms, 3)
            ctx.audit.attempt_finished(ctx.run.request_id, attempt, raw, sdk_latency_ms, code)
            event(
                "model_call",
                **correlation(ctx.run),
                attempt=attempt,
                prompt_version=prompt_version,
                sdk_latency_ms=sdk_latency_ms,
                wrapper_latency_ms=wrapper_latency_ms,
                model_calls=ctx.model_calls,
                repair_count=ctx.repair_count,
                error_code=code,
            )

    @hook_config(can_jump_to=["model", "end"])
    async def aafter_model(self, state, runtime):
        ctx = runtime.context
        message = ctx.last_response
        verdict = validate_response(ctx.contract, ctx.run, message)
        if isinstance(verdict, Accepted):
            ctx.data = verdict.data
            ctx.outcome = "REPAIRED" if ctx.repair_count else "SUCCESS"
            return {
                "outcome": ctx.outcome,
                "data": ctx.data,
                "feedback": None,
                "model_calls": ctx.model_calls,
                "repair_count": ctx.repair_count,
                "messages": [
                    RemoveMessage(id=REMOVE_ALL_MESSAGES),
                    AIMessage(content=safe_json(ctx.data)),
                ],
                "jump_to": "end",
            }
        # Protocol errors do not send model-selected tool names/arguments back as instructions.
        previous = message.content if message and isinstance(message.content, str) else ""
        if verdict.stage == "response_protocol":
            previous = "[INVALID_RESPONSE_PROTOCOL]"
        ctx.feedback = {
            "failed_attempt": ctx.model_calls,
            "validation_stage": verdict.stage,
            "error_signature": [
                f"{'.'.join(map(str, e['path']))}:{e['code']}" for e in verdict.errors
            ],
            "errors": verdict.errors,
            "previous_output": ctx.sanitize(previous),
        }
        ctx.audit.validation_failed(ctx.run.request_id, ctx.model_calls, ctx.feedback)
        event(
            "validation_failed",
            **correlation(ctx.run),
            attempt=ctx.model_calls,
            validation_stage=verdict.stage,
            error_signature=ctx.feedback["error_signature"],
        )
        ctx.error_code = "OUTPUT_INVALID"
        return {
            "feedback": ctx.feedback,
            "data": None,
            "model_calls": ctx.model_calls,
            "repair_count": ctx.repair_count,
            "jump_to": "model" if ctx.model_calls < ctx.contract.max_model_calls else "end",
        }


class Harness:
    def __init__(
        self,
        model,
        contract: OutputContract,
        audit: AuditSink,
        *,
        checkpointer=None,
        call_timeout=30.0,
        run_timeout=90.0,
    ):
        self.contract, self.audit = contract, audit
        self.call_timeout, self.run_timeout = call_timeout, run_timeout
        self.graph = create_agent(
            model=model,
            tools=[],
            middleware=[RecoveryMiddleware()],
            context_schema=RunContext,
            checkpointer=checkpointer,
        )

    async def run(self, run: RunInput) -> RunResult:
        source_message = build_source_message(run, self.contract)
        ctx = RunContext(
            run,
            self.contract,
            self.audit,
            time.monotonic() + self.run_timeout,
            self.call_timeout,
            source_message,
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
                                "thread_id": f"{run.session_id}:{run.task_id}:extractor"
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

    def _finish(self, ctx):
        result = RunResult(
            session_id=ctx.run.session_id,
            task_id=ctx.run.task_id,
            request_id=ctx.run.request_id,
            outcome=ctx.outcome or "FALLBACK",
            data=ctx.data if ctx.outcome else None,
            model_calls=ctx.model_calls,
            repair_count=ctx.repair_count,
            error_code=None if ctx.outcome else ctx.error_code or "OUTPUT_INVALID",
        )
        result.review_id = self.audit.run_finished(result)
        if result.outcome == "FALLBACK" and not result.review_id:
            raise ServiceError("PERSISTENCE_FAILED", result.request_id)
        event(
            "run_finished",
            **result.model_dump(exclude={"data"}),
            source_revision=ctx.run.source_revision,
        )
        return result
