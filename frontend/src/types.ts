export type Severity = "info" | "warning" | "error";

export interface RuntimeStatus {
  running: boolean;
  started_at_ns: number | null;
  device_id: string;
  device_name: string;
  local_ip: string;
  udp_port: number;
  tcp_port: number;
  web_port: number;
  shared_folder: string;
  tls_enabled: boolean;
  certificate_fingerprint: string | null;
  sync_enabled: boolean;
  sync_interval_seconds: number;
  pending_restart: boolean;
}

export interface AgentResult {
  agent: string;
  summary: string;
  severity: Severity;
  evidence: string[];
  causes: string[];
  recommendations: string[];
  facts: Record<string, unknown>;
  enhanced: boolean;
  enhancement_note: string;
  source: "local" | "deepseek";
}

export type AgentRunStatus =
  | "queued"
  | "running"
  | "waiting_approval"
  | "completed"
  | "rejected"
  | "failed";

export interface AgentStep {
  created_at_ns: number;
  kind: string;
  name: string;
  status: string;
  input: unknown;
  output: unknown;
}

export interface SyncPlanAction {
  action_id: string;
  direction: "upload" | "download" | "same" | "conflict" | "delete_report";
  relative_path: string;
  bytes: number;
  reason: string;
  executable: boolean;
  status: "pending" | "running" | "success" | "failed" | "reported";
  transferred_bytes: number;
  error_code: string;
  error_message: string;
}

export interface SyncPlan {
  plan_id: string;
  device_id: string;
  device_name: string;
  path_prefix: string;
  counts: Record<string, number>;
  total_bytes: number;
  risks: string[];
  status: string;
  actions: SyncPlanAction[];
  verification: {
    success: boolean;
    checks: Array<Record<string, unknown>>;
  } | null;
}

export interface AgentRun {
  run_id: string;
  thread_id: string;
  request: string;
  status: AgentRunStatus;
  plan_id: string | null;
  report: string;
  error: string;
  created_at_ns: number;
  updated_at_ns: number;
  steps: AgentStep[];
  plan: SyncPlan | null;
  messages: AgentMessage[];
}

export interface AgentMessage {
  run_id: string;
  role: "user" | "assistant";
  content: string;
  created_at_ns: number;
}

export interface AgentThreadSummary {
  thread_id: string;
  title: string;
  latest_run_id: string;
  status: AgentRunStatus | "";
  run_count: number;
  created_at_ns: number;
  updated_at_ns: number;
}

export interface AgentThreadDetail {
  thread: AgentThreadSummary;
  messages: AgentMessage[];
  latest_run: AgentRun | null;
  next_cursor: string | null;
}

export interface AgentThreadPage {
  items: AgentThreadSummary[];
  next_cursor: string | null;
}

export interface AuditEvent {
  event_id: number;
  created_at_ns: number;
  event_type: string;
  severity: Severity;
  source_ip: string;
  device_id: string;
  request_type: string;
  outcome: string;
  bytes_count: number;
  details: Record<string, unknown>;
}

export interface TransferTask {
  task_id: string;
  kind: string;
  status: "queued" | "running" | "success" | "failed";
  file_name: string;
  device_id: string;
  device_name: string;
  total_bytes: number;
  transferred_bytes: number;
  progress: number;
  created_at_ns: number;
  updated_at_ns: number;
  error_code: string;
  error_message: string;
}

export interface DashboardData {
  runtime: RuntimeStatus;
  counts: {
    online_devices: number;
    paired_devices: number;
    files: number;
    unresolved_conflicts: number;
    risk_alerts: number;
  };
  security: AgentResult;
  recent_events: AuditEvent[];
  transfers: TransferTask[];
}

export interface Device {
  device_id: string;
  device_name: string;
  online: boolean;
  status: string;
  ip: string;
  tcp_port: number | null;
  last_seen: number | null;
  tls_enabled: boolean;
  paired: boolean;
  permission: "unpaired" | "read" | "write" | "admin" | "blocked";
  paired_at_ns: number | null;
  last_authenticated_at_ns: number | null;
  certificate_fingerprint: string | null;
  last_sync_at_ns: number | null;
  stranger: boolean;
}

export interface FileEntry {
  relative_path: string;
  file_name: string;
  file_size: number;
  modified_time_ns: number;
  file_hash: string;
  version: number;
  source_device_id: string;
  source_device_name: string;
  status: "active" | "deleted";
  changed_at_ns: number;
  sync_status:
    | "synced"
    | "pending"
    | "local_only"
    | "deleted_record"
    | "conflict"
    | "error";
  preview_kind: "pdf" | "image" | "text" | null;
}

export interface Conflict {
  conflict_id: number;
  relative_path: string;
  local_version: number;
  remote_version: number;
  local_hash: string;
  remote_hash: string;
  local_device_id: string;
  remote_device_id: string;
  winner_device_id: string;
  conflict_copy_path: string;
  conflict_at_ns: number;
  local_size: number;
  remote_size: number;
  local_modified_time_ns: number;
  remote_modified_time_ns: number;
  local_status: string;
  remote_status: string;
  reason_code: string;
  resolved_at_ns: number | null;
  resolution_note: string;
}

export interface AuditAlert {
  alert_id: number;
  created_at_ns: number;
  rule_code: string;
  severity: Severity;
  source_key: string;
  message: string;
  event_count: number;
  read: boolean;
}

export interface SettingsData {
  active: Record<string, string | number | boolean>;
  configured: Record<string, string | number | boolean>;
  pending_restart: boolean;
  restart_fields: string[];
  immediate_fields: string[];
  deepseek_api_key_configured: boolean;
  openai_api_key_configured: boolean;
}
