# -*- coding: utf-8 -*-
"""
思源笔记 —— 错题拼卷机 (HTML+WeasyPrint 版)

依赖安装：
  pip install requests Pillow python-docx weasyprint

功能：
  1. 从思源笔记 API 获取文档内容
  2. 按积分筛选已掌握/待练习题目
  3. 生成 HTML 练习卷 + 答案卷，使用 CSS 打印媒体排版
  4. 用 WeasyPrint 将 HTML 编译为 PDF

升级 v2 特性：
  - 优先排序：易错池按积分升序（多次错误优先），基础池也按积分升序
  - 入选顺序：先基础、再易错、最后困难补充
  - 困难题在标题前加 ⭐ 标注
  - 新增 MAX_PAGES 最大页数限制（默认 10）
  - 新增 EXCLUDE_RECENT_DAYS 排除最近录入/复习题目（默认 2 天）
"""

import requests
import json
import re
import os
import sys
import math
import html
import io
import base64
import socket
import subprocess
import time
import urllib.parse
from datetime import datetime, timedelta

# 步骤1：配置迁移到独立模块（Config 数据类 + 单例），本模块保留兼容 shim
import config as config_module

try:
    from PIL import Image
    _HAS_PIL = True
except ImportError:
    _HAS_PIL = False
    print("⚠️  未安装 Pillow，将使用保守的紧凑判断。请运行: pip install Pillow")

try:
    from weasyprint import HTML
    from weasyprint.text.fonts import FontConfiguration
    _HAS_WEASYPRINT = True
except ImportError:
    _HAS_WEASYPRINT = False
    print("⚠️  未安装 WeasyPrint，将无法生成 PDF。请运行: pip install weasyprint")


# ============================================================
#  网络地址解析与连通性预检（WSL → Windows 宿主机）
# ============================================================
# 背景：WSL2 默认 NAT 模式下，Windows 宿主机地址 == WSL 的默认网关
# （例如 172.20.0.1/20）。该网段会随 Windows 重启、休眠恢复、网络切换
# （Wi-Fi / 有线 / VPN）而被重新分配，因此把地址硬编码进 config.json 会周期性失效。
#
# 失效时的表现是「连接静默超时」而非「连接被拒绝」：
#   File ".../urllib3/util/connection.py", line 73, in create_connection
#     sock.connect(sa)        ← SYN 无人应答 → requests.exceptions.ConnectTimeout
# 所以这里在运行时动态解析宿主机地址，并用短超时 TCP 预检快速筛选候选。
#
# 注意：不要用 /etc/resolv.conf 的 nameserver 作为宿主机地址。新版 WSL 的
# DNS 隧道会把 nameserver 设为 10.255.255.254，那是 DNS 代理而非宿主机。

# ---- 网络相关默认值（load_config() 会用 config.json 覆盖）----
SIYUAN_HOST_MODE = "auto"     # auto: 多候选自动探测；fixed: 仅用 config 中的地址
CONNECT_TIMEOUT = 3           # TCP 建连超时（秒）
READ_TIMEOUT = 30             # 响应读取超时（秒）
CONNECT_RETRIES = 2           # 建连失败后的额外重试次数
RETRY_BACKOFF = 0.5           # 重试退避基数（秒）：等待 = RETRY_BACKOFF * 2^attempt
PROBE_TIMEOUT = 1.5           # 单个候选地址的 TCP 预检超时（秒）

# ---- 其余配置项默认值（由 load_config() 从 config.json 显式填充）----
# 步骤1：为消除导入期副作用，这里先给出安全默认值，
#        使得 `import siyuan_client` 不再触发读取 config.json。
SIYUAN_URL = ""
API_TOKEN = ""
NOTEBOOK_ID = ""
SIYUAN_DATA_PATH = ""
TARGET_FOLDERS = []
SCORE_THRESHOLD = 3
MIN_PAGES = 2
MAX_PAGES = 10
EXCLUDE_RECENT_DAYS = 2
HEADERS = {}

# ---- 运行时状态 ----
SIYUAN_BASE = ""              # 解析出的实际基址，如 http://172.20.0.1:6806
_SIYUAN_BASE_RESOLVED = False
_ASSET_ERROR_LOGGED = set()   # 已告警过的图片路径，避免重复刷屏
_ASSET_FETCH_FAILED = 0       # 图片经 API 兜底仍失败的累计次数
_CONNECT_FAIL_STREAK = 0      # 连续建连失败次数
_CONNECT_FAIL_BANNER_SHOWN = False

