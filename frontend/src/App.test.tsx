import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { AgentsPage, DevicesPage, SyncPage } from "./App";
import { api, uploadFile } from "./api";
import type { AgentRun, Device, RuntimeStatus } from "./types";

vi.mock("./api", () => ({
  api: vi.fn(),
  eventSocket: vi.fn(),
  uploadFile: vi.fn()
}));

const apiMock = vi.mocked(api);
const uploadMock = vi.mocked(uploadFile);
const notify = vi.fn();

const device: Device = {
  device_id: "peer-123456789",
  device_name: "PC-B",
  online: true,
  status: "online",
  ip: "192.168.1.8",
  tcp_port: 9001,
  last_seen: Date.now() / 1000,
  tls_enabled: true,
  paired: true,
  permission: "write",
  paired_at_ns: Date.now() * 1_000_000,
  last_authenticated_at_ns: null,
  certificate_fingerprint: "AA:BB",
  last_sync_at_ns: null,
  stranger: false
};

const runtime: RuntimeStatus = {
  running: true,
  started_at_ns: Date.now() * 1_000_000,
  device_id: "local",
  device_name: "PC-A",
  local_ip: "127.0.0.1",
  udp_port: 9000,
  tcp_port: 9001,
  web_port: 8765,
  shared_folder: "/tmp/shared",
  tls_enabled: true,
  certificate_fingerprint: "CC:DD",
  sync_enabled: true,
  sync_interval_seconds: 10,
  pending_restart: false
};

const agentRun: AgentRun = {
  run_id: "run-123",
  thread_id: "thread-123",
  request: "同步 PC-B 的 notes 文件夹",
  status: "waiting_approval",
  plan_id: "plan-123",
  report: "",
  error: "",
  created_at_ns: Date.now() * 1_000_000,
  updated_at_ns: Date.now() * 1_000_000,
  messages: [{ role: "user", content: "同步 PC-B 的 notes 文件夹" }],
  steps: [
    {
      created_at_ns: Date.now() * 1_000_000,
      kind: "tool",
      name: "compare_file_indexes",
      status: "success",
      input: { device_id: "peer-123456789", path_prefix: "notes" },
      output: { upload: 1 }
    }
  ],
  plan: {
    plan_id: "plan-123",
    device_id: "peer-123456789",
    device_name: "PC-B",
    path_prefix: "notes",
    counts: { upload: 1, download: 0, conflict: 0, delete_report: 0 },
    total_bytes: 12,
    risks: [],
    status: "waiting_approval",
    verification: null,
    actions: [
      {
        action_id: "action-1",
        direction: "upload",
        relative_path: "notes/a.txt",
        bytes: 12,
        reason: "LOCAL_ONLY",
        executable: true,
        status: "pending",
        transferred_bytes: 0,
        error_code: "",
        error_message: ""
      }
    ]
  }
};

describe("LANSync console pages", () => {
  beforeEach(() => {
    apiMock.mockReset();
    uploadMock.mockReset();
    notify.mockReset();
  });

  it("renders the device empty state", async () => {
    apiMock.mockResolvedValueOnce({ items: [] });
    render(<DevicesPage liveVersion={0} notify={notify} />);
    expect(await screen.findByText("尚未发现设备")).toBeInTheDocument();
  });

  it("opens the device management drawer", async () => {
    apiMock.mockResolvedValueOnce({ items: [device] });
    render(<DevicesPage liveVersion={0} notify={notify} />);
    fireEvent.click(await screen.findByRole("button", { name: "管理" }));
    expect(screen.getByText("设备控制")).toBeInTheDocument();
    expect(screen.getByText("ACCESS CONTROL")).toBeInTheDocument();
  });

  it("keeps authorization when the revoke confirmation is cancelled", async () => {
    apiMock.mockResolvedValueOnce({ items: [device] });
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    render(<DevicesPage liveVersion={0} notify={notify} />);
    fireEvent.click(await screen.findByRole("button", { name: "管理" }));
    fireEvent.click(screen.getByRole("button", { name: "解除授权" }));
    expect(confirm).toHaveBeenCalledOnce();
    expect(apiMock).toHaveBeenCalledTimes(1);
    confirm.mockRestore();
  });

  it("shows browser upload progress and queues the file", async () => {
    apiMock.mockImplementation(async (path: string) => {
      if (path.startsWith("/api/files")) return { items: [], total: 0 };
      if (path === "/api/devices") return { items: [device] };
      if (path === "/api/transfers") return { items: [] };
      if (path === "/api/runtime") return runtime;
      throw new Error(`unexpected path: ${path}`);
    });
    uploadMock.mockImplementation(async (_deviceId, _file, onProgress) => {
      onProgress?.(42);
      await Promise.resolve();
      return { status: "queued" };
    });
    const { container } = render(<SyncPage liveVersion={0} notify={notify} />);
    const input = container.querySelector('input[type="file"]') as HTMLInputElement;
    fireEvent.change(input, {
      target: { files: [new File(["hello"], "report.txt", { type: "text/plain" })] }
    });
    fireEvent.change(await screen.findByRole("combobox", { name: "目标设备" }), {
      target: { value: device.device_id }
    });
    fireEvent.click(screen.getByRole("button", { name: "上传并发送" }));
    expect(await screen.findByRole("progressbar")).toHaveAttribute(
      "aria-valuenow",
      "42"
    );
    await waitFor(() =>
      expect(notify).toHaveBeenCalledWith("文件已上传到本机发送队列")
    );
  });

  it("creates and approves a ReAct sync plan", async () => {
    apiMock.mockResolvedValue(agentRun);
    render(<AgentsPage notify={notify} />);
    fireEvent.change(screen.getByRole("textbox", { name: "Agent 任务" }), {
      target: { value: "同步 PC-B 的 notes 文件夹" }
    });
    fireEvent.click(screen.getByRole("button", { name: "发送任务" }));
    expect(await screen.findByText("等待执行审批")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "批准并执行" }));
    await waitFor(() =>
      expect(apiMock).toHaveBeenCalledWith(
        "/api/agent/runs/run-123/decision",
        expect.objectContaining({ method: "POST" })
      )
    );
  });

  it("renders Agent replies as safe GitHub-flavored Markdown", async () => {
    const markdownRun: AgentRun = {
      ...agentRun,
      status: "completed",
      plan_id: null,
      plan: null,
      messages: [
        agentRun.messages[0],
        {
          role: "assistant",
          content: [
            "## 同步结果",
            "",
            "- **状态**：完成",
            "- 路径：`notes/report.md`",
            "",
            "| 文件 | 结果 |",
            "| --- | --- |",
            "| report.md | 成功 |",
            "",
            "[查看文档](https://example.com/docs)",
            "",
            "<script>alert('unsafe')</script>"
          ].join("\n")
        }
      ]
    };
    apiMock.mockResolvedValue(markdownRun);

    const { container } = render(<AgentsPage notify={notify} />);
    fireEvent.change(screen.getByRole("textbox", { name: "Agent 任务" }), {
      target: { value: "查看同步结果" }
    });
    fireEvent.click(screen.getByRole("button", { name: "发送任务" }));

    expect(await screen.findByRole("heading", { name: "同步结果" })).toBeInTheDocument();
    expect(screen.getByText("notes/report.md").tagName).toBe("CODE");
    expect(screen.getByRole("table")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "查看文档" })).toHaveAttribute(
      "target",
      "_blank"
    );
    expect(container.querySelector("script")).toBeNull();
  });
});
