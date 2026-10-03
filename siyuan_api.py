# -*- coding: utf-8 -*-
"""
思源笔记 —— 网络与 API 仓储层（步骤2 抽取）

职责：
  1. 网络地址解析与连通性预检（WSL2 → Windows 宿主机自动探测）；
  2. HTTP 传输（Session 复用、建连重试、异常分类）；
  3. 思源领域接口封装（目录 / 文档 / 标题 / Kramdown）；
  4. 图片资源获取（本地路径映射 + /api/file/getFile 兜底 + base64 内嵌 + PIL 读取）。

设计约束：
  - 本模块**直接消费** config.get_config()，不读取其它业务模块的全局变量；
  - 不反向 import siyuan_client，保证依赖单向、无循环导入。
"""

from __future__ import annotations

import base64
import io
import json
import os
import re
import socket
import subprocess
import time
import urllib.parse

import requests

import config as config_module

try:
    from PIL import Image
    _HAS_PIL = True
except ImportError:
    _HAS_PIL = False
    print("⚠️  未安装 Pillow，将使用保守的紧凑判断。请运行: pip install Pillow")


# ============================================================
#  运行时状态（自 siyuan_client.py 迁入，权威副本在此）
# ============================================================
SIYUAN_BASE = ""              # 解析出的实际基址，如 http://172.20.0.1:6806
_SIYUAN_BASE_RESOLVED = False
_ASSET_ERROR_LOGGED = set()   # 已告警过的图片路径，避免重复刷屏


class _MutableCounter:
    """
    可变整数计数容器（步骤2 兼容用）。

    背景：siyuan_client.main() 仍以模块全局名 `_ASSET_FETCH_FAILED` 读取该计数，
    而 Python 的 LOAD_GLOBAL **不会**经过模块级 __getattr__，无法靠转发取到最新值。
    改为共享同一个可变对象后，本模块内部的 `+= 1` 会原地更新该对象，
    siyuan_client 通过 import 持有同一引用即可读到最新值；
    且 `if counter:` 与 f-string 格式化的行为与整数保持一致。
    """

    __slots__ = ("value",)

    def __init__(self, value=0):
        self.value = value

    def __iadd__(self, other):
        self.value += other
        return self

    def __int__(self):
        return self.value

    def __bool__(self):
        return self.value != 0

    def __str__(self):
        return str(self.value)

    def __format__(self, spec):
        return format(self.value, spec)

    def __repr__(self):
        return f"_MutableCounter({self.value})"


_ASSET_FETCH_FAILED = _MutableCounter()  # 图片经 API 兜底仍失败的累计次数
_CONNECT_FAIL_STREAK = 0      # 连续建连失败次数
_CONNECT_FAIL_BANNER_SHOWN = False
_ASSET_BYTES_CACHE = {}

# 常见图片扩展名 → MIME 子类型
_IMAGE_MIME = {
    "jpg": "jpeg", "jpeg": "jpeg", "png": "png", "gif": "gif",
    "webp": "webp", "bmp": "bmp", "svg": "svg+xml", "ico": "x-icon",
}

# 复用同一个 Session：复用已建立的 TCP 连接，避免每个请求都重新三次握手
SESSION = requests.Session()
_SESSION_ADAPTER = requests.adapters.HTTPAdapter(pool_connections=10, pool_maxsize=10)
SESSION.mount("http://", _SESSION_ADAPTER)
SESSION.mount("https://", _SESSION_ADAPTER)


def _cfg():
    """获取配置单例（显式触发加载，非导入期副作用）。"""
    return config_module.get_config()


# ============================================================
#  网络地址解析与连通性预检（WSL → Windows 宿主机）
# ============================================================
# 背景：WSL2 默认 NAT 模式下，Windows 宿主机地址 == WSL 的默认网关
# （例如 172.20.0.1/20）。该网段会随 Windows 重启、休眠恢复、网络切换
# （Wi-Fi / 有线 / VPN）而被重新分配，因此把地址硬编码进 config.json 会周期性失效。
#
# 失效时的表现是「连接静默超时」而非「连接被拒绝」：
#   sock.connect(sa)  ← SYN 无人应答 → requests.exceptions.ConnectTimeout
# 所以这里在运行时动态解析宿主机地址，并用短超时 TCP 预检快速筛选候选。
#
# 注意：不要用 /etc/resolv.conf 的 nameserver 作为宿主机地址。新版 WSL 的
# DNS 隧道会把 nameserver 设为 10.255.255.254，那是 DNS 代理而非宿主机。


