# 本地工作区服务

一个独立运行的 FastAPI 服务，提供本地工作区注册、文件清单、模型连接设置和工作区问答。问答由本地 Agent 查找和读取授权材料，由用户选择的模型服务生成回答。文件清单依旧保存最近一次扫描的元数据。

## 安装与启动

需要 Python 3.11 或更高版本。在项目根目录执行：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[test]"
python -m local_workspace_service
```

服务固定监听本机回环地址 `127.0.0.1:8000`：

- 文件清单页面：<http://127.0.0.1:8000/>
- 模型连接设置：<http://127.0.0.1:8000/model-connections>
- 工作区问答：<http://127.0.0.1:8000/agent>
- 交互式 API 文档：<http://127.0.0.1:8000/docs>
- 健康检查：<http://127.0.0.1:8000/health>

默认 SQLite 文件是当前目录的 `data/workspaces.db`。也可以在启动前指定另一个本地文件：

```powershell
$env:LOCAL_WORKSPACE_DB = "D:\LocalWorkspaceData\workspaces.db"
python -m local_workspace_service
```

网页、扫描、凭据管理和任务状态均由本机服务提供。主动测试模型连接或提交云端问答时，后端才会向已保存的目的地址发送请求。

## 模型连接设置

从文件清单页右上角进入“模型连接设置”，或直接打开 <http://127.0.0.1:8000/model-connections>。

页面可以新增、编辑、选择和删除多组本机配置。模型连接属于整个本机应用，不绑定工作区，切换工作区不会改变当前选择。新增配置不会自动成为当前配置，必须点击“设为当前”；同时最多一组配置处于当前状态。删除当前配置后选择会清空，不会自动改选其他配置。

| 协议 | 基础地址示例 | 适配器追加的后缀 |
| --- | --- | --- |
| OpenAI 兼容 Chat Completions | `https://api.openai.com/v1` | `/chat/completions` |
| OpenAI Responses | `https://api.openai.com/v1` | `/responses` |
| Anthropic Messages | `https://api.anthropic.com/v1` | `/messages` |

对代理服务，请填入它要求的完整路径前缀，例如 `https://gateway.example.org/team/v1`。模型名由用户填写。Chat Completions 使用标准的 `max_completion_tokens`，仅接受旧 `max_tokens` 的兼容端点可能需要服务端升级。Responses 使用非流式、`store: false` 的显式上下文和推理关联项。Messages 使用 `anthropic-version: 2023-06-01`。非标准端点会返回协议或响应错误。

