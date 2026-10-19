import asyncio
import time

from langchain.agents.middleware import AgentMiddleware, ModelResponse
from langchain_core.messages import AIMessage, SystemMessage

from middleware.budget import call_timeout
from runtime.contracts import ServiceError
from runtime.events import correlation, event
from runtime.failures import ModelCallFailure, model_error_code
from runtime.prompts import EXTRACTION_PROMPT_VERSION, REPAIR_PROMPT_VERSION


class AuditMiddleware(AgentMiddleware):
    async def awrap_model_call(self, request, handler):
        ctx = request.runtime.context
        wrapper_start = time.monotonic()
        messages = ctx.messages()
        prompt_version = REPAIR_PROMPT_VERSION if ctx.feedback else EXTRACTION_PROMPT_VERSION
        attempt = ctx.model_calls
        ctx.audit.attempt_started(ctx.run.request_id, attempt)
        sdk_latency_ms = 0.0
        raw = None
        code = None
        try:
            async with call_timeout(ctx):
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
            try:
                safe_content = ctx.sanitize(content)
            except Exception:
                raise ServiceError("SAFETY_PROCESSING_FAILED", ctx.run.request_id) from None
            safe = AIMessage(content=safe_content)
            return ModelResponse(result=[safe])
        except asyncio.CancelledError:
            code = "INTERRUPTED"
            raise
        except ServiceError:
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
