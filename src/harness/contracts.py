from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from pydantic import BaseModel


@dataclass(frozen=True)
class OutputContract:
    schema: type[BaseModel]
    system_prompt: str
    sanitize: Callable[[str, tuple[str, ...]], str]
    finalize: Callable[[dict[str, Any], bool, tuple[str, ...]], dict[str, Any]]


@dataclass(frozen=True)
class RunInput:
    session_id: str
    task_id: str
    request_id: str
    sources: list[dict[str, Any]]
    source_revision: int
    pii_seen: bool
    secret_values: tuple[str, ...] = field(default=(), repr=False)


class RunResult(BaseModel):
    session_id: str
    task_id: str
    request_id: str
    outcome: Literal["SUCCESS", "REPAIRED", "FALLBACK"]
    data: dict[str, Any] | None = None
    model_calls: int
    repair_count: int
    review_id: str | None = None
    error_code: str | None = None


class AuditSink(Protocol):
    def run_started(self, run: RunInput) -> None: ...
    def attempt_started(self, request_id: str, attempt: int) -> None: ...
    def attempt_finished(
        self,
        request_id: str,
        attempt: int,
        raw: str | None,
        latency_ms: float,
        error_code: str | None,
    ) -> None: ...
    def validation_failed(
        self, request_id: str, attempt: int, feedback: dict[str, Any]
    ) -> None: ...
    def run_finished(self, result: RunResult) -> str | None: ...


class SourceUnavailable(Exception):
    """An expected source read failure, raised by a source adapter.

    Any other adapter exception is a policy fault and must not be treated as an
    expected read failure.
    """

    def __init__(self, code: str = "SOURCE_UNAVAILABLE"):
        super().__init__(code)
        self.code = code


class ServiceError(Exception):
    def __init__(self, code: str, request_id: str, source_saved: bool | None = None):
        super().__init__(code)
        self.code = code
        self.request_id = request_id
        self.source_saved = source_saved
