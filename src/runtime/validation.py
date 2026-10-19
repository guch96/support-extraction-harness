from dataclasses import dataclass

from langchain_core.messages import AIMessage
from pydantic import ValidationError

from runtime.contracts import OutputContract, RunInput


@dataclass(frozen=True)
class Accepted:
    data: dict


@dataclass(frozen=True)
class Rejected:
    stage: str
    errors: list[dict]


def _project_errors(contract: OutputContract, exc: ValidationError) -> list[dict]:
    errors = []
    for error in exc.errors(include_input=False, include_context=True, include_url=False):
        path = [
            part if isinstance(part, int) or part in contract.schema.model_fields else "extra_field"
            for part in error["loc"]
        ]
        constraint = {
            k: v
            for k, v in error.get("ctx", {}).items()
            if k in {"max_length", "min_length", "le", "ge"} and isinstance(v, (int, float))
        }
        errors.append({"path": path, "code": error["type"], "constraint": constraint})
    return errors


def validate_response(
    contract: OutputContract, run: RunInput, message: AIMessage | None
) -> Accepted | Rejected:
    if message is None or not isinstance(message.content, str) or not message.content.strip():
        return Rejected(
            "response_protocol",
            [{"path": [], "code": "empty_or_nontext_response", "constraint": {}}],
        )
    if message.tool_calls or message.invalid_tool_calls:
        return Rejected(
            "response_protocol",
            [{"path": [], "code": "unexpected_tool_call", "constraint": {}}],
        )
    stage = "raw_schema"
    try:
        value = contract.schema.model_validate_json(message.content, strict=True)
        stage = "final_schema"
        data = contract.finalize(value.model_dump(mode="json"), run.pii_seen, run.secret_values)
        contract.schema.model_validate(data, strict=True)
    except ValidationError as exc:
        return Rejected(stage, _project_errors(contract, exc))
    return Accepted(data)
