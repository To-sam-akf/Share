# 局域网文件共享与同步系统阶段一实现记录

记录日期：2026-06-05

## 一、阶段目标

阶段一目标是完成最小可运行版本，实现同一局域网内设备发现和普通单文件传输。

本阶段完成内容：

- 完成配置文件。
- 实现 UDP 广播设备发现。
- 实现 TCP 文件接收服务端。
- 实现 TCP 文件发送客户端。
- 实现单文件发送。
- 实现接收端保存文件。
- 提供命令行菜单用于查看设备和发送文件。

本阶段未实现内容：

- SHA-256 哈希校验。
- 分块校验。
- 断点续传。
- 文件夹自动同步。
- SQLite 文件索引。
- TLS 加密传输。
- 权限控制和配对码。

以上功能属于后续阶段。

## 二、本次修改文件

### 1. 配置文件

新增 `config.json`：

```json
{
  "device_name": "PC-A",
  "udp_port": 9000,
  "tcp_port": 9001,
  "broadcast_ip": "255.255.255.255",
  "shared_folder": "./shared_folder",
  "chunk_size": 1048576,
  "enable_tls": false
}
```

字段说明：

- `device_name`：当前设备名称，两台电脑测试时建议设置为不同名称。
- `udp_port`：UDP 广播发现端口，默认 `9000`。
- `tcp_port`：TCP 文件传输端口，默认 `9001`。
- `broadcast_ip`：广播地址，默认 `255.255.255.255`。
- `shared_folder`：接收文件保存目录，默认 `./shared_folder`。
- `chunk_size`：文件发送时每次读取的字节数，默认 1MB。
- `enable_tls`：是否启用 TLS，本阶段固定为 `false`，暂不启用。

新增 `config.py`：

- 负责读取 `config.json`。
- 如果配置文件不存在，会自动创建默认配置。
- 自动创建 `shared_folder` 接收目录。
- 将相对路径解析为项目目录下的实际路径。

### 2. UDP 设备发现模块

新增 `discovery/udp_discovery.py`：

- 周期性发送 UDP 广播消息。
- 监听 UDP 广播端口。
- 接收其他设备发送的发现消息。
- 维护在线设备列表。
- 自动过滤本机设备，避免自己发现自己。
- 自动清理长时间未收到广播的设备。

UDP 广播消息格式：

```json
{
  "type": "DISCOVERY",
  "device_id": "设备唯一标识",
  "device_name": "PC-A",
  "ip": "192.168.1.10",
  "tcp_port": 9001,
  "status": "online"
}
```

新增 `discovery/__init__.py`，用于导出发现模块的主要类和函数。

### 3. TCP 文件传输模块

新增 `transfer/protocol.py`：

- 定义 TCP 应用层协议的元信息读写方式。
- 协议格式为：`4 字节 JSON 元信息长度 + JSON 元信息 + 原始文件字节`。
- JSON 元信息最大限制为 64KB，避免异常数据占用过多内存。

新增 `transfer/tcp_server.py`：

- 启动 TCP 服务端监听文件传输端口。
- 接收客户端连接。
- 读取文件元信息。
- 按文件大小接收原始字节并写入接收目录。
- 接收成功后向发送端返回确认消息。
- 接收失败时删除未完成文件，避免留下损坏文件。
- 保存文件时只使用 basename，防止路径穿越。
- 若接收目录已有同名文件，自动保存为 `文件名_1.ext`、`文件名_2.ext`，避免覆盖。

新增 `transfer/tcp_client.py`：

- 连接目标设备 TCP 端口。
- 发送文件元信息。
- 按 `chunk_size` 读取本地文件并发送。
- 等待接收端返回传输结果。

TCP 文件元信息格式：

```json
{
  "type": "FILE_SEND",
  "file_name": "test.txt",
  "file_size": 1024,
  "chunk_size": 1048576
}
```

接收端成功响应格式：

```json
{
  "type": "FILE_RECEIVED",
  "status": "success",
  "file_name": "test.txt",
  "bytes_received": 1024
}
```

新增 `transfer/__init__.py`，用于导出 TCP 服务端和发送函数。

### 4. 命令行入口

修改 `main.py`：

- 启动时读取配置。
- 启动 TCP 文件接收服务端。
- 启动 UDP 发现服务。
- 显示设备名称、本机 IP、UDP 端口、TCP 端口和接收目录。
- 提供命令行菜单：
  - `1. 查看在线设备`
  - `2. 发送单个文件`
  - `0. 退出`
- 退出时停止 UDP 发现线程和 TCP 服务端线程。

### 5. 单元测试

新增 `tests/test_stage_one.py`：

- 测试配置加载和接收目录创建。
- 测试本机回环 TCP 文件发送。
- 测试接收端遇到同名文件时不会覆盖原文件。

新增 `tests/__init__.py`，用于测试包初始化。

## 三、系统整体流程

### 1. 启动流程

1. 用户执行：

```bash
uv run python main.py
```

2. 程序读取 `config.json`。
3. 程序创建 `shared_folder` 接收目录。
4. 程序启动 TCP 服务端，监听 `tcp_port`。
5. 程序启动 UDP 监听线程，监听 `udp_port`。
6. 程序启动 UDP 广播线程，周期性向局域网发送设备信息。
7. 程序进入命令行菜单。