def reset_resolved_base():
    """配置重载后使地址解析缓存失效，确保下一次调用重新探测。"""
    global SIYUAN_BASE, _SIYUAN_BASE_RESOLVED
    SIYUAN_BASE = ""
    _SIYUAN_BASE_RESOLVED = False


def _split_siyuan_url(url):
    """
    拆分 SIYUAN_URL 为 (scheme, host, port)。
    host 允许写成 'auto' 占位符（如 http://auto:6806），表示完全交由自动探测决定。
    """
    try:
        parts = urllib.parse.urlsplit(url if "://" in url else f"http://{url}")
    except ValueError:
        return "http", "", 6806

    scheme = parts.scheme or "http"
    host = (parts.hostname or "").strip()
    try:
        port = parts.port or 6806
    except ValueError:
        port = 6806
    return scheme, host, port


def _detect_host_gateway():
    """
    从 /proc/net/route 解析默认网关地址（NAT 模式下即 Windows 宿主机）。

    Gateway 字段为小端十六进制，例如 "010014AC" → 172.20.0.1。
    纯文件读取，无子进程开销；解析失败返回 None。
    """
    try:
        with open("/proc/net/route", "r", encoding="ascii") as f:
            next(f, None)  # 跳过表头行
            for line in f:
                cols = line.split()
                # Destination 为 00000000 表示默认路由
                if len(cols) >= 3 and cols[1] == "00000000":
                    gw_hex = cols[2]
                    if len(gw_hex) == 8:
                        octets = [str(int(gw_hex[i:i + 2], 16)) for i in (6, 4, 2, 0)]
                        return ".".join(octets)
    except (OSError, ValueError):
        pass
    return None


def _detect_host_gateway_via_ip():
    """/proc/net/route 不可用时的兜底：解析 `ip route show default` 输出。"""
    try:
        proc = subprocess.run(
            ["ip", "-4", "route", "show", "default"],
            capture_output=True, text=True, timeout=2, check=False,
        )
        m = re.search(r"default\s+via\s+(\d+\.\d+\.\d+\.\d+)", proc.stdout or "")
        if m:
            return m.group(1)
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def _tcp_probe(host, port, timeout=None):
    """
    短超时 TCP 预检：只判断能否建立连接，不发送任何业务数据。

    以 probe_timeout（默认 1.5s）级别的成本筛选候选地址，
    避免每个失效候选都白等完整的建连超时。
    """
    probe_timeout = _cfg().probe_timeout if timeout is None else timeout
    try:
        with socket.create_connection((host, port), timeout=probe_timeout):
            return True
    except OSError:
        return False


def _candidate_hosts(configured_host):
    """
    生成候选宿主机地址（保持优先级顺序并去重）：
      1. config.json 中显式配置的地址（为 auto 等占位符时跳过）
      2. 127.0.0.1 / localhost（WSL1 或 mirrored 网络模式下可用）
      3. 默认网关（WSL2 NAT 模式下的 Windows 宿主机）
    """
    placeholders = {"auto", "", "none", "default", "windows", "host.docker.internal"}
    candidates = []
    if configured_host and configured_host.lower() not in placeholders:
        candidates.append(configured_host)
    candidates.extend(["127.0.0.1", "localhost"])
    gateway = _detect_host_gateway() or _detect_host_gateway_via_ip()
    if gateway:
        candidates.append(gateway)

    seen, ordered = set(), []
    for host in candidates:
        if host and host not in seen:
            seen.add(host)
            ordered.append(host)
    return ordered


