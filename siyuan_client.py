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
import urllib.parse
from datetime import datetime, timedelta

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
#  配置加载
# ============================================================
def load_config(config_path="config.json"):
    """
    从 config.json 加载配置，并将变量设置到模块全局作用域。
    如果文件不存在，给出友好提示并退出。

    新增配置项（兼容旧文件，自动设置默认值）：
      - MAX_PAGES: 最大页数限制（默认 10）
      - EXCLUDE_RECENT_DAYS: 排除最近几天内录入/复习的题目（默认 2）
    """
    if not os.path.isfile(config_path):
        print(f"❌ 未找到配置文件 {config_path}")
        print("   请复制 config.json.example 为 config.json，并填写你的实际配置。")
        print("   参考命令: cp config.json.example config.json")
        sys.exit(1)

    with open(config_path, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    # 将配置注入模块全局变量
    global SIYUAN_URL, API_TOKEN, NOTEBOOK_ID, SIYUAN_DATA_PATH
    global TARGET_FOLDERS, SCORE_THRESHOLD, MIN_PAGES, MAX_PAGES, EXCLUDE_RECENT_DAYS, HEADERS

    SIYUAN_URL = cfg["SIYUAN_URL"]
    API_TOKEN = cfg["API_TOKEN"]
    NOTEBOOK_ID = cfg["NOTEBOOK_ID"]
    SIYUAN_DATA_PATH = cfg["SIYUAN_DATA_PATH"]
    TARGET_FOLDERS = cfg["TARGET_FOLDERS"]
    SCORE_THRESHOLD = cfg["SCORE_THRESHOLD"]
    MIN_PAGES = cfg["MIN_PAGES"]

    # 新增配置：最大页数限制，默认 10
    MAX_PAGES = cfg.get("MAX_PAGES", 10)
    # 新增配置：排除最近几天的题目，默认 2 天
    EXCLUDE_RECENT_DAYS = cfg.get("EXCLUDE_RECENT_DAYS", 2)

    HEADERS = {
        "Authorization": f"Token {API_TOKEN}",
        "Content-Type": "application/json"
    }


# 脚本入口时自动加载配置
load_config()


# ============================================================
#  通用 API 调用
# ============================================================
def call_api(endpoint, payload=None):
    url = f"{SIYUAN_URL}{endpoint}"
    try:
        resp = requests.post(url, headers=HEADERS, json=payload, timeout=10)
        if resp.status_code != 200:
            return {"code": -1, "msg": f"HTTP {resp.status_code}: {resp.text}"}
        data = resp.json()
        if data.get("code") != 0:
            return {"code": data.get("code", -1), "msg": data.get("msg", "未知错误")}
        return data
    except requests.exceptions.Timeout:
        return {"code": -1, "msg": "请求超时"}
    except requests.exceptions.ConnectionError:
        return {"code": -1, "msg": f"无法连接到 {SIYUAN_URL}"}
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
        # 1) SQL 模糊匹配：匹配 hpath 或 content（文档标题）
        sql = (f"SELECT id, hpath, content FROM blocks "
               f"WHERE type='d' AND (content LIKE '%{target}%' OR hpath LIKE '%{target}%') "
               f"AND box='{NOTEBOOK_ID}' ORDER BY length(hpath) ASC LIMIT 1")
        result = call_api("/api/query/sql", {"stmt": sql})
        if result.get("code") == 0:
            rows = result.get("data", [])
            if rows:
                row = rows[0]
                block_id = row.get("id", "")
                hpath = row.get("hpath", "")
                if block_id:
                    matched.append({
                        "name": target,
                        "path": f"/data/{NOTEBOOK_ID}/{block_id}"
                    })
                    print(f"   📂 找到目录 {target} → hpath: {hpath}")
                    continue

        # 2) SQL 没找到 → 尝试匹配笔记本名称
        nb_result = call_api("/api/notebook/lsNotebooks")
        if nb_result.get("code") == 0:
            notebooks = nb_result.get("data", [])
            found_nb = False
            for nb in notebooks:
                nb_name = nb.get("name", "")
                nb_id = nb.get("id", "")
                if target in nb_name and nb_id == NOTEBOOK_ID:
                    matched.append({
                        "name": target,
                        "path": f"/data/{NOTEBOOK_ID}"
                    })
                    print(f"   📂 找到目录 {target} → 匹配笔记本名称: {nb_name}")
                    found_nb = True
                    break
            if not found_nb:
                print(f"  DEBUG: 尝试匹配关键词 '{target}' 失败，请检查思源中是否存在该标题的文档。")
        else:
            print(f"  DEBUG: 尝试匹配关键词 '{target}' 失败，请检查思源中是否存在该标题的文档。")
    return matched


def list_sy_files_in_dir(dir_path):
    entries = list_dir_entries(dir_path)
    sy_files = []
    for entry in entries:
        name = entry.get("name", "")
        if name.endswith(".sy") and not entry.get("isDir", False):
            doc_id = name[:-3]
            sy_files.append((name, doc_id))
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
#  精准题目解析（练习卷用）— 重写版
# ============================================================
def parse_question(md_source):
    """
    从源码中提取题目内容。
    匹配：以 # 题目 或 ## 题目 开头的行。
    结束：以 ## 答案 开头的行。
    提取图片路径时需在清理 {:...} 之前执行。
    """
    if not md_source:
        return None

    raw_lines = md_source.split("\n")

    # 寻找 # 题目 或 ## 题目
    start_idx = -1
    for i, line in enumerate(raw_lines):
        s = line.strip()
        if re.match(r'^#{1,2}\s+题目', s):
            start_idx = i
            break

    if start_idx == -1:
        return None

    # 截取到 ## 答案 为止
    content_lines = []
    for line in raw_lines[start_idx + 1:]:
        s = line.strip()
        if re.match(r'^##\s*答案', s):
            break
        if re.match(r'^##\s*💡\s*答案', s):
            break
        content_lines.append(line)

    # 先提取图片路径（在清理 {:...} 之前！）
    image_paths = []
    for line in content_lines:
        for m in re.finditer(r'!\[.*?\]\((/?)assets/([^)]+)\)', line):
            prefix = "/" if m.group(1) else ""
            full = f"{prefix}assets/{m.group(2)}"
            if full not in image_paths:
                image_paths.append(full)

    # 再清理 {:...} 和提取文字
    text_parts = []
    for line in content_lines:
        s = re.sub(r'\{:\s*[^}]*\}', '', line).strip()
        s = re.sub(r'^>\s*', '', s)
        s = re.sub(r'!\[.*?\]\([^)]+\)', '', s)
        s = re.sub(r'\s*\[图片\]\s*', '', s)
        if s.strip():
            text_parts.append(s.strip())

    text = "\n".join(text_parts).strip()
    text = re.sub(r'\s*\[图片\]\s*', '', text)
    # 去除 Kramdown 转义符：\任何字符 → 字符本身
    text = re.sub(r'\\(.)', r'\1', text)

    if not text and not image_paths:
        return None

    return {"text": text, "images": image_paths}


# ============================================================
#  答案解析（答案卷用）— 重写版
# ============================================================
def parse_answer(md_source):
    """
    从源码中提取答案内容。
    匹配：以 ## 答案 或 ## 💡 答案 开头的二级标题。
    结束：以 ## 扩展题目 或下一个 ## 标题开头。
    提取图片路径时需在清理 {:...} 之前执行。
    返回 {"text": "...", "images": [...]}，没有则返回 None。
    """
    if not md_source:
        return None

    raw_lines = md_source.split("\n")

    # 定位 ## 答案（兼容各种写法）
    start_idx = -1
    for i, line in enumerate(raw_lines):
        s = line.strip()
        if re.match(r'^##[^#]*答案', s):
            start_idx = i
            break

    if start_idx == -1:
        return None

    # 取到下一个 ## 或文档结尾，但跳过 "## 扩展题目"
    content_lines = []
    for line in raw_lines[start_idx + 1:]:
        s = line.strip()
        if s.startswith("##"):
            break
        content_lines.append(line)

    # 先提取图片路径（在清理 {:...} 之前！）
    image_paths = []
    for line in content_lines:
        for m in re.finditer(r'!\[.*?\]\((/?)assets/([^)]+)\)', line):
            prefix = "/" if m.group(1) else ""
            full = f"{prefix}assets/{m.group(2)}"
            if full not in image_paths:
                image_paths.append(full)

    # 再清理 {:...} 和提取文字
    text_parts = []
    for line in content_lines:
        s = re.sub(r'\{:\s*[^}]*\}', '', line).strip()
        s = re.sub(r'^>\s*', '', s)
        s = re.sub(r'!\[.*?\]\([^)]+\)', '', s)
        s = re.sub(r'\s*\[图片\]\s*', '', s)
        if s.strip():
            text_parts.append(s.strip())

    text = "\n".join(text_parts).strip()
    text = re.sub(r'\s*\[图片\]\s*', '', text)
    # 去除 Kramdown 转义符：\任何字符 → 字符本身
    text = re.sub(r'\\(.)', r'\1', text)

    if not text and not image_paths:
        print(f"  [DEBUG] parse_answer 提取为空，原始 Kramdown 前 100 字符:")
        print(f"  [DEBUG] {md_source[:100]!r}")
        return None

    return {"text": text, "images": image_paths}


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
        phys = map_image_path(img_path)
        if phys and os.path.isfile(phys):
            if _HAS_PIL:
                try:
                    with Image.open(phys) as im:
                        w, img_h = im.size
                        h += img_h * (340 / w) if w > 0 else 200
                except Exception:
                    h += 200
            else:
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
        phys = map_image_path(img_path)
        if phys is None or not os.path.isfile(phys):
            continue
        try:
            with Image.open(phys) as img:
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
    返回空字符串如果图片文件不存在。
    """
    phys = map_image_path(api_image_path)
    if phys is None:
        return ""
    abs_path = os.path.abspath(phys)
    # 使用 file:// 协议嵌入本地图片，URL-encode 路径中的特殊字符（中文、空格等）
    abs_path_encoded = urllib.parse.quote(abs_path, safe='/:@!*()')
    return f'<img src="file://{abs_path_encoded}" style="max-width: {max_width}; max-height: 80vh; object-fit: contain; display: block; margin: 4px auto;" />'


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

    # 1) 查找目录
    target_dirs = find_target_dirs()
    if not target_dirs:
        print(f"\n❌ 未找到匹配的目录: {TARGET_FOLDERS}")
        print(f"   请确认笔记本名称或其下的文档标题包含: {TARGET_FOLDERS}。")
        return

    print(f"📁 正在针对以下目录扫描：")
    for d in target_dirs:
        print(f"   📂 {d['name']}  ({d['path']})")

    # 2) 收集文档
    all_docs = []  # [(doc_id, title, cat, score)]
    for td in target_dirs:
        sy_files = list_sy_files_in_dir(td["path"])
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
    if not success or not success2:
        print(f"\n⚠️  部分 PDF 文件编译失败，请检查上述错误信息。")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()