### 2. 设备发现流程

1. 每台设备启动后，每隔约 3 秒发送 UDP 广播。
2. 广播内容包含设备 ID、设备名称、IP 地址、TCP 端口和在线状态。
3. 其他设备收到广播后解析 JSON 数据。
4. 如果该设备不是本机，则加入在线设备列表。
5. 用户在菜单中选择 `1`，即可查看当前发现的在线设备。
6. 如果超过一定时间未收到某设备广播，该设备会被清理出在线列表。

### 3. 文件发送流程

1. 用户在菜单中选择 `2`。
2. 程序显示当前在线设备列表。
3. 用户输入目标设备编号。
4. 用户输入本地待发送文件路径。
5. 客户端连接目标设备的 TCP 端口。
6. 客户端发送文件元信息。
7. 客户端按 `chunk_size` 读取并发送文件内容。
8. 接收端保存文件到 `shared_folder`。
9. 接收端返回成功或失败响应。
10. 发送端在命令行显示发送结果。

### 4. 文件接收流程

1. TCP 服务端等待客户端连接。
2. 收到连接后读取 4 字节元信息长度。
3. 根据长度读取 JSON 元信息。
4. 校验请求类型是否为 `FILE_SEND`。
5. 从元信息中取得文件名和文件大小。
6. 将文件保存到 `shared_folder`。
7. 如果同名文件已存在，生成新的文件名，避免覆盖。
8. 如果连接中断或接收失败，删除未完成文件。
9. 接收成功后返回 `FILE_RECEIVED` 响应。

## 四、操作说明

### 1. 单机启动

在项目目录 `/home/sanmu/Share` 下运行：

```bash
uv run python main.py
```

启动后会看到类似输出：

```text
局域网文件共享系统已启动
设备名称：PC-A
本机 IP：192.168.1.10
UDP 发现端口：9000
TCP 接收端口：9001
接收目录：shared_folder
```

### 2. 两台电脑联调

1. 将项目复制到两台处于同一局域网的电脑。
2. 分别修改两台电脑的 `config.json`：

第一台：

```json
"device_name": "PC-A"
```

第二台：

```json
"device_name": "PC-B"
```

3. 两台电脑都运行：

```bash
uv run python main.py
```

4. 在任意一台电脑菜单中输入：

```text
1
```

5. 如果能看到另一台电脑的设备名称、IP 和 TCP 端口，说明 UDP 发现成功。

### 3. 发送文件

1. 在发送端菜单中输入：

```text
2
```

2. 程序会显示在线设备列表。
3. 输入目标设备编号。
4. 输入要发送的文件路径，例如：

```text
/home/sanmu/Desktop/test.txt
```

5. 发送成功后，接收端的 `shared_folder` 目录中会出现该文件。

### 4. 退出程序

在菜单中输入：

```text
0
```

程序会停止 UDP 发现和 TCP 服务端，然后退出。

也可以使用 `Ctrl+C` 中断，程序会执行清理逻辑后退出。

## 五、测试结果

已执行单元测试：

```bash
UV_CACHE_DIR=/tmp/uv-cache uv run python -m unittest
```

测试结果：

```text
Ran 3 tests in 2.018s
OK
```

说明：

- 普通沙箱环境禁止创建 socket，因此第一次运行回环 TCP 测试时出现 `PermissionError: [Errno 1] Operation not permitted`。
- 随后在授权的沙箱外环境中运行同一测试命令，测试全部通过。

已执行语法编译检查：

```bash
UV_CACHE_DIR=/tmp/uv-cache uv run python -m py_compile main.py config.py discovery/udp_discovery.py transfer/protocol.py transfer/tcp_client.py transfer/tcp_server.py tests/test_stage_one.py
```

检查结果：通过，无语法错误。

## 六、注意事项

- 两台设备必须处于同一局域网。
- 防火墙需要允许 UDP `9000` 端口和 TCP `9001` 端口。
- 如果 `255.255.255.255` 广播被路由器或系统拦截，可以在 `config.json` 中将 `broadcast_ip` 改为具体网段广播地址，例如 `192.168.1.255`。
- 如果两台设备在同一台电脑上测试，需要修改其中一个实例的 `tcp_port` 和 `udp_port`，避免端口冲突。
- 本阶段只保证普通文件传输成功，不做哈希校验；文件完整性校验将在阶段二实现。
- 当前接收端只保存文件 basename，不保留原始路径目录结构。
- 接收目录已有同名文件时不会覆盖，会自动生成新文件名。

## 七、阶段一验收方式

验收步骤：

1. 两台电脑连接同一局域网。
2. 两台电脑分别运行 `uv run python main.py`。
3. 两端菜单中选择 `1` 查看在线设备。
4. 确认设备 A 能看到设备 B，设备 B 能看到设备 A。
5. 在设备 A 上选择 `2` 发送一个文本文件到设备 B。
6. 检查设备 B 的 `shared_folder` 中是否出现该文本文件。
7. 打开文件确认内容正确。

达到以上结果，即表示阶段一完成。
