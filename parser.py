# -*- coding: utf-8 -*-
"""
思源笔记 —— 解析与规则层（步骤3 抽取）

职责：
  1. 数据模型：QuestionDoc / QuestionItem（NamedTuple，保持元组下标契约）；
  2. Kramdown 清洗：IAL 剥离、标题识别、块文本/图片提取；
  3. 内容解析：题目 / 答案 / 积分表 / 练习日期；
  4. 分类与标题：基础/易错/困难归类与显示标题生成；
  5. 版式估算：页数、紧凑题判定（按图片自然宽度占比）、半栏配对高度；
  6. 选题引擎：初筛 → 分池 → 排序 → 限额入选 → 困难补充。

设计约束：
  - 依赖方向严格为 parser → siyuan_api → config，不反向依赖 siyuan_client；
  - 配置经 config.get_config() 读取，不再使用模块全局变量；
  - QuestionItem / QuestionDoc 采用 NamedTuple，**完整保留元组下标契约**
    （item[4] / item[5] / d[:4] / 解包，均与旧元组行为一致）。
"""

from __future__ import annotations

import math
import re
from datetime import datetime
from typing import List, NamedTuple, Optional

import config as config_module
from siyuan_api import get_block_kramdown, _get_pil_image, _HAS_PIL


def _cfg():
    """获取配置单例（显式触发加载，非导入期副作用）。"""
    return config_module.get_config()


# ============================================================
#  数据模型（NamedTuple：字段具名 + 元组完全兼容）
# ============================================================
class QuestionDoc(NamedTuple):
    """候选文档元数据（等价于旧元组 (doc_id, title, cat, score)）。"""
    doc_id: str
    title: str
    category: str
    score: Optional[int]


class QuestionItem(NamedTuple):
    """
    入选题目（等价于旧元组
    (doc_id, title, cat, score, text, images, answer_md)）。

    下标契约：item[0]=doc_id … item[4]=text、item[5]=images、item[6]=answer_md，
    因此 `len(item) > 4` / `item[4]` / `item[5]` / 7 元解包等旧写法全部继续可用。
    """
    doc_id: str
    title: str
    category: str
    score: Optional[int]
    text: str
    images: List[str]
    answer_md: str


# ============================================================
#  练习日期解析
# ============================================================
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
        for target in _cfg().target_folders:
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
        # 图片文件名可能包含空格（例如「屏幕截图 2026-10-14 193932-....png」），
        # 因此路径字符类只排除右括号 / 右尖括号，不再排除空白，避免文件名被截断。
        for m in re.finditer(r'!\[.*?\]\(\s*<?(/?)assets/([^)>]+)', line):
            prefix = "/" if m.group(1) else ""
            full = f"{prefix}assets/{m.group(2).strip()}"
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


# ============================================================
#  紧凑题目判断（按「图片自然宽度占页宽比例」判定）
# ============================================================
# 页面几何：renderer 从这里导入，@page margin 与本常量同源。
A4_WIDTH_MM = 210
A4_HEIGHT_MM = 297
PAGE_MARGIN_MM = 10
PAGE_CONTENT_WIDTH_MM = A4_WIDTH_MM - 2 * PAGE_MARGIN_MM       # 190mm
GRID_GUTTER_MM = 6                                             # 双栏单元格间距

# 截图采集倍率：错题截图按 2x（HiDPI）采集，一个图像像素 ≈ 0.5 CSS px。
# 实测本库截图正文墨迹高 ≈23px，对应 16px 字号 @2x，故取 192。
SCREENSHOT_DPI = 192.0

# 宽度阈值（占 A4 内容区宽度）：≤60% 一律半栏；>65% 才给通栏；60~65% 保守按半栏。
COMPACT_MAX_WIDTH_RATIO = 0.60
FULL_WIDTH_MIN_RATIO = 0.65
# 无图片时的文本行数上限（纯文字题才看行数）
COMPACT_MAX_TEXT_LINES = 4

_IMAGE_SIZE_CACHE: dict = {}


def get_image_size(api_image_path):
    """
    返回图片像素尺寸 (w, h)，取不到返回 None。

    结果按资源路径缓存：一次出卷内排版适配会重建 HTML 最多 11 轮，
    不做缓存则每轮都重新解码同一批 PNG。
    """
    if api_image_path in _IMAGE_SIZE_CACHE:
        return _IMAGE_SIZE_CACHE[api_image_path]
    size = None
    if _HAS_PIL:
        img = _get_pil_image(api_image_path)
        if img is not None:
            try:
                with img:
                    w_px, h_px = img.size
                size = (w_px, h_px)
            except Exception:
                size = None
    _IMAGE_SIZE_CACHE[api_image_path] = size
    return size


def image_natural_width_px(api_image_path):
    """图片在 CSS 中的自然显示宽度（px）：像素宽按 SCREENSHOT_DPI 折算。"""
    size = get_image_size(api_image_path)
    if size is None:
        return None
    return size[0] * 96.0 / SCREENSHOT_DPI


def image_width_ratio(api_image_path):
    """图片自然宽度占 A4 内容区宽度的比例；取不到尺寸返回 None。"""
    natural_px = image_natural_width_px(api_image_path)
    if natural_px is None:
        return None
    return (natural_px * 25.4 / 96.0) / PAGE_CONTENT_WIDTH_MM


