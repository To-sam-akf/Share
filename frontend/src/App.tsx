import {
  Activity,
  AlertTriangle,
  Bot,
  Boxes,
  Check,
  ChevronRight,
  CircleDot,
  Clock3,
  CloudUpload,
  Download,
  ExternalLink,
  FileDiff,
  FileText,
  Fingerprint,
  FolderSync,
  Gauge,
  KeyRound,
  Laptop,
  ListFilter,
  LoaderCircle,
  LockKeyhole,
  Menu,
  Network,
  PanelLeft,
  Pencil,
  Play,
  Plus,
  RefreshCw,
  Router,
  Search,
  Send,
  Server,
  Settings,
  ShieldAlert,
  ShieldCheck,
  SlidersHorizontal,
  TerminalSquare,
  Trash2,
  Unplug,
  UserRoundCheck,
  Wifi,
  WifiOff,
  X
} from "lucide-react";
import {
  FormEvent,
  ReactNode,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState
} from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  api,
  eventSocket,
  fileContentUrl,
  saveSharedFile,
  uploadFile
} from "./api";
import type {
  AgentResult,
  AgentMessage,
  AgentRun,
  AgentThreadDetail,
  AgentThreadPage,
  AgentThreadSummary,
  AuditAlert,
  AuditEvent,
  Conflict,
  DashboardData,
  Device,
  FileEntry,
  RuntimeStatus,
  SettingsData,
  Severity,
  TransferTask
} from "./types";

type PageId =
  | "dashboard"
  | "devices"
  | "sync"
  | "conflicts"
  | "security"
  | "agents"
  | "logs";

type BadgeTone = "primary" | "success" | "warning" | "danger" | "muted";

const NAV_ITEMS: Array<{
  id: PageId;
  label: string;
  icon: typeof Gauge;
  index: string;
}> = [
  { id: "dashboard", label: "总览", icon: Gauge, index: "01" },
  { id: "devices", label: "设备", icon: Network, index: "02" },
  { id: "sync", label: "同步中心", icon: FolderSync, index: "03" },
  { id: "conflicts", label: "冲突中心", icon: FileDiff, index: "04" },
  { id: "security", label: "安全中心", icon: ShieldCheck, index: "05" },
  { id: "agents", label: "智能 Agent", icon: Bot, index: "06" },
  { id: "logs", label: "日志与设置", icon: TerminalSquare, index: "07" }
];

const EMPTY_RUNTIME: RuntimeStatus = {
  running: false,
  started_at_ns: null,
  device_id: "",
  device_name: "LANSync",
  local_ip: "-",
  udp_port: 0,
  tcp_port: 0,
  web_port: 8765,
  shared_folder: "",
  tls_enabled: false,
  certificate_fingerprint: null,
  sync_enabled: false,
  sync_interval_seconds: 0,
  pending_restart: false
};

