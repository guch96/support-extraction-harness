from dataclasses import dataclass, field
from typing import NotRequired

from langchain.agents.middleware import AgentState
from langchain_core.messages import AIMessage, HumanMessage

from runtime.context import build_repair_message
from runtime.contracts import AuditSink, OutputContract, RunInput


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
