#!/usr/bin/env python3
"""Postman 数据源插件（只读浏览 + 拖拽导入）

支持双数据源模式：
1. Postman Cloud API (postman):
   - 工作空间列表：GET /workspaces
   - 工作空间详情/集合列表：GET /workspaces/{workspace_id} 或 GET /collections
   - 集合详情：GET /collections/{collection_uid}
   - 支持自定义 base_url（默认 https://api.getpostman.com）与指定 workspace_id
2. Postman 本地集合文件 (postman_file):
   - 本地 *.postman_collection.json (Collection v2.1) 文件读取与多级目录递归解析

特性：
- 完整递归遍历多级 Folder 目录，将层级路径注入 extra ["分组"]，由宿主树形渲染
- 完整支持 Postman Collection v2.1 结构（URL 对象、Headers、Auth 继承、Raw JSON/XML、Form-data、Urlencoded、GraphQL 等）
- 自动将 Postman 请求转换为 MPE 标准 HTTP 节点模板
- 提供 --self-test 命令行自测模式

标准 JSON-RPC 2.0 stdio 通信。
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

# Ensure UTF-8 encoding across Windows, Linux and macOS
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stdin, "reconfigure"):
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")

DEFAULT_POSTMAN_BASE = "https://api.getpostman.com"

# 1. Postman Cloud 数据源定义
POSTMAN_CLOUD_DATASOURCE = {
    "id": "postman",
    "display_name": "Postman",
    "auth": {"type": "api_key"},
    "config_schema": {
        "type": "object",
        "properties": {
            "api_key": {
                "type": "string",
                "format": "password",
                "title": "Postman API Key (X-Api-Key)",
            },
            "workspace_id": {
                "type": "string",
                "title": "工作空间 ID (Workspace ID，可选；填入后直接定位该空间)",
            },
            "base_url": {
                "type": "string",
                "title": "服务地址 (Base URL，默认 https://api.getpostman.com)",
            },
        },
        "required": ["api_key"],
    },
    "resources": [
        {
            "id": "workspace",
            "label": "工作空间",
            "children": "collection",
            "leaf": False,
        },
        {
            "id": "collection",
            "label": "集合",
            "children": "request",
            "leaf": False,
        },
        {
            "id": "request",
            "label": "请求",
            "children": None,
            "leaf": True,
        },
    ],
}

# 2. Postman 本地文件数据源定义
POSTMAN_FILE_DATASOURCE = {
    "id": "postman_file",
    "display_name": "Postman Collection 文件",
    "auth": {"type": "none"},
    "config_schema": {
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "title": "Postman Collection JSON 文件路径 (*.postman_collection.json)",
            },
        },
        "required": ["file_path"],
    },
    "resources": [
        {
            "id": "collection",
            "label": "集合",
            "children": "request",
            "leaf": False,
        },
        {
            "id": "request",
            "label": "请求",
            "children": None,
            "leaf": True,
        },
    ],
}


def extract_auth_params(auth_data: Any) -> Dict[str, Any]:
    """将 Postman auth 属性（可能是 list 或 dict）解析为字典"""
    if isinstance(auth_data, dict):
        return auth_data
    if isinstance(auth_data, list):
        result = {}
        for item in auth_data:
            if isinstance(item, dict):
                k = item.get("key")
                v = item.get("value")
                if k:
                    result[k] = v
        return result
    return {}


def parse_postman_url(url_raw: Any) -> Tuple[str, List[Tuple[str, str]]]:
    """解析 Postman URL（支持 string 或 object），返回 (url_without_disabled_queries, query_params)"""
    if not url_raw:
        return "", []

    if isinstance(url_raw, str):
        return url_raw, []

    if not isinstance(url_raw, dict):
        return str(url_raw), []

    # 结构化 URL 对象
    raw_str = url_raw.get("raw") or ""
    query_list = url_raw.get("query") or []

    valid_queries: List[Tuple[str, str]] = []
    has_disabled_queries = False

    if isinstance(query_list, list):
        for q in query_list:
            if isinstance(q, dict):
                if q.get("disabled") is True:
                    has_disabled_queries = True
                    continue
                k = q.get("key") or ""
                v = q.get("value")
                v_str = str(v) if v is not None else ""
                if k or v_str:
                    valid_queries.append((k, v_str))

    # 如果有 raw 且没有 disabled query，直接使用 raw
    if raw_str and not has_disabled_queries:
        return raw_str, valid_queries

    # 若需要重新组装 URL
    protocol = url_raw.get("protocol") or ""
    host = url_raw.get("host") or []
    path = url_raw.get("path") or []

    host_str = host if isinstance(host, str) else ".".join(str(h) for h in host)
    path_str = path if isinstance(path, str) else "/".join(str(p) for p in path)

    # 规范化路径开头的斜杠
    if path_str and not path_str.startswith("/"):
        path_str = "/" + path_str

    base_part = ""
    if protocol and host_str:
        base_part = f"{protocol}://{host_str}{path_str}"
    elif host_str:
        base_part = f"{host_str}{path_str}"
    elif path_str:
        base_part = path_str
    elif raw_str:
        # 去除 raw 中的 query 部分
        base_part = raw_str.split("?")[0]

    # 追加有效 queries
    if valid_queries:
        query_str_parts = []
        for k, v in valid_queries:
            if v != "":
                query_str_parts.append(f"{k}={v}")
            else:
                query_str_parts.append(k)
        sep = "&" if "?" in base_part else "?"
        base_part += sep + "&".join(query_str_parts)

    return base_part, valid_queries


def convert_postman_request_to_template(
    item_id: str,
    item_raw: Dict[str, Any],
    datasource_id: str = "postman",
    collection_auth: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """将 Postman 请求对象转换为 MPE 标准 NodeTemplate"""
    req = item_raw.get("request") or {}
    if isinstance(req, str):
        req = {"method": "GET", "url": req}

    name = item_raw.get("name") or "HTTP Request"
    method_upper = str(req.get("method") or "GET").upper()
    method_lower = method_upper.lower()

    # 1. URL 解析
    url_obj = req.get("url")
    full_url, _ = parse_postman_url(url_obj)

    # 2. Headers 解析
    headers: Dict[str, str] = {}
    header_list = req.get("header") or []
    if isinstance(header_list, list):
        for h in header_list:
            if isinstance(h, dict):
                if h.get("disabled") is True:
                    continue
                k = h.get("key") or ""
                v = h.get("value")
                if k:
                    headers[k] = str(v) if v is not None else ""

    # 3. Body 解析
    body_obj = req.get("body") or {}
    body_mode = body_obj.get("mode") if isinstance(body_obj, dict) else None

    body_type = "none"
    content_type = "json"
    body: Optional[str] = None
    form_fields: Optional[List[Dict[str, Any]]] = None
    file_path: Optional[str] = None

    if body_mode == "raw":
        body_type = "body"
        raw_text = body_obj.get("raw") or ""
        options = body_obj.get("options") or {}
        raw_opts = options.get("raw") or {}
        lang = str(raw_opts.get("language") or "").lower()

        if lang == "json" or (not lang and (raw_text.strip().startswith("{") or raw_text.strip().startswith("["))):
            content_type = "json"
        elif lang == "xml" or (not lang and raw_text.strip().startswith("<")):
            content_type = "xml"
        elif lang == "html":
            content_type = "html"
        else:
            content_type = "text"
        body = raw_text

    elif body_mode == "urlencoded":
        body_type = "x-www-form-urlencoded"
        content_type = "text"
        form_fields = []
        for param in body_obj.get("urlencoded") or []:
            if isinstance(param, dict):
                if param.get("disabled") is True:
                    continue
                k = param.get("key") or ""
                v = param.get("value")
                form_fields.append({
                    "key": k,
                    "value": str(v) if v is not None else "",
                    "field_type": "text",
                })

    elif body_mode == "formdata":
        body_type = "form-data"
        content_type = "text"
        form_fields = []
        for param in body_obj.get("formdata") or []:
            if isinstance(param, dict):
                if param.get("disabled") is True:
                    continue
                k = param.get("key") or ""
                ptype = param.get("type") or "text"
                ft = "file" if ptype == "file" else "text"
                v = param.get("src" if ft == "file" else "value")
                form_fields.append({
                    "key": k,
                    "value": str(v) if v is not None else "",
                    "field_type": ft,
                })

    elif body_mode == "file":
        body_type = "binary"
        content_type = "text"
        file_obj = body_obj.get("file") or {}
        if isinstance(file_obj, dict):
            file_path = file_obj.get("src") or ""
        else:
            file_path = str(file_obj)

    elif body_mode == "graphql":
        body_type = "body"
        content_type = "json"
        gql_obj = body_obj.get("graphql") or {}
        query_str = gql_obj.get("query") or ""
        variables_val = gql_obj.get("variables")
        gql_payload: Dict[str, Any] = {"query": query_str}
        if variables_val:
            if isinstance(variables_val, str):
                try:
                    gql_payload["variables"] = json.loads(variables_val)
                except Exception:
                    gql_payload["variables"] = variables_val
            else:
                gql_payload["variables"] = variables_val
        body = json.dumps(gql_payload, ensure_ascii=False, indent=2)

    # 4. Auth 解析（Request 优先，其次继承 Collection 级 Auth）
    auth_obj = req.get("auth") or item_raw.get("auth") or collection_auth
    auth_config: Optional[Dict[str, Any]] = None

    if isinstance(auth_obj, dict):
        auth_type = auth_obj.get("type")
        if auth_type == "bearer":
            params = extract_auth_params(auth_obj.get("bearer"))
            token = params.get("token") or ""
            auth_config = {"auth_type": "bearer", "token": str(token)}
        elif auth_type == "basic":
            params = extract_auth_params(auth_obj.get("basic"))
            username = params.get("username") or ""
            password = params.get("password") or ""
            auth_config = {
                "auth_type": "basic",
                "username": str(username),
                "password": str(password),
            }
        elif auth_type == "apikey":
            params = extract_auth_params(auth_obj.get("apikey"))
            k = params.get("key") or ""
            v = params.get("value") or ""
            loc = str(params.get("in") or "header").lower()
            if k:
                if loc == "query":
                    sep = "&" if "?" in full_url else "?"
                    full_url += f"{sep}{k}={v}"
                else:
                    headers[k] = str(v)

    node_data: Dict[str, Any] = {
        "type": "http",
        "url": full_url,
        "method": method_lower,
        "headers": headers if headers else None,
        "body_type": body_type,
        "content_type": content_type,
        "body": body if body else None,
        "form_fields": form_fields if form_fields else None,
        "file_path": file_path if file_path else None,
        "auth": auth_config if auth_config else None,
    }

    template_name = name if name else (f"{method_upper} {full_url}" if full_url else "HTTP Request")

    return {
        "type_id": "http",
        "name": template_name,
        "data": {k: v for k, v in node_data.items() if v is not None},
        "source_ref": {
            "datasource": datasource_id,
            "item_id": item_id,
            "updated_at": item_raw.get("updated_at") or item_raw.get("updatedAt"),
        },
    }


def flatten_collection_items(
    items: List[Dict[str, Any]],
    col_id_prefix: str,
    folder_path: str = "",
    col_name: str = "",
    col_auth: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """递归遍历 Postman Collection item 列表，提取所有的 Request 叶子节点"""
    result: List[Dict[str, Any]] = []

    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            continue

        item_name = item.get("name") or f"Item {idx + 1}"
        sub_items = item.get("item")

        if isinstance(sub_items, list):
            # 目录 / Folder 分组节点：递归下钻
            next_folder = f"{folder_path}/{item_name}" if folder_path else item_name
            # 子项可能继承当前 folder 的 auth（若 folder 定义了 auth）
            folder_auth = item.get("auth") or col_auth
            result.extend(
                flatten_collection_items(
                    sub_items,
                    col_id_prefix=col_id_prefix,
                    folder_path=next_folder,
                    col_name=col_name,
                    col_auth=folder_auth,
                )
            )
        elif "request" in item:
            # 接口 / Request 叶子节点
            raw_id = item.get("id") or item.get("_postman_id") or str(idx)
            item_id = f"{col_id_prefix}:{raw_id}"

            req = item.get("request") or {}
            method_str = "GET"
            path_str = ""

            if isinstance(req, str):
                path_str = req
            elif isinstance(req, dict):
                method_str = str(req.get("method") or "GET").upper()
                url_obj = req.get("url")
                path_str, _ = parse_postman_url(url_obj)

            extra = []
            if folder_path:
                extra.append({"label": "分组", "value": folder_path})
            if col_name:
                extra.append({"label": "集合", "value": col_name})

            # 把 collection-level auth 挂载到 item 的 raw 中供 template 转换
            item_raw_copy = dict(item)
            if col_auth and "auth" not in item_raw_copy:
                item_raw_copy["_inherited_auth"] = col_auth

            result.append({
                "id": item_id,
                "name": item_name,
                "protocol": "http",
                "method": method_str,
                "path": path_str if path_str else None,
                "summary": item.get("description") or (req.get("description") if isinstance(req, dict) else None),
                "extra": extra,
                "raw": item_raw_copy,
            })

    return result


class PostmanCloudClient:
    """Postman Cloud API 客户端"""

    def __init__(self, config: Dict[str, Any]):
        raw_base = str(config.get("base_url") or "").strip()
        self.base_url = raw_base.rstrip("/") if raw_base else DEFAULT_POSTMAN_BASE
        self.api_key = str(config.get("api_key") or "").strip()
        self.workspace_id = str(config.get("workspace_id") or "").strip() if config.get("workspace_id") else None

    def _headers(self) -> Dict[str, str]:
        return {
            "X-Api-Key": self.api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "mpe-postman-datasource/0.1.0",
        }

    def _request(self, path: str, query: Optional[Dict[str, Any]] = None) -> Any:
        url = f"{self.base_url}{path}"
        if query:
            filtered = {k: v for k, v in query.items() if v is not None}
            if filtered:
                url += "?" + urllib.parse.urlencode(filtered)

        req = urllib.request.Request(url, headers=self._headers(), method="GET")
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = resp.read()
                return json.loads(data.decode("utf-8"))
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP {e.code} from {url}: {err_body}") from e
        except Exception as e:
            raise RuntimeError(f"Request to {url} failed: {e}") from e

    def validate_config(self) -> Dict[str, Any]:
        if not self.api_key:
            return {"valid": False, "message": "Postman API Key (X-Api-Key) 不能为空"}
        try:
            if self.workspace_id:
                self._request(f"/workspaces/{self.workspace_id}")
            else:
                self._request("/workspaces")
            return {"valid": True, "message": None}
        except Exception as e:
            return {"valid": False, "message": str(e)}

    def list_workspaces(self) -> List[Dict[str, Any]]:
        if self.workspace_id:
            try:
                res = self._request(f"/workspaces/{self.workspace_id}")
                ws = res.get("workspace") if isinstance(res, dict) else res
                ws_id = ws.get("id") or self.workspace_id
                ws_name = ws.get("name") or f"Workspace {ws_id}"
                ws_type = ws.get("type") or "personal"
                return [{
                    "id": ws_id,
                    "name": ws_name,
                    "protocol": "other",
                    "method": None,
                    "path": None,
                    "summary": f"{ws_type} workspace",
                    "extra": [{"label": "工作空间 ID", "value": ws_id}, {"label": "类型", "value": ws_type}],
                    "raw": ws,
                }]
            except Exception:
                return [{
                    "id": self.workspace_id,
                    "name": f"工作空间 ({self.workspace_id})",
                    "protocol": "other",
                    "method": None,
                    "path": None,
                    "summary": "指定的工作空间",
                    "extra": [{"label": "工作空间 ID", "value": self.workspace_id}],
                    "raw": {"id": self.workspace_id},
                }]

        res = self._request("/workspaces")
        workspaces = res.get("workspaces") if isinstance(res, dict) else []
        result = []
        for ws in workspaces or []:
            ws_id = str(ws.get("id") or "")
            ws_name = ws.get("name") or "未命名工作空间"
            ws_type = ws.get("type") or "workspace"
            result.append({
                "id": ws_id,
                "name": ws_name,
                "protocol": "other",
                "method": None,
                "path": None,
                "summary": f"{ws_type} workspace",
                "extra": [{"label": "工作空间 ID", "value": ws_id}, {"label": "类型", "value": str(ws_type)}],
                "raw": ws,
            })
        return result

    def list_collections(self, workspace_id: Optional[str] = None) -> List[Dict[str, Any]]:
        target_ws = workspace_id or self.workspace_id
        collections: List[Dict[str, Any]] = []

        if target_ws:
            try:
                res = self._request(f"/workspaces/{target_ws}")
                ws = res.get("workspace") if isinstance(res, dict) else {}
                collections = ws.get("collections") or []
            except Exception:
                res = self._request("/collections", query={"workspace": target_ws})
                collections = res.get("collections") if isinstance(res, dict) else []
        else:
            res = self._request("/collections")
            collections = res.get("collections") if isinstance(res, dict) else []

        result = []
        for col in collections:
            col_id = str(col.get("uid") or col.get("id") or "")
            col_name = col.get("name") or "未命名集合"
            result.append({
                "id": col_id,
                "name": col_name,
                "protocol": "other",
                "method": None,
                "path": None,
                "summary": col.get("summary") or col.get("description"),
                "extra": [{"label": "集合 UID", "value": col_id}] if col_id else [],
                "raw": col,
            })
        return result

    def get_collection(self, collection_uid: str) -> Dict[str, Any]:
        res = self._request(f"/collections/{collection_uid}")
        return res.get("collection") if isinstance(res, dict) and "collection" in res else res

    def list_requests(self, collection_uid: str) -> List[Dict[str, Any]]:
        col = self.get_collection(collection_uid)
        items = col.get("item") or []
        col_name = (col.get("info") or {}).get("name") or col.get("name") or "Collection"
        col_auth = col.get("auth")
        return flatten_collection_items(items, col_id_prefix=collection_uid, col_name=col_name, col_auth=col_auth)


class PostmanFileHandler:
    """Postman 本地集合 JSON 文件解析器"""

    @staticmethod
    def load_file(file_path: str) -> Dict[str, Any]:
        if not file_path:
            raise ValueError("file_path 不能为空")
        expanded = os.path.abspath(os.path.expanduser(file_path))
        if not os.path.isfile(expanded):
            raise FileNotFoundError(f"未找到 Postman Collection 文件: {file_path}")
        with open(expanded, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("Postman Collection 文件格式不正确，根节点必须为 JSON Object")
        return data

    @staticmethod
    def validate_config(config: Dict[str, Any]) -> Dict[str, Any]:
        file_path = str(config.get("file_path") or "").strip()
        if not file_path:
            return {"valid": False, "message": "file_path (Collection 文件路径) 不能为空"}
        try:
            data = PostmanFileHandler.load_file(file_path)
            has_info_or_item = ("info" in data) or ("item" in data)
            if not has_info_or_item:
                return {"valid": False, "message": "文件中未检测到 Postman Collection 特征字段 (info/item)"}
            return {"valid": True, "message": None}
        except Exception as e:
            return {"valid": False, "message": str(e)}

    @staticmethod
    def list_collections(file_path: str) -> List[Dict[str, Any]]:
        data = PostmanFileHandler.load_file(file_path)
        info = data.get("info") or {}
        col_name = info.get("name") or os.path.basename(file_path)
        col_id = info.get("_postman_id") or info.get("id") or "local_col"
        desc = info.get("description")
        return [{
            "id": col_id,
            "name": col_name,
            "protocol": "other",
            "method": None,
            "path": None,
            "summary": desc if isinstance(desc, str) else None,
            "extra": [{"label": "文件路径", "value": os.path.abspath(file_path)}],
            "raw": data,
        }]

    @staticmethod
    def list_requests(file_path: str) -> List[Dict[str, Any]]:
        data = PostmanFileHandler.load_file(file_path)
        info = data.get("info") or {}
        col_name = info.get("name") or os.path.basename(file_path)
        col_id = info.get("_postman_id") or info.get("id") or "local_col"
        col_auth = data.get("auth")
        items = data.get("item") or []
        return flatten_collection_items(items, col_id_prefix=col_id, col_name=col_name, col_auth=col_auth)


def is_file_mode(config: Dict[str, Any]) -> bool:
    """判断是否为本地文件数据源配置"""
    return bool(config.get("file_path"))


def handle_browse(
    config: Dict[str, Any],
    resource_id: str,
    parent_id: Optional[str],
    page: int,
    size: int,
) -> List[Dict[str, Any]]:
    if is_file_mode(config):
        file_path = str(config.get("file_path") or "")
        if resource_id == "collection":
            return PostmanFileHandler.list_collections(file_path)
        elif resource_id == "request":
            return PostmanFileHandler.list_requests(file_path)
        else:
            raise ValueError(f"postman_file 不支持资源类型: {resource_id}")
    else:
        client = PostmanCloudClient(config)
        if resource_id == "workspace":
            return client.list_workspaces()
        elif resource_id == "collection":
            return client.list_collections(parent_id)
        elif resource_id == "request":
            col_uid = parent_id
            if not col_uid:
                raise ValueError("browse(request) 缺少 parent_id (collection_uid)")
            return client.list_requests(col_uid)
        else:
            raise ValueError(f"postman 不支持资源类型: {resource_id}")


def handle_get_item(config: Dict[str, Any], resource_id: str, item_id: str) -> Dict[str, Any]:
    if is_file_mode(config):
        file_path = str(config.get("file_path") or "")
        if resource_id == "collection":
            cols = PostmanFileHandler.list_collections(file_path)
            if cols:
                return cols[0]
            raise ValueError(f"未找到集合: {item_id}")
        elif resource_id == "request":
            requests = PostmanFileHandler.list_requests(file_path)
            for req in requests:
                if req["id"] == item_id or req["id"].endswith(f":{item_id}"):
                    return req
            raise ValueError(f"未在文件中找到请求条目: {item_id}")
        else:
            raise ValueError(f"未知资源类型: {resource_id}")
    else:
        client = PostmanCloudClient(config)
        if resource_id == "workspace":
            ws_list = client.list_workspaces()
            for ws in ws_list:
                if ws["id"] == item_id:
                    return ws
            return {
                "id": item_id,
                "name": f"Workspace {item_id}",
                "protocol": "other",
                "method": None,
                "path": None,
                "summary": None,
                "extra": [{"label": "工作空间 ID", "value": item_id}],
                "raw": {"id": item_id},
            }
        elif resource_id == "collection":
            col = client.get_collection(item_id)
            info = col.get("info") or {}
            return {
                "id": item_id,
                "name": info.get("name") or col.get("name") or "Collection",
                "protocol": "other",
                "method": None,
                "path": None,
                "summary": info.get("description"),
                "extra": [{"label": "集合 UID", "value": item_id}],
                "raw": col,
            }
        elif resource_id == "request":
            parts = item_id.split(":", 1)
            if len(parts) != 2:
                raise ValueError(f"非法的请求条目 ID 格式: {item_id}")
            col_uid, req_id = parts[0], parts[1]
            requests = client.list_requests(col_uid)
            for req in requests:
                if req["id"] == item_id or req["id"].endswith(f":{req_id}"):
                    return req
            raise ValueError(f"未在集合 {col_uid} 中找到请求条目: {req_id}")
        else:
            raise ValueError(f"未知资源类型: {resource_id}")


def handle_get_templates(config: Dict[str, Any], item_ids: List[str]) -> List[Dict[str, Any]]:
    templates = []
    datasource_id = "postman_file" if is_file_mode(config) else "postman"

    for item_id in item_ids:
        try:
            item = handle_get_item(config, "request", item_id)
            raw = item.get("raw", {})
            inherited_auth = raw.get("_inherited_auth")
            tpl = convert_postman_request_to_template(
                item_id,
                raw,
                datasource_id=datasource_id,
                collection_auth=inherited_auth,
            )
            templates.append(tpl)
        except Exception as e:
            sys.stderr.write(f"postman get_templates warning for {item_id}: {e}\n")

    return templates


def handle_validate_config(config: Dict[str, Any]) -> Dict[str, Any]:
    if is_file_mode(config):
        return PostmanFileHandler.validate_config(config)
    else:
        client = PostmanCloudClient(config)
        return client.validate_config()


def get_plugin_entry() -> Dict[str, Any]:
    """返回 describe 握手使用的 entry（单一事实来源 = 同级 plugin.json）"""
    if getattr(sys, "frozen", False):
        base_dir = os.path.dirname(os.path.abspath(sys.executable))
    else:
        base_dir = os.path.dirname(os.path.abspath(__file__))
    manifest_path = os.path.join(base_dir, "plugin.json")
    try:
        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)
        entry = manifest.get("entry")
        if isinstance(entry, dict) and entry.get("command"):
            return entry
    except Exception as e:
        sys.stderr.write(f"postman: cannot read {manifest_path}: {e}\n")

    if getattr(sys, "frozen", False):
        binary_name = os.path.basename(sys.executable)
        return {"command": f"./{binary_name}", "args": []}
    return {"command": sys.executable, "args": [os.path.abspath(__file__)]}


def describe_result() -> Dict[str, Any]:
    return {
        "plugin": {
            "name": "postman",
            "version": "0.1.0",
            "kind": "datasource",
            "description": "Postman 数据源（OpenAPI 云端工作空间 + 本地集合文件导入）",
            "entry": get_plugin_entry(),
            "capabilities": {"streaming": False},
        },
        "nodes": [],
        "datasources": [POSTMAN_CLOUD_DATASOURCE, POSTMAN_FILE_DATASOURCE],
    }


def run_stdio_loop():
    """标准 JSON-RPC 2.0 stdio 事件循环"""
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception as e:
            sys.stderr.write(f"postman: malformed json frame: {e}\n")
            continue

        req_id = req.get("id")
        method = req.get("method")
        params = req.get("params", {}) or {}

        if method == "describe":
            resp = {"jsonrpc": "2.0", "id": req_id, "result": describe_result()}
        elif method == "browse":
            try:
                config = params.get("config", {})
                resource_id = params.get("resource_id", "workspace" if not is_file_mode(config) else "collection")
                parent_id = params.get("parent_id")
                page = params.get("page", 1)
                size = params.get("size", 100)
                items = handle_browse(config, resource_id, parent_id, page, size)
                resp = {"jsonrpc": "2.0", "id": req_id, "result": items}
            except Exception as e:
                resp = {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": str(e)}}
        elif method == "get_item":
            try:
                config = params.get("config", {})
                resource_id = params.get("resource_id", "request")
                item_id = params.get("item_id", "")
                item = handle_get_item(config, resource_id, item_id)
                resp = {"jsonrpc": "2.0", "id": req_id, "result": item}
            except Exception as e:
                resp = {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": str(e)}}
        elif method == "get_templates":
            try:
                config = params.get("config", {})
                item_ids = params.get("item_ids", [])
                templates = handle_get_templates(config, item_ids)
                resp = {"jsonrpc": "2.0", "id": req_id, "result": templates}
            except Exception as e:
                resp = {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": str(e)}}
        elif method == "validate_config":
            try:
                config = params.get("config", {})
                res = handle_validate_config(config)
                resp = {"jsonrpc": "2.0", "id": req_id, "result": res}
            except Exception as e:
                resp = {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": str(e)}}
        elif method == "shutdown":
            resp = {"jsonrpc": "2.0", "id": req_id, "result": None}
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()
            break
        else:
            resp = {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": f"Method not found: {method}"}}

        sys.stdout.write(json.dumps(resp) + "\n")
        sys.stdout.flush()


def run_self_test() -> int:
    """--self-test 模式：运行单元自测套件"""
    print("[Self-Test] Starting Postman datasource plugin self-test...")
    passed = True

    # 1. Test describe
    try:
        desc = describe_result()
        assert desc["plugin"]["name"] == "postman"
        assert desc["plugin"]["kind"] == "datasource"
        assert len(desc["datasources"]) == 2
        ds_ids = [d["id"] for d in desc["datasources"]]
        assert "postman" in ds_ids
        assert "postman_file" in ds_ids
        entry = desc["plugin"]["entry"]
        assert entry.get("command"), "describe entry.command must be non-empty"
        manifest_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "plugin.json")
        if os.path.isfile(manifest_path):
            with open(manifest_path, "r", encoding="utf-8") as f:
                manifest = json.load(f)
            disk_entry = manifest.get("entry")
            if isinstance(disk_entry, dict):
                assert entry == disk_entry, f"describe entry {entry} must match plugin.json entry {disk_entry}"
        print("  [PASS] describe test passed")
    except Exception as e:
        print(f"  [FAIL] describe test failed: {e}")
        passed = False

    # 2. Test validate_config
    try:
        # Cloud empty key
        res1 = handle_validate_config({})
        assert res1["valid"] is False
        assert "API Key" in res1["message"]

        # File empty path
        res2 = handle_validate_config({"file_path": ""})
        assert res2["valid"] is False

        # File not exist
        res3 = handle_validate_config({"file_path": "/path/to/non_existent_file_12345.json"})
        assert res3["valid"] is False

        print("  [PASS] validate_config rejection test passed")
    except Exception as e:
        print(f"  [FAIL] validate_config rejection test failed: {e}")
        passed = False

    # 3. Test mock Postman Cloud API
    class MockPostmanCloudClient(PostmanCloudClient):
        def _request(self, path: str, query: Optional[Dict[str, Any]] = None) -> Any:
            if path == "/workspaces":
                return {
                    "workspaces": [
                        {"id": "ws-101", "name": "Team Space", "type": "team"},
                        {"id": "ws-102", "name": "My Workspace", "type": "personal"},
                    ]
                }
            elif path == "/workspaces/ws-101":
                return {
                    "workspace": {
                        "id": "ws-101",
                        "name": "Team Space",
                        "type": "team",
                        "collections": [
                            {"id": "col-abc", "name": "User Service API", "uid": "12345-col-abc"}
                        ],
                    }
                }
            elif path == "/collections/12345-col-abc":
                return {
                    "collection": {
                        "info": {
                            "_postman_id": "col-abc",
                            "name": "User Service API",
                            "description": "User service collection",
                        },
                        "auth": {
                            "type": "bearer",
                            "bearer": [{"key": "token", "value": "{{global_token}}"}],
                        },
                        "item": [
                            {
                                "name": "Auth",
                                "item": [
                                    {
                                        "id": "req-login",
                                        "name": "Login",
                                        "request": {
                                            "method": "POST",
                                            "url": {
                                                "raw": "https://api.example.com/v1/auth/login",
                                                "protocol": "https",
                                                "host": ["api", "example", "com"],
                                                "path": ["v1", "auth", "login"],
                                            },
                                            "header": [
                                                {"key": "Content-Type", "value": "application/json"}
                                            ],
                                            "body": {
                                                "mode": "raw",
                                                "raw": '{"username": "admin", "password": "{{password}}"}',
                                                "options": {"raw": {"language": "json"}},
                                            },
                                        },
                                    }
                                ],
                            },
                            {
                                "id": "req-get-profile",
                                "name": "Get Profile",
                                "request": {
                                    "method": "GET",
                                    "url": "https://api.example.com/v1/users/me",
                                },
                            },
                        ],
                    }
                }
            raise ValueError(f"Unknown mock path: {path}")

    try:
        mock_cloud = MockPostmanCloudClient({"api_key": "mock_secret"})

        # Browse workspaces
        workspaces = mock_cloud.list_workspaces()
        assert len(workspaces) == 2
        assert workspaces[0]["id"] == "ws-101"
        assert workspaces[0]["name"] == "Team Space"

        # Browse collections in workspace
        collections = mock_cloud.list_collections("ws-101")
        assert len(collections) == 1
        assert collections[0]["id"] == "12345-col-abc"
        assert collections[0]["name"] == "User Service API"

        # Browse requests with nested folders
        requests = mock_cloud.list_requests("12345-col-abc")
        assert len(requests) == 2

        # Check req-login has folder tag "Auth"
        login_req = next(r for r in requests if "req-login" in r["id"])
        assert login_req["name"] == "Login"
        assert login_req["method"] == "POST"
        assert any(e["label"] == "分组" and e["value"] == "Auth" for e in login_req["extra"])

        # Check req-get-profile
        profile_req = next(r for r in requests if "req-get-profile" in r["id"])
        assert profile_req["name"] == "Get Profile"
        assert profile_req["method"] == "GET"

        # Convert template with inherited auth
        tpl_profile = convert_postman_request_to_template(
            profile_req["id"],
            profile_req["raw"],
            collection_auth={"type": "bearer", "bearer": [{"key": "token", "value": "test_tok"}]},
        )
        assert tpl_profile["type_id"] == "http"
        assert tpl_profile["data"]["method"] == "get"
        assert tpl_profile["data"]["url"] == "https://api.example.com/v1/users/me"
        assert tpl_profile["data"]["auth"]["auth_type"] == "bearer"
        assert tpl_profile["data"]["auth"]["token"] == "test_tok"

        print("  [PASS] Postman Cloud mock API & hierarchy test passed")
    except Exception as e:
        print(f"  [FAIL] Postman Cloud mock API test failed: {e}")
        passed = False

    # 4. Test Local Postman Collection v2.1 File & conversion of various body & auth modes
    import tempfile

    mock_collection_data = {
        "info": {
            "_postman_id": "file-col-999",
            "name": "Local Test Collection",
            "description": "A collection for local unit tests",
            "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json",
        },
        "item": [
            {
                "name": "User Management",
                "item": [
                    {
                        "name": "Sub Folder",
                        "item": [
                            {
                                "id": "req-nested",
                                "name": "Nested User Info",
                                "request": {
                                    "method": "GET",
                                    "url": {
                                        "raw": "https://{{baseUrl}}/users/info?status=active&disabled_param=1",
                                        "protocol": "https",
                                        "host": ["{{baseUrl}}"],
                                        "path": ["users", "info"],
                                        "query": [
                                            {"key": "status", "value": "active"},
                                            {"key": "disabled_param", "value": "1", "disabled": True},
                                        ],
                                    },
                                    "auth": {
                                        "type": "basic",
                                        "basic": [
                                            {"key": "username", "value": "my_user"},
                                            {"key": "password", "value": "my_pass"},
                                        ],
                                    },
                                },
                            }
                        ],
                    }
                ],
            },
            {
                "id": "req-formdata",
                "name": "Upload Avatar",
                "request": {
                    "method": "POST",
                    "url": "https://api.example.com/upload",
                    "body": {
                        "mode": "formdata",
                        "formdata": [
                            {"key": "description", "value": "avatar file", "type": "text"},
                            {"key": "file", "src": "/path/to/avatar.png", "type": "file"},
                            {"key": "skip_me", "value": "0", "disabled": True},
                        ],
                    },
                },
            },
            {
                "id": "req-urlencoded",
                "name": "OAuth Token",
                "request": {
                    "method": "POST",
                    "url": "https://api.example.com/oauth/token",
                    "body": {
                        "mode": "urlencoded",
                        "urlencoded": [
                            {"key": "grant_type", "value": "client_credentials"},
                            {"key": "client_id", "value": "abc"},
                        ],
                    },
                },
            },
            {
                "id": "req-graphql",
                "name": "Query GraphQL",
                "request": {
                    "method": "POST",
                    "url": "https://api.example.com/graphql",
                    "body": {
                        "mode": "graphql",
                        "graphql": {
                            "query": "query GetUser($id: ID!) { user(id: $id) { name } }",
                            "variables": '{"id": "1001"}',
                        },
                    },
                },
            },
            {
                "id": "req-apikey-query",
                "name": "Query With API Key",
                "request": {
                    "method": "GET",
                    "url": "https://api.example.com/data",
                    "auth": {
                        "type": "apikey",
                        "apikey": [
                            {"key": "key", "value": "api_key"},
                            {"key": "value", "value": "secret123"},
                            {"key": "in", "value": "query"},
                        ],
                    },
                },
            },
        ],
    }

    try:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as tf:
            json.dump(mock_collection_data, tf)
            temp_file_path = tf.name

        try:
            # Test validate_config on valid file
            v_res = PostmanFileHandler.validate_config({"file_path": temp_file_path})
            assert v_res["valid"] is True

            # Test list collections
            cols = PostmanFileHandler.list_collections(temp_file_path)
            assert len(cols) == 1
            assert cols[0]["name"] == "Local Test Collection"

            # Test list requests with nested folder path
            reqs = PostmanFileHandler.list_requests(temp_file_path)
            assert len(reqs) == 5

            nested = next(r for r in reqs if "req-nested" in r["id"])
            assert nested["name"] == "Nested User Info"
            assert any(e["label"] == "分组" and e["value"] == "User Management/Sub Folder" for e in nested["extra"])

            # Test template conversion: Nested with query filtering & basic auth
            tpl_nested = convert_postman_request_to_template(nested["id"], nested["raw"], datasource_id="postman_file")
            assert tpl_nested["type_id"] == "http"
            assert tpl_nested["data"]["auth"]["auth_type"] == "basic"
            assert tpl_nested["data"]["auth"]["username"] == "my_user"
            assert tpl_nested["data"]["auth"]["password"] == "my_pass"
            assert "status=active" in tpl_nested["data"]["url"]
            assert "disabled_param" not in tpl_nested["data"]["url"]

            # Test template conversion: Formdata
            formdata_req = next(r for r in reqs if "req-formdata" in r["id"])
            tpl_form = convert_postman_request_to_template(formdata_req["id"], formdata_req["raw"], datasource_id="postman_file")
            assert tpl_form["data"]["body_type"] == "form-data"
            fields = tpl_form["data"]["form_fields"]
            assert len(fields) == 2
            assert fields[0]["key"] == "description" and fields[0]["field_type"] == "text"
            assert fields[1]["key"] == "file" and fields[1]["field_type"] == "file"

            # Test template conversion: Urlencoded
            urlenc_req = next(r for r in reqs if "req-urlencoded" in r["id"])
            tpl_urlenc = convert_postman_request_to_template(urlenc_req["id"], urlenc_req["raw"], datasource_id="postman_file")
            assert tpl_urlenc["data"]["body_type"] == "x-www-form-urlencoded"
            assert len(tpl_urlenc["data"]["form_fields"]) == 2

            # Test template conversion: GraphQL
            gql_req = next(r for r in reqs if "req-graphql" in r["id"])
            tpl_gql = convert_postman_request_to_template(gql_req["id"], gql_req["raw"], datasource_id="postman_file")
            assert tpl_gql["data"]["body_type"] == "body"
            assert tpl_gql["data"]["content_type"] == "json"
            gql_body = json.loads(tpl_gql["data"]["body"])
            assert "query GetUser" in gql_body["query"]
            assert gql_body["variables"]["id"] == "1001"

            # Test template conversion: API Key in Query
            apikey_req = next(r for r in reqs if "req-apikey-query" in r["id"])
            tpl_apikey = convert_postman_request_to_template(apikey_req["id"], apikey_req["raw"], datasource_id="postman_file")
            assert "api_key=secret123" in tpl_apikey["data"]["url"]

            print("  [PASS] Local Postman Collection file parsing & template conversions passed")
        finally:
            if os.path.exists(temp_file_path):
                os.remove(temp_file_path)
    except Exception as e:
        print(f"  [FAIL] Local Postman Collection file test failed: {e}")
        passed = False

    if passed:
        print("\n[Self-Test] Result: PASS")
        return 0
    else:
        print("\n[Self-Test] Result: FAIL")
        return 1


def main():
    parser = argparse.ArgumentParser(description="Postman datasource plugin")
    parser.add_argument("--self-test", action="store_true", help="Run self-tests and exit")
    args = parser.parse_args()

    if args.self_test:
        sys.exit(run_self_test())
    else:
        run_stdio_loop()


if __name__ == "__main__":
    main()