协议参考见 [OpenAI 工具调用](https://developers.openai.com/api/docs/guides/function-calling)、[OpenAI Chat Completions](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create) 和 [Anthropic Messages](https://platform.claude.com/docs/en/api/messages/create)。

字段说明：

- **配置名称**：本机显示名称，去除首尾空白后按不区分大小写的规则保持唯一。
- **API 基础地址**：完整 HTTPS 地址，或使用 `localhost`、`127.0.0.0/8` 或 `::1` 的本机 HTTP 地址。按上表追加接口后缀，保留用户填写的路径前缀和 `/v1`。后端验证 TLS 证书，拒绝重定向。
- **模型名称或标识**：使用服务商提供的精确模型标识。保存时只校验字段格式。
- **API Key**：可为空，以兼容无需密钥的本机服务。

Windows 下 API Key 通过系统 Credential Manager 保存。SQLite 只保存非敏感配置、`has_api_key` 状态和不可恢复密钥内容的凭据引用；不会保存密钥明文、脱敏假密钥或可恢复编码。服务不会把密钥写入浏览器 `localStorage`/`sessionStorage`，也没有明文回退或硬编码加密密钥。系统凭据存储不可用时，涉及密钥的操作会明确失败。

编辑配置时不会回填真实密钥：

- 密钥输入框留空表示保留原密钥；
- 输入新密钥表示替换原密钥；
- 只有勾选“清除已保存密钥”才会删除；
- 新密钥和清除请求不能同时提交；
- 保存成功后密码输入框会清空，并显示“已保存，尚未验证连接”。

删除操作会在确认框中显示配置名称。删除当前配置时还会提示删除后没有当前模型连接。配置或凭据删除失败时，页面不会提前移除列表项。

点击 **测试普通对话** 会发送固定短问题，检查是否返回有效文本。点击 **测试工具调用** 会要求模型调用一个返回测试编号的本地工具，将结果按调用 ID 回传，再检查最终文本。测试内容与工作区无关，可能产生费用。两种结果分开显示，仅在当前页面保留并绑定已保存的配置版本。“本次未验证通过”描述本次执行结果。保存、删除和设为当前只改本机配置。

## 页面使用方法

1. 打开首页，在“文件夹绝对路径”中输入已有目录（例如 `D:\项目资料`），点击“设为工作区”。名称可不填，默认使用文件夹名称；支持粘贴资源管理器“复制文件地址”产生的带双引号路径。
2. 注册成功后自动选中新工作区；也可从下拉框选择其他已注册工作区。默认允许 `.md` 和 `.docx`，沿用默认忽略规则、大小和扫描数量限制。目录不存在、无权访问、路径不安全或重复注册时会显示提示并保留输入。注册不创建目录，也不自动扫描；高级策略仍通过 API 设置。
3. 页面只读取 SQLite 中保存的最近一次成功扫描结果，不会因为刷新或切换工作区而自动扫描文件系统。
4. 首次使用时点击“重新扫描”。扫描期间按钮会禁用并显示“正在扫描”，不会显示虚构的百分比。
5. 使用“已收录”“已排除”“扫描错误”三个页签查看结果。每类结果按每页 50 条渲染。
6. “已收录”页签可以按文件名或相对路径筛选，也可以按文件修改时间或大小排序。筛选不是正文搜索。

页面中的“已扫描，尚未读取”描述清单收集的元数据。清单中的 `status: "ready"` 表示已取得元数据，`content_hash` 为 `null`。问答工具使用现行策略重新检查文件，并为实际读取的字节计算 SHA-256。

文件的“最后修改时间”来自文件系统并按浏览器本地时区显示；“最近成功扫描时间”是服务生成这份清单的时间，两者含义不同。

此清单始终只是上次扫描时的快照。当前版本没有文件监听能力，无法自动判断扫描后文件是否变化；本地文件发生变化后，需要点击“重新扫描”刷新清单。

### 如何理解特殊状态

- **尚未扫描**：该工作区从未成功返回过扫描结果；这与“已扫描但结果为空”不同。
- **扫描规则已变化**：保存结果使用的完整策略快照与当前策略不同，需要重新扫描。扫描期间策略发生变化也会正确标记为旧策略结果。
- **收录数量限制**：只有实际出现 `file_limit_exceeded` 排除项时，页面才提示有文件因数量上限被排除。
- **遍历被截断**：`traversal_limit_reached` 为 `true`，表示达到目录项遍历预算，清单不完整。
- **存在单项错误**：扫描整体成功并保存，但个别目录或条目无法安全访问；详情显示在“扫描错误”页签。
- **整次扫描失败**：例如注册根目录失效。失败不会替换上一次成功结果，页面继续显示旧结果并明确提示失败。

## 注册工作区并扫描

`root_path` 必须是已存在的本机绝对目录。Windows 下 UNC 共享、映射网络驱动器以及经过重解析点的路径会被拒绝。

```powershell
$body = @{
  name = "学生会资料"
  root_path = "D:\StudentUnionFiles"
  allowed_extensions = @(".md", ".docx")
  ignored_directories = @(".git", ".idea", ".vscode", "node_modules", "__pycache__", ".venv")
  ignored_files = @("draft*")
  limits = @{
    max_file_size_mb = 10
    max_files_per_scan = 1000
    max_entries_per_scan = 10000
  }
  permissions = @{ read = $true; write = $false; delete = $false; execute = $false }
} | ConvertTo-Json -Depth 5

$workspace = Invoke-RestMethod -Method Post `
  -Uri "http://127.0.0.1:8000/api/v1/workspaces" `
  -ContentType "application/json" -Body $body

Invoke-RestMethod -Method Post `
  -Uri "http://127.0.0.1:8000/api/v1/workspaces/$($workspace.workspace_id)/scan"
```

## 工作区 API

原有接口保持兼容：

| 方法与路径 | 用途 |
| --- | --- |
| `POST /api/v1/workspaces` | 注册一个真实存在的本地目录。 |
| `GET /api/v1/workspaces/{workspace_id}` | 查询工作区配置。 |
| `PUT /api/v1/workspaces/{workspace_id}/policy` | 完整替换扫描策略；省略字段使用模型默认值。 |
| `POST /api/v1/workspaces/{workspace_id}/scan` | 主动扫描，并在完整生成后原子替换最近结果。 |

新增接口：

| 方法与路径 | 用途 |
| --- | --- |
| `GET /api/v1/workspaces` | 返回全部已注册工作区，按名称、创建时间和 ID 稳定排序。 |
| `GET /api/v1/workspaces/{workspace_id}/scan-result` | 返回最近一次保存结果、扫描时策略快照和 `needs_rescan`。 |

从未扫描时，结果接口返回：

```json
{
  "status": "never_scanned",
  "result": null,
  "policy_snapshot": null,
  "needs_rescan": false
}
```

存在结果时 `status` 为 `available`，`result` 包含 `included`、`excluded`、`errors` 和 `summary`，`policy_snapshot` 是扫描开始时使用的策略，`needs_rescan` 表示该快照是否与当前策略不同。未知工作区返回 `404`。

同一服务进程内，同一工作区同时只允许一个扫描。第二个请求返回 `409`，防止较早开始的结果覆盖较新的结果。默认启动方式只使用一个 Uvicorn 工作进程。

## 模型连接 API

| 方法与路径 | 用途 |
| --- | --- |
| `GET /api/v1/model-connections` | 返回 `{items, active_connection_id}`；每项包含 `has_api_key` 和 `is_active`。 |
| `POST /api/v1/model-connections` | 新增配置；不会自动设为当前。 |
| `GET /api/v1/model-connections/{id}` | 查询单组非敏感配置。 |
| `PATCH /api/v1/model-connections/{id}` | 部分更新配置；空 `api_key` 表示保留，`clear_api_key: true` 表示清除。 |
| `PUT /api/v1/model-connections/{id}/activate` | 使用稳定配置 ID 设为当前配置。 |
| `DELETE /api/v1/model-connections/{id}` | 删除系统凭据与 SQLite 配置；若为当前配置则清空选择。 |

协议字段取值分别为 `openai_chat_completions`、`openai_responses` 和 `anthropic_messages`。所有普通响应都不包含 `api_key`、凭据引用或任何可恢复密钥的内容。未知 ID 返回 `404`，名称冲突返回 `409`，系统凭据存储不可用返回 `503`。

模型连接的 GET、保存、切换和删除操作都不会发送模型网络请求。“已保存”只表示配置及凭据已持久化，不表示连接成功、在线或模型可用。

## 扫描安全边界

- 扫描使用已验证的目录句柄并相对父目录打开子目录，不跟随符号链接、Windows junction 或其他重解析点。
- 内置的 `.env`、`*.key`、`*.pem`、`*secret*`、`*token*` 始终生效，即使配置 `ignored_files: []`。
- 扩展名和忽略规则不区分大小写；允许扩展名只能是 `.md` 和 `.docx`。
- `max_file_size_mb` 限制单文件大小，`max_files_per_scan` 限制收录数量，`max_entries_per_scan` 限制本次处理的目录项数量。
- 被忽略目录只记录目录本身并剪枝，不枚举其内部文件。
- 页面只通过本地 API 获取数据，不直接访问文件系统或 SQLite；文件名、路径和错误文本均作为普通文本渲染。

## 工作区问答

1. 保存连接，分别运行普通对话和工具调用测试，并点击“设为当前”。
2. 在首页注册材料目录，再进入“工作区问答”选择该目录。问答按当前规则现场查找资料，历史清单用于查看扫描结果。
3. 核对页面上的连接和外发目的地，点击“启用云端问答”。问题和必要的文件清单、元数据或原文片段会发送给所选模型服务。后端按工作区、连接 ID、版本和地址校验授权。连接版本或地址变更后重新核对并启用。
4. 输入一个问题，例如“会议记录有哪些待办？” 提交后页面显示实际请求模型、查找资料和读取文件的事件。
5. 点击回答下的引用查看回答时发送给模型的文本片段、相对路径、定位和 SHA-256。引用 ID 必须来自本次任务。出处对应真实片段，结论仍应结合原文判断。

| 方法与路径 | 用途 |
| --- | --- |
| `POST /api/v1/model-connections/{id}/test-chat` | 用已保存的配置执行普通对话测试。 |
| `POST /api/v1/model-connections/{id}/test-tools` | 执行固定工具及结果回传测试。 |
| `POST /api/v1/agent/cloud-enable` | 用 `workspace_id`、`connection_id` 和页面看到的 `connection_version` 确认外发目的地。 |
| `POST /api/v1/agent/tasks` | 传 `workspace_id`、`question`、`request_key`、`connection_id` 和 `connection_version`，返回 202 和任务 ID。同一个请求编号重复提交时返回原任务，编号用于其他工作区或问题时返回 409。 |
| `GET /api/v1/agent/tasks/{task_id}` | 查询等待、执行中、成功、失败或达到限制，以及进度与结果。 |
| `GET /api/v1/agent/tasks/{task_id}/sources/{source_id}` | 查看已完成任务的有效引用及回答时片段。 |

未授权的提交返回 403，预期连接或版本变化时返回 409。每个任务固定提交时的连接参数和凭据。模型调用使用非流式 HTTP，连接失败可由用户手动重试。问答每次独立执行。任务及引用最多保留 50 条，完成后保存 1 小时，在查询或新建时清理过期项。服务重启后授权、任务和回答记录清空。

### 文档范围和默认预算

- Markdown 支持 UTF-8 及 BOM，定位到原文行号。Word 支持 `.docx` 正文段落和顶层表格的单元格定位。图片、批注、文本框、嵌套表格、页眉页脚和页码暂不支持。解析仅读取正文 XML，外部链接和宏不会执行。
- 文件从已验证的父目录句柄相对打开，校验并读取同一文件句柄。拒绝链接、junction、重解析点和多硬链接文件。单文档上限为工作区规则与 20 MiB 的较小值，Markdown 最多 20 万行。检测到读中变化时重试一次。版本哈希只对应本次读取的字节；文件系统元数据校验无法保证操作系统级原子快照。
- Word 上限为 1,000 个 ZIP 条目、声明解压总量 50 MiB、正文 XML 20 MiB、20 万元素及 128 层嵌套。拒绝 DTD、加密文档及正文的非 Store/Deflate 压缩。
- 文件列表每页最多 100 条。分段读取默认 12,000 字符、最多 30,000，按工具返回的片段索引和片段内字符偏移继续。关键词搜索按字面进行大小写折叠，支持相对路径范围。最多尝试 200 个文件、读取 50 MiB、检查 200 万字符、返回 50 条及 30,000 字符。扫描和匹配共用 10 秒预算。遇到单文档错误、扫描限制或搜索截断时，会提供覆盖说明。文件系统操作无法即时打断，检查点会阻止超时后继续处理。
- 每个任务最多 6 次模型请求、8 次串行工具执行、120 秒总时限。单请求最多 30 秒及 4 MiB 响应。问题最多 4,000 字符，工具结果累计最多 80,000 字符，模型输出上限为每次 4,096 token，最终回答最多 30,000 字符。同时最多执行 4 个问答任务。

四个只读工具分别是 `list_workspace_files`、`get_file_metadata`、`read_workspace_file`、`search_workspace`。任务在后端绑定工作区，工具接受相对路径、关键词和有界分页参数。审计只保留任务编号、工具、适用的相对路径、耗时和结果状态。

## SQLite 兼容升级与结果保存

启动时使用 `CREATE TABLE IF NOT EXISTS` 增加最近扫描结果、模型连接和应用设置表，不删除或重建原有 `workspaces` 表。每个工作区只保留一份最近结果，不保存历史版本。

扫描结果和策略快照先在内存完整序列化，再通过单条 SQLite UPSERT 在事务中替换。因此查询者只会看到完整的旧结果或完整的新结果。根目录失效等整次扫描失败不会执行替换。

## 测试

```powershell
python -m pytest -ra
```

`pytest` 会在检测到 Node.js 时执行前端异步状态回归测试；没有 Node.js 时只跳过该项。也可以直接运行，无需安装 npm 包：

```powershell
node --test tests/frontend_state.test.cjs tests/model_connections_frontend.test.cjs tests/agent_frontend.test.cjs
```

前端测试以模拟请求和轻量 DOM 覆盖工作区切换、模型设置并发、分开的连接测试状态、重复提交、失败恢复和引用查看。

模型协议和问答测试使用可注入的模拟传输、临时工作区、临时 SQLite 数据库及内存假凭据。Windows 上创建符号链接需要对应系统权限，缺少权限时相关测试会跳过。真实服务需按工作区问答步骤手动验收。
