import os
import time
from dataclasses import dataclass
from pathlib import Path

import aiosqlite
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from harness.contracts import RunInput
from harness.runtime import Harness
from infrastructure.sqlite import SQLiteStore, identifier
from support_extraction.policy import contract
from support_extraction.safety import sanitize, sensitive_values


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
    async def open(cls, directory: Path, model_factory, *, clock=time.time):
        service = cls()
        service.store = SQLiteStore(Path(directory) / "audit.sqlite3", clock=clock)
        path = Path(directory) / "checkpoints.sqlite3"
        path.touch(mode=0o600, exist_ok=True)
        os.chmod(path, 0o600)
        service.checkpoint_connection = await aiosqlite.connect(path)
        service.checkpointer = AsyncSqliteSaver(service.checkpoint_connection)
        service.model_factory = model_factory
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

    async def review(self, review_id, *, raw=False):
        await self.maintenance()
        review = self.store.review(review_id)
        cutoff = self.store.clock() - 7 * 86400
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
            threads = self.store.clear(session_id)
        except LookupError as exc:
            raise InputError(str(exc)) from None
        for thread in threads:
            await self.checkpointer.adelete_thread(thread)

    async def submit(self, submission: Submission):
        await self.maintenance()
        request_id = identifier()
        try:
            session_id, task_id = self.store.reserve(
                submission.session_id, submission.task_id, submission.new_task, request_id
            )
        except LookupError as exc:
            raise InputError(str(exc), request_id) from None
        text = submission.text
        known = sensitive_values(text)
        self.store.append_source(request_id, text, sanitize(text, known), bool(known))
        task = self.store.task(task_id)
        sources = self.store.sources(task_id)
        known = tuple(
            value for s in sources if s["raw_text"] for value in sensitive_values(s["raw_text"])
        )
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
            self.model_factory(), contract(), self.store, checkpointer=self.checkpointer
        )
        return await harness.run(run)