function useRemote<T>(
  loader: () => Promise<T>,
  dependencies: unknown[]
): {
  data: T | null;
  loading: boolean;
  error: string;
  reload: () => void;
} {
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [version, setVersion] = useState(0);
  const reload = useCallback(() => setVersion((value) => value + 1), []);

  useEffect(() => {
    let active = true;
    setLoading(true);
    loader()
      .then((result) => {
        if (active) {
          setData(result);
          setError("");
        }
      })
      .catch((reason: unknown) => {
        if (active) {
          setError(reason instanceof Error ? reason.message : "请求失败");
        }
      })
      .finally(() => active && setLoading(false));
    return () => {
      active = false;
    };
    // Loader is intentionally supplied by each page.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...dependencies, version]);
  return { data, loading, error, reload };
}

export default function App() {
  const [page, setPage] = useState<PageId>("dashboard");
  const [runtime, setRuntime] = useState<RuntimeStatus>(EMPTY_RUNTIME);
  const [menuOpen, setMenuOpen] = useState(false);
  const [connected, setConnected] = useState(false);
  const [liveVersion, setLiveVersion] = useState(0);
  const [notice, setNotice] = useState("");

  const refreshRuntime = useCallback(() => {
    api<RuntimeStatus>("/api/runtime").then(setRuntime).catch(() => undefined);
  }, []);

  useEffect(() => {
    refreshRuntime();
    let socket: WebSocket | null = null;
    let retry: number | undefined;
    let disposed = false;
    const connect = () => {
      socket = eventSocket();
      socket.onopen = () => setConnected(true);
      socket.onmessage = (event) => {
        const payload = JSON.parse(event.data) as { type: string };
        if (payload.type !== "heartbeat") {
          setLiveVersion((value) => value + 1);
          refreshRuntime();
        }
      };
      socket.onclose = () => {
        setConnected(false);
        if (!disposed) retry = window.setTimeout(connect, 1800);
      };
    };
    connect();
    return () => {
      disposed = true;
      if (retry) window.clearTimeout(retry);
      socket?.close();
    };
  }, [refreshRuntime]);

  const showNotice = (message: string) => {
    setNotice(message);
    window.setTimeout(() => setNotice(""), 2800);
  };

  const content = {
    dashboard: (
      <DashboardPage
        liveVersion={liveVersion}
        onNavigate={setPage}
        notify={showNotice}
      />
    ),
    devices: (
      <DevicesPage liveVersion={liveVersion} notify={showNotice} />
    ),
    sync: <SyncPage liveVersion={liveVersion} notify={showNotice} />,
    conflicts: (
      <ConflictsPage liveVersion={liveVersion} notify={showNotice} />
    ),
    security: (
      <SecurityPage liveVersion={liveVersion} notify={showNotice} />
    ),
    agents: <AgentsPage liveVersion={liveVersion} notify={showNotice} />,
    logs: <LogsSettingsPage liveVersion={liveVersion} notify={showNotice} />
  }[page];

  return (
    <div className="app-shell">
      <aside className={`sidebar ${menuOpen ? "sidebar-open" : ""}`}>
        <div className="brand">
          <div className="brand-mark">
            <Boxes size={22} />
          </div>
          <div>
            <strong>LANSync</strong>
            <span>AGENT / NODE CONSOLE</span>
          </div>
          <button
            className="icon-button mobile-close"
            onClick={() => setMenuOpen(false)}
            aria-label="关闭导航"
          >
            <X size={18} />
          </button>
        </div>
        <div className="node-identity">
          <span className="eyebrow">LOCAL NODE</span>
          <strong>{runtime.device_name}</strong>
          <code>{runtime.local_ip}</code>
        </div>
        <nav>
          {NAV_ITEMS.map((item) => {
            const Icon = item.icon;
            return (
              <button
                key={item.id}
                className={page === item.id ? "nav-item active" : "nav-item"}
                onClick={() => {
                  setPage(item.id);
                  setMenuOpen(false);
                }}
              >
                <span className="nav-index">{item.index}</span>
                <Icon size={18} />
                <span>{item.label}</span>
                <ChevronRight className="nav-arrow" size={15} />
              </button>
            );
          })}
        </nav>
        <div className="sidebar-foot">
          <StatusLine
            tone={runtime.tls_enabled ? "success" : "danger"}
            icon={runtime.tls_enabled ? LockKeyhole : Unplug}
            label={runtime.tls_enabled ? "TLS 已加密" : "TLS 未启用"}
          />
          <StatusLine
            tone={runtime.sync_enabled ? "success" : "muted"}
            icon={FolderSync}
            label={runtime.sync_enabled ? "自动同步运行中" : "自动同步已关闭"}
          />
        </div>
      </aside>

      <div className="workspace">
        <header className="topbar">
          <button
            className="icon-button menu-button"
            onClick={() => setMenuOpen(true)}
            aria-label="打开导航"
          >
            <Menu size={20} />
          </button>
          <div>
            <span className="eyebrow">LAN CONTROL PLANE</span>
            <h1>{NAV_ITEMS.find((item) => item.id === page)?.label}</h1>
          </div>
          <div className="topbar-status">
            {runtime.pending_restart && (
              <Badge tone="warning" icon={RefreshCw}>
                配置待重启
              </Badge>
            )}
            <Badge tone={connected ? "success" : "danger"} icon={CircleDot}>
              {connected ? "实时通道正常" : "实时通道重连中"}
            </Badge>
            <div className="port-readout">
              <span>TCP</span>
              <code>{runtime.tcp_port}</code>
            </div>
          </div>
        </header>
        <main>{content}</main>
      </div>
      {notice && <div className="toast">{notice}</div>}
      {menuOpen && (
        <button
          className="mobile-scrim"
          aria-label="关闭导航"
          onClick={() => setMenuOpen(false)}
        />
      )}
    </div>
  );
}

function DashboardPage({
  liveVersion,
  onNavigate,
  notify
}: {
  liveVersion: number;
  onNavigate: (page: PageId) => void;
  notify: (message: string) => void;
}) {
  const { data, loading, error, reload } = useRemote(
    () => api<DashboardData>("/api/dashboard"),
    [liveVersion]
  );
  const runSync = async () => {
    await api("/api/sync/run", { method: "POST" });
    notify("同步任务已进入队列");
    reload();
  };
  if (loading && !data) return <PageLoader />;
  if (error && !data) return <ErrorState message={error} retry={reload} />;
  const dashboard = data!;
  const stats = [
    ["在线设备", dashboard.counts.online_devices, Wifi, "success"],
    ["已配对设备", dashboard.counts.paired_devices, UserRoundCheck, "primary"],
    ["同步文件", dashboard.counts.files, FileText, "primary"],
    ["未处理冲突", dashboard.counts.unresolved_conflicts, FileDiff, "warning"],
    ["风险告警", dashboard.counts.risk_alerts, ShieldAlert, "danger"]
  ] as const;
  return (
    <Page>
      <section className="hero-grid">
        <Panel className="hero-panel span-8">
          <div className="panel-heading">
            <div>
              <span className="eyebrow">SYSTEM OVERVIEW</span>
              <h2>{dashboard.runtime.device_name}</h2>
            </div>
            <Badge
              tone={dashboard.runtime.running ? "success" : "danger"}
              icon={Server}
            >
              {dashboard.runtime.running ? "节点服务正常" : "节点服务停止"}
            </Badge>
          </div>
          <div className="node-map">
            <div className="node-map-core">
              <Laptop size={34} />
              <strong>{dashboard.runtime.local_ip}</strong>
              <span>THIS DEVICE</span>
            </div>
            <div className="node-map-line" />
            <div className="node-map-peer">
              <Router size={28} />
              <strong>{dashboard.counts.online_devices}</strong>
              <span>LAN PEERS</span>
            </div>
          </div>
          <div className="hero-actions">
            <Button icon={Play} onClick={runSync}>
              立即同步
            </Button>
            <Button
              variant="secondary"
              icon={Send}
              onClick={() => onNavigate("sync")}
            >
              发送文件
            </Button>
            <Button
              variant="ghost"
              icon={Network}
              onClick={() => onNavigate("devices")}
            >
              添加设备
            </Button>
          </div>
        </Panel>
        <Panel className="status-stack span-4">
          <span className="eyebrow">CORE STATUS</span>
          <StatusBlock
            icon={LockKeyhole}
            label="传输加密"
            value={dashboard.runtime.tls_enabled ? "TLS 1.2+" : "未启用"}
            tone={dashboard.runtime.tls_enabled ? "success" : "danger"}
          />
          <StatusBlock
            icon={FolderSync}
            label="自动同步"
            value={
              dashboard.runtime.sync_enabled
                ? `${dashboard.runtime.sync_interval_seconds}s 周期`
                : "已关闭"
            }
            tone={dashboard.runtime.sync_enabled ? "success" : "muted"}
          />
          <StatusBlock
            icon={ShieldCheck}
            label="安全评估"
            value={severityLabel(dashboard.security.severity)}
            tone={toneForSeverity(dashboard.security.severity)}
          />
        </Panel>
      </section>

      <section className="stat-grid">
        {stats.map(([label, value, Icon, tone], index) => (
          <article className={`stat-card tone-${tone}`} key={label}>
            <div className="stat-index">0{index + 1}</div>
            <Icon size={20} />
            <strong>{value}</strong>
            <span>{label}</span>
          </article>
        ))}
      </section>

      <section className="content-grid">
        <Panel className="span-7">
          <PanelTitle
            eyebrow="EVENT STREAM"
            title="最近同步动态"
            action={<IconAction icon={RefreshCw} label="刷新" onClick={reload} />}
          />
          <EventList events={dashboard.recent_events} />
        </Panel>
        <Panel className="span-5">
          <PanelTitle eyebrow="ACTIVE QUEUE" title="传输队列" />
          <TransferList tasks={dashboard.transfers} />
        </Panel>
      </section>
    </Page>
  );
}

export function DevicesPage({
  liveVersion,
  notify
}: {
  liveVersion: number;
  notify: (message: string) => void;
}) {
  const { data, loading, error, reload } = useRemote(
    () => api<{ items: Device[] }>("/api/devices"),
    [liveVersion]
  );
  const [selected, setSelected] = useState<Device | null>(null);
  const [pairTarget, setPairTarget] = useState<Device | null>(null);
  const [pairCode, setPairCode] = useState("");
  const [agent, setAgent] = useState<AgentResult | null>(null);

  const pair = async (event: FormEvent) => {
    event.preventDefault();
    if (!pairTarget) return;
    await api(`/api/devices/${pairTarget.device_id}/pair`, {
      method: "POST",
      body: JSON.stringify({ pair_code: pairCode })
    });
    notify(`已与 ${pairTarget.device_name} 完成配对`);
    setPairTarget(null);
    setPairCode("");
    reload();
  };
  const setPermission = async (permission: string) => {
    if (!selected) return;
    await api(`/api/devices/${selected.device_id}/permission`, {
      method: "PATCH",
      body: JSON.stringify({ permission })
    });
    notify("设备权限已更新");
    setSelected(null);
    reload();
  };
  const diagnose = async () => {
    if (!selected) return;
    const result = await api<AgentResult>(
      `/api/devices/${selected.device_id}/diagnose`,
      { method: "POST" }
    );
    setAgent(result);
  };
  const revoke = async () => {
    if (!selected || !window.confirm(`解除 ${selected.device_name} 的授权？`))
      return;
    await api(`/api/devices/${selected.device_id}/authorization`, {
      method: "DELETE"
    });
    notify("设备授权已解除");
    setSelected(null);
    reload();
  };
  return (
    <Page>
      <PageIntro
        eyebrow="PEER REGISTRY"
        title="局域网设备"
        description="统一查看发现状态、授权关系、TLS 身份与最近同步时间。"
        actions={<Button icon={RefreshCw} onClick={reload}>重新扫描</Button>}
      />
      {error && <InlineError message={error} />}
      <Panel>
        <div className="table-toolbar">
          <div className="toolbar-summary">
            <Network size={18} />
            <strong>{data?.items.length ?? 0}</strong>
            <span>台已知设备</span>
          </div>
          {loading && <LoaderCircle className="spin" size={18} />}
        </div>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>设备</th>
                <th>连接状态</th>
                <th>地址</th>
                <th>权限</th>
                <th>TLS</th>
                <th>最后同步</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {(data?.items ?? []).map((device) => (
                <tr key={device.device_id}>
                  <td>
                    <div className="primary-cell">
                      <Laptop size={17} />
                      <div>
                        <strong>{device.device_name}</strong>
                        <code>{shortId(device.device_id)}</code>
                      </div>
                      {device.stranger && <Badge tone="warning">陌生设备</Badge>}
                    </div>
                  </td>
                  <td>
                    <Badge
                      tone={device.online ? "success" : "muted"}
                      icon={device.online ? Wifi : WifiOff}
                    >
                      {device.online ? "在线" : "离线"}
                    </Badge>
                  </td>
                  <td className="mono">
                    {device.ip || "-"}
                    {device.tcp_port ? `:${device.tcp_port}` : ""}
                  </td>
                  <td>
                    <PermissionBadge permission={device.permission} />
                  </td>
                  <td>
                    <Badge
                      tone={device.tls_enabled ? "success" : "danger"}
                      icon={device.tls_enabled ? LockKeyhole : Unplug}
                    >
                      {device.tls_enabled ? "已加密" : "明文"}
                    </Badge>
                  </td>
                  <td>{formatTime(device.last_sync_at_ns)}</td>
                  <td className="row-actions">
                    {!device.paired && device.online ? (
                      <Button
                        size="small"
                        icon={KeyRound}
                        onClick={() => setPairTarget(device)}
                      >
                        配对
                      </Button>
                    ) : (
                      <Button
                        size="small"
                        variant="ghost"
                        onClick={() => setSelected(device)}
                      >
                        管理
                      </Button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {!loading && !data?.items.length && (
            <EmptyState
              icon={WifiOff}
              title="尚未发现设备"
              description="确认其他节点已启动，并允许 UDP 广播通过防火墙。"
            />
          )}
        </div>
      </Panel>

      {selected && (
        <Drawer title="设备控制" onClose={() => setSelected(null)}>
          <div className="drawer-device">
            <Laptop size={30} />
            <div>
              <h3>{selected.device_name}</h3>
              <code>{selected.device_id}</code>
            </div>
          </div>
          <DefinitionList
            items={[
              ["网络地址", selected.ip ? `${selected.ip}:${selected.tcp_port}` : "离线"],
              ["当前权限", selected.permission],
              ["TLS 指纹", selected.certificate_fingerprint || "未绑定"],
              ["最后认证", formatTime(selected.last_authenticated_at_ns)],
              ["最后同步", formatTime(selected.last_sync_at_ns)]
            ]}
          />
          <div className="drawer-section">
            <span className="eyebrow">ACCESS CONTROL</span>
            <div className="permission-grid">
              {["read", "write", "admin", "blocked"].map((permission) => (
                <button
                  key={permission}
                  className={
                    selected.permission === permission
                      ? "permission-option selected"
                      : "permission-option"
                  }
                  onClick={() => setPermission(permission)}
                >
                  {permission}
                </button>
              ))}
            </div>
          </div>
          <div className="drawer-actions">
            <Button icon={Activity} onClick={diagnose}>
              运行连接诊断
            </Button>
            <Button variant="danger" icon={Unplug} onClick={revoke}>
              解除授权
            </Button>
          </div>
          {agent && <AgentResultCard result={agent} />}
        </Drawer>
      )}
      {pairTarget && (
        <Modal title={`配对 ${pairTarget.device_name}`} onClose={() => setPairTarget(null)}>
          <form onSubmit={pair} className="form-stack">
            <p className="muted-copy">
              输入目标设备控制台上显示的六位配对码。成功后双方默认获得 write 权限。
            </p>
            <label>
              <span>六位配对码</span>
              <input
                autoFocus
                inputMode="numeric"
                maxLength={6}
                pattern="\d{6}"
                value={pairCode}
                onChange={(event) =>
                  setPairCode(event.target.value.replace(/\D/g, ""))
                }
                placeholder="000000"
                className="pair-input"
              />
            </label>
            <Button type="submit" icon={KeyRound} disabled={pairCode.length !== 6}>
              建立双向配对
            </Button>
          </form>
        </Modal>
      )}
    </Page>
  );
}

export function SyncPage({
  liveVersion,
  notify
}: {
  liveVersion: number;
  notify: (message: string) => void;
}) {
  const [query, setQuery] = useState("");
  const [status, setStatus] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [deviceId, setDeviceId] = useState("");
  const [uploadProgress, setUploadProgress] = useState<number | null>(null);
  const [actionError, setActionError] = useState("");
  const [fileActionError, setFileActionError] = useState("");
  const [savingPath, setSavingPath] = useState("");
  const fileInput = useRef<HTMLInputElement>(null);
  const files = useRemote(
    () =>
      api<{ items: FileEntry[]; total: number }>(
        `/api/files?limit=200&query=${encodeURIComponent(query)}&status=${encodeURIComponent(status)}`
      ),
    [liveVersion, query, status]
  );
  const devices = useRemote(
    () => api<{ items: Device[] }>("/api/devices"),
    [liveVersion]
  );
  const transfers = useRemote(
    () => api<{ items: TransferTask[] }>("/api/transfers"),
    [liveVersion]
  );
  const runtime = useRemote(() => api<RuntimeStatus>("/api/runtime"), [liveVersion]);
  const send = async () => {
    if (!file || !deviceId) return;
    setActionError("");
    setUploadProgress(0);
    try {
      await uploadFile(deviceId, file, setUploadProgress);
      notify("文件已上传到本机发送队列");
      setFile(null);
      if (fileInput.current) fileInput.current.value = "";
      transfers.reload();
    } catch (reason) {
      setActionError(reason instanceof Error ? reason.message : "上传失败");
    } finally {
      window.setTimeout(() => setUploadProgress(null), 500);
    }
  };
  const runSync = async () => {
    await api("/api/sync/run", { method: "POST" });
    notify("已触发一次完整扫描与同步");
  };
  const saveFile = async (entry: FileEntry) => {
    setFileActionError("");
    setSavingPath(entry.relative_path);
    try {
      const result = await saveSharedFile(entry.relative_path, entry.file_name);
      if (result === "saved") {
        notify("文件已保存到所选目录");
      } else if (result === "downloaded") {
        notify("文件已交给浏览器下载");
      }
    } catch (reason) {
      setFileActionError(
        reason instanceof Error ? reason.message : "文件保存失败"
      );
    } finally {
      setSavingPath("");
    }
  };
  const pairedOnline = (devices.data?.items ?? []).filter(
    (item) => item.online && item.paired && !["blocked", "read"].includes(item.permission)
  );
  return (
    <Page>
      <PageIntro
        eyebrow="SYNC ORCHESTRATION"
        title="同步中心"
        description="查看索引版本、同步基线和所有活动传输。"
        actions={<Button icon={Play} onClick={runSync}>立即同步</Button>}
      />
      <section className="content-grid">
        <Panel className="span-8">
          <PanelTitle eyebrow="SHARED ROOT" title="共享目录" />
          <div className="path-display">
            <FolderSync size={21} />
            <code>{runtime.data?.shared_folder || "-"}</code>
          </div>
          <div className="inline-statuses">
            <Badge
              tone={runtime.data?.sync_enabled ? "success" : "muted"}
              icon={FolderSync}
            >
              {runtime.data?.sync_enabled ? "自动同步开启" : "自动同步关闭"}
            </Badge>
            <Badge tone="primary" icon={Clock3}>
              {runtime.data?.sync_interval_seconds || "-"} 秒扫描
            </Badge>
          </div>
        </Panel>
        <Panel className="span-4 warning-panel">
          <AlertTriangle size={22} />
          <div>
            <strong>删除保护策略</strong>
            <p>本地删除只建立删除记录，当前不会直接删除远端文件。</p>
          </div>
        </Panel>
      </section>

      <Panel>
        <PanelTitle eyebrow="MANUAL TRANSFER" title="发送单个文件" />
        {actionError && <InlineError message={actionError} />}
        <div className="upload-row">
          <label className="file-picker">
            <CloudUpload size={20} />
            <span>{file?.name || "选择本机文件"}</span>
            <input
              ref={fileInput}
              type="file"
              onChange={(event) => setFile(event.target.files?.[0] || null)}
            />
          </label>
          <select
            aria-label="目标设备"
            value={deviceId}
            onChange={(event) => setDeviceId(event.target.value)}
          >
            <option value="">选择目标设备</option>
            {pairedOnline.map((device) => (
              <option key={device.device_id} value={device.device_id}>
                {device.device_name} / {device.ip}
              </option>
            ))}
          </select>
          <Button
            icon={uploadProgress === null ? Send : LoaderCircle}
            disabled={!file || !deviceId || uploadProgress !== null}
            onClick={send}
          >
            {uploadProgress === null ? "上传并发送" : `上传中 ${uploadProgress}%`}
          </Button>
        </div>
        {uploadProgress !== null && (
          <div
            className="upload-progress"
            role="progressbar"
            aria-label="文件上传进度"
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={uploadProgress}
          >
            <span style={{ width: `${uploadProgress}%` }} />
          </div>
        )}
      </Panel>

      <section className="content-grid">
        <Panel className="span-8">
          <PanelTitle eyebrow="FILE INDEX" title={`文件索引 · ${files.data?.total ?? 0}`} />
          {fileActionError && <InlineError message={fileActionError} />}
          <div className="filter-row">
            <label className="search-box">
              <Search size={16} />
              <input
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                placeholder="搜索相对路径"
              />
            </label>
            <select
              aria-label="文件同步状态"
              value={status}
              onChange={(event) => setStatus(event.target.value)}
            >
              <option value="">全部状态</option>
              <option value="synced">已同步</option>
              <option value="pending">等待同步</option>
              <option value="local_only">仅本机</option>
              <option value="deleted_record">删除记录</option>
              <option value="conflict">冲突</option>
            </select>
          </div>
          <div className="table-wrap compact-table">
            <table>
              <thead>
                <tr>
                  <th>相对路径</th>
                  <th>大小</th>
                  <th>版本</th>
                  <th>来源</th>
                  <th>修改时间</th>
                  <th>状态</th>
                  <th className="row-actions">操作</th>
                </tr>
              </thead>
              <tbody>
                {(files.data?.items ?? []).map((entry) => (
                  <tr key={entry.relative_path}>
                    <td className="path-cell">{entry.relative_path}</td>
                    <td>{formatBytes(entry.file_size)}</td>
                    <td className="mono">v{entry.version}</td>
                    <td>{entry.source_device_name || shortId(entry.source_device_id)}</td>
                    <td>{formatTime(entry.modified_time_ns)}</td>
                    <td><SyncBadge status={entry.sync_status} /></td>
                    <td className="row-actions">
                      {entry.status === "active" && (
                        <div className="file-actions">
                          {entry.preview_kind && (
                            <a
                              className="button ghost small"
                              href={fileContentUrl(entry.relative_path, "inline")}
                              target="_blank"
                              rel="noreferrer"
                            >
                              <ExternalLink size={15} />
                              打开
                            </a>
                          )}
                          <Button
                            size="small"
                            variant="secondary"
                            icon={savingPath === entry.relative_path ? LoaderCircle : Download}
                            disabled={Boolean(savingPath)}
                            onClick={() => saveFile(entry)}
                          >
                            {savingPath === entry.relative_path ? "保存中" : "另存为"}
                          </Button>
                        </div>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            {!files.loading && !files.data?.items.length && (
              <EmptyState icon={FileText} title="没有匹配文件" description="调整筛选条件或向共享目录添加文件。" />
            )}
          </div>
        </Panel>
        <Panel className="span-4">
          <PanelTitle eyebrow="TRANSFER TASKS" title="活动与最近任务" />
          <TransferList tasks={transfers.data?.items ?? []} />
        </Panel>
      </section>
    </Page>
  );
}

function ConflictsPage({
  liveVersion,
  notify
}: {
  liveVersion: number;
  notify: (message: string) => void;
}) {
  const [filter, setFilter] = useState("unresolved");
  const conflicts = useRemote(
    () => api<{ items: Conflict[] }>(`/api/conflicts?resolved=${filter}`),
    [liveVersion, filter]
  );
  const [selected, setSelected] = useState<Conflict | null>(null);
  const [analysis, setAnalysis] = useState<AgentResult | null>(null);
  const analyze = async (conflict: Conflict) => {
    setSelected(conflict);
    setAnalysis(
      await api<AgentResult>(`/api/conflicts/${conflict.conflict_id}/analyze`, {
        method: "POST"
      })
    );
  };
  const resolve = async (conflict: Conflict) => {
    const note = window.prompt("处理备注（可留空）", "") ?? "";
    await api(`/api/conflicts/${conflict.conflict_id}/resolve`, {
      method: "POST",
      body: JSON.stringify({ note })
    });
    notify("冲突已标记为处理完成");
    setSelected(null);
    conflicts.reload();
  };
  return (
    <Page>
      <PageIntro
        eyebrow="CONFLICT REGISTER"
        title="冲突中心"
        description="系统保留主版本与冲突副本，不会在此执行不可逆覆盖。"
        actions={
          <div className="segmented">
            <button className={filter === "unresolved" ? "active" : ""} onClick={() => setFilter("unresolved")}>未处理</button>
            <button className={filter === "resolved" ? "active" : ""} onClick={() => setFilter("resolved")}>已处理</button>
            <button className={filter === "all" ? "active" : ""} onClick={() => setFilter("all")}>全部</button>
          </div>
        }
      />
      <Panel>
        <div className="conflict-list">
          {(conflicts.data?.items ?? []).map((conflict) => (
            <article className="conflict-row" key={conflict.conflict_id}>
              <div className="conflict-symbol"><FileDiff size={22} /></div>
              <div className="conflict-main">
                <div>
                  <strong>{conflict.relative_path}</strong>
                  <Badge tone="warning">{reasonLabel(conflict.reason_code)}</Badge>
                  {conflict.resolved_at_ns && <Badge tone="success">已处理</Badge>}
                </div>
                <span>
                  {formatTime(conflict.conflict_at_ns)} · 主版本来自{" "}
                  {shortId(conflict.winner_device_id)}
                </span>
              </div>
              <div className="conflict-actions">
                <Button size="small" variant="ghost" onClick={() => setSelected(conflict)}>比较版本</Button>
                <Button size="small" icon={Bot} onClick={() => analyze(conflict)}>Agent 分析</Button>
              </div>
            </article>
          ))}
          {!conflicts.loading && !conflicts.data?.items.length && (
            <EmptyState icon={Check} title="没有冲突记录" description="当前文件版本处于一致状态。" />
          )}
        </div>
      </Panel>
      {selected && (
        <Drawer title="版本比较" onClose={() => { setSelected(null); setAnalysis(null); }}>
          <h3 className="drawer-path">{selected.relative_path}</h3>
          <div className="version-compare">
            <VersionCard
              title="本机版本"
              version={selected.local_version}
              size={selected.local_size}
              modified={selected.local_modified_time_ns}
              hash={selected.local_hash}
              winner={selected.winner_device_id === selected.local_device_id}
            />
            <VersionCard
              title="远端版本"
              version={selected.remote_version}
              size={selected.remote_size}
              modified={selected.remote_modified_time_ns}
              hash={selected.remote_hash}
              winner={selected.winner_device_id === selected.remote_device_id}
            />
          </div>
          <div className="drawer-actions">
            <a className="button secondary" href={`/api/conflicts/${selected.conflict_id}/file/main`} target="_blank" rel="noreferrer">打开主文件</a>
            {selected.conflict_copy_path && (
              <a className="button ghost" href={`/api/conflicts/${selected.conflict_id}/file/copy`} target="_blank" rel="noreferrer">打开冲突副本</a>
            )}
            {!selected.resolved_at_ns && (
              <Button icon={Check} onClick={() => resolve(selected)}>标记已处理</Button>
            )}
          </div>
          {analysis && <AgentResultCard result={analysis} />}
        </Drawer>
      )}
    </Page>
  );
}

function SecurityPage({
  liveVersion,
  notify
}: {
  liveVersion: number;
  notify: (message: string) => void;
}) {
  const summary = useRemote(
    () => api<AgentResult>("/api/security/summary"),
    [liveVersion]
  );
  const alerts = useRemote(
    () => api<{ items: AuditAlert[] }>("/api/security/alerts?limit=100"),
    [liveVersion]
  );
  const [pairCode, setPairCode] = useState("");
  const [revealed, setRevealed] = useState(false);
  const [auditResult, setAuditResult] = useState<AgentResult | null>(null);
  const reveal = async () => {
    const result = await api<{ pair_code: string }>("/api/security/pair-code");
    setPairCode(result.pair_code);
    setRevealed(true);
  };
  const regenerate = async () => {
    if (!window.confirm("重新生成配对码？已有授权不会被撤销。")) return;
    const result = await api<{ pair_code: string }>(
      "/api/security/pair-code/regenerate",
      { method: "POST" }
    );
    setPairCode(result.pair_code);
    setRevealed(true);
    notify("本机配对码已重新生成");
  };
  const audit = async () => {
    setAuditResult(
      await api<AgentResult>("/api/security/audit", { method: "POST" })
    );
  };
  const markRead = async (alertId: number) => {
    await api(`/api/security/alerts/${alertId}/read`, { method: "POST" });
    alerts.reload();
  };
  const facts = summary.data?.facts ?? {};
  const cards = [
    ["TLS 状态", facts.tls_enabled ? "已启用" : "未启用", LockKeyhole],
    ["授权设备", String(facts.authorized_count ?? 0), UserRoundCheck],
    ["陌生设备", String(facts.stranger_count ?? 0), Network],
    ["认证失败", String(facts.failure_count ?? 0), ShieldAlert]
  ] as const;
  return (
    <Page>
      <PageIntro
        eyebrow="TRUST & AUDIT"
        title="安全中心"
        description="检查证书、授权边界、异常访问与共享范围风险。"
        actions={<Button icon={ShieldCheck} onClick={audit}>一键安全审计</Button>}
      />
      <section className="security-grid">
        {cards.map(([label, value, Icon]) => (
          <Panel key={label} className="security-metric">
            <Icon size={20} />
            <span>{label}</span>
            <strong>{value}</strong>
          </Panel>
        ))}
        <Panel className="pair-code-panel">
          <span className="eyebrow">PAIRING CODE</span>
          <div className="pair-code-display">{revealed ? pairCode : "••••••"}</div>
          <div className="button-row">
            <Button size="small" variant="secondary" icon={Fingerprint} onClick={reveal}>
              {revealed ? "刷新显示" : "显示配对码"}
            </Button>
            <Button size="small" variant="ghost" icon={RefreshCw} onClick={regenerate}>重新生成</Button>
          </div>
        </Panel>
      </section>
      {summary.data && <AgentResultCard result={auditResult || summary.data} />}
      <Panel>
        <PanelTitle eyebrow="ANOMALY ALERTS" title="异常访问告警" />
        <div className="alert-list">
          {(alerts.data?.items ?? []).map((alert) => (
            <article className={alert.read ? "alert-row read" : "alert-row"} key={alert.alert_id}>
              <ShieldAlert size={20} />
              <div>
                <div>
                  <Badge tone={toneForSeverity(alert.severity)}>{severityLabel(alert.severity)}</Badge>
                  <strong>{alert.rule_code}</strong>
                </div>
                <p>{alert.message}</p>
                <span>{alert.source_key} · {alert.event_count} 次 · {formatTime(alert.created_at_ns)}</span>
              </div>
              {!alert.read && <Button size="small" variant="ghost" onClick={() => markRead(alert.alert_id)}>标记已读</Button>}
            </article>
          ))}
          {!alerts.loading && !alerts.data?.items.length && (
            <EmptyState icon={ShieldCheck} title="暂无异常告警" description="异常检测规则未发现需要处理的访问模式。" />
          )}
        </div>
      </Panel>
    </Page>
  );
}

export function AgentsPage({
  liveVersion = 0,
  notify
}: {
  liveVersion?: number;
  notify: (message: string) => void;
}) {
  const [threads, setThreads] = useState<AgentThreadSummary[]>([]);
  const [threadsCursor, setThreadsCursor] = useState<string | null>(null);
  const [threadsLoading, setThreadsLoading] = useState(true);
  const [selectedThreadId, setSelectedThreadId] = useState<string | null>(null);
  const [messages, setMessages] = useState<AgentMessage[]>([]);
  const [historyCursor, setHistoryCursor] = useState<string | null>(null);
  const [threadLoading, setThreadLoading] = useState(false);
  const [run, setRun] = useState<AgentRun | null>(null);
  const [message, setMessage] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");
  const [sessionDrawerOpen, setSessionDrawerOpen] = useState(false);
  const [editingThreadId, setEditingThreadId] = useState("");
  const [editingTitle, setEditingTitle] = useState("");
  const selectedThreadRef = useRef<string | null>(null);
  const messageListRef = useRef<HTMLDivElement>(null);
  const agents = [
    {
      id: "connection",
      title: "连接诊断",
      icon: Activity,
      description: "检查在线设备、TCP、网段与 TLS。",
      prompt: "诊断当前可用远端设备的连接状态，并给出处理建议。"
    },
    {
      id: "conflict",
      title: "冲突分析",
      icon: FileDiff,
      description: "解释双端变化和冲突副本。",
      prompt: "分析当前未处理的文件冲突，不要执行文件传输。"
    },
    {
      id: "security",
      title: "安全审计",
      icon: ShieldCheck,
      description: "审查 TLS、授权和异常访问。",
      prompt: "运行一次安全审计，说明风险和建议，不要执行文件传输。"
    }
  ];

  const fetchThreads = useCallback(async (cursor = "", replace = true) => {
    setThreadsLoading(true);
    try {
      const query = new URLSearchParams({ limit: "50" });
      if (cursor) query.set("cursor", cursor);
      const response = await api<AgentThreadPage>(
        `/api/agent/threads?${query.toString()}`
      );
      setThreads((current) => replace
        ? response.items
        : [...current, ...response.items]
      );
      setThreadsCursor(response.next_cursor);
    } finally {
      setThreadsLoading(false);
    }
  }, []);

  const loadThread = useCallback(async (threadId: string) => {
    setThreadLoading(true);
    try {
      const response = await api<AgentThreadDetail>(
        `/api/agent/threads/${encodeURIComponent(threadId)}?limit=50`
      );
      if (selectedThreadRef.current !== threadId) return;
      setMessages(response.messages);
      setHistoryCursor(response.next_cursor);
      setRun(response.latest_run);
      window.requestAnimationFrame(() => {
        if (messageListRef.current) {
          messageListRef.current.scrollTop = messageListRef.current.scrollHeight;
        }
      });
    } finally {
      if (selectedThreadRef.current === threadId) setThreadLoading(false);
    }
  }, []);

  useEffect(() => {
    void fetchThreads().catch((reason) => {
      setError(reason instanceof Error ? reason.message : "无法加载 Agent 会话");
    });
  }, [fetchThreads]);

  useEffect(() => {
    if (!liveVersion) return;
    void fetchThreads().catch(() => undefined);
  }, [liveVersion, fetchThreads]);

  const openThread = (threadId: string) => {
    selectedThreadRef.current = threadId;
    setSelectedThreadId(threadId);
    setMessages([]);
    setHistoryCursor(null);
    setRun(null);
    setError("");
    setSessionDrawerOpen(false);
    void loadThread(threadId).catch((reason) => {
      if (selectedThreadRef.current === threadId) {
        setError(reason instanceof Error ? reason.message : "无法加载 Agent 会话");
      }
    });
  };

  const newThread = () => {
    selectedThreadRef.current = null;
    setSelectedThreadId(null);
    setMessages([]);
    setHistoryCursor(null);
    setRun(null);
    setMessage("");
    setError("");
    setSessionDrawerOpen(false);
  };

  const refresh = useCallback(async (runId: string, threadId: string) => {
    const response = await api<AgentRun>(`/api/agent/runs/${runId}`);
    if (selectedThreadRef.current !== threadId) return response;
    setRun(response);
    if (!isAgentRunActive(response.status)) {
      await Promise.all([loadThread(threadId), fetchThreads()]);
    }
    return response;
  }, [fetchThreads, loadThread]);

  useEffect(() => {
    if (!run || !selectedThreadId || !isAgentRunActive(run.status)) return;
    const timer = window.setInterval(() => {
      void refresh(run.run_id, selectedThreadId).catch(() => undefined);
    }, 1000);
    return () => window.clearInterval(timer);
  }, [run?.run_id, run?.status, selectedThreadId, liveVersion, refresh]);

  const startRun = async (request: string) => {
    const trimmed = request.trim();
    if (!trimmed) return;
    setSubmitting(true);
    setError("");
    try {
      const response = await api<AgentRun>("/api/agent/runs", {
        method: "POST",
        body: JSON.stringify({
          message: trimmed,
          thread_id: selectedThreadId || ""
        })
      });
      selectedThreadRef.current = response.thread_id;
      setSelectedThreadId(response.thread_id);
      setRun(response);
      setMessages((current) => {
        const retained = selectedThreadId ? current : [];
        return [...retained, ...response.messages];
      });
      setMessage("");
      notify("Agent 任务已启动");
      try {
        await Promise.all([loadThread(response.thread_id), fetchThreads()]);
      } catch (refreshError) {
        setError(
          refreshError instanceof Error
            ? refreshError.message
            : "任务已启动，但会话刷新失败"
        );
      }
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "无法启动 Agent");
    } finally {
      setSubmitting(false);
    }
  };

  const submit = (event: FormEvent) => {
    event.preventDefault();
    void startRun(message);
  };

  const decide = async (approved: boolean) => {
    if (!run) return;
    setSubmitting(true);
    setError("");
    try {
      const response = await api<AgentRun>(
        `/api/agent/runs/${run.run_id}/decision`,
        {
          method: "POST",
          body: JSON.stringify({ approved })
        }
      );
      setRun(response);
      notify(approved ? "同步计划已批准" : "同步计划已拒绝");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "审批失败");
    } finally {
      setSubmitting(false);
    }
  };

  const selectDevice = async (deviceId: string) => {
    if (!run) return;
    setSubmitting(true);
    setError("");
    try {
      const response = await api<AgentRun>(
        `/api/agent/runs/${run.run_id}/decision`,
        {
          method: "POST",
          body: JSON.stringify({ device_id: deviceId })
        }
      );
      setRun(response);
      notify("目标设备已选择");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "设备选择失败");
    } finally {
      setSubmitting(false);
    }
  };

  const loadEarlier = async () => {
    if (!selectedThreadId || !historyCursor || threadLoading) return;
    const threadId = selectedThreadId;
    const list = messageListRef.current;
    const previousHeight = list?.scrollHeight || 0;
    setThreadLoading(true);
    try {
      const query = new URLSearchParams({
        limit: "50",
        cursor: historyCursor
      });
      const response = await api<AgentThreadDetail>(
        `/api/agent/threads/${encodeURIComponent(threadId)}?${query.toString()}`
      );
      if (selectedThreadRef.current !== threadId) return;
      setMessages((current) => [...response.messages, ...current]);
      setHistoryCursor(response.next_cursor);
      window.requestAnimationFrame(() => {
        if (messageListRef.current) {
          messageListRef.current.scrollTop =
            messageListRef.current.scrollHeight - previousHeight;
        }
      });
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "无法加载更早的消息");
    } finally {
      if (selectedThreadRef.current === threadId) setThreadLoading(false);
    }
  };

  const saveThreadTitle = async (threadId: string) => {
    const title = editingTitle.trim();
    if (!title) return;
    setSubmitting(true);
    try {
      await api<AgentThreadSummary>(
        `/api/agent/threads/${encodeURIComponent(threadId)}`,
        {
          method: "PATCH",
          body: JSON.stringify({ title })
        }
      );
      setEditingThreadId("");
      await fetchThreads();
      notify("会话名称已更新");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "无法修改会话名称");
    } finally {
      setSubmitting(false);
    }
  };

  const deleteThread = async (thread: AgentThreadSummary) => {
    if (!window.confirm(`永久删除会话“${thread.title}”及其全部记录？`)) return;
    setSubmitting(true);
    try {
      await api<{ status: string }>(
        `/api/agent/threads/${encodeURIComponent(thread.thread_id)}`,
        { method: "DELETE" }
      );
      if (selectedThreadRef.current === thread.thread_id) newThread();
      await fetchThreads();
      notify("会话已删除");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "无法删除会话");
    } finally {
      setSubmitting(false);
    }
  };

  const selectionDevices = agentSelectionDevices(run);
  const activeRun = isAgentRunActive(run?.status);
  const selectedThread = threads.find(
    (thread) => thread.thread_id === selectedThreadId
  );

  return (
    <Page>
      <PageIntro
        eyebrow="INTELLIGENCE LAYER"
        title="ReAct 同步 Agent"
        description="Agent 会自主查询设备和索引、生成确定性计划，并在文件传输前等待你的批准。"
      />
      <button
        className="agent-session-toggle"
        onClick={() => setSessionDrawerOpen(true)}
      >
        <PanelLeft size={16} />
        会话列表
      </button>
      {sessionDrawerOpen && (
        <button
          className="agent-session-scrim"
          aria-label="关闭会话列表"
          onClick={() => setSessionDrawerOpen(false)}
        />
      )}
      <section className="agent-session-layout">
        <aside className={`agent-session-sidebar ${sessionDrawerOpen ? "open" : ""}`}>
          <div className="agent-session-head">
            <div><span className="eyebrow">SESSIONS</span><strong>Agent 会话</strong></div>
            <button className="icon-button" aria-label="关闭会话列表" onClick={() => setSessionDrawerOpen(false)}>
              <X size={16} />
            </button>
          </div>
          <button className="agent-new-session" onClick={newThread}>
            <Plus size={16} />
            新会话
          </button>
          <div className="agent-session-list">
            {threads.map((thread) => (
              <article
                className={`agent-session-item ${selectedThreadId === thread.thread_id ? "active" : ""}`}
                key={thread.thread_id}
              >
                {editingThreadId === thread.thread_id ? (
                  <form
                    className="agent-session-edit"
                    onSubmit={(event) => {
                      event.preventDefault();
                      void saveThreadTitle(thread.thread_id);
                    }}
                  >
                    <input
                      aria-label="会话名称"
                      maxLength={80}
                      value={editingTitle}
                      onChange={(event) => setEditingTitle(event.target.value)}
                      autoFocus
                    />
                    <button aria-label="保存会话名称" disabled={!editingTitle.trim() || submitting}>
                      <Check size={14} />
                    </button>
                    <button type="button" aria-label="取消修改" onClick={() => setEditingThreadId("")}>
                      <X size={14} />
                    </button>
                  </form>
                ) : (
                  <>
                    <button className="agent-session-select" onClick={() => openThread(thread.thread_id)}>
                      <strong>{thread.title}</strong>
                      <span>
                        {thread.run_count} 轮 · {formatTime(thread.updated_at_ns)}
                      </span>
                    </button>
                    <div className="agent-session-actions">
                      <Badge tone={agentStatusTone(thread.status || undefined)}>
                        {agentStatusLabel(thread.status || undefined)}
                      </Badge>
                      <button
                        aria-label={`重命名 ${thread.title}`}
                        onClick={() => {
                          setEditingThreadId(thread.thread_id);
                          setEditingTitle(thread.title);
                        }}
                      >
                        <Pencil size={13} />
                      </button>
                      <button
                        aria-label={`删除 ${thread.title}`}
                        disabled={isAgentRunActive(thread.status)}
                        onClick={() => void deleteThread(thread)}
                      >
                        <Trash2 size={13} />
                      </button>
                    </div>
                  </>
                )}
              </article>
            ))}
            {!threads.length && !threadsLoading && (
              <p className="agent-session-empty">暂无历史会话</p>
            )}
          </div>
          {threadsCursor && (
            <button
              className="agent-load-more"
              disabled={threadsLoading}
              onClick={() => void fetchThreads(threadsCursor, false)}
            >
              {threadsLoading ? "加载中…" : "加载更多会话"}
            </button>
          )}
        </aside>

        <div className="agent-session-content">
          <div className="agent-session-toolbar">
            <div>
              <span className="eyebrow">CURRENT SESSION</span>
              <strong>{selectedThread?.title || "新会话"}</strong>
            </div>
            {activeRun && <Badge tone={agentStatusTone(run?.status)}>{agentStatusLabel(run?.status)}</Badge>}
          </div>

          <section className="agent-preset-grid">
            {agents.map((agent) => {
              const Icon = agent.icon;
              return <button
                  className="agent-preset"
                  key={agent.id}
                  onClick={() => void startRun(agent.prompt)}
                  disabled={submitting || activeRun}
                >
                  <Icon size={27} />
                  <span><strong>{agent.title}</strong>{agent.description}</span>
                  <Play size={16} />
                </button>;
            })}
          </section>

          <section className="agent-workspace">
            <Panel className="agent-chat">
              <PanelTitle eyebrow="USER REQUEST" title="任务对话" />
              <div className="agent-messages" ref={messageListRef}>
                {historyCursor && (
                  <button className="agent-load-earlier" disabled={threadLoading} onClick={() => void loadEarlier()}>
                    {threadLoading ? "加载中…" : "加载更早的消息"}
                  </button>
                )}
                {messages.map((item) => (
                  <article className={`agent-message ${item.role}`} key={`${item.run_id}-${item.role}`}>
                    <span>{item.role === "user" ? "你" : "Agent"}</span>
                    {item.role === "assistant" ? (
                      <AgentMarkdown content={item.content} />
                    ) : (
                      <p>{item.content}</p>
                    )}
                  </article>
                ))}
                {!messages.length && !threadLoading && (
                  <EmptyState
                    icon={Bot}
                    title={selectedThreadId ? "这个会话还没有消息" : "描述一个同步任务"}
                    description="例如：同步 PC-B 的 notes 文件夹。Agent 会先规划，不会直接传输。"
                  />
                )}
              </div>
              <form className="agent-composer" onSubmit={submit}>
                <textarea
                  aria-label="Agent 任务"
                  value={message}
                  onChange={(event) => setMessage(event.target.value)}
                  placeholder={activeRun ? "请先等待当前任务完成" : "输入设备名称和目录，例如：同步 PC-B 的 notes 文件夹"}
                  disabled={activeRun}
                  rows={3}
                />
                <Button
                  type="submit"
                  icon={submitting ? LoaderCircle : Send}
                  disabled={submitting || activeRun || !message.trim()}
                >
                  {submitting ? "提交中" : activeRun ? "任务处理中" : "发送任务"}
                </Button>
              </form>
            </Panel>

            <Panel className="agent-trace">
              <PanelTitle eyebrow="REACT TRACE" title="最新一轮执行步骤" />
              <div className="agent-run-head">
                <Badge tone={agentStatusTone(run?.status)}>
                  {run?.status === "waiting_approval" && !run.plan
                    ? "等待选择"
                    : agentStatusLabel(run?.status)}
                </Badge>
                {run && <code>{run.run_id.slice(0, 12)}</code>}
              </div>
              <div className="agent-timeline">
                {run?.steps.map((step, index) => (
                  <article className="agent-step" key={`${step.created_at_ns}-${index}`}>
                    <span className={`step-dot ${step.status}`} />
                    <div>
                      <strong>{toolLabel(step.name)}</strong>
                      <span>{step.kind} / {step.status}</span>
                      {step.kind === "observation" && (
                        <pre>{compactJson(step.output)}</pre>
                      )}
                    </div>
                  </article>
                ))}
                {run && !run.steps.length && <p className="muted-copy">等待 Planner 启动。</p>}
                {!run && <p className="muted-copy">发送消息后，这里会显示最新一轮执行过程。</p>}
              </div>
            </Panel>
          </section>

          {run?.status === "waiting_approval" && !run.plan && selectionDevices.length > 0 && (
            <Panel>
              <PanelTitle eyebrow="CLARIFICATION" title="请选择目标设备" />
              <div className="device-choice-grid">
                {selectionDevices.map((device) => (
                  <button
                    className="device-choice"
                    key={device.device_id}
                    onClick={() => void selectDevice(device.device_id)}
                    disabled={submitting}
                  >
                    <Laptop size={20} />
                    <span><strong>{device.device_name}</strong><code>{device.ip}</code></span>
                    <ChevronRight size={16} />
                  </button>
                ))}
              </div>
            </Panel>
          )}

          {run?.plan && (
            <Panel className="agent-plan">
          <PanelTitle
            eyebrow="SYNC PLAN"
            title={`${run.plan.device_name} / ${run.plan.path_prefix || "共享目录根目录"}`}
          />
          <div className="plan-summary">
            <Metric label="上传" value={String(run.plan.counts.upload || 0)} />
            <Metric label="下载" value={String(run.plan.counts.download || 0)} />
            <Metric label="冲突" value={String(run.plan.counts.conflict || 0)} />
            <Metric label="删除差异" value={String(run.plan.counts.delete_report || 0)} />
            <Metric label="传输量" value={formatBytes(run.plan.total_bytes)} />
          </div>
          <div className="plan-actions">
            {run.plan.actions
              .filter((action) => action.direction !== "same")
              .map((action) => (
                <div className="plan-action" key={action.action_id}>
                  <Badge tone={actionTone(action.direction, action.status)}>
                    {directionLabel(action.direction)}
                  </Badge>
                  <code>{action.relative_path}</code>
                  <span>
                    {action.status}
                    {action.executable && action.bytes > 0
                      ? ` / ${Math.round((action.transferred_bytes * 100) / action.bytes)}%`
                      : ""}
                  </span>
                  {action.error_message && <small>{action.error_message}</small>}
                </div>
              ))}
          </div>
          {run.status === "waiting_approval" && (
            <div className="approval-bar">
              <div>
                <strong>等待执行审批</strong>
                <span>删除和冲突只报告；批准后仅执行上传与下载。</span>
              </div>
              <Button variant="ghost" icon={X} onClick={() => void decide(false)} disabled={submitting}>
                拒绝
              </Button>
              <Button icon={Check} onClick={() => void decide(true)} disabled={submitting}>
                批准并执行
              </Button>
            </div>
          )}
            </Panel>
          )}
        </div>
      </section>

      {error && (
        <ErrorState
          message={error}
          retry={() => selectedThreadId ? openThread(selectedThreadId) : void fetchThreads()}
        />
      )}
    </Page>
  );
}

function isAgentRunActive(status?: AgentRun["status"] | ""): boolean {
  return status === "queued" || status === "running" || status === "waiting_approval";
}

function AgentMarkdown({ content }: { content: string }) {
  return (
    <div className="agent-markdown">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          a: ({ children, ...props }) => (
            <a {...props} target="_blank" rel="noreferrer">
              {children}
            </a>
          )
        }}
      >
        {content}
      </ReactMarkdown>
    </div>
  );
}

function agentStatusLabel(status?: AgentRun["status"]): string {
  return {
    queued: "排队中",
    running: "执行中",
    waiting_approval: "等待审批",
    completed: "已完成",
    rejected: "已拒绝",
    failed: "失败"
  }[status || "queued"];
}

function agentStatusTone(status?: AgentRun["status"]): BadgeTone {
  if (status === "completed") return "success";
  if (status === "failed") return "danger";
  if (status === "waiting_approval" || status === "rejected") return "warning";
  return "primary";
}

function toolLabel(name: string): string {
  return {
    planner: "Agent Planner",
    build_plan: "生成同步计划",
    human_decision: "人工审批",
    execute_sync_plan: "执行同步计划",
    verify_sync_plan: "验证传输",
    final_report: "最终报告",
    model_fallback: "本地规划降级"
  }[name] || name;
}

function compactJson(value: unknown): string {
  const encoded = JSON.stringify(value, null, 2);
  return encoded.length > 420 ? `${encoded.slice(0, 420)}…` : encoded;
}

function directionLabel(direction: string): string {
  return {
    upload: "上传",
    download: "下载",
    conflict: "冲突",
    delete_report: "删除差异",
    same: "一致"
  }[direction] || direction;
}

function actionTone(direction: string, status: string): BadgeTone {
  if (status === "failed" || direction === "conflict") return "danger";
  if (direction === "delete_report") return "warning";
  if (status === "success") return "success";
  return "primary";
}

function agentSelectionDevices(run: AgentRun | null): Array<{
  device_id: string;
  device_name: string;
  ip: string;
}> {
  if (!run) return [];
  const step = [...run.steps]
    .reverse()
    .find((item) => item.kind === "clarification" && item.name === "select_device");
  if (!step || typeof step.output !== "object" || step.output === null) return [];
  const devices = (step.output as { devices?: unknown }).devices;
  if (!Array.isArray(devices)) return [];
  return devices.filter(
    (item): item is { device_id: string; device_name: string; ip: string } =>
      typeof item === "object" &&
      item !== null &&
      typeof (item as { device_id?: unknown }).device_id === "string" &&
      typeof (item as { device_name?: unknown }).device_name === "string" &&
      typeof (item as { ip?: unknown }).ip === "string"
  );
}

function LogsSettingsPage({
  liveVersion,
  notify
}: {
  liveVersion: number;
  notify: (message: string) => void;
}) {
  const [tab, setTab] = useState<"logs" | "settings">("logs");
  return (
    <Page>
      <PageIntro
        eyebrow="OPERATIONS"
        title="日志与设置"
        description="访问事件可审计，核心网络配置变更将在重启后生效。"
        actions={
          <div className="segmented">
            <button className={tab === "logs" ? "active" : ""} onClick={() => setTab("logs")}>访问日志</button>
            <button className={tab === "settings" ? "active" : ""} onClick={() => setTab("settings")}>系统设置</button>
          </div>
        }
      />
      {tab === "logs" ? (
        <LogsPanel liveVersion={liveVersion} />
      ) : (
        <SettingsPanel liveVersion={liveVersion} notify={notify} />
      )}
    </Page>
  );
}

function LogsPanel({ liveVersion }: { liveVersion: number }) {
  const pageSize = 25;
  const [severity, setSeverity] = useState("");
  const [outcome, setOutcome] = useState("");
  const [eventType, setEventType] = useState("");
  const [deviceId, setDeviceId] = useState("");
  const [startedAt, setStartedAt] = useState("");
  const [endedAt, setEndedAt] = useState("");
  const [page, setPage] = useState(0);
  const devices = useRemote(
    () => api<{ items: Device[] }>("/api/devices"),
    [liveVersion]
  );
  const startedAtNs = dateTimeToNanoseconds(startedAt);
  const endedAtNs = dateTimeToNanoseconds(endedAt);
  const logs = useRemote(
    () =>
      api<{ items: AuditEvent[]; total: number }>(
        `/api/logs?limit=${pageSize}&offset=${page * pageSize}` +
          `&device_id=${encodeURIComponent(deviceId)}` +
          `&severity=${severity}&outcome=${outcome}` +
          `&event_type=${encodeURIComponent(eventType)}` +
          `${startedAtNs ? `&started_at_ns=${startedAtNs}` : ""}` +
          `${endedAtNs ? `&ended_at_ns=${endedAtNs}` : ""}`
      ),
    [
      liveVersion,
      severity,
      outcome,
      eventType,
      deviceId,
      startedAtNs,
      endedAtNs,
      page
    ]
  );
  useEffect(() => {
    setPage(0);
  }, [severity, outcome, eventType, deviceId, startedAt, endedAt]);
  const totalPages = Math.max(1, Math.ceil((logs.data?.total ?? 0) / pageSize));
  return (
    <Panel>
      <div className="filter-grid">
        <label className="search-box">
          <ListFilter size={16} />
          <input value={eventType} onChange={(event) => setEventType(event.target.value)} placeholder="事件类型精确筛选" />
        </label>
        <select value={deviceId} onChange={(event) => setDeviceId(event.target.value)}>
          <option value="">全部设备</option>
          {(devices.data?.items ?? []).map((device) => (
            <option key={device.device_id} value={device.device_id}>
              {device.device_name}
            </option>
          ))}
        </select>
        <select value={severity} onChange={(event) => setSeverity(event.target.value)}>
          <option value="">全部级别</option>
          <option value="info">信息</option>
          <option value="warning">警告</option>
          <option value="error">错误</option>
        </select>
        <select value={outcome} onChange={(event) => setOutcome(event.target.value)}>
          <option value="">全部结果</option>
          <option value="success">成功</option>
          <option value="failed">失败</option>
          <option value="rejected">拒绝</option>
        </select>
        <label className="date-filter">
          <span>开始时间</span>
          <input
            type="datetime-local"
            value={startedAt}
            onChange={(event) => setStartedAt(event.target.value)}
          />
        </label>
        <label className="date-filter">
          <span>结束时间</span>
          <input
            type="datetime-local"
            value={endedAt}
            onChange={(event) => setEndedAt(event.target.value)}
          />
        </label>
      </div>
      {logs.error && <InlineError message={logs.error} />}
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>时间</th><th>级别</th><th>事件</th><th>来源</th><th>请求</th><th>结果</th><th>流量</th>
            </tr>
          </thead>
          <tbody>
            {(logs.data?.items ?? []).map((event) => (
              <tr key={event.event_id}>
                <td>{formatTime(event.created_at_ns)}</td>
                <td><Badge tone={toneForSeverity(event.severity)}>{severityLabel(event.severity)}</Badge></td>
                <td className="mono">{event.event_type}</td>
                <td className="mono">{event.source_ip || shortId(event.device_id) || "-"}</td>
                <td>{event.request_type || "-"}</td>
                <td>{event.outcome}</td>
                <td>{formatBytes(event.bytes_count)}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {!logs.loading && !logs.data?.items.length && (
          <EmptyState
            icon={TerminalSquare}
            title="没有匹配日志"
            description="调整设备、类型、结果、等级或时间范围筛选。"
          />
        )}
      </div>
      <div className="pagination">
        <span>
          共 {logs.data?.total ?? 0} 条 · 第 {Math.min(page + 1, totalPages)} /{" "}
          {totalPages} 页
        </span>
        <div className="button-row">
          <Button
            size="small"
            variant="ghost"
            disabled={page === 0}
            onClick={() => setPage((value) => Math.max(0, value - 1))}
          >
            上一页
          </Button>
          <Button
            size="small"
            variant="ghost"
            disabled={page + 1 >= totalPages}
            onClick={() => setPage((value) => value + 1)}
          >
            下一页
          </Button>
        </div>
      </div>
    </Panel>
  );
}

function SettingsPanel({
  liveVersion,
  notify
}: {
  liveVersion: number;
  notify: (message: string) => void;
}) {
  const settings = useRemote(
    () => api<SettingsData>("/api/settings"),
    [liveVersion]
  );
  const [form, setForm] = useState<Record<string, string | number | boolean>>({});
  useEffect(() => {
    if (settings.data) setForm(settings.data.configured);
  }, [settings.data]);
  const save = async (event: FormEvent) => {
    event.preventDefault();
    const original = settings.data?.configured ?? {};
    const changes = Object.fromEntries(
      Object.entries(form).filter(([key, value]) => value !== original[key])
    );
    if (!Object.keys(changes).length) {
      notify("没有需要保存的变更");
      return;
    }
    await api("/api/settings", {
      method: "PATCH",
      body: JSON.stringify(changes)
    });
    notify("设置已保存");
    settings.reload();
  };
  if (!settings.data) return <PageLoader />;
  const fields: Array<[string, string, "text" | "number" | "boolean"]> = [
    ["device_name", "设备名称", "text"],
    ["shared_folder", "共享目录", "text"],
    ["udp_port", "UDP 发现端口", "number"],
    ["tcp_port", "TCP 传输端口", "number"],
    ["web_port", "Web 控制台端口", "number"],
    ["broadcast_ip", "广播地址", "text"],
    ["chunk_size", "分块大小（字节）", "number"],
    ["enable_tls", "启用 TLS", "boolean"],
    ["sync_enabled", "启用自动同步", "boolean"],
    ["sync_interval_seconds", "同步扫描周期（秒）", "number"],
    ["security_audit_interval_seconds", "安全审计周期（秒）", "number"],
    ["audit_log_retention_days", "日志保留天数", "number"],
    ["shared_size_risk_bytes", "共享大小风险阈值", "number"],
    ["shared_file_count_risk", "共享文件数风险阈值", "number"],
    ["agent_provider", "Agent 模型提供商", "text"],
    ["agent_api_url", "Agent API 地址", "text"],
    ["agent_model", "Agent 模型", "text"],
    ["agent_timeout_seconds", "Agent 超时（秒）", "number"]
  ];
  return (
    <form onSubmit={save}>
      {settings.data.pending_restart && (
        <div className="restart-banner">
          <RefreshCw size={19} />
          <div>
            <strong>部分配置将在重启后生效</strong>
            <span>当前运行服务仍使用顶部状态栏显示的活动配置。</span>
          </div>
        </div>
      )}
      <Panel>
        <PanelTitle eyebrow="NODE CONFIGURATION" title="系统设置" />
        <div className="settings-grid">
          {fields.map(([key, label, kind]) => {
            const restart = settings.data!.restart_fields.includes(key);
            return (
              <label className="setting-field" key={key}>
                <span>
                  {label}
                  <em className={restart ? "restart-tag" : "live-tag"}>
                    {restart ? "重启后生效" : "即时生效"}
                  </em>
                </span>
                {kind === "boolean" ? (
                  <button
                    type="button"
                    className={form[key] ? "switch on" : "switch"}
                    onClick={() => setForm({ ...form, [key]: !form[key] })}
                  >
                    <span />
                    {form[key] ? "开启" : "关闭"}
                  </button>
                ) : (
                  <input
                    type={kind}
                    value={String(form[key] ?? "")}
                    onChange={(event) =>
                      setForm({
                        ...form,
                        [key]:
                          kind === "number"
                            ? Number(event.target.value)
                            : event.target.value
                      })
                    }
                  />
                )}
              </label>
            );
          })}
        </div>
        <div className="settings-foot">
          <div className="api-key-state">
            <KeyRound size={17} />
            DeepSeek API Key：
            <strong>{settings.data.deepseek_api_key_configured ? "已通过环境变量配置" : "未配置"}</strong>
          </div>
          <Button type="submit" icon={Check}>保存设置</Button>
        </div>
      </Panel>
    </form>
  );
}

function Page({ children }: { children: ReactNode }) {
  return <div className="page">{children}</div>;
}

function PageIntro({
  eyebrow,
  title,
  description,
  actions
}: {
  eyebrow: string;
  title: string;
  description: string;
  actions?: ReactNode;
}) {
  return (
    <div className="page-intro">
      <div>
        <span className="eyebrow">{eyebrow}</span>
        <h2>{title}</h2>
        <p>{description}</p>
      </div>
      {actions && <div className="intro-actions">{actions}</div>}
    </div>
  );
}

function Panel({
  children,
  className = ""
}: {
  children: ReactNode;
  className?: string;
}) {
  return <section className={`panel ${className}`}>{children}</section>;
}

function PanelTitle({
  eyebrow,
  title,
  action
}: {
  eyebrow: string;
  title: string;
  action?: ReactNode;
}) {
  return (
    <div className="panel-title">
      <div><span className="eyebrow">{eyebrow}</span><h3>{title}</h3></div>
      {action}
    </div>
  );
}

function Metric({ label, value }: { label: string; value: string }) {
  return <div className="plan-metric"><span>{label}</span><strong>{value}</strong></div>;
}

function Button({
  children,
  icon: Icon,
  variant = "primary",
  size = "normal",
  ...props
}: {
  children: ReactNode;
  icon?: typeof Gauge;
  variant?: "primary" | "secondary" | "ghost" | "danger";
  size?: "normal" | "small";
} & React.ButtonHTMLAttributes<HTMLButtonElement>) {
  return (
    <button className={`button ${variant} ${size}`} {...props}>
      {Icon && <Icon className={props.disabled ? "" : undefined} size={size === "small" ? 15 : 17} />}
      {children}
    </button>
  );
}

function Badge({
  children,
  tone = "muted",
  icon: Icon
}: {
  children: ReactNode;
  tone?: BadgeTone;
  icon?: typeof Gauge;
}) {
  return (
    <span className={`badge ${tone}`}>
      {Icon && <Icon size={13} />}
      {children}
    </span>
  );
}

function StatusLine({
  tone,
  icon: Icon,
  label
}: {
  tone: "success" | "danger" | "muted";
  icon: typeof Gauge;
  label: string;
}) {
  return <div className={`status-line ${tone}`}><Icon size={15} /><span>{label}</span></div>;
}

function StatusBlock({
  icon: Icon,
  label,
  value,
  tone
}: {
  icon: typeof Gauge;
  label: string;
  value: string;
  tone: "success" | "warning" | "danger" | "muted";
}) {
  return (
    <div className={`status-block ${tone}`}>
      <Icon size={19} />
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function IconAction({
  icon: Icon,
  label,
  onClick
}: {
  icon: typeof Gauge;
  label: string;
  onClick: () => void;
}) {
  return <button className="icon-action" aria-label={label} onClick={onClick}><Icon size={17} /></button>;
}

function EventList({ events }: { events: AuditEvent[] }) {
  if (!events.length) return <EmptyState icon={Activity} title="暂无活动" description="设备发现和同步事件将在此出现。" />;
  return (
    <div className="event-list">
      {events.map((event) => (
        <div className="event-row" key={event.event_id}>
          <span className={`event-dot ${event.severity}`} />
          <div>
            <strong>{eventLabel(event.event_type)}</strong>
            <span>{event.source_ip || shortId(event.device_id) || "本机服务"}</span>
          </div>
          <time>{formatTime(event.created_at_ns)}</time>
        </div>
      ))}
    </div>
  );
}

function TransferList({ tasks }: { tasks: TransferTask[] }) {
  if (!tasks.length) return <EmptyState icon={Send} title="传输队列为空" description="手动发送和同步进度将在此显示。" />;
  return (
    <div className="transfer-list">
      {tasks.map((task) => (
        <article className="transfer-item" key={task.task_id}>
          <div className="transfer-head">
            <FileText size={17} />
            <div><strong>{task.file_name}</strong><span>{task.device_name}</span></div>
            <TaskBadge status={task.status} />
          </div>
          <div className="progress-track"><span style={{ width: `${task.progress}%` }} /></div>
          <div className="transfer-meta">
            <span>{formatBytes(task.transferred_bytes)} / {formatBytes(task.total_bytes)}</span>
            <strong>{task.progress.toFixed(0)}%</strong>
          </div>
          {task.error_message && <p className="task-error">{task.error_message}</p>}
        </article>
      ))}
    </div>
  );
}

function AgentResultCard({
  result,
  expanded = false
}: {
  result: AgentResult;
  expanded?: boolean;
}) {
  return (
    <Panel className={`agent-result severity-${result.severity} ${expanded ? "expanded" : ""}`}>
      <div className="agent-result-head">
        <Badge tone={toneForSeverity(result.severity)} icon={Bot}>{severityLabel(result.severity)}</Badge>
        <Badge tone={result.enhanced ? "primary" : "muted"}>{result.enhanced ? "DeepSeek 增强" : "本地规则"}</Badge>
      </div>
      <h3>{result.summary}</h3>
      <div className="agent-columns">
        <ResultList title="检测证据" items={result.evidence} />
        <ResultList title="可能原因" items={result.causes} />
        <ResultList title="处理建议" items={result.recommendations} />
      </div>
      {result.enhancement_note && <p className="enhancement-note">{result.enhancement_note}</p>}
    </Panel>
  );
}

function ResultList({ title, items }: { title: string; items: string[] }) {
  return (
    <div className="result-list">
      <span className="eyebrow">{title}</span>
      {items.length ? <ul>{items.map((item) => <li key={item}>{item}</li>)}</ul> : <p>无</p>}
    </div>
  );
}

function Drawer({
  title,
  onClose,
  children
}: {
  title: string;
  onClose: () => void;
  children: ReactNode;
}) {
  return (
    <>
      <button className="drawer-scrim" onClick={onClose} aria-label="关闭详情" />
      <aside className="drawer">
        <div className="drawer-head"><span>{title}</span><button className="icon-button" onClick={onClose}><X size={19} /></button></div>
        <div className="drawer-body">{children}</div>
      </aside>
    </>
  );
}

function Modal({
  title,
  onClose,
  children
}: {
  title: string;
  onClose: () => void;
  children: ReactNode;
}) {
  return (
    <div className="modal-scrim">
      <section className="modal">
        <div className="drawer-head"><span>{title}</span><button className="icon-button" onClick={onClose}><X size={19} /></button></div>
        <div className="modal-body">{children}</div>
      </section>
    </div>
  );
}

function DefinitionList({ items }: { items: Array<[string, string]> }) {
  return <dl className="definition-list">{items.map(([key, value]) => <div key={key}><dt>{key}</dt><dd>{value}</dd></div>)}</dl>;
}

function VersionCard({
  title,
  version,
  size,
  modified,
  hash,
  winner
}: {
  title: string;
  version: number;
  size: number;
  modified: number;
  hash: string;
  winner: boolean;
}) {
  return (
    <article className={winner ? "version-card winner" : "version-card"}>
      <div><strong>{title}</strong>{winner && <Badge tone="success">主版本</Badge>}</div>
      <DefinitionList items={[
        ["版本", `v${version}`],
        ["大小", formatBytes(size)],
        ["修改时间", formatTime(modified)],
        ["SHA-256", hash]
      ]} />
    </article>
  );
}

function EmptyState({
  icon: Icon,
  title,
  description
}: {
  icon: typeof Gauge;
  title: string;
  description: string;
}) {
  return <div className="empty-state"><Icon size={28} /><strong>{title}</strong><p>{description}</p></div>;
}

function PageLoader() {
  return <div className="page-loader"><LoaderCircle className="spin" size={28} /><span>正在加载节点数据</span></div>;
}

function ErrorState({ message, retry }: { message: string; retry: () => void }) {
  return <div className="page-loader error"><AlertTriangle size={28} /><strong>{message}</strong><Button onClick={retry} icon={RefreshCw}>重试</Button></div>;
}

function InlineError({ message }: { message: string }) {
  return <div className="inline-error"><AlertTriangle size={17} />{message}</div>;
}

function PermissionBadge({ permission }: { permission: Device["permission"] }) {
  const tone = permission === "blocked" ? "danger" : permission === "unpaired" ? "muted" : permission === "admin" ? "warning" : "primary";
  return <Badge tone={tone}>{permission}</Badge>;
}

function TaskBadge({ status }: { status: TransferTask["status"] }) {
  const labels = { queued: "等待", running: "传输中", success: "成功", failed: "失败" };
  const tones = { queued: "muted", running: "primary", success: "success", failed: "danger" } as const;
  return <Badge tone={tones[status]}>{labels[status]}</Badge>;
}

function SyncBadge({ status }: { status: FileEntry["sync_status"] }) {
  const map = {
    synced: ["已同步", "success"],
    pending: ["等待同步", "primary"],
    local_only: ["仅本机", "muted"],
    deleted_record: ["删除记录", "warning"],
    conflict: ["冲突", "danger"],
    error: ["错误", "danger"]
  } as const;
  return <Badge tone={map[status][1]}>{map[status][0]}</Badge>;
}

function formatBytes(value: number): string {
  if (!value) return "0 B";
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  const index = Math.min(Math.floor(Math.log(value) / Math.log(1024)), units.length - 1);
  return `${(value / 1024 ** index).toFixed(index ? 1 : 0)} ${units[index]}`;
}

function formatTime(nanoseconds: number | null): string {
  if (!nanoseconds) return "-";
  const milliseconds = nanoseconds > 10 ** 14 ? nanoseconds / 1_000_000 : nanoseconds * 1000;
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false
  }).format(new Date(milliseconds));
}

function dateTimeToNanoseconds(value: string): number {
  const milliseconds = value ? new Date(value).getTime() : 0;
  return Number.isFinite(milliseconds) ? milliseconds * 1_000_000 : 0;
}

function shortId(value: string): string {
  return value ? `${value.slice(0, 8)}…` : "";
}

function severityLabel(severity: Severity): string {
  return { info: "正常", warning: "需关注", error: "高风险" }[severity];
}

function toneForSeverity(severity: Severity): "success" | "warning" | "danger" {
  return { info: "success", warning: "warning", error: "danger" }[severity] as
    | "success"
    | "warning"
    | "danger";
}

function reasonLabel(code: string): string {
  return {
    BOTH_MODIFIED: "双方均已修改",
    DELETE_MODIFY: "删除与修改并发",
    BASELINE_DIVERGED: "同步基线分歧"
  }[code] || code;
}

function eventLabel(type: string): string {
  return {
    device_online: "设备上线",
    device_offline: "设备离线",
    file_sent: "文件已发送",
    file_received: "文件已接收",
    sync_failed: "同步失败",
    conflict_detected: "检测到冲突",
    security_audit: "安全审计",
    pairing_succeeded: "设备配对成功",
    permission_changed: "权限已更新"
  }[type] || type.replaceAll("_", " ");
}
