# 局域网文件共享与同步系统

这是一个使用 Python Socket 实现的局域网文件共享与同步系统。当前包含阶段一至七的
网络核心、LangGraph ReAct Agent，以及仅绑定本机的 React + FastAPI 管理控制台：

![front end](./pic/frontier.png)
- UDP 广播发现局域网设备。
- TCP 单文件发送、SHA-256 校验、分块传输和断点续传。
- 递归扫描共享目录并使用 SQLite 保存文件索引。
- 在线设备逐条交换文件索引并比较路径、哈希和修改时间。
- 自动双向同步新增及修改文件，并保留原始相对目录结构。
- 使用删除墓碑记录本地删除，阻止旧副本立即恢复。
- 同步路径安全校验及同名不同目录文件的独立续传状态。
- 使用共同同步基线检测双端修改并生成冲突副本。
- 使用配对码、访问令牌和四级设备权限保护网络操作。
- 使用 TLS 1.2+ 加密配对、文件传输、索引交换和自动同步。
- 记录结构化访问日志并检测认证失败、异常连接和批量访问。
- 提供连接诊断、冲突分析和安全审计工具。
- 使用 LangGraph 编排设备发现、索引比较、计划审批、双向传输与哈希验证。
- Agent 写操作必须经过人工审批，删除差异和冲突默认只报告、不自动覆盖。
- 提供设备、同步、冲突、安全、Agent、日志与设置七个 Web 功能区。
- 使用 SameSite 本机会话、CSRF 请求头和 WebSocket 实时事件保护控制台写操作。

## 运行

需要 Python 3.11 及 `uv`：

```bash
cd frontend
npm ci
npm run build
cd ..
uv run python main.py
```

浏览器访问 `http://127.0.0.1:8765`。FastAPI 会直接提供
`frontend/dist` 中的生产构建，Web Host 固定为 `127.0.0.1`。

需要使用原命令行菜单时：

```bash
uv run python main.py --cli
```

两台设备应处于同一局域网，并使用相同的 UDP 端口。`config.json` 示例：

```json
{
  "device_name": "PC-A",
  "udp_port": 9000,
  "tcp_port": 9001,
  "broadcast_ip": "255.255.255.255",
  "shared_folder": "./shared_folder",
  "chunk_size": 1048576,
  "enable_tls": true,
  "sync_enabled": true,
  "sync_interval_seconds": 10,
  "agent_api_url": "https://api.deepseek.com",
  "agent_provider": "deepseek",
  "agent_model": "deepseek-v4-flash",
  "agent_timeout_seconds": 20,
  "security_audit_interval_seconds": 600,
  "audit_log_retention_days": 30,
  "shared_size_risk_bytes": 10737418240,
  "shared_file_count_risk": 10000,
  "web_port": 8765
}
```

`sync_enabled` 控制自动同步和同步请求接收，`sync_interval_seconds` 控制扫描间隔。
防火墙需要允许配置的 UDP 和 TCP 端口。

## TLS 与配对

启用 TLS 后，程序首次启动会在
`shared_folder/.lan-sync/tls/` 自动生成 ECDSA 自签名证书和私钥。TCP 服务最低使用
TLS 1.2，UDP 设备发现仍为明文广播。

配对成功时双方会保存对端证书的 SHA-256 指纹。后续证书不匹配时连接会被拒绝，并提示
核实设备后重新配对。阶段五创建的授权记录没有证书指纹，因此升级并启用 TLS 后需要
重新配对一次。

`enable_tls=false` 可用于旧测试和兼容场景，但安全审计会将明文传输标记为高风险。
阶段七协议版本为 `7`，新增经过认证的 `FILE_FETCH_REQUEST` 分块下载；参与配对、
传输和同步的节点需要同时升级。

## 同步与冲突

启动时程序会扫描共享目录，并将索引保存到
`shared_folder/.lan-sync/index.sqlite3`。索引记录相对路径、大小、修改时间、
SHA-256、版本、来源设备和状态。阶段四数据库会在启动时自动迁移。

同步流程：

1. 后台线程定期扫描目录并记录新增、修改和删除。
2. 自动同步跳过未配对或已阻止的设备。
3. 双方交换文件索引及各自记录的共同同步基线。
4. 只有一端偏离基线时，正常同步该端版本。
5. 两端都偏离基线时判定为冲突，并确定唯一主版本。
6. 接收端先保留本地失败版本，再原子保存主版本。
7. 同步完成后记录双方版本、共同哈希和最后同步时间。

主版本按修改时间、来源设备 ID 和版本号确定。失败版本使用稳定名称保存，例如：

```text
实验报告_PC-B_9f3a21c0_冲突副本_a12bc34d.docx
```

冲突副本作为普通文件继续同步，因此两端最终都会保留主文件和冲突版本。相同冲突不会
重复生成副本。阶段五仍不向远端执行删除；删除与修改并发时保留活动文件并记录冲突。

