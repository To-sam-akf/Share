from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Any, Literal, TypedDict

from langchain.agents import create_agent
from langchain.agents.middleware import ModelCallLimitMiddleware
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import tool
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from .model_factory import create_chat_model
from .store import AgentRunStore
from .sync_tools import SyncCoordinator


class WorkflowState(TypedDict, total=False):
    run_id: str
    request: str
    plan_id: str
    planner_summary: str
    approved: bool
    revision_count: int
    verification: dict[str, Any]
    report: str
    model_calls: int
    analysis_only: bool


class ReActSyncAgent:
    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime
        self.store = AgentRunStore(runtime.config.shared_folder)
        self.coordinator = SyncCoordinator(runtime, self.store)
        self.checkpoint_path = (
            runtime.config.shared_folder / ".lan-sync" / "agent-checkpoints.sqlite3"
        )
        self._tasks: dict[str, asyncio.Task] = {}

    def create_run(self, message: str, thread_id: str = "") -> dict[str, Any]:
        request = str(message).strip()
        if not request:
            raise ValueError("message must not be empty")
        run_id = uuid.uuid4().hex
        actual_thread_id = str(thread_id).strip() or uuid.uuid4().hex
        self.store.create_run(run_id, actual_thread_id, request)
        self._schedule(run_id, None)
        payload = self.store.get_run(run_id)
        assert payload is not None
        return payload

    def decide(
        self,
        run_id: str,
        *,
        approved: bool | None = None,
        device_id: str = "",
    ) -> dict[str, Any]:
        run = self.store.get_run(run_id)
        if run is None:
            raise KeyError(run_id)
        if run["status"] != "waiting_approval":
            raise ValueError("agent run is not waiting for approval")
        if run.get("plan") is None:
            if not device_id:
                raise ValueError("device_id is required for device selection")
            resume_value = {"device_id": device_id}
        else:
            if approved is None:
                raise ValueError("approved is required for plan approval")
            resume_value = {"approved": bool(approved)}
        self.store.update_run(run_id, status="running")
        self._schedule(run_id, Command(resume=resume_value))
        payload = self.store.get_run(run_id)
        assert payload is not None
        return payload

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        return self.store.get_run(run_id)

    def list_threads(self, limit: int = 50) -> list[dict[str, Any]]:
        return self.store.list_threads(limit)

    def _schedule(self, run_id: str, command: Command | None) -> None:
        existing = self._tasks.get(run_id)
        if existing is not None and not existing.done():
            raise ValueError("agent run is already active")
        task = asyncio.create_task(self._run(run_id, command))
        self._tasks[run_id] = task
        task.add_done_callback(lambda _: self._tasks.pop(run_id, None))

    async def _run(self, run_id: str, command: Command | None) -> None:
        run = self.store.get_run(run_id)
        if run is None:
            return
        self.store.update_run(run_id, status="running", error="")
        self.runtime.events.publish(
            "agent_step",
            {"run_id": run_id, "name": "workflow", "status": "running"},
        )
        try:
            async with AsyncSqliteSaver.from_conn_string(
                str(self.checkpoint_path)
            ) as saver:
                graph = self._build_graph(run_id, saver)
                config = {
                    "configurable": {"thread_id": run_id},
                    "recursion_limit": 50,
                }
                graph_input: WorkflowState | Command
                if command is None:
                    graph_input = {
                        "run_id": run_id,
                        "request": str(run["request"]),
                        "revision_count": 0,
                        "model_calls": 0,
                    }
                else:
                    graph_input = command
                result = await graph.ainvoke(graph_input, config)
                # LangGraph may schedule the final SQLite checkpoint write just
                # after ainvoke returns. Let it drain before closing aiosqlite.
                await asyncio.sleep(0.05)
                await saver.conn.commit()
            if result.get("__interrupt__"):
                plan = self.store.load_plan(str(result.get("plan_id", "")))
                self.store.update_run(run_id, status="waiting_approval")
                self.runtime.events.publish(
                    "agent_interrupt",
                    {"run_id": run_id, "plan": plan},
                )
        except Exception as exc:
            report = "任务执行失败。请查看右侧执行步骤中的错误信息。"
            self.store.update_run(
                run_id,
                status="failed",
                report=report,
                error=f"{type(exc).__name__}: {exc}",
            )
            self.store.append_step(
                run_id,
                kind="workflow",
                name="workflow",
                status="failed",
                output_data={"error": str(exc)},
            )
            self.runtime.events.publish(
                "agent_completed",
                {
                    "run_id": run_id,
                    "status": "failed",
                    "report": report,
                    "error": str(exc),
                },
            )

    def _build_graph(self, run_id: str, saver: AsyncSqliteSaver):
        builder = StateGraph(WorkflowState)
        builder.add_node("planner", self._planner_node)
        builder.add_node("analysis_report", self._analysis_report_node)
        builder.add_node("build_plan", self._build_plan_node)
        builder.add_node("approval", self._approval_node)
        builder.add_node("execute", self._execute_node)
        builder.add_node("verify", self._verify_node)
        builder.add_node("report", self._report_node)
        builder.add_node("rejected", self._rejected_node)
        builder.add_edge(START, "planner")
        builder.add_conditional_edges(
            "planner",
            lambda state: (
                "analysis_report" if state.get("analysis_only") else "build_plan"
            ),
            {
                "analysis_report": "analysis_report",
                "build_plan": "build_plan",
            },
        )
        builder.add_edge("analysis_report", END)
        builder.add_edge("build_plan", "approval")
        builder.add_conditional_edges(
            "approval",
            lambda state: "execute" if state.get("approved") else "rejected",
            {"execute": "execute", "rejected": "rejected"},
        )
        builder.add_edge("execute", "verify")
        builder.add_conditional_edges(
            "verify",
            self._after_verify,
            {"retry": "planner", "report": "report"},
        )
        builder.add_edge("report", END)
        builder.add_edge("rejected", END)
        return builder.compile(checkpointer=saver, name=f"sync-agent-{run_id[:8]}")

    async def _planner_node(self, state: WorkflowState) -> WorkflowState:
        run_id = state["run_id"]
        request = state["request"]
        previous_plan_id = state.get("plan_id", "")
        analysis_only = self._is_analysis_request(request)
        device_query_only = (
            not analysis_only and self._is_device_query_request(request)
        )
        sync_action = self._is_sync_action_request(request)
        conversation_only = (
            not analysis_only and not device_query_only and not sync_action
        )
        read_only = analysis_only or device_query_only or conversation_only
        selected_device_id = ""
        if not read_only:
            selected, candidates = self.coordinator.device_candidates(request)
            if selected is None and len(candidates) > 1:
                choices = [
                    {
                        "device_id": device["device_id"],
                        "device_name": device["device_name"],
                        "ip": device["ip"],
                    }
                    for device in candidates
                ]
                self.store.append_step(
                    run_id,
                    kind="clarification",
                    name="select_device",
                    status="waiting",
                    output_data={"devices": choices},
                )
                selection = interrupt(
                    {
                        "type": "device_selection",
                        "run_id": run_id,
                        "devices": choices,
                    }
                )
                selected_device_id = str(selection.get("device_id", ""))
                if selected_device_id not in {
                    item["device_id"] for item in choices
                }:
                    raise ValueError("selected device is not available")
            elif selected is not None:
                selected_device_id = str(selected["device_id"])
        self.store.append_step(
            run_id,
            kind="reasoning",
            name="planner",
            status="running",
            input_data={"request": request, "revision": state.get("revision_count", 0)},
        )
        model = create_chat_model(self.runtime.config)
        summary = ""
        if model is not None:
            prompt = request
            run_record = self.store.get_run(run_id)
            if run_record is not None:
                history = self.store.thread_context(
                    str(run_record["thread_id"]),
                    exclude_run_id=run_id,
                )
                if history:
                    prompt = (
                        "此前同一会话的已完成记录：\n"
                        + json.dumps(history, ensure_ascii=False)
                        + "\n\n当前请求："
                        + request
                    )
            if selected_device_id:
                prompt += f"\n已选择目标设备 ID：{selected_device_id}"
            if state.get("verification"):
                prompt += (
                    "\n上一次执行或验证失败，请根据以下 Observation 重新检查并生成新计划："
                    + json.dumps(state["verification"], ensure_ascii=False)
                )
            try:
                if conversation_only:
                    message = await model.ainvoke(
                        [
                            SystemMessage(
                                content=(
                                    "你是 LANSync Agent，负责局域网设备发现、连接诊断、"
                                    "安全审计、冲突分析和文件同步规划。简洁回答身份、"
                                    "能力或使用方式问题；不要声称已经执行工具或文件操作。"
                                )
                            ),
                            HumanMessage(content=prompt),
                        ]
                    )
                    model_calls = state.get("model_calls", 0) + 1
                    summary = str(getattr(message, "content", "") or "")
                else:
                    react_agent = create_agent(
                        model,
                        self._tools(run_id),
                        system_prompt=(
                            "你是局域网文件同步 ReAct Planner。先调用工具获取事实，"
                            "禁止猜测设备、路径或文件状态。一次任务只能选择一个远端设备"
                            "和一个目录前缀。同步、上传或下载任务必须调用 "
                            "generate_sync_plan 结束规划。若用户明确只要求连接诊断、"
                            "冲突分析、安全审计或查看设备，则只调用对应只读工具并"
                            "直接回答。查看设备时必须调用 discover_devices。"
                            "execute_sync_plan 需要人工审批，不要尝试绕过审批。"
                        ),
                        middleware=[
                            ModelCallLimitMiddleware(
                                thread_limit=12,
                                exit_behavior="end",
                            )
                        ],
                        name="lan-sync-planner",
                    )
                    response = await react_agent.ainvoke(
                        {
                            "messages": [HumanMessage(content=prompt)],
                            "thread_model_call_count": state.get("model_calls", 0),
                        },
                        {"recursion_limit": 30},
                    )
                    model_calls = int(
                        response.get(
                            "thread_model_call_count",
                            state.get("model_calls", 0),
                        )
                    )
                    messages = response.get("messages", [])
                    if messages:
                        summary = str(getattr(messages[-1], "content", "") or "")
            except Exception as exc:
                model_calls = state.get("model_calls", 0)
                self.store.append_step(
                    run_id,
                    kind="observation",
                    name="model_fallback",
                    status="warning",
                    output_data={"error": f"{type(exc).__name__}: {exc}"},
                )

        run = self.store.get_run(run_id)
        plan_id = str(run.get("plan_id") or "") if run else ""
        analysis_tools = {
            "diagnose_connection",
            "analyze_conflicts",
            "run_security_audit",
        }
        used_analysis_tool = bool(
            run
            and any(
                step["kind"] == "observation"
                and step["name"] in analysis_tools
                and step["status"] == "success"
                for step in run["steps"]
            )
        )
        used_device_tool = bool(
            run
            and any(
                step["kind"] == "observation"
                and step["name"] == "discover_devices"
                and step["status"] == "success"
                for step in run["steps"]
            )
        )
        if conversation_only:
            if not summary:
                summary = self._local_conversation_report()
        elif device_query_only:
            if not summary or not used_device_tool:
                summary = self._local_device_report(run_id)
        elif analysis_only:
            if not summary or not used_analysis_tool:
                summary = self._local_analysis_report(request)
        elif not plan_id or (
            state.get("revision_count", 0) > 0 and plan_id == previous_plan_id
        ):
            device_id, prefix = self.coordinator.resolve_request(
                request,
                selected_device_id,
            )
            plan = self.coordinator.generate_sync_plan(run_id, device_id, prefix)
            plan_id = plan["plan_id"]
            summary = summary or "已使用确定性规划器生成同步计划。"

        self.store.append_step(
            run_id,
            kind="reasoning",
            name="planner",
            status="success",
            output_data={"plan_id": plan_id, "summary": summary},
        )
        return {
            "plan_id": plan_id,
            "planner_summary": summary,
            "model_calls": locals().get(
                "model_calls",
                state.get("model_calls", 0),
            ),
            "analysis_only": read_only,
            "report": summary if read_only else "",
        }

    def _analysis_report_node(self, state: WorkflowState) -> WorkflowState:
        report = state.get("report") or "只读分析已完成。"
        self.store.update_run(
            state["run_id"],
            status="completed",
            report=report,
            error="",
        )
        self.store.append_step(
            state["run_id"],
            kind="report",
            name="final_report",
            status="completed",
            output_data={"report": report},
        )
        self.runtime.events.publish(
            "agent_completed",
            {"run_id": state["run_id"], "status": "completed", "report": report},
        )
        return {"report": report}

    async def _build_plan_node(self, state: WorkflowState) -> WorkflowState:
        plan = self.store.load_plan(state["plan_id"])
        if plan is None:
            raise KeyError("planner did not create a sync plan")
        self.store.append_step(
            state["run_id"],
            kind="plan",
            name="build_plan",
            status="success",
            output_data={
                "plan_id": plan["plan_id"],
                "counts": plan["counts"],
                "total_bytes": plan["total_bytes"],
            },
        )
        return {}

    def _approval_node(self, state: WorkflowState) -> WorkflowState:
        plan = self.store.load_plan(state["plan_id"])
        if plan is None:
            raise KeyError("sync plan does not exist")
        decision = interrupt(
            {
                "type": "sync_plan_approval",
                "run_id": state["run_id"],
                "plan": plan,
            }
        )
        approved = bool(decision.get("approved"))
        plan["status"] = "approved" if approved else "rejected"
        plan["approved_at_ns"] = time.time_ns() if approved else None
        self.store.save_plan(plan["plan_id"], state["run_id"], plan)
        self.store.append_step(
            state["run_id"],
            kind="approval",
            name="human_decision",
            status="approved" if approved else "rejected",
            output_data={"approved": approved},
        )
        return {"approved": approved}

    async def _execute_node(self, state: WorkflowState) -> WorkflowState:
        run_id = state["run_id"]
        self.store.append_step(
            run_id,
            kind="action",
            name="execute_sync_plan",
            status="running",
            input_data={"plan_id": state["plan_id"]},
        )
        try:
            plan = await asyncio.to_thread(
                self.coordinator.execute_sync_plan,
                state["plan_id"],
            )
            status = "success" if plan["status"] == "executed" else "failed"
            output = {"status": plan["status"]}
        except Exception as exc:
            status = "failed"
            output = {
                "status": "execution_failed",
                "error_code": getattr(exc, "code", type(exc).__name__),
                "error": str(exc),
            }
        self.store.append_step(
            run_id,
            kind="action",
            name="execute_sync_plan",
            status=status,
            output_data=output,
        )
        return {"verification": output}

    async def _verify_node(self, state: WorkflowState) -> WorkflowState:
        run_id = state["run_id"]
        try:
            verification = await asyncio.to_thread(
                self.coordinator.verify_sync_plan,
                state["plan_id"],
            )
        except Exception as exc:
            verification = {
                "success": False,
                "error_code": getattr(exc, "code", type(exc).__name__),
                "error": str(exc),
            }
        revisions = state.get("revision_count", 0)
        if not verification.get("success"):
            revisions += 1
        self.store.append_step(
            run_id,
            kind="verification",
            name="verify_sync_plan",
            status="success" if verification.get("success") else "failed",
            output_data=verification,
        )
        return {"verification": verification, "revision_count": revisions}

    @staticmethod
    def _after_verify(state: WorkflowState) -> Literal["retry", "report"]:
        return (
            "retry"
            if not state.get("verification", {}).get("success")
            and state.get("revision_count", 0) <= 2
            else "report"
        )

    async def _report_node(self, state: WorkflowState) -> WorkflowState:
        plan = self.store.load_plan(state["plan_id"])
        if plan is None:
            raise KeyError("sync plan does not exist")
        successful = sum(
            action["status"] == "success" for action in plan["actions"]
        )
        failed = sum(action["status"] == "failed" for action in plan["actions"])
        report = (
            f"同步任务已完成验证。成功传输 {successful} 个文件，失败 {failed} 个；"
            f"发现冲突 {plan['counts']['conflict']} 个，删除差异 "
            f"{plan['counts']['delete_report']} 个（均未自动处理）。"
        )
        model = create_chat_model(self.runtime.config)
        if model is not None and state.get("model_calls", 0) < 12:
            try:
                response = await model.ainvoke(
                    [
                        SystemMessage(
                            content=(
                                "根据结构化同步结果生成简洁中文最终报告。"
                                "不得添加 JSON 中不存在的事实。"
                            )
                        ),
                        HumanMessage(
                            content=json.dumps(
                                {
                                    "counts": plan["counts"],
                                    "status": plan["status"],
                                    "verification": state.get("verification"),
                                    "failed_actions": [
                                        {
                                            "path": action["relative_path"],
                                            "error": action["error_message"],
                                        }
                                        for action in plan["actions"]
                                        if action["status"] == "failed"
                                    ],
                                },
                                ensure_ascii=False,
                            )
                        ),
                    ]
                )
                if response.content:
                    report = str(response.content)
            except Exception:
                pass
        status = (
            "completed"
            if state.get("verification", {}).get("success")
            else "failed"
        )
        self.store.update_run(
            state["run_id"],
            status=status,
            report=report,
            error="" if status == "completed" else "同步验证未通过。",
        )
        self.store.append_step(
            state["run_id"],
            kind="report",
            name="final_report",
            status=status,
            output_data={"report": report},
        )
        self.runtime.events.publish(
            "agent_completed",
            {"run_id": state["run_id"], "status": status, "report": report},
        )
        return {"report": report}

    def _rejected_node(self, state: WorkflowState) -> WorkflowState:
        report = "同步计划已由用户拒绝，未执行任何文件传输。"
        self.store.update_run(
            state["run_id"],
            status="rejected",
            report=report,
        )
        self.runtime.events.publish(
            "agent_completed",
            {"run_id": state["run_id"], "status": "rejected", "report": report},
        )
        return {"report": report}

    def _tools(self, run_id: str):
        coordinator = self.coordinator
        store = self.store

        def call(name: str, payload: dict[str, Any], function):
            store.append_step(
                run_id,
                kind="tool",
                name=name,
                status="running",
                input_data=payload,
            )
            self.runtime.events.publish(
                "agent_step",
                {
                    "run_id": run_id,
                    "kind": "tool",
                    "name": name,
                    "status": "running",
                    "input": payload,
                },
            )
            try:
                result = function()
                store.append_step(
                    run_id,
                    kind="observation",
                    name=name,
                    status="success",
                    output_data=result,
                )
                return json.dumps(result, ensure_ascii=False, default=str)
            except Exception as exc:
                error = {
                    "error_code": getattr(exc, "code", type(exc).__name__),
                    "error": str(exc),
                }
                store.append_step(
                    run_id,
                    kind="observation",
                    name=name,
                    status="failed",
                    output_data=error,
                )
                return json.dumps(error, ensure_ascii=False)

        @tool("discover_devices")
        def discover_devices() -> str:
            """列出在线设备、配对状态、权限和 TLS 身份。"""
            return call("discover_devices", {}, coordinator.discover_devices)

        @tool("diagnose_connection")
        def diagnose_connection(device_id: str) -> str:
            """诊断指定设备的 UDP、TCP、网段和 TLS 连接。"""
            return call(
                "diagnose_connection",
                {"device_id": device_id},
                lambda: self.runtime.run_agent(
                    "connection",
                    device_id=device_id,
                    enhance=False,
                ),
            )

        @tool("list_local_files")
        def list_local_files(path_prefix: str = "") -> str:
            """读取共享目录中指定相对目录前缀的本地文件索引。"""
            return call(
                "list_local_files",
                {"path_prefix": path_prefix},
                lambda: [
                    entry.to_payload()
                    for entry in coordinator.list_local_files(path_prefix)
                ],
            )

        @tool("list_remote_files")
        def list_remote_files(device_id: str, path_prefix: str = "") -> str:
            """读取已配对在线设备的远端文件索引。"""
            return call(
                "list_remote_files",
                {"device_id": device_id, "path_prefix": path_prefix},
                lambda: [
                    entry.to_payload()
                    for entry in coordinator.list_remote_files(
                        device_id,
                        path_prefix,
                    )
                ],
            )

        @tool("compare_file_indexes")
        def compare_file_indexes(device_id: str, path_prefix: str = "") -> str:
            """比较本地和远端索引，分类上传、下载、冲突和删除差异。"""
            return call(
                "compare_file_indexes",
                {"device_id": device_id, "path_prefix": path_prefix},
                lambda: coordinator.compare_file_indexes(device_id, path_prefix),
            )

        @tool("analyze_conflicts")
        def analyze_conflicts(device_id: str, path_prefix: str = "") -> str:
            """分析指定设备和目录下已记录的文件冲突。"""
            return call(
                "analyze_conflicts",
                {"device_id": device_id, "path_prefix": path_prefix},
                lambda: {
                    "analysis": self.runtime.run_agent(
                        "conflict",
                        enhance=False,
                    ),
                    "conflicts": [
                        item
                        for item in self.runtime.conflicts_payload(False)
                        if (
                            item["remote_device_id"] == device_id
                            and (
                                not path_prefix
                                or item["relative_path"] == path_prefix
                                or item["relative_path"].startswith(
                                    f"{path_prefix.strip('/')}/"
                                )
                            )
                        )
                    ],
                },
            )

        @tool("run_security_audit")
        def run_security_audit() -> str:
            """运行本地安全审计并返回风险与建议。"""
            return call(
                "run_security_audit",
                {},
                lambda: self.runtime.run_agent("security", enhance=False),
            )

        @tool("generate_sync_plan")
        def generate_sync_plan(device_id: str, path_prefix: str = "") -> str:
            """根据最新索引生成等待人工审批的不可变同步计划。"""
            return call(
                "generate_sync_plan",
                {"device_id": device_id, "path_prefix": path_prefix},
                lambda: coordinator.generate_sync_plan(
                    run_id,
                    device_id,
                    path_prefix,
                ),
            )

        @tool("execute_sync_plan")
        def execute_sync_plan(plan_id: str) -> str:
            """执行已由用户批准的同步计划；未审批时只返回拒绝信息。"""
            return call(
                "execute_sync_plan",
                {"plan_id": plan_id},
                lambda: coordinator.execute_sync_plan(plan_id),
            )

        @tool("verify_sync_plan")
        def verify_sync_plan(plan_id: str) -> str:
            """重新读取双方索引并校验已执行计划的文件 SHA-256。"""
            return call(
                "verify_sync_plan",
                {"plan_id": plan_id},
                lambda: coordinator.verify_sync_plan(plan_id),
            )

        @tool("get_transfer_status")
        def get_transfer_status(plan_id: str) -> str:
            """查询计划中每个文件的传输状态和失败原因。"""
            return call(
                "get_transfer_status",
                {"plan_id": plan_id},
                lambda: coordinator.transfer_status(plan_id),
            )

        return [
            discover_devices,
            diagnose_connection,
            list_local_files,
            list_remote_files,
            compare_file_indexes,
            analyze_conflicts,
            run_security_audit,
            generate_sync_plan,
            execute_sync_plan,
            verify_sync_plan,
            get_transfer_status,
        ]

    @staticmethod
    def _is_analysis_request(request: str) -> bool:
        normalized = request.lower()
        analysis_words = ("诊断", "审计", "分析冲突", "冲突分析")
        action_words = ("同步", "上传", "下载", "传输", "发送文件")
        explicitly_read_only = "不要执行" in normalized or "只分析" in normalized
        return any(word in normalized for word in analysis_words) and (
            explicitly_read_only
            or not any(word in normalized for word in action_words)
        )

    @staticmethod
    def _is_device_query_request(request: str) -> bool:
        normalized = request.lower()
        device_words = ("设备", "终端", "主机", "节点", "device")
        query_words = (
            "列出",
            "查看",
            "查询",
            "显示",
            "有哪些",
            "设备列表",
            "谁在线",
            "多少台",
            "在线",
        )
        action_words = (
            "同步",
            "上传",
            "下载",
            "传输",
            "发送文件",
            "配对",
            "诊断",
            "审计",
            "冲突",
        )
        return (
            any(word in normalized for word in device_words)
            and any(word in normalized for word in query_words)
            and not any(word in normalized for word in action_words)
        )

    @staticmethod
    def _is_sync_action_request(request: str) -> bool:
        normalized = request.lower()
        action_words = (
            "同步",
            "上传",
            "下载",
            "传输",
            "发送文件",
            "sync",
            "upload",
            "download",
            "transfer",
        )
        return any(word in normalized for word in action_words)

    @staticmethod
    def _local_conversation_report() -> str:
        return (
            "我是 LANSync Agent，负责局域网设备发现、连接诊断、安全审计、"
            "冲突分析和文件同步规划。文件传输前我会先生成计划，并等待你批准。"
        )

    def _local_device_report(self, run_id: str) -> str:
        devices = self.coordinator.discover_devices()
        self.store.append_step(
            run_id,
            kind="observation",
            name="discover_devices",
            status="success",
            output_data=devices,
        )
        online = [device for device in devices if device.get("online")]
        if not online:
            return (
                "暂未发现其他在线设备。请确认对方程序已启动，"
                "且防火墙允许 UDP 广播。"
            )

        lines = [f"发现 {len(online)} 台在线设备："]
        for device in online:
            address = str(device.get("ip") or "地址未知")
            if device.get("tcp_port") is not None:
                address += f":{device['tcp_port']}"
            pairing = "已配对" if device.get("paired") else "未配对"
            permission = str(device.get("permission") or "未知")
            tls = "已启用" if device.get("tls_enabled") else "未启用"
            lines.append(
                f"- {device.get('device_name') or device.get('device_id')} "
                f"（{address}，{pairing}，权限：{permission}，TLS：{tls}）"
            )
        return "\n".join(lines)

    def _local_analysis_report(self, request: str) -> str:
        if "安全" in request or "审计" in request:
            result = self.runtime.run_agent("security", enhance=False)
        elif "冲突" in request:
            result = self.runtime.run_agent("conflict", enhance=False)
        else:
            result = self.runtime.run_agent("connection", enhance=False)
        parts = [str(result["summary"])]
        if result.get("causes"):
            parts.append("原因：" + "；".join(result["causes"]))
        if result.get("recommendations"):
            parts.append("建议：" + "；".join(result["recommendations"]))
        return "\n".join(parts)
