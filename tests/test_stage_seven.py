from __future__ import annotations

import asyncio
import hashlib
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agents.orchestrator import ReActSyncAgent
from agents.store import AgentRunStore, AgentThreadActiveError
from agents.sync_tools import SyncCoordinator, normalize_path_prefix
from discovery import DiscoveredDevice
from security import PERMISSION_WRITE, SecurityStore
from sync import FileIndex
from transfer import TCPFileServer
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage


class ToolCallingFakeModel(FakeMessagesListChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


class FailingChatModel:
    async def ainvoke(self, messages):
        raise RuntimeError("AuthenticationError: invalid API key")


class SuccessfulChatModel:
    async def ainvoke(self, messages):
        return AIMessage(content="我是模型生成的 LANSync Agent 回答。")


class StaticDiscovery:
    def __init__(self, devices=None) -> None:
        self.devices = list(devices or [])

    def list_devices(self):
        return list(self.devices)


class StageSevenSyncAgentTests(unittest.TestCase):
    def test_agent_threads_backfill_paginate_rename_and_delete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            store = AgentRunStore(folder)
            store.create_run("run-a1", "thread-a", "first request")
            store.update_run("run-a1", status="completed", report="first report")
            store.create_run("run-a2", "thread-a", "second request")
            store.update_run("run-a2", status="completed", report="second report")
            store.create_run("run-b1", "thread-b", "other request")
            store.update_run("run-b1", status="completed", report="other report")

            first_page = store.list_threads(limit=1)
            self.assertEqual(len(first_page["items"]), 1)
            self.assertIsNotNone(first_page["next_cursor"])
            second_page = store.list_threads(
                limit=1,
                cursor=first_page["next_cursor"],
            )
            self.assertEqual(len(second_page["items"]), 1)
            self.assertNotEqual(
                first_page["items"][0]["thread_id"],
                second_page["items"][0]["thread_id"],
            )

            detail = store.get_thread("thread-a", limit=1)
            self.assertEqual(
                [message["content"] for message in detail["messages"]],
                ["second request", "second report"],
            )
            older = store.get_thread(
                "thread-a",
                limit=1,
                cursor=detail["next_cursor"],
            )
            self.assertEqual(
                [message["content"] for message in older["messages"]],
                ["first request", "first report"],
            )

            renamed = store.rename_thread("thread-a", "  Renamed thread  ")
            self.assertEqual(renamed["title"], "Renamed thread")
            store.append_step(
                "run-a1",
                kind="test",
                name="step",
                status="success",
            )
            store.save_plan("plan-a", "run-a1", {"plan_id": "plan-a"})
            store.delete_thread("thread-a")
            self.assertIsNone(store.get_thread("thread-a"))
            with sqlite3.connect(store.database_path) as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM agent_steps WHERE run_id LIKE 'run-a%'"
                    ).fetchone()[0],
                    0,
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM sync_plans WHERE run_id LIKE 'run-a%'"
                    ).fetchone()[0],
                    0,
                )

            with sqlite3.connect(store.database_path) as connection:
                connection.execute(
                    "DELETE FROM agent_threads WHERE thread_id = 'thread-b'"
                )
            reloaded = AgentRunStore(folder)
            backfilled = reloaded.get_thread("thread-b")
            self.assertEqual(backfilled["thread"]["title"], "other request")

    def test_agent_thread_rejects_concurrent_run_and_active_delete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = AgentRunStore(Path(tmp))
            store.create_run("run-active", "thread-active", "first")
            with self.assertRaises(AgentThreadActiveError):
                store.create_run("run-second", "thread-active", "second")
            with self.assertRaises(AgentThreadActiveError):
                store.thread_run_ids_for_delete("thread-active")
            with self.assertRaises(AgentThreadActiveError):
                store.delete_thread("thread-active")
            with self.assertRaises(KeyError):
                store.create_run(
                    "run-missing",
                    "missing-thread",
                    "missing",
                    require_existing_thread=True,
                )

    def test_agent_thread_delete_removes_langgraph_checkpoints(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as tmp:
                folder = Path(tmp)
                runtime = SimpleNamespace(
                    config=SimpleNamespace(shared_folder=folder),
                    events=SimpleNamespace(publish=lambda *_: None),
                )
                agent = ReActSyncAgent(runtime)
                agent.store.create_run("run-delete", "thread-delete", "done")
                agent.store.update_run("run-delete", status="completed")
                with sqlite3.connect(agent.checkpoint_path) as connection:
                    connection.executescript(
                        """
                        CREATE TABLE checkpoints (
                            thread_id TEXT NOT NULL,
                            checkpoint_ns TEXT NOT NULL DEFAULT '',
                            checkpoint_id TEXT NOT NULL,
                            PRIMARY KEY (
                                thread_id, checkpoint_ns, checkpoint_id
                            )
                        );
                        CREATE TABLE writes (
                            thread_id TEXT NOT NULL,
                            checkpoint_ns TEXT NOT NULL DEFAULT '',
                            checkpoint_id TEXT NOT NULL,
                            task_id TEXT NOT NULL,
                            idx INTEGER NOT NULL,
                            channel TEXT NOT NULL,
                            PRIMARY KEY (
                                thread_id, checkpoint_ns, checkpoint_id,
                                task_id, idx
                            )
                        );
                        """
                    )
                    connection.execute(
                        """
                        INSERT INTO checkpoints (
                            thread_id, checkpoint_ns, checkpoint_id
                        ) VALUES (?, '', 'checkpoint')
                        """,
                        ("run-delete",),
                    )
                    connection.execute(
                        """
                        INSERT INTO writes (
                            thread_id, checkpoint_ns, checkpoint_id,
                            task_id, idx, channel
                        ) VALUES (?, '', 'checkpoint', 'task', 0, 'channel')
                        """,
                        ("run-delete",),
                    )

                await agent.delete_thread("thread-delete")
                self.assertIsNone(agent.get_thread("thread-delete"))
                with sqlite3.connect(agent.checkpoint_path) as connection:
                    self.assertEqual(
                        connection.execute(
                            "SELECT COUNT(*) FROM checkpoints"
                        ).fetchone()[0],
                        0,
                    )
                    self.assertEqual(
                        connection.execute("SELECT COUNT(*) FROM writes").fetchone()[0],
                        0,
                    )

        asyncio.run(scenario())

    def test_connection_diagnosis_takes_priority_over_device_listing(self) -> None:
        self.assertTrue(ReActSyncAgent._is_analysis_request("诊断在线设备"))
        self.assertFalse(ReActSyncAgent._is_device_query_request("诊断在线设备"))

    def test_identity_question_is_not_treated_as_sync_action(self) -> None:
        self.assertFalse(ReActSyncAgent._is_sync_action_request("你是谁"))
        self.assertTrue(ReActSyncAgent._is_sync_action_request("同步 PC-B 的 notes"))

    def test_path_prefix_rejects_traversal_and_wildcards(self) -> None:
        self.assertEqual(normalize_path_prefix("notes/research"), "notes/research")
        self.assertEqual(normalize_path_prefix(""), "")
        with self.assertRaises(ValueError):
            normalize_path_prefix("../secret")
        with self.assertRaises(ValueError):
            normalize_path_prefix("notes/*")

    def test_bidirectional_plan_executes_and_verifies_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder_a = root / "a"
            folder_b = root / "b"
            (folder_a / "notes").mkdir(parents=True)
            (folder_b / "notes").mkdir(parents=True)
            (folder_a / "notes" / "local.txt").write_text(
                "from-a",
                encoding="utf-8",
            )
            deleted_path = folder_a / "notes" / "deleted.txt"
            deleted_path.write_text("remove-me", encoding="utf-8")
            (folder_b / "notes" / "remote.txt").write_text(
                "from-b",
                encoding="utf-8",
            )

            index_a = FileIndex(folder_a, "device-a", "PC-A")
            index_b = FileIndex(folder_b, "device-b", "PC-B")
            index_a.scan()
            index_b.scan()
            deleted_path.unlink()
            index_a.scan()
            security_a = SecurityStore(folder_a)
            security_b = SecurityStore(folder_b)
            token = SecurityStore.generate_token()
            security_a.authorize_device(
                "device-b",
                "PC-B",
                token,
                PERMISSION_WRITE,
            )
            security_b.authorize_device(
                "device-a",
                "PC-A",
                token,
                PERMISSION_WRITE,
            )
            server = TCPFileServer(
                "127.0.0.1",
                0,
                folder_b,
                chunk_size=4,
                file_index=index_b,
                security_store=security_b,
                device_id="device-b",
                device_name="PC-B",
            )
            server.start()
            try:
                device = DiscoveredDevice(
                    device_id="device-b",
                    device_name="PC-B",
                    ip="127.0.0.1",
                    tcp_port=server.bound_port,
                    status="online",
                    last_seen=time.time(),
                    tls_enabled=False,
                    certificate_fingerprint=None,
                )
                discovery = StaticDiscovery([device])
                runtime = SimpleNamespace(
                    config=SimpleNamespace(
                        shared_folder=folder_a,
                        chunk_size=4,
                        enable_tls=False,
                    ),
                    device_id="device-a",
                    file_index=index_a,
                    security_store=security_a,
                    sync_service=SimpleNamespace(request_timeout=5.0),
                    discovery=discovery,
                    events=SimpleNamespace(publish=lambda *_: None),
                )
                runtime.find_online_device = lambda device_id: (
                    device if device_id == "device-b" else None
                )
                runtime.devices_payload = lambda: [
                    {
                        "device_id": "device-b",
                        "device_name": "PC-B",
                        "online": True,
                        "paired": True,
                        "permission": "write",
                        "tls_enabled": False,
                    }
                ]
                store = AgentRunStore(folder_a)
                store.create_run("run-1", "thread-1", "同步 PC-B 的 notes")
                coordinator = SyncCoordinator(runtime, store)
                plan = coordinator.generate_sync_plan(
                    "run-1",
                    "device-b",
                    "notes",
                )
                self.assertEqual(plan["counts"]["upload"], 1)
                self.assertEqual(plan["counts"]["download"], 1)
                self.assertEqual(plan["counts"]["delete_report"], 1)
                download = next(
                    action
                    for action in plan["actions"]
                    if action["direction"] == "download"
                )
                remote_hash = download["remote"]["file_hash"]
                transfer_key = hashlib.sha256(
                    f"device-b\0notes/remote.txt\0{remote_hash}".encode("utf-8")
                ).hexdigest()
                fetch_folder = folder_a / ".lan-sync" / "fetch"
                fetch_folder.mkdir(parents=True, exist_ok=True)
                (fetch_folder / f"{transfer_key}.part").write_bytes(b"from")
                plan["status"] = "approved"
                store.save_plan(plan["plan_id"], "run-1", plan)

                executed = coordinator.execute_sync_plan(plan["plan_id"])
                self.assertEqual(executed["status"], "executed")
                self.assertTrue(
                    all(
                        action["transferred_bytes"] == action["bytes"]
                        for action in executed["actions"]
                        if action["executable"]
                    )
                )
                verification = coordinator.verify_sync_plan(plan["plan_id"])
                self.assertTrue(verification["success"])
                self.assertEqual(
                    (folder_a / "notes" / "remote.txt").read_text(encoding="utf-8"),
                    "from-b",
                )
                self.assertEqual(
                    (folder_b / "notes" / "local.txt").read_text(encoding="utf-8"),
                    "from-a",
                )
                self.assertFalse((folder_b / "notes" / "deleted.txt").exists())
            finally:
                server.stop()

    def test_plan_becomes_stale_before_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = root / "local"
            folder.mkdir()
            store = AgentRunStore(folder)
            store.create_run("run-1", "thread-1", "sync")
            coordinator = SyncCoordinator(SimpleNamespace(), store)
            plan = {
                "plan_id": "plan-1",
                "run_id": "run-1",
                "status": "approved",
                "snapshot_fingerprint": "old",
            }
            store.save_plan("plan-1", "run-1", plan)
            coordinator._current_fingerprint = lambda _: "new"  # type: ignore[method-assign]
            with self.assertRaisesRegex(Exception, "索引已变化"):
                coordinator.execute_sync_plan("plan-1")
            self.assertEqual(store.load_plan("plan-1")["status"], "stale")

    def test_langgraph_interrupt_resumes_after_approval(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as tmp:
                folder = Path(tmp)
                runtime = SimpleNamespace(
                    config=SimpleNamespace(shared_folder=folder),
                    events=SimpleNamespace(publish=lambda *_: None),
                )
                agent = ReActSyncAgent(runtime)

                def generate(run_id: str, device_id: str, prefix: str):
                    plan = {
                        "plan_id": "plan-approval",
                        "run_id": run_id,
                        "device_id": device_id,
                        "device_name": "PC-B",
                        "path_prefix": prefix,
                        "snapshot_fingerprint": "snapshot",
                        "counts": {
                            "upload": 1,
                            "download": 0,
                            "same": 0,
                            "conflict": 0,
                            "delete_report": 0,
                        },
                        "total_bytes": 4,
                        "risks": [],
                        "actions": [
                            {
                                "action_id": "action-1",
                                "direction": "upload",
                                "relative_path": "notes/a.txt",
                                "bytes": 4,
                                "reason": "LOCAL_ONLY",
                                "executable": True,
                                "status": "pending",
                                "error_code": "",
                                "error_message": "",
                                "local": None,
                                "remote": None,
                            }
                        ],
                        "status": "waiting_approval",
                        "approved_at_ns": None,
                        "created_at_ns": time.time_ns(),
                        "verification": None,
                    }
                    agent.store.save_plan(plan["plan_id"], run_id, plan)
                    return plan

                def execute(plan_id: str):
                    plan = agent.store.load_plan(plan_id)
                    plan["status"] = "executed"
                    plan["actions"][0]["status"] = "success"
                    agent.store.save_plan(plan_id, plan["run_id"], plan)
                    return plan

                agent.coordinator.resolve_request = lambda *_: ("device-b", "notes")
                agent.coordinator.device_candidates = lambda _: (
                    None,
                    [
                        {
                            "device_id": "device-b",
                            "device_name": "PC-B",
                            "ip": "192.168.1.8",
                        },
                        {
                            "device_id": "device-c",
                            "device_name": "PC-C",
                            "ip": "192.168.1.9",
                        },
                    ],
                )
                agent.coordinator.generate_sync_plan = generate
                agent.coordinator.execute_sync_plan = execute
                agent.coordinator.verify_sync_plan = lambda _: {
                    "success": True,
                    "checks": [],
                }

                with patch("agents.orchestrator.create_chat_model", return_value=None):
                    created = agent.create_run("同步 PC-B 的 notes")
                    for _ in range(100):
                        await asyncio.sleep(0.02)
                        current = agent.get_run(created["run_id"])
                        if current["status"] == "waiting_approval":
                            break
                    self.assertEqual(current["status"], "waiting_approval")
                    self.assertIsNone(current["plan"])
                    agent.decide(created["run_id"], device_id="device-b")
                    for _ in range(100):
                        await asyncio.sleep(0.02)
                        current = agent.get_run(created["run_id"])
                        if (
                            current["status"] == "waiting_approval"
                            and current["plan"] is not None
                        ):
                            break
                    self.assertIsNotNone(current["plan"])
                    agent.decide(created["run_id"], approved=True)
                    for _ in range(100):
                        await asyncio.sleep(0.02)
                        current = agent.get_run(created["run_id"])
                        if current["status"] == "completed":
                            break
                    self.assertEqual(current["status"], "completed")
                    self.assertIn("成功传输 1 个文件", current["report"])
                    for _ in range(100):
                        task = agent._tasks.get(created["run_id"])
                        if task is None or task.done():
                            break
                        await asyncio.sleep(0.02)

        asyncio.run(scenario())

    def test_read_only_agent_task_finishes_without_approval(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as tmp:
                runtime = SimpleNamespace(
                    config=SimpleNamespace(shared_folder=Path(tmp)),
                    events=SimpleNamespace(publish=lambda *_: None),
                    run_agent=lambda name, **_: {
                        "summary": f"{name} complete",
                        "causes": [],
                        "recommendations": ["review"],
                    },
                )
                agent = ReActSyncAgent(runtime)
                with patch("agents.orchestrator.create_chat_model", return_value=None):
                    created = agent.create_run("运行安全审计，不要执行文件传输")
                    for _ in range(100):
                        await asyncio.sleep(0.02)
                        current = agent.get_run(created["run_id"])
                        if current["status"] == "completed":
                            break
                self.assertEqual(current["status"], "completed")
                self.assertIsNone(current["plan"])
                self.assertIn("security complete", current["report"])
                for _ in range(100):
                    task = agent._tasks.get(created["run_id"])
                    if task is None or task.done():
                        break
                    await asyncio.sleep(0.02)

        asyncio.run(scenario())

    def test_device_list_request_finishes_without_sync_plan(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as tmp:
                runtime = SimpleNamespace(
                    config=SimpleNamespace(shared_folder=Path(tmp)),
                    events=SimpleNamespace(publish=lambda *_: None),
                    devices_payload=lambda: [
                        {
                            "device_id": "device-b",
                            "device_name": "PC-B",
                            "online": True,
                            "ip": "192.168.1.8",
                            "tcp_port": 9001,
                            "paired": False,
                            "permission": "unpaired",
                            "tls_enabled": True,
                        },
                        {
                            "device_id": "device-c",
                            "device_name": "PC-C",
                            "online": False,
                            "ip": "",
                            "tcp_port": None,
                            "paired": True,
                            "permission": "write",
                            "tls_enabled": True,
                        },
                    ],
                )
                agent = ReActSyncAgent(runtime)
                with patch("agents.orchestrator.create_chat_model", return_value=None):
                    created = agent.create_run("请列出在线设备")
                    for _ in range(100):
                        await asyncio.sleep(0.02)
                        current = agent.get_run(created["run_id"])
                        if current["status"] == "completed":
                            break

                self.assertEqual(current["status"], "completed")
                self.assertIsNone(current["plan"])
                self.assertIn("发现 1 台在线设备", current["report"])
                self.assertIn("PC-B", current["report"])
                self.assertNotIn("PC-C", current["report"])
                self.assertTrue(
                    any(
                        step["name"] == "discover_devices"
                        and step["status"] == "success"
                        for step in current["steps"]
                    )
                )
                for _ in range(100):
                    task = agent._tasks.get(created["run_id"])
                    if task is None or task.done():
                        break
                    await asyncio.sleep(0.02)

        asyncio.run(scenario())

    def test_identity_question_falls_back_when_model_authentication_fails(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as tmp:
                runtime = SimpleNamespace(
                    config=SimpleNamespace(shared_folder=Path(tmp)),
                    events=SimpleNamespace(publish=lambda *_: None),
                )
                agent = ReActSyncAgent(runtime)
                agent.store.create_run("run-identity", "thread-identity", "你是谁")
                with patch(
                    "agents.orchestrator.create_chat_model",
                    return_value=FailingChatModel(),
                ):
                    state = await agent._planner_node(
                        {
                            "run_id": "run-identity",
                            "request": "你是谁",
                            "revision_count": 0,
                            "model_calls": 0,
                        }
                    )
                agent._analysis_report_node(
                    {
                        "run_id": "run-identity",
                        "report": state["report"],
                    }
                )
                current = agent.get_run("run-identity")

                self.assertEqual(current["status"], "completed")
                self.assertIsNone(current["plan"])
                self.assertIn("我是 LANSync Agent", current["report"])
                self.assertEqual(current["messages"][-1]["role"], "assistant")
                self.assertTrue(
                    any(
                        step["name"] == "model_fallback"
                        and step["status"] == "warning"
                        for step in current["steps"]
                    )
                )

        asyncio.run(scenario())

    def test_identity_question_returns_successful_model_answer_without_plan(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as tmp:
                runtime = SimpleNamespace(
                    config=SimpleNamespace(shared_folder=Path(tmp)),
                    events=SimpleNamespace(publish=lambda *_: None),
                )
                agent = ReActSyncAgent(runtime)
                agent.store.create_run("run-model", "thread-model", "你是谁")
                with patch(
                    "agents.orchestrator.create_chat_model",
                    return_value=SuccessfulChatModel(),
                ):
                    state = await agent._planner_node(
                        {
                            "run_id": "run-model",
                            "request": "你是谁",
                            "revision_count": 0,
                            "model_calls": 0,
                        }
                    )

                self.assertEqual(state["plan_id"], "")
                self.assertTrue(state["analysis_only"])
                self.assertEqual(
                    state["report"],
                    "我是模型生成的 LANSync Agent 回答。",
                )

        asyncio.run(scenario())

    def test_fake_chat_model_calls_generate_plan_tool(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as tmp:
                runtime = SimpleNamespace(
                    config=SimpleNamespace(shared_folder=Path(tmp)),
                    events=SimpleNamespace(publish=lambda *_: None),
                )
                agent = ReActSyncAgent(runtime)
                agent.store.create_run("run-fake", "thread-fake", "同步 notes")

                def generate(run_id: str, device_id: str, prefix: str):
                    plan = {
                        "plan_id": "plan-fake",
                        "run_id": run_id,
                        "device_id": device_id,
                        "device_name": "PC-B",
                        "path_prefix": prefix,
                        "snapshot_fingerprint": "snapshot",
                        "counts": {
                            "upload": 0,
                            "download": 0,
                            "same": 0,
                            "conflict": 0,
                            "delete_report": 0,
                        },
                        "total_bytes": 0,
                        "risks": [],
                        "actions": [],
                        "status": "waiting_approval",
                        "approved_at_ns": None,
                        "created_at_ns": time.time_ns(),
                        "verification": None,
                    }
                    agent.store.save_plan(plan["plan_id"], run_id, plan)
                    return plan

                agent.coordinator.generate_sync_plan = generate
                agent.coordinator.device_candidates = lambda _: (
                    {"device_id": "device-b"},
                    [{"device_id": "device-b"}],
                )
                fake = ToolCallingFakeModel(
                    responses=[
                        AIMessage(
                            content="",
                            tool_calls=[
                                {
                                    "name": "generate_sync_plan",
                                    "args": {
                                        "device_id": "device-b",
                                        "path_prefix": "notes",
                                    },
                                    "id": "call-1",
                                    "type": "tool_call",
                                }
                            ],
                        ),
                        AIMessage(content="同步计划已生成。"),
                    ]
                )
                with patch("agents.orchestrator.create_chat_model", return_value=fake):
                    result = await agent._planner_node(
                        {
                            "run_id": "run-fake",
                            "request": "同步 PC-B 的 notes 文件夹",
                            "revision_count": 0,
                            "model_calls": 0,
                        }
                    )
                self.assertEqual(result["plan_id"], "plan-fake")
                run = agent.store.get_run("run-fake")
                self.assertTrue(
                    any(step["name"] == "generate_sync_plan" for step in run["steps"])
                )

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