def resolve_siyuan_base(force=False, verbose=True):
    """
    解析并缓存实际可用的思源 API 基址。

    返回基址字符串（如 http://172.20.0.1:6806）；
    所有候选地址均不可达时返回 None，由调用方 fail-fast 并输出处置清单。
    """
    global SIYUAN_BASE, _SIYUAN_BASE_RESOLVED

    cfg = _cfg()
    if _SIYUAN_BASE_RESOLVED and not force:
        return SIYUAN_BASE or None

    scheme, configured_host, port = _split_siyuan_url(cfg.siyuan_url)

    if str(cfg.siyuan_host_mode).lower() == "fixed":
        candidates = [configured_host or "127.0.0.1"]
        if verbose:
            print("🌐 地址模式: fixed（仅使用 config.json 中的地址，不做自动探测）")
    else:
        candidates = _candidate_hosts(configured_host)

    gateway = _detect_host_gateway() or _detect_host_gateway_via_ip()

    for host in candidates:
        if _tcp_probe(host, port):
            SIYUAN_BASE = f"{scheme}://{host}:{port}"
            _SIYUAN_BASE_RESOLVED = True
            if verbose:
                tag = "  ← 默认网关（NAT 模式下的 Windows 宿主机）" if host == gateway else ""
                print(f"🌐 思源 API 可达: {SIYUAN_BASE}{tag}")
            return SIYUAN_BASE
        if verbose:
            print(f"   ⚠️  候选地址不可达: {host}:{port}（{cfg.probe_timeout}s TCP 预检未通过）")

    SIYUAN_BASE = ""
    _SIYUAN_BASE_RESOLVED = True  # 已探测过，避免每次调用重复探测
    return None


def get_siyuan_base():
    """获取基址；尚未解析时先解析（防御性懒加载），全部失败则回退到配置值。"""
    if not _SIYUAN_BASE_RESOLVED:
        resolve_siyuan_base()
    return SIYUAN_BASE or _cfg().siyuan_url.rstrip("/")


def _note_connect_failure():
    """连续建连失败达 3 次时打印一次处置提示，避免逐条请求刷屏。"""
    global _CONNECT_FAIL_STREAK, _CONNECT_FAIL_BANNER_SHOWN
    _CONNECT_FAIL_STREAK += 1
    if _CONNECT_FAIL_STREAK >= 3 and not _CONNECT_FAIL_BANNER_SHOWN:
        _CONNECT_FAIL_BANNER_SHOWN = True
        print("\n" + "!" * 60)
        print("❗ 连续 3 次无法建立 TCP 连接。若此前可正常使用，常见原因与处置：")
        print("   1) WSL NAT 网关地址漂移 → 保持 SIYUAN_HOST_MODE 为 auto（默认自动探测）")
        print("   2) 宿主机休眠/唤醒后 NAT 端点失效 → Windows 侧执行 wsl --shutdown 后重开 WSL")
        print("   3) 防火墙 profile 变为 Public → 放行规则需使用 -Profile Any")
        print("   4) Wi-Fi/有线/VPN 切换导致 vNIC 重建 → 重新运行本脚本即可自动重新解析")
        print("   提示：ping 不通宿主机属 WSL2 正常现象，判断链路请用 TCP 探测。")
        print("!" * 60 + "\n")


def _post_json(url, payload, headers=None, connect_timeout=None,
               read_timeout=None, retries=None):
    """
    带「建连阶段」重试的 POST 封装。

    重试安全性说明（重要）：
      - 仅对建连阶段失败（ConnectTimeout / ConnectionError，即 TCP 尚未建立）重试。
        此时一个字节都未发出，服务端无任何副作用，故对 POST 也是安全的。
      - ReadTimeout（连接已建立但响应超时）不重试 —— 请求可能已送达并正在执行，
        重放会导致重复操作。
    本脚本调用的接口（/api/query/sql、getBlockKramdown、getHPathByID 等）均为只读查询，
    这里仍按最严格原则处理，避免日后新增写接口时被静默重放。
    """
    cfg = _cfg()
    c_timeout = cfg.connect_timeout if connect_timeout is None else connect_timeout
    r_timeout = cfg.read_timeout if read_timeout is None else read_timeout
    max_retries = cfg.connect_retries if retries is None else retries

    last_error = None
    for attempt in range(max_retries + 1):
        try:
            return SESSION.post(
                url,
                headers=cfg.headers if headers is None else headers,
                json=payload,
                timeout=(c_timeout, r_timeout),
            )
        except requests.exceptions.ReadTimeout:
            raise  # 连接已建立，不可重放
        except (requests.exceptions.ConnectTimeout,
                requests.exceptions.ConnectionError) as e:
            last_error = e
            if attempt >= max_retries:
                break
            wait = cfg.retry_backoff * (2 ** attempt)
            print(f"   🔁 建连失败（第 {attempt + 1}/{max_retries} 次重试，等待 {wait:.1f}s）: {e}")
            time.sleep(wait)

    if last_error is not None:
        raise last_error
    raise requests.exceptions.ConnectionError(url)


