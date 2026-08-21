# MPE Postman 数据源插件 (`mpe-plugin-postman`)

[English Version](./README_EN.md)

> **定位**：本仓库是 MPE (Multi-Protocol Flow Executor) 的**官方数据源插件之一**。它基于标准 stdio 上的 JSON-RPC 2.0 协议运行，支持通过 Postman 官方 OpenAPI（云端工作空间与集合）及本地导出的 Postman Collection JSON (v2.1) 文件浏览、分级检索接口，并支持一键拖拽将请求转换为 MPE 画布标准 HTTP 节点模板。

---

## 0. 运行环境与安装说明

### 1. 最终用户（通过 MPE 插件市场安装）
- **零环境依赖**：官方分发包（Windows / Linux / macOS）已通过 CI 自动打包为独立的免环境可执行二进制文件（Windows 上为 `postman.exe`，Linux/macOS 上为 `postman`）。
- **开箱即用**：用户的操作系统**无需预先安装 Python 或配置任何环境变量**，由 MPE 宿主管理生命周期并直接拉起运行。

### 2. 开发者（本地从源码开发与测试）
- **Python 版本要求**：Python 3.8+（已在 Python 3.8 / 3.9 / 3.10 / 3.11 / 3.12 / 3.13 环境下测试通过）。
- **零外部依赖（无需 `pip install`）**：本插件完全基于 Python 标准库开发（仅使用 `urllib.request`、`json`、`argparse`、`sys`、`typing` 等标准模块），无需安装任何第三方 pip 包。
- **本地构建独立二进制（可选）**：如需本地编译二进制产物，只需安装 `pyinstaller`：
  ```bash
  pip install pyinstaller
  pyinstaller --onefile --name postman postman.py
  ```

---

## 1. 架构与工作方式

```
Host (mpe / mpe-cli)                    Plugin process (postman / postman.exe)
   │  扫描 plugins/ 目录                       │
   │  ── describe ───────────────────────────► │  返回双数据源描述 (postman / postman_file)
   │  ◄─────────── datasources spec ───────── │
   │  ── validate_config(config) ────────────► │  校验 API Key 有效性或本地文件可读性
   │  ◄─────────── { valid: true } ─────────── │
   │  ── browse(config, resource, parent_id)─► │  层级浏览：工作空间 -> 集合 -> 请求
   │  ◄─────────── item list (含分组 extra) ── │
   │  ── get_templates(config, item_ids) ────► │  拉取请求并转为 HTTP 节点模板
   │  ◄─────────── node templates ──────────── │
```

- **通信通道**：标准输入/输出（stdin/stdout），每行一个 JSON-RPC 2.0 文档（LF 换行）。
- **驻留模式**：按需拉起（OnDemand），轻量无状态。
- **拖拽导入**：在 MPE 前端数据源面板中浏览 Postman 接口时，直接拖入画布即可生成具备完整 URL、Params、Headers、Raw JSON/XML/Form-data/Urlencoded/GraphQL Body 及认证配置的 HTTP 请求节点。

---

## 2. 项目结构

```
plugins/postman/
├── postman.py                # 插件主逻辑（JSON-RPC 循环 + Postman Client + 本地文件解析器 + 节点转换器 + 自测套件）
├── plugin.json               # 插件清单文件（声明 entry 为 ./postman、kind: datasource、capabilities 等）
├── .gitignore                # Git 忽略配置（忽略 Python 缓存、虚拟环境、打包产物）
├── .github/
│   └── workflows/
│       └── ci.yml            # CI 自动化自测 + PyInstaller 跨平台二进制打包 + Release 发布 + Registry 同步
├── README.md                 # 中文说明文档（默认）
└── README_EN.md              # 英文说明文档
```

---

## 3. 双数据源模式与配置参数

本插件提供两种数据源接入方式：

### 数据源 1：Postman Cloud API (`postman`)
通过 Postman OpenAPI 连接云端工作空间与集合：

