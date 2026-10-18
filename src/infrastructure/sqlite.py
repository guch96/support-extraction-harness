import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path


def identifier():
    return uuid.uuid4().hex


class SQLiteStore:
    def __init__(self, path: Path, clock=time.time):
        path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
        path.touch(mode=0o600, exist_ok=True)
        os.chmod(path, 0o600)
        self.clock = clock
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA secure_delete=ON;
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY, current_task_id TEXT, last_used REAL, cleared INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY, session_id TEXT, revision INTEGER DEFAULT 0,
                pii_seen INTEGER DEFAULT 0, active_request TEXT
            );
            CREATE TABLE IF NOT EXISTS sources (
                task_id TEXT, sequence INTEGER, kind TEXT, safe_text TEXT, raw_text TEXT,
                original_length INTEGER, created_at REAL, PRIMARY KEY(task_id, sequence)
            );
            CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY, session_id TEXT, task_id TEXT,
                source_revision INTEGER DEFAULT 0,
                source_saved INTEGER DEFAULT 0, status TEXT, created_at REAL, data TEXT,
                error_code TEXT, model_calls INTEGER DEFAULT 0, repair_count INTEGER DEFAULT 0,
                review_id TEXT UNIQUE
            );
            CREATE TABLE IF NOT EXISTS attempts (
                request_id TEXT, attempt INTEGER, started_at REAL, raw TEXT, latency_ms REAL,
                error_code TEXT, feedback TEXT, PRIMARY KEY(request_id, attempt)
            );
        """)

    @contextmanager
    def transaction(self):
        try:
            with self.conn:
                yield self.conn
        except sqlite3.Error:
            raise OSError("PERSISTENCE_FAILED") from None

    def close(self):
        self.conn.close()

    def new_session(self):
        session = identifier()
        with self.transaction() as db:
            db.execute("INSERT INTO sessions(id,last_used) VALUES (?,?)", (session, self.clock()))
        return session

    def reserve(self, session_id, task_id, new_task, request_id):
        session_id = session_id or self.new_session()
        with self.transaction() as db:
            session = db.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
            if session is None:
                raise LookupError("SESSION_NOT_FOUND")
            task_id = task_id or (None if new_task else session["current_task_id"])
            if (
                task_id is not None
                and not db.execute(
                    "SELECT 1 FROM tasks WHERE id=? AND session_id=?", (task_id, session_id)
                ).fetchone()
            ):
                raise LookupError("TASK_NOT_FOUND")
            if (
                task_id is not None
                and db.execute(
                    "SELECT active_request FROM tasks WHERE id=?", (task_id,)
                ).fetchone()[0]
            ):
                raise LookupError("TASK_BUSY")
            if task_id is None:
                task_id = identifier()
                db.execute("INSERT INTO tasks(id,session_id) VALUES (?,?)", (task_id, session_id))
            db.execute(
                "UPDATE sessions SET current_task_id=?,last_used=? WHERE id=?",
                (task_id, self.clock(), session_id),
            )
            db.execute("UPDATE tasks SET active_request=? WHERE id=?", (request_id, task_id))
            db.execute(
                "INSERT INTO runs(id,session_id,task_id,status,created_at) VALUES (?,?,?,?,?)",
                (request_id, session_id, task_id, "PREPARING", self.clock()),
            )
        return session_id, task_id

    def append_source(self, request_id, text, safe_text, pii_seen):
        with self.transaction() as db:
            run = db.execute("SELECT * FROM runs WHERE id=?", (request_id,)).fetchone()
            task = db.execute("SELECT * FROM tasks WHERE id=?", (run["task_id"],)).fetchone()
            revision = task["revision"] + 1
            db.execute(
                "INSERT INTO sources VALUES (?,?,?,?,?,?,?)",
                (
                    task["id"],
                    revision,
                    "transcript" if revision == 1 else "supplement",
                    safe_text,
                    text,
                    len(text),
                    self.clock(),
                ),
            )
            db.execute(
                "UPDATE tasks SET revision=?,pii_seen=? WHERE id=?",
                (revision, bool(task["pii_seen"] or pii_seen), task["id"]),
            )
            db.execute(
                "UPDATE runs SET source_revision=?,source_saved=1 WHERE id=?",
                (revision, request_id),
            )

    def sources(self, task_id, revision=None):
        return [
            dict(row)
            for row in self.conn.execute(
                "SELECT * FROM sources WHERE task_id=? AND sequence<=? ORDER BY sequence",
                (task_id, revision or 2**31),
            )
        ]

    def task(self, task_id):
        return dict(self.conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())

    def restore(self, session_id):
        session = self.conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        if session is None:
            raise LookupError("SESSION_NOT_FOUND")
        with self.transaction() as db:
            db.execute("UPDATE sessions SET last_used=? WHERE id=?", (self.clock(), session_id))
        result = dict(session)
        result["sources"] = [
            {"sequence": s["sequence"], "kind": s["kind"], "text": s["safe_text"]}
            for s in self.sources(session["current_task_id"])
        ]
        result["tasks"] = [
            dict(row)
            for row in self.conn.execute(
                "SELECT id,revision,pii_seen FROM tasks WHERE session_id=?", (session_id,)
            )
        ]
        result["runs"] = [
            dict(row)
            for row in self.conn.execute(
                "SELECT id,status,data,source_revision FROM runs "
                "WHERE task_id=? ORDER BY created_at",
                (session["current_task_id"],),
            )
        ]
        return result

    def run_started(self, run):
        with self.transaction() as db:
            db.execute("UPDATE runs SET status='RUNNING' WHERE id=?", (run.request_id,))

    def attempt_started(self, request_id, attempt):
        with self.transaction() as db:
            db.execute(
                "INSERT INTO attempts(request_id,attempt,started_at) VALUES (?,?,?)",
                (request_id, attempt, self.clock()),
            )
            db.execute(
                "UPDATE runs SET model_calls=?,repair_count=? WHERE id=?",
                (attempt, attempt - 1, request_id),
            )

    def attempt_finished(self, request_id, attempt, raw, latency_ms, error_code):
        with self.transaction() as db:
            db.execute(
                "UPDATE attempts SET raw=?,latency_ms=?,error_code=? "
                "WHERE request_id=? AND attempt=?",
                (raw, latency_ms, error_code, request_id, attempt),
            )

    def validation_failed(self, request_id, attempt, feedback):
        with self.transaction() as db:
            db.execute(
                "UPDATE attempts SET feedback=? WHERE request_id=? AND attempt=?",
                (json.dumps(feedback, ensure_ascii=False), request_id, attempt),
            )

    def run_finished(self, result):
        review_id = identifier() if result.outcome == "FALLBACK" else None
        with self.transaction() as db:
            db.execute(
                "UPDATE runs SET status=?,data=?,error_code=?,review_id=? WHERE id=?",
                (
                    result.outcome,
                    json.dumps(result.data, ensure_ascii=False),
                    result.error_code,
                    review_id,
                    result.request_id,
                ),
            )
            db.execute(
                "UPDATE tasks SET active_request=NULL,pii_seen=MAX(pii_seen,?) WHERE id=?",
                (bool(result.data and result.data["detected_pii"]), result.task_id),
            )
        return review_id

    def review(self, review_id):
        row = self.conn.execute("SELECT * FROM runs WHERE review_id=?", (review_id,)).fetchone()
        if row is None:
            raise LookupError("REVIEW_NOT_FOUND")
        review = dict(row)
        review["status"] = "PENDING"
        review["sources"] = self.sources(row["task_id"], row["source_revision"])
        review["attempts"] = [
            dict(item)
            for item in self.conn.execute(
                "SELECT * FROM attempts WHERE request_id=? ORDER BY attempt", (row["id"],)
            )
        ]
        for attempt in review["attempts"]:
            attempt["feedback"] = json.loads(attempt["feedback"]) if attempt["feedback"] else None
        return review
