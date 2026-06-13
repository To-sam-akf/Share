**EXE 打包计划**

目标：生成可安装、可卸载、自动打开控制台，并正确配置防火墙的 Windows 10/11 x64 应用。

## 阶段一：确定发布结构

采用：

- PyInstaller `onedir`
- Inno Setup 安装器
- Python 3.11
- Windows x64 构建环境
- 用户数据目录：`%LOCALAPPDATA%\LANSync`
- 共享目录：`%USERPROFILE%\Documents\LANSync`

产物：

```text
LANSync-Setup-x64.exe
```

## 阶段二：调整运行路径

需要让程序区分：

- 安装资源：EXE、前端文件，只读
- 用户数据：配置、SQLite、TLS 证书，可写

计划修改：

- `config.json` 默认写入 `%LOCALAPPDATA%\LANSync`
- `.env` 从用户数据目录加载
- `shared_folder` 默认指向用户文档目录
- 前端资源兼容 PyInstaller 的运行资源路径
- 日志和数据库不得写入 `Program Files`

验收：从任意工作目录启动 EXE 都能正常运行。

## 阶段三：桌面启动体验

增加 Windows 启动入口，负责：

- 防止重复启动
- 启动 FastAPI 服务
- 等待 `127.0.0.1:8765` 可用
- 自动打开默认浏览器
- 启动失败时显示错误窗口
- 可选托盘菜单：打开控制台、打开共享目录、退出

正式版使用 `--noconsole`，调试版保留控制台。

## 阶段四：构建前端

构建流程：

```powershell
cd frontend
npm ci
npm run typecheck
npm test
npm run build
```

将以下资源打入程序：

```text
frontend/dist/index.html
frontend/dist/assets/*
```

验收：断网情况下也能完整打开控制台。

## 阶段五：配置 PyInstaller

创建固定的 `.spec` 文件：

- 入口使用桌面启动器
- 添加 `frontend/dist`
- 收集 LangChain/LangGraph 元数据
- 收集 `cryptography`、`tiktoken`、`pydantic-core`
- 收集 `sqlite-vec`、`xxhash` 等原生组件
- 添加图标和 Windows 版本信息

先构建调试版：

```powershell
pyinstaller --clean LANSync.spec
```

验收：

- 新 Windows 用户环境无需安装 Python
- TLS、SQLite、UDP/TCP、Agent 降级模式正常
- 中文路径和中文用户名可用

## 阶段六：安装程序

使用 Inno Setup 制作安装器：

- 安装到 `%LOCALAPPDATA%\Programs\LANSync`
- 创建开始菜单和桌面快捷方式
- 保留用户配置与共享文件
- 卸载时询问是否删除应用数据
- 检测旧版本并支持覆盖升级
- 安装结束后启动 LANSync

不建议安装到 `Program Files`，这样普通用户更新更方便。

## 阶段七：防火墙规则

安装阶段申请管理员授权，仅添加：

```text
UDP 9000 入站：Private + LocalSubnet
TCP 9001 入站：Private + LocalSubnet
```

规则绑定程序路径，并使用固定名称：

```text
LANSync Discovery
LANSync Transfer
```

卸载时删除对应规则。不要关闭整个 Windows 防火墙。

## 阶段八：测试矩阵

至少测试：

- Windows 10 x64
- Windows 11 x64
- 全新用户环境
- 中文用户名和中文目录
- 无管理员权限启动
- Windows 防火墙开启
- 两台真实设备发现、配对、传输、同步
- 重启、升级、卸载后重装
- 未配置 API Key 时正常使用本地规则
- 10 MB、1 GB 文件及断点续传

## 阶段九：签名与发布

正式发布前：

- 为 EXE 和安装器添加 Authenticode 签名
- 生成 SHA-256 校验值
- 固定版本号
- 保存构建日志
- 在干净虚拟机完成最终验收

## 推荐里程碑

1. **M1**：PyInstaller 调试版可以启动。
2. **M2**：路径和前端资源完全兼容打包环境。
3. **M3**：双设备发现、配对、传输通过。
4. **M4**：Inno Setup 安装与防火墙规则完成。
5. **M5**：签名并生成正式安装包。

预计需要新增构建入口、PyInstaller spec、Inno Setup 脚本和 Windows 构建脚本，并小范围调整配置及资源路径。