# ============================================================
#  通用 API 调用
# ============================================================
def call_api(endpoint, payload=None):
    """
    通用 API 调用。

    异常分类说明：ConnectTimeout 在 requests 中同时是 Timeout 与 ConnectionError
    的子类，若先捕获 Timeout 就会被误报为「请求超时」，从而掩盖真实的「连不上」。
    因此这里按 ConnectTimeout → ReadTimeout → ConnectionError 的顺序精确区分。
    """
    global _CONNECT_FAIL_STREAK

    cfg = _cfg()
    base = get_siyuan_base()
    url = f"{base}{endpoint}"
    try:
        resp = _post_json(url, payload)
        _CONNECT_FAIL_STREAK = 0
        if resp.status_code != 200:
            return {"code": -1, "msg": f"HTTP {resp.status_code}: {resp.text}"}
        data = resp.json()
        if data.get("code") != 0:
            return {"code": data.get("code", -1), "msg": data.get("msg", "未知错误")}
        return data
    except requests.exceptions.ConnectTimeout:
        _note_connect_failure()
        return {"code": -1, "msg": (
            f"无法建立到 {base} 的 TCP 连接（建连超时 {cfg.connect_timeout}s，"
            f"已重试 {cfg.connect_retries} 次）—— 请检查 WSL 网关漂移 / 防火墙 / 宿主机是否休眠"
        )}
    except requests.exceptions.ReadTimeout:
        return {"code": -1, "msg": (
            f"连接已建立但思源在 {cfg.read_timeout}s 内未响应（读取超时）: {endpoint}"
        )}
    except requests.exceptions.ConnectionError as e:
        _note_connect_failure()
        return {"code": -1, "msg": f"无法连接到 {base}（连接被拒绝或中断）: {e}"}
    except json.JSONDecodeError:
        return {"code": -1, "msg": "返回非 JSON 格式"}
    except Exception as e:
        return {"code": -1, "msg": str(e)}


# ============================================================
#  目录 & 文件遍历
# ============================================================
def list_dir_entries(dir_path):
    result = call_api("/api/file/readDir", {"path": dir_path})
    if result.get("code") != 0:
        print(f"  ⚠️  读取目录失败 [{dir_path}]：{result.get('msg')}")
        return []
    return result.get("data", [])


def find_target_dirs():
    cfg = _cfg()
    matched = []
    for target in cfg.target_folders:
        target_stripped = target.strip()

        # 1) SQL 模糊匹配（全量查询，不限制笔记本）
        sql = (f"SELECT id, hpath, content, box FROM blocks "
               f"WHERE type='d' AND (content LIKE '%{target_stripped}%' OR hpath LIKE '%{target_stripped}%') "
               f"ORDER BY hpath ASC")
        result = call_api("/api/query/sql", {"stmt": sql})
        if result.get("code") == 0:
            rows = result.get("data", [])
            print(f"  [DEBUG] SQL 查询结果: {rows}")
            if rows:
                # 优先匹配当前笔记本中的文档
                best_row = None
                for row in rows:
                    nb_id = row.get("box", "")
                    if nb_id == cfg.notebook_id:
                        best_row = row
                        break
                if best_row is None:
                    best_row = rows[0]

                block_id = best_row.get("id", "")
                hpath = best_row.get("hpath", "")
                if block_id:
                    matched.append({
                        "name": target_stripped,
                        "id": block_id,
                        "hpath": hpath,
                        "type": "doc"
                    })
                    print(f"   📂 找到目录 {target_stripped} → hpath: {hpath}, id: {block_id}")
                    continue
        else:
            print(f"  [ERROR] SQL 查询失败: {result.get('msg')}")

        # 2) SQL 没找到 → 尝试匹配笔记本名称（去除前后空格后比较）
        nb_result = call_api("/api/notebook/lsNotebooks")
        if nb_result.get("code") == 0:
            notebooks = nb_result.get("data", [])
            found_nb = False
            for nb in notebooks:
                nb_name = nb.get("name", "").strip()
                nb_id = nb.get("id", "")
                if target_stripped in nb_name:
                    matched.append({
                        "name": target_stripped,
                        "id": nb_id,
                        "hpath": f"/{nb_name}",
                        "type": "notebook"
                    })
                    print(f"   📂 找到目录 {target_stripped} → 匹配笔记本名称: {nb_name} (id: {nb_id})")
                    found_nb = True
                    break
            if not found_nb:
                print(f"  DEBUG: 尝试匹配关键词 '{target_stripped}' 失败，请检查思源中是否存在该标题的文档。")
        else:
            print(f"  DEBUG: 尝试匹配关键词 '{target_stripped}' 失败，请检查思源中是否存在该标题的文档。")
    return matched