| 参数名 | 类型 | 必填 | 默认值 | 说明 |
|---|---|---|---|---|
| `api_key` | string | 是 | - | Postman API Key (`X-Api-Key`) |
| `workspace_id` | string | 否 | - | 工作空间 ID（可选；配置后直接定位该空间） |
| `base_url` | string | 否 | `https://api.getpostman.com` | Postman API 服务基础地址 |

**资源层级**：`工作空间 (workspace)` → `集合 (collection)` → `请求 (request)`

### 数据源 2：Postman 本地集合文件 (`postman_file`)
直接读取本地导出的 Postman Collection v2.1 JSON 文件：

| 参数名 | 类型 | 必填 | 默认值 | 说明 |
|---|---|---|---|---|
| `file_path` | string | 是 | - | 本地 `*.postman_collection.json` 文件的绝对路径 |

**资源层级**：`集合 (collection)` → `请求 (request)`

---

## 4. 特性支持与转换规则

1. **多级目录树状分组**：
   - 递归遍历 Postman Collection 中的 `item`（Folder 目录结构）；
   - 自动生成多级目录路径（如 `用户模块/认证/OAuth2`），并通过 `extra: [{"label": "分组", "value": "..."}]` 注入；
   - 宿主 `DataSourcePanel` 自动渲染为树状可折叠目录。
2. **完整请求体类型转换**：
   - `raw` (JSON / XML / Text / HTML) → 映射为 `body_type: "body"` 与对应 `content_type`；
   - `formdata` → 映射为 `body_type: "form-data"`，自动区分 text 与 file 字段；
   - `urlencoded` → 映射为 `body_type: "x-www-form-urlencoded"`；
   - `file` (Binary) → 映射为 `body_type: "binary"`；
   - `graphql` → 自动包装为 `{ "query": "...", "variables": ... }` 的标准 JSON Body。
3. **变量与 URL 保持**：
   - 完美保留 Postman `{{variable}}` 语法（原生契合 MPE 变量插值引擎）；
   - 自动过滤 `disabled: true` 的 Query 参数与 Headers。
4. **认证 (Auth) 继承与转换**：
   - 支持 Request 级认证及 Collection 级默认认证继承；
   - 支持 Bearer Token、Basic Auth、API Key（Header / Query）。

---

## 5. 本地开发与测试

### 运行内置离线自测试套件
插件内置了完整的 Mock 离线自测套件，覆盖 `describe`、`validate_config`、`browse`、`get_item`、`get_templates` 等全流程：

```bash
# 从源码运行
python3 postman.py --self-test

# 或运行编译后的二进制
./dist/postman --self-test
```

输出示例：
```
[Self-Test] Starting Postman datasource plugin self-test...
  [PASS] describe test passed
  [PASS] validate_config rejection test passed
  [PASS] Postman Cloud mock API & hierarchy test passed
  [PASS] Local Postman Collection file parsing & template conversions passed

[Self-Test] Result: PASS
```

### 标准输入输出交互测试
启动 stdio 循环：
```bash
python3 postman.py
```
输入 JSON-RPC 请求：
```json
{"jsonrpc":"2.0","id":1,"method":"describe","params":{}}
```

---

## 6. 构建与发布

当向本仓库推送 `v*` 标签（例如 `v0.1.0`）时，GitHub Actions 会自动：
1. 运行 `postman.py --self-test` 自动化离线测试；
2. 在 Ubuntu、Windows、macOS 矩阵 Runner 上使用 PyInstaller 编译为原生二进制文件；
3. 打包各平台独立分发包（`postman-<version>-<platform>.zip` 及 `.sha256` 校验和）；
4. 发布 GitHub Release；
5. 自动触发 `multi-protocol-flow/mpe-plugin-registry` 仓库工作流，完成官方插件市场注册表的同步与更新。