def is_compact_item(item):
    """
    判断题目是否放入半栏（可与另一道题并排）。

    规则（宽度优先）：
      - 有图片：取各图自然宽度占比的最大值；
                  ≤ COMPACT_MAX_WIDTH_RATIO（60%）→ 半栏（一律，不再看行数）；
                  > FULL_WIDTH_MIN_RATIO（65%）→ 通栏；
                  60%~65% 之间 → 半栏（宁可在半栏内微缩，也不放通栏）。
      - 无图片：非空文本行数 ≤ COMPACT_MAX_TEXT_LINES → 半栏，否则通栏。
      - 图片尺寸取不到 / Pillow 缺失 → 通栏（保守，避免宽图塞进半栏）。
    """
    text = item[4] if len(item) > 4 else ""
    images = item[5] if len(item) > 5 else []

    if not images:
        text_lines = len([l for l in text.split("\n") if l.strip()]) if text else 0
        return text_lines <= COMPACT_MAX_TEXT_LINES

    max_ratio = 0.0
    for img_path in images:
        ratio = image_width_ratio(img_path)
        if ratio is None:
            return False
        if ratio > max_ratio:
            max_ratio = ratio
    return max_ratio <= FULL_WIDTH_MIN_RATIO


def estimate_compact_height(item):
    """
    估算题目在半栏容器中的渲染高度（CSS px），用于把高度接近的题目配对到同一行，
    减少页底留白。

    仅用于配对排序，允许近似：文本按 20 字/行、图片按半栏可用宽度等比缩放。
    """
    text = item[4] if len(item) > 4 else ""
    images = item[5] if len(item) > 5 else []

    half_width_px = (PAGE_CONTENT_WIDTH_MM - GRID_GUTTER_MM) / 2 * 96 / 25.4   # ≈ 348
    # 卡片内边距 + 标题（12pt 粗体 + 2mm 下边距）
    h = 8.0 + 26.0

    text_lines = 0
    if text:
        text_lines = sum(math.ceil(len(p) / 20) for p in text.split("\n") if p.strip())
    # 11pt / 1.6 行高 ≈ 23.5px
    h += text_lines * 23.5
    if not images:
        h += max(4, math.ceil(text_lines * 0.8) + 2) * 30

    for img_path in images:
        size = get_image_size(img_path)
        if size is None:
            h += 200.0
            continue
        w_px, h_px = size
        natural_w = w_px * 96.0 / SCREENSHOT_DPI
        natural_h = h_px * 96.0 / SCREENSHOT_DPI
        scale = min(1.0, half_width_px / natural_w) if natural_w > 0 else 1.0
        h += natural_h * scale + 8.0   # 图片上下 margin 各 4px
    return h


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
    cfg = _cfg()
    score_threshold = cfg.score_threshold
    min_pages = cfg.min_pages
    max_pages = cfg.max_pages

    # 1) 初筛：总积分 < SCORE_THRESHOLD 或 积分未检测到
    eligible = []
    skipped_texts = []
    for d in all_docs:
        doc_id, title, cat, score = d[:4]
        if score is not None:
            if score >= score_threshold:
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
            return QuestionItem(doc_id, title, cat, score,
                                parsed["text"], parsed["images"], md)
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
            if estimate_total_pages(selected) >= max_pages:
                print(f"   ⏹️  已达最大页数限制 {max_pages} 页，停止添加 {cat} 题")
                break
            item = fetch_content(d)
            if item:
                selected.append(item)

    # 5) 页数不足 MIN_PAGES 时，从困难池按积分升序补充
    current_pages = estimate_total_pages(selected)
    print(f"📊 当前已选题数: {len(selected)}，估算页数: ~{current_pages} 页")

    if current_pages < min_pages and pools.get("困难"):
        needed = pools["困难"]
        # 困难池已按积分升序排序，优先选择错误最多的困难题
        print(f"📌 页数不足 {min_pages} 页，从困难池补充（按积分升序，共 {len(needed)} 道候选题）……")

        for d in needed:
            if estimate_total_pages(selected) >= min_pages:
                break
            # 补充时也要检查最大页数限制
            if estimate_total_pages(selected) >= max_pages:
                print(f"   ⏹️  已达最大页数限制 {max_pages} 页，停止补充困难题")
                break
            item = fetch_content(d)
            if item:
                selected.append(item)
                print(f"   ➕ 补充困难题: {shorten_title(d[1])} (积分: {d[3]})")
    elif current_pages < min_pages:
        yellow = "\033[93m"
        reset = "\033[0m"
        print(f"\n{yellow}{'⚠️ ' * 10}")
        print(f"  ⚠️  警告：当前仅 ~{current_pages} 页，不足 {min_pages} 页，")
        print('       且无"困难"题可补充。请考虑增加 TARGET_FOLDERS 范围')
        print(f"       或降低 SCORE_THRESHOLD 阈值。")
        print(f"{'⚠️ ' * 10}{reset}\n")

    final_pages = estimate_total_pages(selected)
    print(f"📊 最终选题数: {len(selected)}，估算页数: ~{final_pages} 页")
    return selected