def list_sy_files_in_dir(target_dir):
    """
    使用 SQL 查询获取目标目录下的所有子文档。
    不再依赖 /api/file/readDir 物理扫描文件系统。

    参数：
      target_dir: dict，包含 "id" 和 "hpath" 两个 key
                  - id: 目标文档的 block ID
                  - hpath: 目标文档的 hpath（路径字符串）

    返回：
      [(doc_name, doc_id), ...]  其中 doc_name 为文档标题的末段（用于显示）
    """
    hpath = target_dir.get("hpath", "")
    doc_id = target_dir.get("id", "")

    sql = (f"SELECT id, content FROM blocks "
           f"WHERE type='d' AND hpath LIKE '{hpath}/%' "
           f"ORDER BY hpath ASC")
    result = call_api("/api/query/sql", {"stmt": sql})

    sy_files = []
    if result.get("code") == 0:
        rows = result.get("data", [])
        for row in rows:
            child_id = row.get("id", "")
            content = row.get("content", "")
            if child_id:
                # 从 content 提取标题（content 中第一段通常是标题文本）
                # content 可能是纯文本，提取第一行作为 doc_name
                title_line = content.strip().split("\n")[0] if content else child_id
                sy_files.append((title_line, child_id))
    else:
        print(f"  ⚠️  SQL 查询目录子文档失败 [{hpath}]：{result.get('msg')}")

    return sy_files


def get_doc_title(doc_id):
    result = call_api("/api/filetree/getHPathByID", {"id": doc_id})
    if result.get("code") != 0:
        return None
    return result.get("data")


def get_block_kramdown(doc_id):
    result = call_api("/api/block/getBlockKramdown", {"id": doc_id})
    if result.get("code") != 0:
        return None
    data = result.get("data")
    if isinstance(data, dict):
        return data.get("kramdown", "")
    return ""


# ============================================================
#  物理图片路径映射（增强版）
# ============================================================
def map_image_path(api_image_path):
    """
    将思源 API 返回的图片路径映射到物理文件路径。
    支持多种路径格式：
      - /assets/xxx.png
      - assets/xxx.png
      - /data/{NOTEBOOK_ID}/assets/xxx.png
    """
    cfg = _cfg()
    data_path = cfg.siyuan_data_path
    notebook_id = cfg.notebook_id
    clean = api_image_path.lstrip("/")

    # 候选 1: /assets/xxx.png → SIYUAN_DATA_PATH / assets / xxx.png
    if clean.startswith("assets/"):
        cand = os.path.join(data_path, clean)
        if os.path.isfile(cand):
            return cand

    # 候选 2: SIYUAN_DATA_PATH / clean
    cand = os.path.join(data_path, clean)
    if os.path.isfile(cand):
        return cand

    # 候选 3: SIYUAN_DATA_PATH / notebook_id / clean
    cand = os.path.join(data_path, notebook_id, clean)
    if os.path.isfile(cand):
        return cand

    # 候选 4: 如果 clean 包含 notebook_id/，去掉 notebook_id/ 前缀再试
    if clean.startswith(notebook_id + "/"):
        sub = clean[len(notebook_id) + 1:]
        cand = os.path.join(data_path, sub)
        if os.path.isfile(cand):
            return cand

    # 候选 5: 用 basename 在 data 根目录和笔记本目录下搜索
    for root_dir in [data_path, os.path.join(data_path, notebook_id)]:
        full = os.path.join(root_dir, os.path.basename(clean))
        if os.path.isfile(full):
            return full

    # 候选 6: /data/{NOTEBOOK_ID}/assets/xxx.png → SIYUAN_DATA_PATH / assets/xxx.png
    if api_image_path.startswith("/data/"):
        cand = api_image_path.replace("/data/", data_path + "/", 1)
        if os.path.isfile(cand):
            return cand

    return None