# 复用同一个 Session：复用已建立的 TCP 连接，避免每个请求都重新三次握手
SESSION = requests.Session()
_SESSION_ADAPTER = requests.adapters.HTTPAdapter(pool_connections=10, pool_maxsize=10)
SESSION.mount("http://", _SESSION_ADAPTER)
SESSION.mount("https://", _SESSION_ADAPTER)


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

    以 PROBE_TIMEOUT（1.5s）级别的成本筛选候选地址，
    避免每个失效候选都白等完整的 CONNECT_TIMEOUT。
    """
    probe_timeout = PROBE_TIMEOUT if timeout is None else timeout
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

    if _SIYUAN_BASE_RESOLVED and not force:
        return SIYUAN_BASE or None

    scheme, configured_host, port = _split_siyuan_url(SIYUAN_URL)

    if str(SIYUAN_HOST_MODE).lower() == "fixed":
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
            print(f"   ⚠️  候选地址不可达: {host}:{port}（{PROBE_TIMEOUT}s TCP 预检未通过）")

    SIYUAN_BASE = ""
    _SIYUAN_BASE_RESOLVED = True  # 已探测过，避免每次调用重复探测
    return None


def get_siyuan_base():
    """获取基址；尚未解析时先解析（防御性懒加载），全部失败则回退到配置值。"""
    if not _SIYUAN_BASE_RESOLVED:
        resolve_siyuan_base()
    return SIYUAN_BASE or SIYUAN_URL.rstrip("/")


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
    c_timeout = CONNECT_TIMEOUT if connect_timeout is None else connect_timeout
    r_timeout = READ_TIMEOUT if read_timeout is None else read_timeout
    max_retries = CONNECT_RETRIES if retries is None else retries

    last_error = None
    for attempt in range(max_retries + 1):
        try:
            return SESSION.post(
                url,
                headers=HEADERS if headers is None else headers,
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
            wait = RETRY_BACKOFF * (2 ** attempt)
            print(f"   🔁 建连失败（第 {attempt + 1}/{max_retries} 次重试，等待 {wait:.1f}s）: {e}")
            time.sleep(wait)

    if last_error is not None:
        raise last_error
    raise requests.exceptions.ConnectionError(url)


# ============================================================
#  配置加载
# ============================================================
def load_config(config_path="config.json"):
    """
    加载配置并同步到本模块全局变量（迁移期兼容 shim）。

    步骤1 说明：
      - 真正的解析逻辑已迁移至 [`config.py`](config.py) 的 Config / load_config；
      - 本函数保留原名称与原全局变量名，把 Config 的字段回填到模块全局作用域，
        使其余函数仍可按旧方式读取 SIYUAN_URL / TARGET_FOLDERS / HEADERS 等，
        从而在不改动业务逻辑的前提下完成迁移；
      - 本函数**不再在模块导入时自动执行**（原第 362 行的裸调用已删除），
        改由 main() 显式调用，消除导入期副作用。
    """
    global SIYUAN_URL, API_TOKEN, NOTEBOOK_ID, SIYUAN_DATA_PATH
    global TARGET_FOLDERS, SCORE_THRESHOLD, MIN_PAGES, MAX_PAGES, EXCLUDE_RECENT_DAYS, HEADERS
    global SIYUAN_HOST_MODE, CONNECT_TIMEOUT, READ_TIMEOUT, CONNECT_RETRIES, RETRY_BACKOFF
    global PROBE_TIMEOUT

    cfg = config_module.load_config(config_path)
    config_module.set_config(cfg)

    SIYUAN_URL = cfg.siyuan_url
    API_TOKEN = cfg.api_token
    NOTEBOOK_ID = cfg.notebook_id
    SIYUAN_DATA_PATH = cfg.siyuan_data_path
    TARGET_FOLDERS = cfg.target_folders
    SCORE_THRESHOLD = cfg.score_threshold
    MIN_PAGES = cfg.min_pages
    MAX_PAGES = cfg.max_pages
    EXCLUDE_RECENT_DAYS = cfg.exclude_recent_days
    SIYUAN_HOST_MODE = cfg.siyuan_host_mode
    CONNECT_TIMEOUT = cfg.connect_timeout
    READ_TIMEOUT = cfg.read_timeout
    CONNECT_RETRIES = cfg.connect_retries
    RETRY_BACKOFF = cfg.retry_backoff
    PROBE_TIMEOUT = cfg.probe_timeout
    HEADERS = cfg.headers

    # 配置变更后使地址解析缓存失效，确保下次调用重新探测
    reset_resolved_base()
    return cfg


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
            f"无法建立到 {base} 的 TCP 连接（建连超时 {CONNECT_TIMEOUT}s，"
            f"已重试 {CONNECT_RETRIES} 次）—— 请检查 WSL 网关漂移 / 防火墙 / 宿主机是否休眠"
        )}
    except requests.exceptions.ReadTimeout:
        return {"code": -1, "msg": (
            f"连接已建立但思源在 {READ_TIMEOUT}s 内未响应（读取超时）: {endpoint}"
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
    matched = []
    for target in TARGET_FOLDERS:
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
                    if nb_id == NOTEBOOK_ID:
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


def parse_latest_date(md_source):
    """
    从文档正文的 Markdown 表格中提取最新练习/录入日期。
    查找包含"录入与练习日期"列的表，从最后一行向上遍历，
    提取对应列的日期文本并解析为 datetime 对象。

    支持的日期格式：2026年4月12日、2026-04-12、2026/4/12 等。

    返回 datetime 对象，如果未找到任何日期则返回 None。
    """
    if not md_source:
        return None

    # 1) 找出包含"录入与练习日期"文本的表格块
    table_blocks = re.findall(r'^(?:\|.*\n?)+', md_source, re.MULTILINE)

    target_block = None
    for block in table_blocks:
        if "录入与练习日期" in block:
            target_block = block
            break

    if target_block is None:
        return None

    # 2) 按行分割并找出表头行
    lines = [l.strip() for l in target_block.split("\n") if l.strip()]

    # 找到表头行（包含"录入与练习日期"的行）
    date_col_idx = 2  # 默认索引 2
    for line in lines:
        if "录入与练习日期" in line:
            # 通过 | 分割找出"录入与练习日期"所在的列索引
            parts = [p.strip() for p in line.split("|")]
            for j, p in enumerate(parts):
                if "录入与练习日期" in p:
                    date_col_idx = j
                    break
            break

    # 3) 从该表格的最后一行向上遍历数据行
    for line in reversed(lines):
        # 跳过分隔行
        if re.match(r'^[\s\|:\-]+$', line) and "---" in line:
            continue
        # 跳过表头行
        if "录入与练习日期" in line:
            continue

        parts = [p.strip() for p in line.split("|")]
        if date_col_idx >= len(parts):
            continue

        cell_text = parts[date_col_idx]
        # 清理 {:...} 属性
        cell_text = re.sub(r'\{:\s*[^}]*\}', '', cell_text).strip()

        if not cell_text:
            continue

        # 4) 使用正则匹配日期：兼容 2026年4月12日、2026-04-12、2026/4/12 等
        m = re.search(r'(\d{4})\s*[-年/]\s*(\d{1,2})\s*[-月/]\s*(\d{1,2})', cell_text)
        if m:
            year = int(m.group(1))
            month = int(m.group(2))
            day = int(m.group(3))
            try:
                return datetime(year, month, day)
            except ValueError:
                continue

    # 5) 整个表格没有解析到任何日期，返回 None
    return None


# ============================================================
#  分类 / 标题格式化
# ============================================================
def classify_document(title):
    t = title
    if "困难" in t:
        return "困难"
    if "易错" in t:
        return "易错"
    if "基础" in t:
        return "基础"
    return "基础"


def extract_parent_ds(title):
    parts = title.lstrip("/").split("/")
    if len(parts) >= 1:
        first = parts[0]
        m = re.search(r'([A-Z]+\d+)', first)
        if m:
            return m.group(1)
        # 如果找不到 [A-Z]+\d+ 模式，检查是否包含 TARGET_FOLDERS 中的关键词
        for target in TARGET_FOLDERS:
            if target in first:
                return target
        return first.split("#")[0].strip()
    return ""


def format_question_title(full_title):
    parent_ds = extract_parent_ds(full_title)

    parts = full_title.split("/")
    last = parts[-1] if parts else full_title

    num_match = re.search(r'(\d+)', last)
    doc_num = num_match.group(1) if num_match else ""

    if parent_ds and doc_num:
        return f"{parent_ds}: {doc_num}"
    elif parent_ds:
        return parent_ds
    elif doc_num:
        return doc_num
    else:
        clean = last.split("#")[0].strip()
        return clean


def shorten_title(title):
    parts = title.split("/")
    last = parts[-1] if parts else title
    clean = last.split("#")[0].strip()
    return clean


# ============================================================
#  文档源码获取
# ============================================================
def get_block_kramdown(doc_id):
    result = call_api("/api/block/getBlockKramdown", {"id": doc_id})
    if result.get("code") != 0:
        return None
    data = result.get("data")
    if isinstance(data, dict):
        return data.get("kramdown", "")
    return ""


# ============================================================
#  积分表解析（重写版）
# ============================================================
def parse_score_table(md_source):
    """
    解析文档中第一个包含"总积分"字样的 Markdown 表格。
    特征：表格固定 5 列，行可能以 | 或 || 开头。
    逻辑：
      - 用 re.findall 抓取表格块（| 开头到空行之间的段落）
      - 找到包含"总积分"的表格
      - 按行分割，从最后一行向上遍历
      - 对每一行按 | 分割，检查倒数第二个元素（parts[-2]，因为表格通常以 | 结尾）
      - 跳过空单元格行（如 |练习|||||），继续向上找
      - 找到包含数字（含负数）的行后，提取数字返回
    返回 int，无表格返回 None。
    """
    if not md_source:
        return None

    # 1) 用 re.findall 抓取所有表格块
    table_blocks = re.findall(r'^(?:\|.*\n?)+', md_source, re.MULTILINE)

    # 2) 找到包含"总积分"的表格
    target_block = None
    for block in table_blocks:
        if "总积分" in block:
            target_block = block
            break

    if target_block is None:
        return None

    # 3) 按行分割
    lines = [l.strip() for l in target_block.split("\n") if l.strip()]

    # 4) 从最后一行向上遍历，寻找倒数第二列包含数字的数据行
    for line in reversed(lines):
        # 跳过分隔行
        if re.match(r'^[\s\|:\-]+$', line) and "---" in line:
            continue
        if line.count("|") < 2:
            continue

        # 按 | 分割，取倒数第二个元素（表格通常以 | 结尾，content 在 parts[-2]）
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 3:
            continue
        target_cell = parts[-2]

        # 清理 {:...} 属性
        target_cell_clean = re.sub(r'\{:\s*[^}]*\}', '', target_cell).strip()

        print(f"  [DEBUG] 检查行: {line}")
        print(f"  [DEBUG] 倒数第二列内容: {target_cell_clean!r}")

        # 如果单元格为空，继续向上找
        if not target_cell_clean:
            continue

        # 提取数字（支持负数）
        m = re.search(r'(-?\d+(?:\.\d+)?)', target_cell_clean)
        if m:
            value = int(float(m.group(1)))
            print(f"  [DEBUG] 提取到积分值: {value}")
            return value

    # 所有数据行都遍历完仍未找到数字
    return None


# ============================================================
#  Kramdown 清洗工具（兼容思源新版 IAL 格式）
# ============================================================
# 思源新版会在块的正文/标题行后附加 IAL 属性块，例如：
#   # 题目 {: id="..." updated="..."}
#   > 正文
#   > {: id="..." updated="..."}
#   >
#   {: id="..." updated="..."}
# 因此这里先统一清洗 IAL，再做标题匹配与正文提取。
_IAL_RE = re.compile(r'\{:\s*[^}]*\}')


def _strip_ial(text):
    """移除行内 Kramdown IAL 属性块 {: ... }（兼容行内与独立成行两种写法）。"""
    return _IAL_RE.sub('', text or '')


def _heading_text(line):
    """
    若该行是 Markdown 标题，返回去掉 # 前缀与 IAL 后的标题文本；否则返回 None。
    兼容：'# 题目'、'## 答案'、'## 答案 {: id="..."}'、'## 💡 答案'、'#题目' 等。
    """
    m = re.match(r'^\s*(#{1,6})\s*(.*)$', line or '')
    if not m:
        return None
    return _strip_ial(m.group(2)).strip()


def _is_question_heading(heading_text):
    """判断是否为题目区标题（# 题目 / ## 题目），排除“扩展题目XX”。"""
    if not heading_text:
        return False
    core = heading_text.strip('* \t　')
    return core.startswith('题目')


