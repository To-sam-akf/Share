from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any


ACTIVE_RUN_STATUSES = ("queued", "running", "waiting_approval")


class AgentThreadActiveError(ValueError):
    pass


class AgentRunStore:
    def __init__(self, shared_folder: str | Path) -> None:
        internal = Path(shared_folder) / ".lan-sync"
        internal.mkdir(parents=True, exist_ok=True)
        self.database_path = internal / "agent-runs.sqlite3"
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS agent_threads (
                    thread_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    created_at_ns INTEGER NOT NULL,
                    updated_at_ns INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_agent_threads_updated
                ON agent_threads(updated_at_ns, thread_id);

                CREATE TABLE IF NOT EXISTS agent_runs (
                    run_id TEXT PRIMARY KEY,
                    thread_id TEXT NOT NULL,
                    request TEXT NOT NULL,
                    status TEXT NOT NULL,
                    plan_id TEXT,
                    report TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '',
                    created_at_ns INTEGER NOT NULL,
                    updated_at_ns INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_agent_runs_thread
                ON agent_runs(thread_id, created_at_ns);

                CREATE TABLE IF NOT EXISTS agent_steps (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    created_at_ns INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    input_json TEXT NOT NULL,
                    output_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_agent_steps_run
                ON agent_steps(run_id, id);

                CREATE TABLE IF NOT EXISTS sync_plans (
                    plan_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at_ns INTEGER NOT NULL,
                    updated_at_ns INTEGER NOT NULL
                );
                """
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO agent_threads (
                    thread_id, title, created_at_ns, updated_at_ns
                )
                SELECT
                    runs.thread_id,
                    substr(
                        (
                            SELECT first_run.request
                            FROM agent_runs first_run
                            WHERE first_run.thread_id = runs.thread_id
                            ORDER BY first_run.created_at_ns, first_run.run_id
                            LIMIT 1
                        ),
                        1,
                        80
                    ),
                    MIN(runs.created_at_ns),
                    MAX(runs.updated_at_ns)
                FROM agent_runs runs
                GROUP BY runs.thread_id
                """
            )

    def create_run(
        self,
        run_id: str,
        thread_id: str,
        request: str,
        status: str = "queued",
        *,
        require_existing_thread: bool = False,
    ) -> None:
        now = time.time_ns()
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            thread = connection.execute(
                "SELECT thread_id FROM agent_threads WHERE thread_id = ?",
                (thread_id,),
            ).fetchone()
            if thread is None:
                if require_existing_thread:
                    raise KeyError(thread_id)
                connection.execute(
                    """
                    INSERT INTO agent_threads (
                        thread_id, title, created_at_ns, updated_at_ns
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (thread_id, self._default_title(request), now, now),
                )
            active = connection.execute(
                """
                SELECT run_id
                FROM agent_runs
                WHERE thread_id = ?
                  AND status IN (?, ?, ?)
                LIMIT 1
                """,
                (thread_id, *ACTIVE_RUN_STATUSES),
            ).fetchone()
            if active is not None:
                raise AgentThreadActiveError(
                    "agent thread already has an active run"
                )
            connection.execute(
                """
                INSERT INTO agent_runs (
                    run_id, thread_id, request, status,
                    created_at_ns, updated_at_ns
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (run_id, thread_id, request, status, now, now),
            )
            connection.execute(
                """
                UPDATE agent_threads
                SET updated_at_ns = ?
                WHERE thread_id = ?
                """,
                (now, thread_id),
            )

    def update_run(self, run_id: str, **changes: Any) -> None:
        allowed = {"status", "plan_id", "report", "error"}
        values = {key: value for key, value in changes.items() if key in allowed}
        if not values:
            return
        values["updated_at_ns"] = time.time_ns()
        assignments = ", ".join(f"{key} = ?" for key in values)
        with self._lock, self._connect() as connection:
            connection.execute(
                f"UPDATE agent_runs SET {assignments} WHERE run_id = ?",
                (*values.values(), run_id),
            )
            connection.execute(
                """
                UPDATE agent_threads
                SET updated_at_ns = ?
                WHERE thread_id = (
                    SELECT thread_id FROM agent_runs WHERE run_id = ?
                )
                """,
                (values["updated_at_ns"], run_id),
            )

    def append_step(
        self,
        run_id: str,
        *,
        kind: str,
        name: str,
        status: str,
        input_data: Any = None,
        output_data: Any = None,
    ) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO agent_steps (
                    run_id, created_at_ns, kind, name, status,
                    input_json, output_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    time.time_ns(),
                    str(kind),
                    str(name),
                    str(status),
                    json.dumps(input_data, ensure_ascii=False, default=str),
                    json.dumps(output_data, ensure_ascii=False, default=str),
                ),
            )

    def save_plan(self, plan_id: str, run_id: str, payload: dict[str, Any]) -> None:
        now = time.time_ns()
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO sync_plans (
                    plan_id, run_id, payload_json, created_at_ns, updated_at_ns
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(plan_id) DO UPDATE SET
                    payload_json = excluded.payload_json,
                    updated_at_ns = excluded.updated_at_ns
                """,
                (plan_id, run_id, encoded, now, now),
            )
        self.update_run(run_id, plan_id=plan_id)

    def load_plan(self, plan_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM sync_plans WHERE plan_id = ?",
                (plan_id,),
            ).fetchone()
        return json.loads(row["payload_json"]) if row is not None else None

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM agent_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            steps = connection.execute(
                """
                SELECT created_at_ns, kind, name, status,
                       input_json, output_json
                FROM agent_steps
                WHERE run_id = ?
                ORDER BY id
                """,
                (run_id,),
            ).fetchall()
        if row is None:
            return None
        payload = dict(row)
        payload["steps"] = [
            {
                "created_at_ns": int(step["created_at_ns"]),
                "kind": str(step["kind"]),
                "name": str(step["name"]),
                "status": str(step["status"]),
                "input": json.loads(step["input_json"]),
                "output": json.loads(step["output_json"]),
            }
            for step in steps
        ]
        payload["plan"] = (
            self.load_plan(str(row["plan_id"])) if row["plan_id"] else None
        )
        payload["messages"] = [
            {
                "run_id": str(row["run_id"]),
                "role": "user",
                "content": str(row["request"]),
                "created_at_ns": int(row["created_at_ns"]),
            }
        ]
        if row["report"]:
            payload["messages"].append(
                {
                    "run_id": str(row["run_id"]),
                    "role": "assistant",
                    "content": str(row["report"]),
                    "created_at_ns": int(row["updated_at_ns"]),
                }
            )
        return payload

    def list_threads(
        self,
        limit: int = 50,
        cursor: str = "",
    ) -> dict[str, Any]:
        page_size = max(1, min(int(limit), 200))
        cursor_values = self._decode_cursor(cursor)
        where = ""
        parameters: list[Any] = []
        if cursor_values is not None:
            cursor_time, cursor_id = cursor_values
            where = """
                WHERE t.updated_at_ns < ?
                   OR (t.updated_at_ns = ? AND t.thread_id < ?)
            """
            parameters.extend((cursor_time, cursor_time, cursor_id))
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT
                    t.thread_id,
                    t.title,
                    t.created_at_ns,
                    t.updated_at_ns,
                    (
                        SELECT r.run_id
                        FROM agent_runs r
                        WHERE r.thread_id = t.thread_id
                        ORDER BY r.created_at_ns DESC, r.run_id DESC
                        LIMIT 1
                    ) AS latest_run_id,
                    (
                        SELECT r.status
                        FROM agent_runs r
                        WHERE r.thread_id = t.thread_id
                        ORDER BY r.created_at_ns DESC, r.run_id DESC
                        LIMIT 1
                    ) AS status,
                    (
                        SELECT COUNT(*)
                        FROM agent_runs r
                        WHERE r.thread_id = t.thread_id
                    ) AS run_count
                FROM agent_threads t
                {where}
                ORDER BY t.updated_at_ns DESC, t.thread_id DESC
                LIMIT ?
                """,
                (*parameters, page_size + 1),
            ).fetchall()
        has_more = len(rows) > page_size
        page = rows[:page_size]
        items = [self._thread_summary(row) for row in page]
        next_cursor = (
            self._encode_cursor(
                int(page[-1]["updated_at_ns"]),
                str(page[-1]["thread_id"]),
            )
            if has_more and page
            else None
        )
        return {"items": items, "next_cursor": next_cursor}

    def get_thread(
        self,
        thread_id: str,
        *,
        limit: int = 50,
        cursor: str = "",
    ) -> dict[str, Any] | None:
        page_size = max(1, min(int(limit), 200))
        cursor_values = self._decode_cursor(cursor)
        where = ""
        parameters: list[Any] = [str(thread_id)]
        if cursor_values is not None:
            cursor_time, cursor_id = cursor_values
            where = """
                AND (
                    created_at_ns < ?
                    OR (created_at_ns = ? AND run_id < ?)
                )
            """
            parameters.extend((cursor_time, cursor_time, cursor_id))
        with self._lock, self._connect() as connection:
            thread = connection.execute(
                """
                SELECT
                    t.thread_id,
                    t.title,
                    t.created_at_ns,
                    t.updated_at_ns,
                    (
                        SELECT r.run_id
                        FROM agent_runs r
                        WHERE r.thread_id = t.thread_id
                        ORDER BY r.created_at_ns DESC, r.run_id DESC
                        LIMIT 1
                    ) AS latest_run_id,
                    (
                        SELECT r.status
                        FROM agent_runs r
                        WHERE r.thread_id = t.thread_id
                        ORDER BY r.created_at_ns DESC, r.run_id DESC
                        LIMIT 1
                    ) AS status,
                    (
                        SELECT COUNT(*)
                        FROM agent_runs r
                        WHERE r.thread_id = t.thread_id
                    ) AS run_count
                FROM agent_threads t
                WHERE t.thread_id = ?
                """,
                (str(thread_id),),
            ).fetchone()
            if thread is None:
                return None
            rows = connection.execute(
                f"""
                SELECT run_id, request, report, created_at_ns, updated_at_ns
                FROM agent_runs
                WHERE thread_id = ?
                {where}
                ORDER BY created_at_ns DESC, run_id DESC
                LIMIT ?
                """,
                (*parameters, page_size + 1),
            ).fetchall()
        has_more = len(rows) > page_size
        page = rows[:page_size]
        messages: list[dict[str, Any]] = []
        for row in reversed(page):
            messages.append(
                {
                    "run_id": str(row["run_id"]),
                    "role": "user",
                    "content": str(row["request"]),
                    "created_at_ns": int(row["created_at_ns"]),
                }
            )
            if row["report"]:
                messages.append(
                    {
                        "run_id": str(row["run_id"]),
                        "role": "assistant",
                        "content": str(row["report"]),
                        "created_at_ns": int(row["updated_at_ns"]),
                    }
                )
        next_cursor = (
            self._encode_cursor(
                int(page[-1]["created_at_ns"]),
                str(page[-1]["run_id"]),
            )
            if has_more and page
            else None
        )
        latest_run_id = str(thread["latest_run_id"] or "")
        return {
            "thread": self._thread_summary(thread),
            "messages": messages,
            "latest_run": self.get_run(latest_run_id) if latest_run_id else None,
            "next_cursor": next_cursor,
        }

    def rename_thread(self, thread_id: str, title: str) -> dict[str, Any]:
        normalized = str(title).strip()
        if not normalized or len(normalized) > 80:
            raise ValueError("title must be between 1 and 80 characters")
        now = time.time_ns()
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE agent_threads
                SET title = ?, updated_at_ns = ?
                WHERE thread_id = ?
                """,
                (normalized, now, str(thread_id)),
            )
            if cursor.rowcount == 0:
                raise KeyError(thread_id)
        detail = self.get_thread(thread_id, limit=1)
        assert detail is not None
        return detail["thread"]

    def thread_run_ids_for_delete(self, thread_id: str) -> list[str]:
        with self._lock, self._connect() as connection:
            thread = connection.execute(
                "SELECT thread_id FROM agent_threads WHERE thread_id = ?",
                (str(thread_id),),
            ).fetchone()
            if thread is None:
                raise KeyError(thread_id)
            active = connection.execute(
                """
                SELECT run_id
                FROM agent_runs
                WHERE thread_id = ?
                  AND status IN (?, ?, ?)
                LIMIT 1
                """,
                (str(thread_id), *ACTIVE_RUN_STATUSES),
            ).fetchone()
            if active is not None:
                raise AgentThreadActiveError(
                    "active agent thread cannot be deleted"
                )
            rows = connection.execute(
                "SELECT run_id FROM agent_runs WHERE thread_id = ?",
                (str(thread_id),),
            ).fetchall()
        return [str(row["run_id"]) for row in rows]

    def delete_thread(self, thread_id: str) -> None:
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            thread = connection.execute(
                "SELECT thread_id FROM agent_threads WHERE thread_id = ?",
                (str(thread_id),),
            ).fetchone()
            if thread is None:
                raise KeyError(thread_id)
            active = connection.execute(
                """
                SELECT run_id
                FROM agent_runs
                WHERE thread_id = ?
                  AND status IN (?, ?, ?)
                LIMIT 1
                """,
                (str(thread_id), *ACTIVE_RUN_STATUSES),
            ).fetchone()
            if active is not None:
                raise AgentThreadActiveError(
                    "active agent thread cannot be deleted"
                )
            connection.execute(
                """
                DELETE FROM sync_plans
                WHERE run_id IN (
                    SELECT run_id FROM agent_runs WHERE thread_id = ?
                )
                """,
                (str(thread_id),),
            )
            connection.execute(
                """
                DELETE FROM agent_steps
                WHERE run_id IN (
                    SELECT run_id FROM agent_runs WHERE thread_id = ?
                )
                """,
                (str(thread_id),),
            )
            connection.execute(
                "DELETE FROM agent_runs WHERE thread_id = ?",
                (str(thread_id),),
            )
            connection.execute(
                "DELETE FROM agent_threads WHERE thread_id = ?",
                (str(thread_id),),
            )

    def thread_context(
        self,
        thread_id: str,
        *,
        exclude_run_id: str = "",
        limit: int = 6,
    ) -> list[dict[str, str]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT run_id, request, report
                FROM agent_runs
                WHERE thread_id = ?
                ORDER BY created_at_ns DESC
                LIMIT ?
                """,
                (str(thread_id), max(1, min(int(limit) + 1, 20))),
            ).fetchall()
        messages: list[dict[str, str]] = []
        for row in reversed(rows):
            if exclude_run_id and row["run_id"] == exclude_run_id:
                continue
            messages.append({"role": "user", "content": str(row["request"])})
            if row["report"]:
                messages.append(
                    {"role": "assistant", "content": str(row["report"])}
                )
        return messages[-limit * 2 :]

    @staticmethod
    def _default_title(request: str) -> str:
        return str(request).strip()[:80] or "新会话"

    @staticmethod
    def _encode_cursor(timestamp_ns: int, item_id: str) -> str:
        return f"{int(timestamp_ns)}:{item_id}"

    @staticmethod
    def _decode_cursor(cursor: str) -> tuple[int, str] | None:
        value = str(cursor).strip()
        if not value:
            return None
        timestamp, separator, item_id = value.partition(":")
        if not separator or not item_id:
            raise ValueError("invalid cursor")
        try:
            timestamp_ns = int(timestamp)
        except ValueError as exc:
            raise ValueError("invalid cursor") from exc
        if timestamp_ns < 0:
            raise ValueError("invalid cursor")
        return timestamp_ns, item_id

    @staticmethod
    def _thread_summary(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "thread_id": str(row["thread_id"]),
            "title": str(row["title"]),
            "latest_run_id": str(row["latest_run_id"] or ""),
            "status": str(row["status"] or ""),
            "run_count": int(row["run_count"]),
            "created_at_ns": int(row["created_at_ns"]),
            "updated_at_ns": int(row["updated_at_ns"]),
        }
