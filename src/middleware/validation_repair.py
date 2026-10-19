from langchain.agents.middleware import AgentMiddleware, hook_config
from langchain_core.messages import AIMessage, RemoveMessage
from langgraph.graph.message import REMOVE_ALL_MESSAGES

from runtime.context import safe_json
from runtime.events import correlation, event
from runtime.state import ExtractionState
from runtime.validation import Accepted, validate_response


class ValidationRepairMiddleware(AgentMiddleware):
    state_schema = ExtractionState

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
            previous = previous if previous.strip() else "[INVALID_RESPONSE_PROTOCOL]"
        ctx.feedback = {
            "failed_attempt": ctx.model_calls,
            "validation_stage": verdict.stage,
            "error_signature": [
                f"{'.'.join(map(str, e['path']))}:{e['code']}" for e in verdict.errors
            ],
            "errors": verdict.errors,
            "previous_output": ctx.sanitize(previous),
        }
        ctx.repairs.append(
            {
                "attempt": ctx.model_calls,
                "stage": verdict.stage,
                "signature": list(ctx.feedback["error_signature"]),
            }
        )
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
