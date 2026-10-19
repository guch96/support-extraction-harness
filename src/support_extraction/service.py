import asyncio
import os
import time
from dataclasses import dataclass
from pathlib import Path

import aiosqlite
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from infrastructure.sqlite import RAW_RETENTION_SECONDS, SQLiteStore, identifier
from runtime.contracts import (
    RunInput,
    RunResult,
    ServiceError,
    SourceUnavailable,
    extractor_thread,
)
from runtime.engine import Harness
from runtime.events import event
from streaming.events import Phase
from streaming.phase_sink import PhaseSink
from support_extraction.demo import DEMO_TEXT, DemoFailureModel
from support_extraction.policy import contract
from support_extraction.safety import sanitize, scan, sensitive_values


@dataclass(frozen=True)
class Submission:
    text: str | None = None
    record_id: str | None = None
    session_id: str | None = None
    task_id: str | None = None
    new_task: bool = False


class InputError(ValueError):
    def __init__(self, code: str, request_id: str | None = None):
        super().__init__(code)
        self.code = code
        self.request_id = request_id or identifier()


class ExtractionService:
    @classmethod
    async def open(
        cls,
        directory: Path,
        model_factory,
        *,
        clock=time.time,
        max_source_chars=20000,
        max_turns=20,
        source_reader=None,
        allowed_records=frozenset(),
        session_ttl_days=7,
    ):
        service = cls()
        service.store = SQLiteStore(
            Path(directory) / "audit.sqlite3",
            clock=clock,
            session_ttl_seconds=session_ttl_days * 86400,
        )
        path = Path(directory) / "checkpoints.sqlite3"
        path.touch(mode=0o600, exist_ok=True)
        os.chmod(path, 0o600)
        service.checkpoint_connection = await aiosqlite.connect(path)
        service.checkpointer = AsyncSqliteSaver(service.checkpoint_connection)
        service.model_factory = model_factory
        service.max_source_chars, service.max_turns = max_source_chars, max_turns
        service.source_reader = source_reader
        service.allowed_records = frozenset(allowed_records)
        service.store.recover_interrupted()
        await service.maintenance()
        return service

    async def close(self):
        await self.checkpoint_connection.close()
        self.store.close()

    async def restore(self, session_id):
        await self.maintenance()
        try:
            return self.store.restore(session_id)
        except LookupError as exc:
            raise InputError(str(exc)) from None

    async def history(self, session_id):
        await self.maintenance()
        try:
            return self.store.history(session_id)
        except LookupError as exc:
            raise InputError(str(exc)) from None

    async def review(self, review_id, *, raw=False):
        await self.maintenance()
        try:
            review = self.store.review(review_id)
        except LookupError as exc:
            raise InputError(str(exc)) from None
        cutoff = self.store.clock() - RAW_RETENTION_SECONDS
        for source in review["sources"]:
            source["raw_status"] = "EXPIRED" if source["created_at"] <= cutoff else "AVAILABLE"
        for attempt in review["attempts"]:
            attempt["raw_status"] = (
                "EXPIRED"
                if attempt["started_at"] <= cutoff
                else "AVAILABLE"
                if attempt["raw"]
                else "UNAVAILABLE"
            )
        if not raw:
            for source in review["sources"]:
                source.pop("raw_text", None)
            for attempt in review["attempts"]:
                attempt.pop("raw", None)
        return review

    async def reviews(self):
        await self.maintenance()
        return self.store.reviews()

    async def maintenance(self):
        for session_id in self.store.cleanup():
            await self.clear(session_id)

    async def clear(self, session_id):
        try:
            task_ids = self.store.clear(session_id)
        except LookupError as exc:
            raise InputError(str(exc)) from None
        for task_id in task_ids:
            await self.checkpointer.adelete_thread(extractor_thread(session_id, task_id))

    async def submit(self, submission: Submission, phases: PhaseSink | None = None):
        return await self._submit(submission, phases, self.model_factory)

    async def seed_review_demo(self, session_id: str | None = None) -> RunResult:
        """Generate one PENDING review with the local failing model.

        The demo uses the same reserve/source/Harness/review path as a normal
        submission; only the model factory is replaced. The configured model
        factory is never invoked.
        """
        return await self._submit(
            Submission(text=DEMO_TEXT, session_id=session_id),
            model_factory=DemoFailureModel,
        )

    async def _submit(
        self,
        submission: Submission,
        phases: PhaseSink | None = None,
        model_factory=None,
    ):
        model_factory = model_factory or self.model_factory
        await self.maintenance()
        request_id = identifier()
        if (
            (submission.text is None) == (submission.record_id is None)
            or (
                submission.text is not None
                and (not isinstance(submission.text, str) or not submission.text.strip())
            )
            or (
                submission.task_id is not None
                and (submission.session_id is None or submission.new_task)
            )
        ):
            raise InputError("INVALID_INPUT", request_id)
        text = submission.text
        input_mode = "record" if submission.record_id is not None else "text"
        read_error = None
        if submission.record_id is not None:
            if phases is not None:
                phases.emit(Phase.READING, "正在读取预置记录")
            access_started = time.monotonic()
            if submission.record_id not in self.allowed_records or self.source_reader is None:
                event(
                    "source_denied",
                    request_id=request_id,
                    reason="ACCESS_DENIED",
                    latency_ms=round((time.monotonic() - access_started) * 1000, 3),
                )
                raise InputError("ACCESS_DENIED", request_id)
            started = time.monotonic()
            failure = None
            try:
                async with asyncio.timeout(10):
                    text = await self.source_reader.read(submission.record_id)
                if not isinstance(text, str) or not text.strip():
                    raise SourceUnavailable("INVALID_SOURCE_RESPONSE")
            except SourceUnavailable as exc:
                read_error = exc.code
                failure = read_error
                text = ""
            except Exception:
                failure = "SOURCE_POLICY_FAILED"
                raise ServiceError(failure, request_id, source_saved=False) from None
            finally:
                fields = {
                    "request_id": request_id,
                    "reason": "ACCESS_GRANTED",
                    "latency_ms": round((time.monotonic() - started) * 1000, 3),
                    "outcome": "FAILED" if failure else "RECEIVED",
                }
                if failure:
                    fields["error_code"] = failure
                event("source_read", **fields)
        try:
            session_id, task_id = self.store.reserve(
                submission.session_id,
                submission.task_id,
                submission.new_task or submission.record_id is not None,
                request_id,
                source_length=len(text),
                max_source_chars=self.max_source_chars,
                max_turns=self.max_turns,
            )
        except LookupError as exc:
            raise InputError(str(exc), request_id) from None
        source_saved = False
        try:
            if read_error is not None:
                result = RunResult.fallback(
                    session_id,
                    task_id,
                    request_id,
                    read_error,
                    diagnostics={
                        "input_mode": input_mode,
                        "record_id": submission.record_id,
                        "source_revision": self.store.task(task_id)["revision"],
                        "pii": {"detected": False, "kinds": [], "redactions": 0},
                        "tags_neutralized": 0,
                        "repairs": [],
                        "model_errors": [{"attempt": 0, "code": read_error}],
                        "budget": {
                            "max_calls": contract().max_model_calls,
                            "model_calls": 0,
                            "repair_count": 0,
                        },
                    },
                )
                result.review_id = self.store.run_finished(result)
                if phases is not None:
                    phases.emit(Phase.FAILED, "未取得合格结果，已转人工审核")
                event(
                    "run_finished",
                    **result.model_dump(exclude={"data", "diagnostics"}),
                )
                return result
            try:
                known = sensitive_values(text)
                facts = scan(text, known)
                safe_text = sanitize(text, known)
            except Exception:
                raise ServiceError("SAFETY_PROCESSING_FAILED", request_id) from None
            self.store.append_source(request_id, text, safe_text, bool(known))
            source_saved = True
            task = self.store.task(task_id)
            sources = self.store.sources(task_id)
            try:
                known = tuple(
                    value
                    for s in sources
                    if s["raw_text"]
                    for value in sensitive_values(s["raw_text"])
                )
            except Exception:
                raise ServiceError("SAFETY_PROCESSING_FAILED", request_id) from None
            run = RunInput(
                session_id,
                task_id,
                request_id,
                [
                    {"sequence": s["sequence"], "kind": s["kind"], "text": s["safe_text"]}
                    for s in sources
                ],
                task["revision"],
                bool(task["pii_seen"]),
                known,
            )
            harness = Harness(
                model_factory(),
                contract(),
                self.store,
                phase_sink=phases,
                checkpointer=self.checkpointer,
            )
            return await harness.run(
                run,
                diagnostics={
                    "input_mode": input_mode,
                    "record_id": submission.record_id,
                    "pii": {
                        "detected": bool(facts["kinds"] or facts["redactions"]),
                        "kinds": facts["kinds"],
                        "redactions": facts["redactions"],
                    },
                    "tags_neutralized": facts["tags"],
                },
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            code = (
                exc.code
                if isinstance(exc, ServiceError)
                else ("PERSISTENCE_FAILED" if isinstance(exc, OSError) else "INTERNAL_ERROR")
            )
            try:
                self.store.run_failed(request_id, code)
            except OSError:
                code = "PERSISTENCE_FAILED"
            raise ServiceError(code, request_id, source_saved=source_saved) from None