## 配对与权限

首次启动会生成并持久化六位配对码，保存在
`shared_folder/.lan-sync/security.sqlite3`。在一台设备的菜单中选择配对并输入
目标设备显示的配对码，即可一次建立双向配对。双方保存同一个随机访问令牌，新设备
默认权限为 `write`。

- `read`：允许请求文件索引。
- `write`：包含 `read`，允许手动发送和自动同步。
- `admin`：允许全部当前网络操作。
- `blocked`：拒绝全部受保护请求及重新配对。

菜单支持查看授权设备、修改权限、解除授权和重新生成配对码。换码不会撤销已有令牌。
访问令牌只在 TLS 加密连接内发送；日志不会记录访问令牌、配对码、API Key 或私钥。

## 访问日志与异常检测

审计事件写入：

- `shared_folder/.lan-sync/audit.sqlite3`
- `shared_folder/.lan-sync/access.log`

文本日志达到 5 MiB 后自动轮转，数据库按 `audit_log_retention_days` 清理。默认检测：

- 5 分钟内 5 次认证或配对失败。
- 1 分钟内 10 次畸形请求。
- 1 分钟内 30 次连接。
- 5 分钟内 60 次索引读取。
- 1 小时内传输 100 个文件或 5 GiB。
- 已阻止设备再次访问。

检测只生成告警，不会自动封禁设备。同一来源和规则在 10 分钟内只生成一条告警。

## ReAct Agent

Web 控制台支持用自然语言发起指定设备、指定目录的双向同步。LangGraph 工作流为：

1. Planner 使用只读工具发现设备、读取索引并分析连接、冲突和安全状态。
2. 确定性同步内核生成包含上传、下载、冲突和删除差异的不可变计划。
3. 工作流通过 LangGraph `interrupt` 暂停，等待用户批准。
4. 批准后执行分块上传和下载，再重新读取双方索引验证 SHA-256。
5. 验证失败最多重新规划两次，最终输出结构化报告。

可调用工具包括 `discover_devices`、`diagnose_connection`、`list_local_files`、
`list_remote_files`、`compare_file_indexes`、`analyze_conflicts`、
`run_security_audit`、`generate_sync_plan`、`execute_sync_plan`、
`verify_sync_plan` 和 `get_transfer_status`。

连接诊断、冲突分析和安全审计仍保留为快捷任务及 CLI 入口。没有 API Key、断网或模型
失败时，系统使用确定性规划和本地规则继续工作。默认使用 DeepSeek，也可以将
`agent_provider`、`agent_api_url` 和 `agent_model` 改为 OpenAI 配置。设置 API Key：

```bash
export DEEPSEEK_API_KEY="新生成的密钥"
# 使用 OpenAI provider 时：
export OPENAI_API_KEY="新生成的密钥"
uv run python main.py
```

不要把 Key 写入 `config.json` 或提交到仓库。Agent checkpoint 和运行记录保存在
`shared_folder/.lan-sync/agent-checkpoints.sqlite3` 与 `agent-runs.sqlite3`。

Web 控制台的 Agent 页面支持多会话历史。每个会话可继续对话、重命名和永久删除；
同一会话有任务处于排队、执行或等待审批状态时，不能发送下一轮消息或删除该会话。
会话列表和单会话历史均使用游标分页，默认每次读取最近 50 轮。

## 手动发送

命令行菜单仍支持选择已配对设备并发送任意单个文件。手动发送只使用文件 basename，
接收目录已有同名文件时自动生成 `_1`、`_2` 等名称，不会被同步覆盖逻辑影响。

## 打开与下载共享文件

Web 控制台的“同步中心”可以打开或另存共享目录中的活动文件。PDF、常见位图、纯文本
和 JSON 会在浏览器新标签页预览；HTML、SVG、Office、压缩包和其他类型始终作为附件
下载，避免共享内容在控制台同源环境中执行。

Chrome 和 Edge 会优先显示系统文件保存选择器，可将文件直接保存到其他目录。浏览器
不支持文件保存选择器时会回退到普通下载，最终目录由浏览器下载设置决定。

## 测试

```bash
UV_CACHE_DIR=/tmp/uv-cache uv run python -m unittest discover -s tests -v
cd frontend
npm test
npm run typecheck
npm run build
npm run test:e2e
```

测试覆盖阶段一至七，包括大文件分块、上传/下载断点续传、目录索引、数据库迁移、删除墓碑、
路径穿越、权限矩阵、TLS 文件传输与同步、证书指纹绑定、审计脱敏、异常阈值、Agent
诊断、ReAct 审批恢复和模型降级；控制台测试覆盖 CSRF、配置生效策略、上传清理、
统一传输任务、日志筛选、Agent 计划审批、步骤时间线与七个功能区导航。