# ============================================================
#  资源读取兜底：通过思源 API 直接拉取图片（不依赖本地文件系统）
# ============================================================
def _asset_api_candidates(api_image_path):
    """
    根据思源 API 返回的图片路径，生成 /api/file/getFile 的候选路径。
    思源资源统一存放在 workspace/data/assets/ 下，因此 'assets/xxx' 需补成 'data/assets/xxx'。
    """
    notebook_id = _cfg().notebook_id
    clean = (api_image_path or "").lstrip("/")
    cands = []
    if clean.startswith("assets/"):
        cands.append("data/" + clean)
    if clean.startswith("data/"):
        cands.append(clean)
    if clean.startswith(notebook_id + "/"):
        sub = clean[len(notebook_id) + 1:]
        if sub.startswith("assets/"):
            cands.append("data/" + sub)
    cands.append(clean)  # 兜底
    # 去重且保持顺序
    seen, out = set(), []
    for c in cands:
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out


def get_asset_bytes(api_image_path):
    """
    通过思源 /api/file/getFile 接口拉取资源文件字节（带缓存）。

    这是对 map_image_path（本地文件系统）的兜底方案：
    当 SIYUAN_DATA_PATH 配置错误、目录不存在或与新版思源不一致时，
    仍能取到图片，从而修复“题目/答案卡片正文（含图片）为空”的问题。

    与旧实现的区别：网络层失败不再被 `except Exception: continue` 静默吞掉，
    而是打印一次告警，避免出现「图片无声丢失却毫无提示」的情况。

    返回 bytes，失败返回 None。
    """
    global _ASSET_FETCH_FAILED

    if not api_image_path:
        return None
    if api_image_path in _ASSET_BYTES_CACHE:
        return _ASSET_BYTES_CACHE[api_image_path]

    cfg = _cfg()
    url = f"{get_siyuan_base()}/api/file/getFile"
    result = None
    network_error = None

    for cand in _asset_api_candidates(api_image_path):
        for payload_path in (f"/{cand}", cand):
            try:
                resp = _post_json(
                    url,
                    {"path": payload_path},
                    connect_timeout=cfg.probe_timeout,
                    read_timeout=cfg.read_timeout,
                    retries=0,  # 图片数量多，不做重试，避免整体变慢
                )
            except requests.exceptions.RequestException as e:
                network_error = e
                continue
            if resp.status_code == 200 and resp.content:
                ctype = resp.headers.get("Content-Type", "")
                # 成功时返回原始字节；失败时思源返回 JSON（application/json）
                if "application/json" not in ctype:
                    result = resp.content
                    break
        if result is not None:
            break

    if result is not None:
        _ASSET_BYTES_CACHE[api_image_path] = result
        return result

    _ASSET_FETCH_FAILED += 1
    if api_image_path not in _ASSET_ERROR_LOGGED:
        _ASSET_ERROR_LOGGED.add(api_image_path)
        if network_error is not None:
            print(f"   ⚠️  图片获取失败（网络层）: {api_image_path} — {network_error}")
        else:
            print(f"   ⚠️  图片获取失败（资源不存在或非图片）: {api_image_path}")

    if network_error is None:
        # 已取得确定的 HTTP 响应（资源不存在等）→ 负缓存，避免重复请求；
        # 网络层失败不做负缓存，后续仍有机会重试成功。
        _ASSET_BYTES_CACHE[api_image_path] = None
    return None


def _image_data_uri(api_image_path):
    """将思源资源图片转换为 data: base64 URI（用于 WeasyPrint 内嵌），失败返回 None。"""
    raw = get_asset_bytes(api_image_path)
    if not raw:
        return None
    ext = os.path.splitext(api_image_path)[1].lower().lstrip(".")
    mime = _IMAGE_MIME.get(ext, "png")
    b64 = base64.b64encode(raw).decode("ascii")
    return f"data:image/{mime};base64,{b64}"


def _get_pil_image(api_image_path):
    """
    尝试获取 PIL Image 对象：优先本地文件，失败则回退 API 字节。
    返回 Image 对象或 None（调用方负责 close）。
    """
    if not _HAS_PIL:
        return None
    phys = map_image_path(api_image_path)
    try:
        if phys is not None:
            return Image.open(phys)
        raw = get_asset_bytes(api_image_path)
        if raw:
            return Image.open(io.BytesIO(raw))
    except Exception:
        return None
    return None
