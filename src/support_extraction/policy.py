import json
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, StringConstraints

from harness.contracts import OutputContract
from support_extraction.safety import sanitize


class Extraction(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    customer_id: str | None
    issue_category: Literal["BILLING", "TECHNICAL", "ACCOUNT", "OTHER"]
    urgency: Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"]
    summary: Annotated[str, StringConstraints(max_length=200)]
    actionable_steps: list[str]
    detected_pii: bool


def finalize(data: dict, pii_seen: bool, known: tuple[str, ...]) -> dict:
    data = {
        key: sanitize(value, known) if isinstance(value, str) else value
        for key, value in data.items()
    }
    data["actionable_steps"] = [sanitize(value, known) for value in data["actionable_steps"]]
    data["detected_pii"] = data["detected_pii"] or pii_seen
    return data


def contract() -> OutputContract:
    template = Path(__file__).with_name("system.txt").read_text()
    system = template.replace(
        "{{extraction_json_schema}}", json.dumps(Extraction.model_json_schema(), ensure_ascii=False)
    )
    return OutputContract(Extraction, system, sanitize, finalize)