def _is_answer_heading(heading_text):
    """
    判断是否为主答案区标题（## 答案 / ## 💡 答案），
    排除“扩展题目01 答案”这类带前缀的标题。
    """
    if not heading_text:
        return False
    core = heading_text.strip('* \t　').lstrip('💡✅☑✔️').strip()
    return core.startswith('答案')


def _clean_md_line(line):
    """
    清洗单行 Kramdown 正文：
      1. 去除 IAL 属性块 {: ... }
      2. 去除引用标记 >
      3. 去除图片语法与 [图片] 占位符
    返回清洗后的文本（可能为空字符串）。
    """
    s = _strip_ial(line).strip()
    s = re.sub(r'^>\s*', '', s)
    s = re.sub(r'!\[.*?\]\([^)]+\)', '', s)
    s = re.sub(r'\s*\[图片\]\s*', '', s)
    return s.strip()


def _extract_block(content_lines):
    """
    从一组正文行中提取文字与图片路径。
    注意：图片路径必须在清洗 IAL / 引用标记之前提取。
    返回 {"text": "...", "images": [...]}，若均无内容则返回 None。
    """
    # 1) 先提取图片路径（在清理 {:...} 与 > 之前）
    image_paths = []
    for line in content_lines:
        for m in re.finditer(r'!\[.*?\]\(\s*<?(/?)assets/([^)>\s]+)', line):
            prefix = "/" if m.group(1) else ""
            full = f"{prefix}assets/{m.group(2)}"
            if full not in image_paths:
                image_paths.append(full)

    # 2) 再清洗文本
    text_parts = []
    for line in content_lines:
        s = _clean_md_line(line)
        if s:
            text_parts.append(s)

    text = "\n".join(text_parts).strip()
    text = re.sub(r'\s*\[图片\]\s*', '', text)
    # 去除 Kramdown 转义符：\任何字符 → 字符本身
    text = re.sub(r'\\(.)', r'\1', text)

    if not text and not image_paths:
        return None
    return {"text": text, "images": image_paths}


