import asyncio
import time

from langchain.agents.middleware import AgentMiddleware, hook_config

from runtime.failures import ModelCallFailure
from runtime.state import ExtractionState


def call_timeout(ctx):
    """Bound one model call by both the run deadline and the per-call timeout."""
    return asyncio.timeout(min(ctx.call_timeout, ctx.deadline - time.monotonic()))


class BudgetMiddleware(AgentMiddleware):
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
        remaining = ctx.deadline - time.monotonic()
        if remaining <= 0 or ctx.model_calls >= ctx.contract.max_model_calls:
            ctx.error_code = "BUDGET_EXHAUSTED"
            raise ModelCallFailure()
        ctx.model_calls += 1
        ctx.repair_count = ctx.model_calls - 1
        return await handler(request)
