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
#  步骤2：网络 / 思源 API / 图片资源 已迁移至 siyuan_api.py
# ============================================================
# 本模块通过下方 import 做“re-export shim”，保证其余尚未拆解的代码
# 仍可按原名称调用（find_target_dirs / call_api / get_asset_bytes ...）。
# siyuan_api.py 内部直接消费 config.get_config()，不读取本模块的全局变量。
from siyuan_api import (
    # 地址解析与连通性
    reset_resolved_base,
    _split_siyuan_url,
    _detect_host_gateway,
    _detect_host_gateway_via_ip,
    _tcp_probe,
    _candidate_hosts,
    resolve_siyuan_base,
    get_siyuan_base,
    # HTTP 传输
    _note_connect_failure,
    _post_json,
    call_api,
    # 思源仓储
    list_dir_entries,
    find_target_dirs,
    list_sy_files_in_dir,
    get_doc_title,
    get_block_kramdown,
    # 图片资源
    map_image_path,
    _asset_api_candidates,
    get_asset_bytes,
    _image_data_uri,
    _get_pil_image,
    # 运行态计数器：以“共享可变对象”方式显式绑定（LOAD_GLOBAL 不走 __getattr__）
    _ASSET_FETCH_FAILED,
)
import siyuan_api as _siyuan_api

# ============================================================
#  步骤3：解析 / 标题分类 / 版式估算 / 选题引擎 已迁移至 parser.py
# ============================================================
# 同样以 re-export shim 暴露全部原函数名与数据模型，其余未拆解代码零改动。
from parser import (
    # 数据模型（NamedTuple，兼容元组下标）
    QuestionDoc,
    QuestionItem,
    # 解析
    parse_latest_date,
    parse_score_table,
    parse_question,
    parse_answer,
    # 分类 / 标题
    classify_document,
    extract_parent_ds,
    format_question_title,
    shorten_title,
    # 版式估算
    estimate_lines,
    estimate_total_pages,
    estimate_compact_height,
    is_compact_item,
    # 选题引擎
    select_questions,
    # Kramdown 清洗工具
    _IAL_RE,
    _strip_ial,
    _heading_text,
    _is_question_heading,
    _is_answer_heading,
    _clean_md_line,
    _extract_block,
)

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

# ---- 运行时状态：已迁移至 siyuan_api.py（步骤2）----
# SIYUAN_BASE / _SIYUAN_BASE_RESOLVED / SESSION / _SESSION_ADAPTER /
# _ASSET_BYTES_CACHE / _ASSET_ERROR_LOGGED / _CONNECT_FAIL_STREAK /
# _CONNECT_FAIL_BANNER_SHOWN / _IMAGE_MIME 均以 siyuan_api.py 为准，
# 由下方 __getattr__ 在“属性访问”时动态转发。
#
# 例外：_ASSET_FETCH_FAILED 已在上方 import 中显式绑定为“共享可变计数对象”。
# 原因是 Python 的 LOAD_GLOBAL 不经过模块级 __getattr__，而 main() 仍以
# 全局名 `if _ASSET_FETCH_FAILED:` 读取它，必须绑定同一对象才能拿到最新值。
_DYNAMIC_STATE_NAMES = {
    "SIYUAN_BASE",
    "_SIYUAN_BASE_RESOLVED",
    "SESSION",
    "_SESSION_ADAPTER",
    "_ASSET_BYTES_CACHE",
    "_ASSET_ERROR_LOGGED",
    "_ASSET_FETCH_FAILED",
    "_CONNECT_FAIL_STREAK",
    "_CONNECT_FAIL_BANNER_SHOWN",
    "_IMAGE_MIME",
}


def __getattr__(name):
    """PEP 562 模块级动态属性：转发 siyuan_api 中的运行态变量。"""
    if name in _DYNAMIC_STATE_NAMES:
        return getattr(_siyuan_api, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# ============================================================
#  网络地址解析 / HTTP 传输（步骤2 已迁移至 siyuan_api.py）
# ============================================================
# reset_resolved_base / _split_siyuan_url / _detect_host_gateway /
# _detect_host_gateway_via_ip / _tcp_probe / _candidate_hosts /
# resolve_siyuan_base / get_siyuan_base / _note_connect_failure / _post_json
# 均由 siyuan_api.py 提供，并经顶部 re-export shim 暴露。


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
#  通用 API 调用（步骤2 已迁移至 siyuan_api.py）
# ============================================================
# call_api 由 siyuan_api.py 提供，并经顶部 re-export shim 暴露。


# ============================================================
#  目录 & 文件遍历（步骤2 已迁移至 siyuan_api.py）
# ============================================================
# list_dir_entries / find_target_dirs / list_sy_files_in_dir / get_doc_title
# 均由 siyuan_api.py 提供，并经顶部 re-export shim 暴露。


# ============================================================
#  练习日期解析（步骤3 已迁移至 parser.py）
# ============================================================
# parse_latest_date 由 parser.py 提供，并经顶部 re-export shim 暴露。


# ============================================================
#  分类 / 标题格式化（步骤3 已迁移至 parser.py）
# ============================================================
# classify_document / extract_parent_ds / format_question_title / shorten_title
# 均由 parser.py 提供，并经顶部 re-export shim 暴露。


# ============================================================
#  文档源码获取（步骤2 已迁移至 siyuan_api.py）
# ============================================================
# get_block_kramdown 由 siyuan_api.py 提供，并经顶部 re-export shim 暴露。


# ============================================================
#  积分表解析（步骤3 已迁移至 parser.py）
# ============================================================
# parse_score_table 由 parser.py 提供，并经顶部 re-export shim 暴露。


# ============================================================
#  Kramdown 清洗工具（步骤3 已迁移至 parser.py）
# ============================================================
# _IAL_RE / _strip_ial / _heading_text / _is_question_heading /
# _is_answer_heading / _clean_md_line / _extract_block
# 均由 parser.py 提供，并经顶部 re-export shim 暴露。


# ============================================================
#  精准题目解析（步骤3 已迁移至 parser.py）
# ============================================================
# parse_question 由 parser.py 提供，并经顶部 re-export shim 暴露。


# ============================================================
#  答案解析（步骤3 已迁移至 parser.py）
# ============================================================
# parse_answer 由 parser.py 提供，并经顶部 re-export shim 暴露。


# ============================================================
#  物理图片路径映射 / 资源兜底（步骤2 已迁移至 siyuan_api.py）
# ============================================================
# map_image_path / _asset_api_candidates / get_asset_bytes /
# _image_data_uri / _get_pil_image 及 _ASSET_BYTES_CACHE / _IMAGE_MIME
# 均由 siyuan_api.py 提供，并通过本文件顶部的 re-export shim 暴露。


# ============================================================
#  页数估算（步骤3 已迁移至 parser.py）
# ============================================================
# estimate_lines / estimate_total_pages / estimate_compact_height
# 均由 parser.py 提供，并经顶部 re-export shim 暴露。


# ============================================================
#  紧凑题目判断（步骤3 已迁移至 parser.py）
# ============================================================
# is_compact_item 由 parser.py 提供，并经顶部 re-export shim 暴露。


# ============================================================
#  选题引擎（步骤3 已迁移至 parser.py）
# ============================================================
# select_questions 由 parser.py 提供，并经顶部 re-export shim 暴露。


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

            # 步骤3：改用 QuestionDoc（NamedTuple，兼容旧元组解包与下标访问）
            all_docs.append(QuestionDoc(doc_id, title, cat, score))

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