# ============================================================
#  精准题目解析（练习卷用）— 重写版 v2.4
# ============================================================
def parse_question(md_source):
    """
    从源码中提取题目内容。
    匹配：以 # 题目 / ## 题目 为标题的行（兼容行尾附带 IAL 属性）。
    结束：遇到主答案标题（## 答案 / ## 💡 答案 / # 答案 等）。
    提取图片路径时需在清理 {:...} 之前执行。
    返回 {"text": "...", "images": [...]}，没有则返回 None。
    """
    if not md_source:
        return None

    raw_lines = md_source.split("\n")

    # 1) 定位题目标题（兼容行尾 IAL）
    start_idx = -1
    for i, line in enumerate(raw_lines):
        if _is_question_heading(_heading_text(line)):
            start_idx = i
            break

    if start_idx == -1:
        return None

    # 2) 截取到主答案标题为止（# 答案 / ## 答案 / ## 💡 答案 及其带 IAL 的写法）
    content_lines = []
    for line in raw_lines[start_idx + 1:]:
        if _is_answer_heading(_heading_text(line)):
            break
        content_lines.append(line)

    return _extract_block(content_lines)


# ============================================================
#  答案解析（答案卷用）— 重写版 v2.4
# ============================================================
def parse_answer(md_source):
    """
    从源码中提取答案内容。
    匹配：主答案标题（## 答案 / ## 💡 答案 及其带 IAL 的写法）。
    结束：遇到下一个非答案标题（例如 ## 扩展题目01）或文档结尾。
    提取图片路径时需在清理 {:...} 之前执行。
    返回 {"text": "...", "images": [...]}，没有则返回 None。
    """
    if not md_source:
        return None

    raw_lines = md_source.split("\n")

    # 1) 定位主答案标题（兼容行尾 IAL）
    start_idx = -1
    for i, line in enumerate(raw_lines):
        if _is_answer_heading(_heading_text(line)):
            start_idx = i
            break

    if start_idx == -1:
        return None

    # 2) 取到下一个非答案标题为止（自动跳过“扩展题目”等后续区块）
    content_lines = []
    for line in raw_lines[start_idx + 1:]:
        ht = _heading_text(line)
        if ht is not None and not _is_answer_heading(ht):
            break
        content_lines.append(line)

    return _extract_block(content_lines)


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
    clean = api_image_path.lstrip("/")

    # 候选 1: /assets/xxx.png → SIYUAN_DATA_PATH / assets / xxx.png
    if clean.startswith("assets/"):
        cand = os.path.join(SIYUAN_DATA_PATH, clean)
        if os.path.isfile(cand):
            return cand

    # 候选 2: SIYUAN_DATA_PATH / clean
    cand = os.path.join(SIYUAN_DATA_PATH, clean)
    if os.path.isfile(cand):
        return cand

    # 候选 3: SIYUAN_DATA_PATH / notebook_id / clean
    cand = os.path.join(SIYUAN_DATA_PATH, NOTEBOOK_ID, clean)
    if os.path.isfile(cand):
        return cand

    # 候选 4: 如果 clean 包含 notebook_id/，去掉 notebook_id/ 前缀再试
    if clean.startswith(NOTEBOOK_ID + "/"):
        sub = clean[len(NOTEBOOK_ID) + 1:]
        cand = os.path.join(SIYUAN_DATA_PATH, sub)
        if os.path.isfile(cand):
            return cand

    # 候选 5: 用 basename 在 data 根目录和笔记本目录下搜索
    for root_dir in [SIYUAN_DATA_PATH, os.path.join(SIYUAN_DATA_PATH, NOTEBOOK_ID)]:
        full = os.path.join(root_dir, os.path.basename(clean))
        if os.path.isfile(full):
            return full

    # 候选 6: /data/{NOTEBOOK_ID}/assets/xxx.png → SIYUAN_DATA_PATH / assets/xxx.png
    if api_image_path.startswith("/data/"):
        cand = api_image_path.replace("/data/", SIYUAN_DATA_PATH + "/", 1)
        if os.path.isfile(cand):
            return cand

    return None


# ============================================================
#  资源读取兜底：通过思源 API 直接拉取图片（不依赖本地文件系统）
# ============================================================
_ASSET_BYTES_CACHE = {}

# 常见图片扩展名 → MIME 子类型
_IMAGE_MIME = {
    "jpg": "jpeg", "jpeg": "jpeg", "png": "png", "gif": "gif",
    "webp": "webp", "bmp": "bmp", "svg": "svg+xml", "ico": "x-icon",
}


