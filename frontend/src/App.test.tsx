import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { AgentsPage, DevicesPage, SyncPage } from "./App";
import { api, saveSharedFile, uploadFile } from "./api";
import type {
  AgentRun,
  AgentThreadDetail,
  AgentThreadPage,
  AgentThreadSummary,
  Device,
  FileEntry,
  RuntimeStatus
} from "./types";

vi.mock("./api", () => ({
  api: vi.fn(),
  eventSocket: vi.fn(),
  fileContentUrl: (relativePath: string, disposition: string) => {
    const query = new URLSearchParams({ path: relativePath, disposition });
    return `/api/files/content?${query.toString()}`;
  },
  saveSharedFile: vi.fn(),
  uploadFile: vi.fn()
}));

const apiMock = vi.mocked(api);
const saveFileMock = vi.mocked(saveSharedFile);
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

const now = Date.now() * 1_000_000;

const activeFile: FileEntry = {
  relative_path: "收到的文件/报告 2026.pdf",
  file_name: "报告 2026.pdf",
  file_size: 1024,
  modified_time_ns: now,
  file_hash: "a".repeat(64),
  version: 1,
  source_device_id: device.device_id,
  source_device_name: device.device_name,
  status: "active",
  changed_at_ns: now,
  sync_status: "synced",
  preview_kind: "pdf"
};

const deletedFile: FileEntry = {
  ...activeFile,
  relative_path: "deleted.txt",
  file_name: "deleted.txt",
  status: "deleted",
  sync_status: "deleted_record",
  preview_kind: null
};

