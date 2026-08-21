# MPE Postman Datasource Plugin (`mpe-plugin-postman`)

[中文版本](./README.md)

> **Role**: This repository is one of the **official datasource plugins** for MPE (Multi-Protocol Flow Executor). Running over standard stdio JSON-RPC 2.0, it integrates with Postman OpenAPI (Cloud Workspaces & Collections) and local Postman Collection v2.1 JSON files, providing hierarchical API browsing, search, and one-click drag-and-drop conversion into standard MPE HTTP node templates.

---

## 0. Runtime & Installation

### 1. End Users (via MPE Plugin Market)
- **Zero Environment Dependencies**: Official distribution packages (Windows / Linux / macOS) are compiled into standalone native executables (`postman.exe` on Windows, `postman` on Linux/macOS) via GitHub Actions CI.
- **Out of the Box**: No Python installation or environment variables required on the host system. Lifecycle is managed automatically by MPE.

### 2. Developers (Local Development & Testing)
- **Python Version**: Python 3.8+ (tested on Python 3.8, 3.9, 3.10, 3.11, 3.12, 3.13).
- **Zero External Dependencies (No `pip install`)**: Built entirely on standard library modules (`urllib.request`, `json`, `argparse`, `os`, `sys`, `typing`).
- **Building Local Standalone Binary (Optional)**:
  ```bash
  pip install pyinstaller
  pyinstaller --onefile --name postman postman.py
  ```

---

## 1. Architecture & Workflow

```
Host (mpe / mpe-cli)                    Plugin process (postman / postman.exe)
   │  Scan plugins/ directory                  │
   │  ── describe ───────────────────────────► │  Return dual datasource specs (postman / postman_file)
   │  ◄─────────── datasources spec ───────── │
   │  ── validate_config(config) ────────────► │  Validate API Key or file readability
   │  ◄─────────── { valid: true } ─────────── │
   │  ── browse(config, resource, parent_id)─► │  Hierarchical browsing: workspace -> collection -> request
   │  ◄─────────── item list (with folders) ── │
   │  ── get_templates(config, item_ids) ────► │  Convert request into HTTP node templates
   │  ◄─────────── node templates ──────────── │
```

- **Transport**: Standard input/output (stdin/stdout), one JSON-RPC 2.0 frame per line (LF delimited).
- **Process Model**: OnDemand invocation, lightweight and stateless.
- **Drag & Drop**: Dragging endpoints from the MPE sidebar into the canvas generates fully configured HTTP nodes (URL, Params, Headers, Body, Auth).

---

## 2. Project Structure

```
plugins/postman/
├── postman.py                # Main logic (JSON-RPC loop + Postman Client + Collection parser + template converter + self-test)
├── plugin.json               # Plugin manifest (entry: ./postman, kind: datasource, capabilities)
├── .gitignore                # Git ignore rules
├── .github/
│   └── workflows/
│       └── ci.yml            # CI workflow (offline self-test + PyInstaller multi-OS build + GitHub Release + Registry sync)
├── README.md                 # Chinese documentation
└── README_EN.md              # English documentation
```

---

## 3. Dual Datasource Configuration

### Datasource 1: Postman Cloud API (`postman`)
Connect to Postman Cloud workspaces and collections via OpenAPI:

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `api_key` | string | Yes | - | Postman API Key (`X-Api-Key`) |
| `workspace_id` | string | No | - | Specific Workspace ID (optional; focuses on this workspace directly) |
| `base_url` | string | No | `https://api.getpostman.com` | Postman API Base URL |

**Resource Hierarchy**: `workspace` → `collection` → `request`

### Datasource 2: Postman Local Collection File (`postman_file`)
Directly parse local Postman Collection v2.1 JSON files:

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `file_path` | string | Yes | - | Absolute path to local `*.postman_collection.json` file |

**Resource Hierarchy**: `collection` → `request`

---

## 4. Key Features & Conversion Rules

1. **Multi-level Folder Tree**:
   - Recursively walks nested Postman collection folders;
   - Injects folder path into `extra: [{"label": "分组", "value": "..."}]`;
   - Rendered as collapsible tree groups in MPE `DataSourcePanel`.
2. **Comprehensive Body Type Support**:
   - `raw` (JSON / XML / Text / HTML) → mapped to `body_type: "body"` with matched `content_type`;
   - `formdata` → mapped to `body_type: "form-data"`, distinguishing text and file fields;
   - `urlencoded` → mapped to `body_type: "x-www-form-urlencoded"`;
   - `file` (Binary) → mapped to `body_type: "binary"`;
   - `graphql` → packaged into standard `{ "query": "...", "variables": ... }` JSON payload.
3. **Variable & URL Preservation**:
   - Keeps Postman `{{variable}}` syntax intact (natively interpolated by MPE engine);
   - Automatically excludes `disabled: true` query parameters and headers.
4. **Auth Inheritance & Translation**:
   - Inherits collection-level default auth if request auth is not explicitly specified;
   - Supports Bearer Token, Basic Auth, API Key (Header / Query).

---

## 5. Local Development & Testing

### Running Offline Self-Test
The plugin includes a full mock test suite covering `describe`, `validate_config`, `browse`, `get_item`, and `get_templates`:

```bash
# Run from source
python3 postman.py --self-test

# Or run compiled binary
./dist/postman --self-test
```

Example output:
```
[Self-Test] Starting Postman datasource plugin self-test...
  [PASS] describe test passed
  [PASS] validate_config rejection test passed
  [PASS] Postman Cloud mock API & hierarchy test passed
  [PASS] Local Postman Collection file parsing & template conversions passed

[Self-Test] Result: PASS
```

---

## 6. Build & Release

Pushing a `v*` tag (e.g. `v0.1.0`) triggers GitHub Actions to:
1. Run `postman.py --self-test`;
2. Build standalone binaries for Linux, Windows, and macOS via PyInstaller;
3. Package `.zip` distributions and `.sha256` checksums;
4. Publish a GitHub Release;
5. Trigger registry sync with `multi-protocol-flow/mpe-plugin-registry`.