def _asset_api_candidates(api_image_path):
    """
    根据思源 API 返回的图片路径，生成 /api/file/getFile 的候选路径。
    思源资源统一存放在 workspace/data/assets/ 下，因此 'assets/xxx' 需补成 'data/assets/xxx'。
    """
    clean = (api_image_path or "").lstrip("/")
    cands = []
    if clean.startswith("assets/"):
        cands.append("data/" + clean)
    if clean.startswith("data/"):
        cands.append(clean)
    if clean.startswith(NOTEBOOK_ID + "/"):
        sub = clean[len(NOTEBOOK_ID) + 1:]
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

    url = f"{get_siyuan_base()}/api/file/getFile"
    result = None
    network_error = None

    for cand in _asset_api_candidates(api_image_path):
        for payload_path in (f"/{cand}", cand):
            try:
                resp = _post_json(
                    url,
                    {"path": payload_path},
                    connect_timeout=PROBE_TIMEOUT,
                    read_timeout=READ_TIMEOUT,
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


# ============================================================
#  页数估算
# ============================================================
def estimate_lines(item):
    text = item[4] if len(item) > 4 else ""
    images = item[5] if len(item) > 5 else []
    title_lines = 1
    text_lines = max(1, math.ceil(len(text) / 50))
    image_lines = len(images) * 12
    blank_lines = 1
    return title_lines + text_lines + image_lines + blank_lines


def estimate_total_pages(items):
    if not items:
        return 0
    total_lines = sum(estimate_lines(it) for it in items)
    lines_per_page = 50
    return max(1, math.ceil(total_lines / lines_per_page))


def estimate_compact_height(item):
    """
    估算半栏题目在最终渲染时所需的物理高度（像素）。
    用于对半栏题按高度排序，使高度相近的题目自动配对到同一排，
    避免因高度差异过大触发跨页防断规则、在页底留下大量空白。

    计算方式：
      - 文本行数：按半栏宽度 20 字/行计算
      - 留白行数：纯文字题按文字行数的 0.8 倍 + 2（至少4行），有图片则不留白
      - 单行高度：30px（文本 + 留白统一标准）
      - 图片高度：按半栏宽度 340px 等比缩放
    """
    text = item[4] if len(item) > 4 else ""
    images = item[5] if len(item) > 5 else []

    h = 0
    # 1. 计算文本与留白高度
    text_lines = 0
    if text:
        text_lines = sum(math.ceil(len(p) / 20) for p in text.split("\n") if p.strip())

    spacer_lines = 0 if images else max(4, math.ceil(text_lines * 0.8) + 2)
    h += (text_lines + spacer_lines) * 30

    # 2. 计算图片高度（假设半栏宽度约为 340px）
    for img_path in images:
        im = _get_pil_image(img_path)
        if im is None:
            h += 200
            continue
        try:
            with im:
                w, img_h = im.size
                h += img_h * (340 / w) if w > 0 else 200
        except Exception:
            h += 200
    return h


# ============================================================
#  紧凑题目判断
# ============================================================
def is_compact_item(item):
    """
    判断题目是否为"小型题"（适合并排摆放）。
    标准：所有图片都是 Type 3 (Ratio < 1.3) 且文本行数 < 5。
    """
    text = item[4] if len(item) > 4 else ""
    images = item[5] if len(item) > 5 else []

    text_lines = len([l for l in text.split("\n") if l.strip()]) if text else 0
    if text_lines >= 5:
        return False

    if not _HAS_PIL or not images:
        return True

    for img_path in images:
        img = _get_pil_image(img_path)
        if img is None:
            continue
        try:
            with img:
                w_px, h_px = img.size
            ratio = w_px / h_px if h_px > 0 else 1.0
            if ratio >= 1.7:
                return False
        except Exception:
            continue

    return True


# ============================================================
#  选题引擎 (Selection Engine) — 升级版
# ============================================================
def select_questions(all_docs):
    """
    all_docs: [(doc_id, title, category, score), ...]
    返回最终入选的文档列表 [(doc_id, title, cat, score, text, images, answer_md), ...]

    升级 v2 排序与选择逻辑：
      1. 初筛：总积分 < SCORE_THRESHOLD 或 积分未检测到 → 进入候选池
      2. 分类：按 cat 分入"基础"、"易错"、"困难"三个池
      3. 排序：每个池均按积分升序排序（积分越低 → 错误越多 → 越优先）
      4. 入选顺序：先遍历加入"基础"题，再遍历加入"易错"题
      5. 困难补充：如果页数不足 MIN_PAGES，从"困难"池按积分升序补充
      6. 页数上限：每次添加前检查 estimate_total_pages(selected) 是否已达 MAX_PAGES，
         如果已达上限，立即停止添加任何题目
    """
    # 1) 初筛：总积分 < SCORE_THRESHOLD 或 积分未检测到
    eligible = []
    skipped_texts = []
    for d in all_docs:
        doc_id, title, cat, score = d[:4]
        if score is not None:
            if score >= SCORE_THRESHOLD:
                short = shorten_title(title)
                skipped_texts.append(short)
                print(f"跳过已掌握题目：{short}")
                continue
        eligible.append(d)

    if skipped_texts:
        print()

    # 2) 分类收集
    pools = {"基础": [], "易错": [], "困难": []}
    for d in eligible:
        cat = d[2]
        pools.setdefault(cat, []).append(d)

    def fetch_content(item):
        doc_id, title, cat, score = item[:4]
        md = get_block_kramdown(doc_id)
        parsed = parse_question(md) if md else None
        if parsed:
            return (doc_id, title, cat, score, parsed["text"], parsed["images"], md)
        return None

    # 3) 对每个池按积分升序排序（积分越低表示错误越多，越优先入选）
    #    注意：score 为 None 的题目视为"最优先"（未检测到积分表 = 全新题目）
    def sort_by_score_asc(d):
        """积分升序排序 key：None 视为 -1（最优先）"""
        s = d[3]
        return -1 if s is None else s

    for cat_name in pools:
        pools[cat_name].sort(key=sort_by_score_asc)

    # 4) 按顺序入选：先基础，再易错
    selected = []
    for cat in ["基础", "易错"]:
        for d in pools.get(cat, []):
            # 检查页数上限：如果已达 MAX_PAGES，停止添加
            if estimate_total_pages(selected) >= MAX_PAGES:
                print(f"   ⏹️  已达最大页数限制 {MAX_PAGES} 页，停止添加 {cat} 题")
                break
            item = fetch_content(d)
            if item:
                selected.append(item)

    # 5) 页数不足 MIN_PAGES 时，从困难池按积分升序补充
    current_pages = estimate_total_pages(selected)
    print(f"📊 当前已选题数: {len(selected)}，估算页数: ~{current_pages} 页")

    if current_pages < MIN_PAGES and pools.get("困难"):
        needed = pools["困难"]
        # 困难池已按积分升序排序，优先选择错误最多的困难题
        print(f"📌 页数不足 {MIN_PAGES} 页，从困难池补充（按积分升序，共 {len(needed)} 道候选题）……")

        for d in needed:
            if estimate_total_pages(selected) >= MIN_PAGES:
                break
            # 补充时也要检查最大页数限制
            if estimate_total_pages(selected) >= MAX_PAGES:
                print(f"   ⏹️  已达最大页数限制 {MAX_PAGES} 页，停止补充困难题")
                break
            item = fetch_content(d)
            if item:
                selected.append(item)
                print(f"   ➕ 补充困难题: {shorten_title(d[1])} (积分: {d[3]})")
    elif current_pages < MIN_PAGES:
        yellow = "\033[93m"
        reset = "\033[0m"
        print(f"\n{yellow}{'⚠️ ' * 10}")
        print(f"  ⚠️  警告：当前仅 ~{current_pages} 页，不足 {MIN_PAGES} 页，")
        print('       且无"困难"题可补充。请考虑增加 TARGET_FOLDERS 范围')
        print(f"       或降低 SCORE_THRESHOLD 阈值。")
        print(f"{'⚠️ ' * 10}{reset}\n")

    final_pages = estimate_total_pages(selected)
    print(f"📊 最终选题数: {len(selected)}，估算页数: ~{final_pages} 页")
    return selected


# ============================================================
#  HTML 工具函数
# ============================================================
def _html_image_tag(api_image_path, max_width="100%"):
    """
    将思源 API 图片路径转换为 HTML <img> 标签。

    优先使用本地文件（file:// 协议）；当本地映射失败（例如 SIYUAN_DATA_PATH
    配置错误或目录不存在）时，回退到思源 API 拉取图片并以内嵌 data: base64 URI
    渲染，确保图片不会丢失。
    返回空字符串表示两种方式都取不到图片。
    """
    style = (f'max-width: {max_width}; max-height: 80vh; '
             f'object-fit: contain; display: block; margin: 4px auto;')

    phys = map_image_path(api_image_path)
    if phys is not None:
        abs_path = os.path.abspath(phys)
        # 使用 file:// 协议嵌入本地图片，URL-encode 路径中的特殊字符（中文、空格等）
        abs_path_encoded = urllib.parse.quote(abs_path, safe='/:@!*()')
        return f'<img src="file://{abs_path_encoded}" style="{style}" />'

    # 本地映射失败 → 通过思源 API 拉取并内嵌为 data URI
    data_uri = _image_data_uri(api_image_path)
    if data_uri:
        return f'<img src="{data_uri}" style="{style}" />'
    return ""


def _html_escape(text):
    """转义 HTML 特殊字符（使用标准库 html.escape）。"""
    if not text:
        return ""
    return html.escape(text, quote=True)


def _html_build_question_body(item, idx, is_compact=False):
    """
    构建单道题目的 HTML 内容字符串（不含外层容器标签）。
    返回 (html_content, has_images) 元组。

    升级 v2：如果 cat == "困难"，在标题前加 ⭐ 符号。
    """
    doc_id, full_title, cat, score, text, images, answer_md = item
    display_title = format_question_title(full_title)

    # 升级 v2：困难题在标题前加 ⭐ 符号
    if cat == "困难":
        display_title = f"⭐ {display_title}"

    parts = []

    # 标题
    parts.append(f'<div class="q-title">{idx}. {_html_escape(display_title)}</div>')

    # 题目文本
    if text:
        for para in text.split("\n"):
            para = para.strip()
            if para:
                parts.append(f'<div class="q-text">{_html_escape(para)}</div>')

    # 图片
    has_images = bool(images)
    for img_path in images:
        tag = _html_image_tag(img_path)
        if tag:
            parts.append(f'<div class="q-image">{tag}</div>')

    # 预留做题书写空间
    # 如果题目包含图片（images 列表不为空），则不追加留白（因为图片通常自带答题空间）
    if not has_images:
        # 纯文字题目，根据文本量动态计算留白
        chars_per_line = 20 if is_compact else 40
        text_lines = 0
        for para in text.split("\n"):
            para = para.strip()
            if para:
                text_lines += math.ceil(len(para) / chars_per_line)
        spacer_lines = max(4, math.ceil(text_lines * 0.8) + 2)
        spacer_height = spacer_lines * 30
        parts.append(f'<div class="q-spacer" style="height: {spacer_height}px; width: 100%; display: block; color: transparent;">&nbsp;</div>')

    return "\n".join(parts), has_images


# ============================================================
#  HTML 源码生成器 —— 练习卷（重写版）
# ============================================================
def generate_html_practice(selected_items, current_time_str, target_folders_str):
    """
    生成 HTML 练习卷源码字符串。
    使用 CSS Grid 双栏布局 + 通栏排版，保持原始题目顺序。

    排版策略：
    - 按原始顺序遍历题目，保持题号连续
    - 紧凑型（compact）题目两两配对放入 CSS Grid 双栏容器
    - 常规型（normal）题目通栏排版
    - 每道题目的容器使用 break-inside: avoid 防止跨页截断
    - 相比上一版的改进：不再将所有 compact/normal 分组，而是保持自然顺序
    """
    date_str = current_time_str[:10]

    # 构建 CSS
    css = f"""
    @page {{
        size: A4;
        margin: 10mm;
        @bottom-center {{
            content: "{_html_escape(target_folders_str)} | {_html_escape(current_time_str)}";
            font-size: 9pt;
            color: #666;
        }}
    }}
    * {{
        box-sizing: border-box;
    }}
    body {{
        font-family: "Noto Sans CJK SC", "WenQuanYi Micro Hei", "Microsoft YaHei", "微软雅黑", "STHeiti", sans-serif;
        font-size: 11pt;
        line-height: 1.6;
        color: #222;
    }}
    .header {{
        text-align: center;
        font-size: 16pt;
        font-weight: bold;
        font-family: "Noto Sans CJK SC", "WenQuanYi Micro Hei", "Microsoft YaHei", "微软雅黑", "STHeiti", sans-serif;
        margin-bottom: 10mm;
        padding-bottom: 5mm;
        border-bottom: 2px solid #333;
    }}
    /* Grid 网格组 — 包裹连续紧凑题 */
    .compact-grid {{
        display: grid;
        grid-template-columns: 1fr 1fr;
        gap: 6mm;
        margin-bottom: 4mm;
        align-items: start;
    }}
    /* 通栏题目 */
    .full-width {{
        width: 100%;
        margin-bottom: 4mm;
    }}
    /* 单道题目的容器 — break-inside: avoid 防跨页 */
    .question-card {{
        break-inside: avoid;
        page-break-inside: avoid;
        padding: 2mm 3mm;
        border: 1px solid #ddd;
        border-radius: 2mm;
        background: #fafafa;
        /* 用 min-height 确保卡片不会收缩到 0 高度 */
        min-height: 20mm;
    }}
    .question-card .q-title {{
        font-weight: bold;
        font-size: 12pt;
        font-family: "Noto Sans CJK SC", "WenQuanYi Micro Hei", "Microsoft YaHei", "微软雅黑", "STHeiti", sans-serif;
        margin-bottom: 2mm;
    }}
    .question-card .q-text {{
        margin-bottom: 1mm;
        white-space: pre-wrap;
    }}
    .question-card .q-image {{
        text-align: center;
        margin: 2mm 0;
    }}
    .question-card .q-image img {{
        max-width: 100%;
        max-height: 80vh;
        object-fit: contain;
    }}
    @media print {{
        .question-card {{
            break-inside: avoid;
            page-break-inside: avoid;
        }}
    }}
    """

    # 构建 body
    body_parts = []
    body_parts.append(f'<div class="header">今日练习 {date_str}</div>')

    grid_buffer = []

    def flush_grid():
        if not grid_buffer:
            return
        if len(grid_buffer) == 1:
            # 只有1个半栏题时，直接转为通栏，避免右侧全空
            body_parts.append(f'<div class="full-width">{grid_buffer[0]}</div>')
        else:
            body_parts.append('<div class="compact-grid">')
            for card in grid_buffer:
                body_parts.append(card)
            body_parts.append('</div>')
        grid_buffer.clear()

    idx = 1
    for item in selected_items:
        is_compact = is_compact_item(item)
        html_body, _ = _html_build_question_body(item, idx, is_compact=is_compact)

        if is_compact:
            # 收集连续的半栏题目
            grid_buffer.append(f'<div class="question-card">{html_body}</div>')
        else:
            # 遇到通栏题目时，先清空输出之前的半栏网格
            flush_grid()
            body_parts.append(f'<div class="full-width"><div class="question-card">{html_body}</div></div>')

        idx += 1

    # 循环结束后，清空最后剩余的半栏网格
    flush_grid()

    html_body_str = "\n".join(body_parts)

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<style>{css}</style>
</head>
<body>
{html_body_str}
</body>
</html>"""
    return html


# ============================================================
#  HTML 源码生成器 —— 答案卷（重写版）
# ============================================================
def generate_html_answer(selected_items, current_time_str, target_folders_str):
    """
    生成 HTML 答案卷源码字符串。
    通栏排版，每道题显示序号 + 答案内容 + 答案图片。
    使用 break-inside: avoid 防止单个答案跨页。

    升级 v2：如果 cat == "困难"，在标题前加 ⭐ 符号。
    """
    date_str = current_time_str[:10]

    css = f"""
    @page {{
        size: A4;
        margin: 10mm;
        @bottom-center {{
            content: "答案 | {_html_escape(target_folders_str)} | {_html_escape(current_time_str)}";
            font-size: 9pt;
            color: #666;
        }}
    }}
    * {{
        box-sizing: border-box;
    }}
    body {{
        font-family: "Noto Sans CJK SC", "WenQuanYi Micro Hei", "Microsoft YaHei", "微软雅黑", "STHeiti", sans-serif;
        font-size: 11pt;
        line-height: 1.6;
        color: #222;
    }}
    .header {{
        text-align: center;
        font-size: 16pt;
        font-weight: bold;
        font-family: "Noto Sans CJK SC", "WenQuanYi Micro Hei", "Microsoft YaHei", "微软雅黑", "STHeiti", sans-serif;
        margin-bottom: 10mm;
        padding-bottom: 5mm;
        border-bottom: 2px solid #333;
    }}
    .answer-card {{
        break-inside: avoid;
        page-break-inside: avoid;
        padding: 3mm 4mm;
        margin-bottom: 4mm;
        border: 1px solid #ccc;
        border-radius: 2mm;
        background: #f5f5f5;
    }}
    .answer-card .a-title {{
        font-weight: bold;
        font-size: 11pt;
        margin-bottom: 2mm;
        color: #c00;
    }}
    .answer-card .a-text {{
        margin-bottom: 1mm;
        white-space: pre-wrap;
    }}
    .answer-card .a-image {{
        text-align: center;
        margin: 2mm 0;
    }}
    .answer-card .a-image img {{
        max-width: 70%;
        max-height: 80vh;
        object-fit: contain;
    }}
    @media print {{
        .answer-card {{
            break-inside: avoid;
            page-break-inside: avoid;
        }}
    }}
    """

    body_parts = []
    body_parts.append(f'<div class="header">【答案】今日练习 {date_str}</div>')

    for idx, item in enumerate(selected_items, 1):
        doc_id, full_title, cat, score, text, images, answer_md = item
        display_title = format_question_title(full_title)

        # 升级 v2：困难题在答案标题前也加 ⭐ 符号
        if cat == "困难":
            display_title = f"⭐ {display_title}"

        # 解析答案
        answer_data = parse_answer(answer_md) if answer_md else None
        answer_text = answer_data["text"] if answer_data else ""
        answer_images = answer_data["images"] if answer_data else []

        card_parts = []
        card_parts.append(f'<div class="a-title">{idx}. 【答案】{_html_escape(display_title)}</div>')

        if answer_text:
            for para in answer_text.split("\n"):
                para = para.strip()
                if para:
                    card_parts.append(f'<div class="a-text">{_html_escape(para)}</div>')

        for img_path in answer_images:
            tag = _html_image_tag(img_path, max_width="70%")
            if tag:
                card_parts.append(f'<div class="a-image">{tag}</div>')

        body_parts.append(f'<div class="answer-card">{"".join(card_parts)}</div>')

    html_body_str = "\n".join(body_parts)

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<style>{css}</style>
</head>
<body>
{html_body_str}
</body>
</html>"""
    return html


# ============================================================
#  HTML → PDF 编译函数（使用 WeasyPrint）
# ============================================================
def compile_html_to_pdf(html_content, output_pdf_path):
    """
    将 HTML 字符串通过 WeasyPrint 编译为 PDF 文件。

    参数：
      html_content  : str - 完整的 HTML 源码（含 <!DOCTYPE html>）
      output_pdf_path: str - 输出的 PDF 文件路径

    返回：
      (success: bool, message: str)
    """
    if not _HAS_WEASYPRINT:
        return False, "❌ WeasyPrint 未安装。请运行: pip install weasyprint"

    try:
        # 使用 FontConfiguration 确保 WeasyPrint 能找到系统字体（尤其是 Windows）
        font_config = FontConfiguration()
        HTML(string=html_content).write_pdf(
            output_pdf_path,
            font_config=font_config
        )
        return True, f"✅ PDF 生成成功: {output_pdf_path}"
    except Exception as e:
        return False, f"❌ PDF 生成失败: {e}"


# ============================================================
#  主流程
# ============================================================
def main():
    # 步骤1：配置改为显式加载，消除模块导入期副作用
    load_config()

    now = datetime.now()
    current_time_str = now.strftime('%Y-%m-%d %H:%M')
    file_time_str = now.strftime('%Y%m%d_%H%M')

    practice_pdf = f"今日练习_{file_time_str}.pdf"
    answer_pdf = f"今日练习_答案_{file_time_str}.pdf"

    target_folders_str = ", ".join(TARGET_FOLDERS)

    print("=" * 60)
    print("📋 思源笔记 —— 错题拼卷机 (HTML + WeasyPrint 版)")
    print("=" * 60)

    print(f"\n🎯 当前选题范围: {TARGET_FOLDERS}")
    print(f"   积分阈值: < {SCORE_THRESHOLD} (已掌握跳过)")
    print(f"   目标页数: {MIN_PAGES} ~ {MAX_PAGES} 页")
    print(f"   排除最近 {EXCLUDE_RECENT_DAYS} 天内新增/复习的题目")
    print()

    # 0) 连通性预检（fail-fast）
    #    先确认能连上思源，避免一路跑到 SQL 查询阶段才因超时失败，
    #    也避免宿主机不可达时对每个文档重复等待完整的建连超时。
    if resolve_siyuan_base() is None:
        _scheme, _host, _port = _split_siyuan_url(SIYUAN_URL)
        _probed = ", ".join(f"{h}:{_port}" for h in _candidate_hosts(_host))
        print("❌ 无法连接到思源笔记 API，已提前终止。")
        print(f"   配置地址: {SIYUAN_URL}（SIYUAN_HOST_MODE={SIYUAN_HOST_MODE}）")
        print(f"   已探测候选: {_probed}")
        print()
        print("   排查清单：")
        print("     1) 思源是否开启网络伺服（设置 → 关于 → 网络伺服），监听地址建议改为 0.0.0.0")
        print("     2) Windows 防火墙入站放行 6806/TCP，且使用 -Profile Any")
        print("        PowerShell(管理员): New-NetFirewallRule -DisplayName 'SiYuan 6806 (WSL)' "
              "-Direction Inbound -Protocol TCP -LocalPort 6806 -Action Allow -Profile Any")
        print("     3) 如需彻底摆脱网关漂移：在 %USERPROFILE%\\.wslconfig 增加 [wsl2] "
              "networkingMode=mirrored，再执行 wsl --shutdown")
        print("     4) 休眠/唤醒后 NAT 端点可能失效：Windows 侧执行 wsl --shutdown 后重开 WSL")
        print("     注意：ping 不通宿主机是 WSL2 正常现象，判断链路请用 TCP 探测。")
        sys.exit(1)

    print(f"🔗 链路已就绪，超时配置: 建连 {CONNECT_TIMEOUT}s / 读取 {READ_TIMEOUT}s，"
          f"建连重试 {CONNECT_RETRIES} 次\n")

    # 1) 查找目录
    target_dirs = find_target_dirs()
    if not target_dirs:
        print(f"\n❌ 未找到匹配的目录: {TARGET_FOLDERS}")
        print(f"   请确认笔记本名称或其下的文档标题包含: {TARGET_FOLDERS}。")
        return

    print(f"📁 正在针对以下目录扫描：")
    for d in target_dirs:
        print(f"   📂 {d['name']}  (hpath: {d['hpath']}, id: {d['id']})")

    # 2) 收集文档
    all_docs = []  # [(doc_id, title, cat, score)]
    # [DEBUG] 设置环境变量 DEBUG_KRAMDOWN=1 时，导出首个文档原始 Kramdown 便于排查
    _debug_kramdown = os.environ.get("DEBUG_KRAMDOWN", "") == "1"
    _debug_kramdown_written = False
    for td in target_dirs:
        sy_files = list_sy_files_in_dir(td)
        print(f"\n📂 [{td['name']}] 找到 {len(sy_files)} 个文档")

        for name, doc_id in sy_files:
            title = get_doc_title(doc_id)
            if title is None:
                print(f"   ⚠️  {name} → 获取标题失败")
                continue
            cat = classify_document(title)
            short = shorten_title(title)

            print(f"   📄 {name} → {short}  [{cat}]")

            md = get_block_kramdown(doc_id)

            # [DEBUG] 将第一个成功获取到的文档原始 Kramdown 写入 debug_kramdown.txt，
            # 用于排查思源新版导出格式（IAL 位置、换行方式等）导致的正文提取为空问题。
            if _debug_kramdown and not _debug_kramdown_written and md:
                try:
                    with open("debug_kramdown.txt", "w", encoding="utf-8") as _df:
                        _df.write(f"# doc_name: {name}\n")
                        _df.write(f"# doc_id: {doc_id}\n")
                        _df.write(f"# title: {title}\n")
                        _df.write("# ---- raw kramdown begin ----\n")
                        _df.write(md)
                        _df.write("\n# ---- raw kramdown end ----\n")
                    print(f"   🐞 [DEBUG] 已将首个文档原始 Kramdown 写入 debug_kramdown.txt（{len(md)} 字符）")
                except Exception as _e:
                    print(f"   ⚠️  [DEBUG] 写入 debug_kramdown.txt 失败: {_e}")
                _debug_kramdown_written = True

            # 通过 Markdown 表格中的练习日期判断是否需要跳过
            latest_date = parse_latest_date(md) if md else None
            if latest_date is not None:
                days_diff = (datetime.now().date() - latest_date.date()).days
                if days_diff <= EXCLUDE_RECENT_DAYS:
                    short = shorten_title(title)
                    print(f"   [跳过] 距上次练习/录入不足 {EXCLUDE_RECENT_DAYS} 天：{short} (距今 {days_diff} 天)")
                    continue

            score = parse_score_table(md) if md else None
            if score is not None:
                print(f"       总积分: {score}")
            else:
                print(f"       总积分: 未检测到积分表")

            all_docs.append((doc_id, title, cat, score))

    if not all_docs:
        print("\n❌ 未找到任何文档。")
        return

    # 3) 选题
    print(f"\n{'=' * 60}")
    print("🎯 选题引擎启动")
    print(f"{'=' * 60}")

    selected = select_questions(all_docs)

    if not selected:
        print("\n❌ 没有符合选题条件的题目。")
        return

    # 4) 高度智能配对：对半栏题按估算高度排序，使高度相近的题目自动配对到同一排
    compact_items = [item for item in selected if is_compact_item(item)]
    normal_items = [item for item in selected if not is_compact_item(item)]
    compact_items.sort(key=estimate_compact_height, reverse=True)
    selected = compact_items + normal_items
    print(f"📐 重排完成：{len(compact_items)} 道半栏题 + {len(normal_items)} 道通栏题")

    # 5) 生成练习卷 HTML → PDF
    print(f"\n{'=' * 60}")
    print(f"📦 正在生成练习卷 HTML ({len(selected)} 道大题)……")
    practice_html = generate_html_practice(selected, current_time_str, target_folders_str)

    print(f"   ⏳ 正在编译练习卷 PDF……")
    success, msg = compile_html_to_pdf(practice_html, practice_pdf)
    print(f"   {msg}")

    # 5) 生成答案卷 HTML → PDF
    print(f"\n📦 正在生成答案卷 HTML ({len(selected)} 道)……")
    answer_html = generate_html_answer(selected, current_time_str, target_folders_str)

    print(f"   ⏳ 正在编译答案卷 PDF……")
    success2, msg2 = compile_html_to_pdf(answer_html, answer_pdf)
    print(f"   {msg2}")

    # 6) 统计
    print(f"\n{'=' * 60}")
    print(f"📊 统计")
    print(f"   共 {len(selected)} 道大题")
    print(f"   练习卷: {practice_pdf}")
    print(f"   答案卷: {answer_pdf}")
    print(f"   估算 ~{estimate_total_pages(selected)} 页 (A4)")
    if _ASSET_FETCH_FAILED:
        print(f"   ⚠️  有 {_ASSET_FETCH_FAILED} 处图片未能获取（详见上方告警；"
              f"请检查 SIYUAN_DATA_PATH 与网络连通性）")
    if not success or not success2:
        print(f"\n⚠️  部分 PDF 文件编译失败，请检查上述错误信息。")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()