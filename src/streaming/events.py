from dataclasses import dataclass
from enum import StrEnum


class Phase(StrEnum):
    """User-visible progress of one chat extraction run."""

    READING = "reading"
    CALLING_MODEL = "calling_model"
    REPAIRING = "repairing"
    VALIDATING = "validating"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass(frozen=True)
class ChatEvent:
    seq: int
    phase: Phase
    label: str