const agentRun: AgentRun = {
  run_id: "run-123",
  thread_id: "thread-123",
  request: "同步 PC-B 的 notes 文件夹",
  status: "waiting_approval",
  plan_id: "plan-123",
  report: "",
  error: "",
  created_at_ns: now,
  updated_at_ns: now,
  messages: [
    {
      run_id: "run-123",
      role: "user",
      content: "同步 PC-B 的 notes 文件夹",
      created_at_ns: now
    }
  ],
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

const agentThread: AgentThreadSummary = {
  thread_id: "thread-123",
  title: "同步 notes",
  latest_run_id: "run-123",
  status: "waiting_approval",
  run_count: 1,
  created_at_ns: now,
  updated_at_ns: now
};

const threadPage: AgentThreadPage = {
  items: [agentThread],
  next_cursor: null
};

function threadDetail(
  run: AgentRun = agentRun,
  messages = run.messages,
  nextCursor: string | null = null
): AgentThreadDetail {
  return {
    thread: { ...agentThread, status: run.status },
    messages,
    latest_run: run,
    next_cursor: nextCursor
  };
}

describe("LANSync console pages", () => {
  beforeEach(() => {
    apiMock.mockReset();
    saveFileMock.mockReset();
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

  it("opens and saves active shared files but hides actions for deleted records", async () => {
    apiMock.mockImplementation(async (path: string) => {
      if (path.startsWith("/api/files")) {
        return { items: [activeFile, deletedFile], total: 2 };
      }
      if (path === "/api/devices") return { items: [] };
      if (path === "/api/transfers") return { items: [] };
      if (path === "/api/runtime") return runtime;
      throw new Error(`unexpected path: ${path}`);
    });
    saveFileMock.mockResolvedValue("saved");

    render(<SyncPage liveVersion={0} notify={notify} />);

    const open = await screen.findByRole("link", { name: "打开" });
    expect(open).toHaveAttribute(
      "href",
      "/api/files/content?path=%E6%94%B6%E5%88%B0%E7%9A%84%E6%96%87%E4%BB%B6%2F%E6%8A%A5%E5%91%8A+2026.pdf&disposition=inline"
    );
    expect(screen.getAllByRole("button", { name: "另存为" })).toHaveLength(1);

    fireEvent.click(screen.getByRole("button", { name: "另存为" }));
    await waitFor(() =>
      expect(saveFileMock).toHaveBeenCalledWith(
        activeFile.relative_path,
        activeFile.file_name
      )
    );
    expect(notify).toHaveBeenCalledWith("文件已保存到所选目录");
  });

  it("creates and approves a ReAct sync plan", async () => {
    apiMock.mockImplementation(async (path: string, init?: RequestInit) => {
      if (path.startsWith("/api/agent/threads/thread-123")) {
        return threadDetail();
      }
      if (path.startsWith("/api/agent/threads?")) return threadPage;
      if (path === "/api/agent/runs" && init?.method === "POST") return agentRun;
      if (path === "/api/agent/runs/run-123/decision") return agentRun;
      throw new Error(`unexpected path: ${path}`);
    });
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
          run_id: "run-123",
          created_at_ns: now + 1,
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
    apiMock.mockImplementation(async (path: string, init?: RequestInit) => {
      if (path.startsWith("/api/agent/threads/thread-123")) {
        return threadDetail(markdownRun);
      }
      if (path.startsWith("/api/agent/threads?")) return threadPage;
      if (path === "/api/agent/runs" && init?.method === "POST") {
        return markdownRun;
      }
      throw new Error(`unexpected path: ${path}`);
    });

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

  it("starts blank and loads a selected session with its full conversation", async () => {
    const completedRun: AgentRun = {
      ...agentRun,
      status: "completed",
      plan_id: null,
      plan: null,
      report: "第二轮完成",
      messages: [
        {
          run_id: "run-123",
          role: "user",
          content: "第二轮问题",
          created_at_ns: now
        },
        {
          run_id: "run-123",
          role: "assistant",
          content: "第二轮完成",
          created_at_ns: now + 1
        }
      ]
    };
    const history = [
      {
        run_id: "run-old",
        role: "user" as const,
        content: "第一轮问题",
        created_at_ns: now - 2
      },
      {
        run_id: "run-old",
        role: "assistant" as const,
        content: "第一轮回答",
        created_at_ns: now - 1
      },
      ...completedRun.messages
    ];
    apiMock.mockImplementation(async (path: string) => {
      if (path.startsWith("/api/agent/threads/thread-123")) {
        return threadDetail(completedRun, history);
      }
      if (path.startsWith("/api/agent/threads?")) {
        return {
          ...threadPage,
          items: [{ ...agentThread, status: "completed", run_count: 2 }]
        };
      }
      throw new Error(`unexpected path: ${path}`);
    });

    render(<AgentsPage notify={notify} />);
    expect(screen.getByText("描述一个同步任务")).toBeInTheDocument();
    expect(screen.queryByText("第一轮回答")).not.toBeInTheDocument();
    fireEvent.click(await screen.findByRole("button", { name: /^同步 notes/ }));
    expect(await screen.findByText("第一轮问题")).toBeInTheDocument();
    expect(screen.getByText("第一轮回答")).toBeInTheDocument();
    expect(screen.getByText("第二轮完成")).toBeInTheDocument();
  });

  it("continues the selected session and creates a new session with an empty id", async () => {
    const completedRun = {
      ...agentRun,
      status: "completed" as const,
      plan_id: null,
      plan: null
    };
    const postBodies: Array<Record<string, string>> = [];
    apiMock.mockImplementation(async (path: string, init?: RequestInit) => {
      if (path.startsWith("/api/agent/threads/thread-123")) {
        return threadDetail(completedRun);
      }
      if (path.startsWith("/api/agent/threads?")) return threadPage;
      if (path === "/api/agent/runs" && init?.method === "POST") {
        postBodies.push(JSON.parse(String(init.body)));
        return completedRun;
      }
      throw new Error(`unexpected path: ${path}`);
    });

    render(<AgentsPage notify={notify} />);
    fireEvent.click(await screen.findByRole("button", { name: /^同步 notes/ }));
    await screen.findByText("同步 PC-B 的 notes 文件夹");
    fireEvent.change(screen.getByRole("textbox", { name: "Agent 任务" }), {
      target: { value: "继续这个会话" }
    });
    fireEvent.click(screen.getByRole("button", { name: "发送任务" }));
    await waitFor(() => expect(postBodies[0].thread_id).toBe("thread-123"));

    fireEvent.click(screen.getByRole("button", { name: "新会话" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Agent 任务" }), {
      target: { value: "新的会话" }
    });
    fireEvent.click(screen.getByRole("button", { name: "发送任务" }));
    await waitFor(() => expect(postBodies[1].thread_id).toBe(""));
  });

  it("loads earlier messages and manages session names and deletion", async () => {
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    let detailCalls = 0;
    const completedRun: AgentRun = {
      ...agentRun,
      status: "completed",
      plan_id: null,
      plan: null
    };
    const completedPage: AgentThreadPage = {
      ...threadPage,
      items: [{ ...agentThread, status: "completed" }]
    };
    apiMock.mockImplementation(async (path: string, init?: RequestInit) => {
      if (path.includes("cursor=older")) {
        return threadDetail(completedRun, [
          {
            run_id: "run-old",
            role: "user",
            content: "更早的问题",
            created_at_ns: now - 1
          }
        ]);
      }
      if (path.startsWith("/api/agent/threads/thread-123") && !init?.method) {
        detailCalls += 1;
        return threadDetail(completedRun, completedRun.messages, "older");
      }
      if (path.startsWith("/api/agent/threads?")) return completedPage;
      if (path === "/api/agent/threads/thread-123" && init?.method === "PATCH") {
        return { ...agentThread, title: "新名称" };
      }
      if (path === "/api/agent/threads/thread-123" && init?.method === "DELETE") {
        return { status: "success" };
      }
      throw new Error(`unexpected path: ${path}`);
    });

    render(<AgentsPage notify={notify} />);
    fireEvent.click(await screen.findByRole("button", { name: /^同步 notes/ }));
    fireEvent.click(await screen.findByRole("button", { name: "加载更早的消息" }));
    expect(await screen.findByText("更早的问题")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "重命名 同步 notes" }));
    fireEvent.change(screen.getByRole("textbox", { name: "会话名称" }), {
      target: { value: "新名称" }
    });
    fireEvent.click(screen.getByRole("button", { name: "保存会话名称" }));
    await waitFor(() => expect(notify).toHaveBeenCalledWith("会话名称已更新"));

    fireEvent.click(screen.getByRole("button", { name: "删除 同步 notes" }));
    await waitFor(() => expect(confirm).toHaveBeenCalledOnce());
    expect(detailCalls).toBeGreaterThan(0);
    confirm.mockRestore();
  });
});
