# -*- coding: utf-8 -*-
"""
思源笔记 —— 渲染与产出层（步骤4 抽取）

职责：
  1. HTML 工具：图片标签（本地 file:// 优先 + API base64 兜底）、文本转义；
  2. 题目正文 HTML 片段组装（标题 / 文本 / 图片 / 作答留白）；
  3. 练习卷 / 答案卷 HTML 源码生成（CSS 打印排版）；
  4. 两遍式排版测量：用 WeasyPrint 渲染结果中的锚点校验「每题完整落在单页内」，
     不满足则收缩该题图片高度、再退化为紧凑字号，仍不满足则报错而不写出 PDF；
  5. 调用 WeasyPrint 将 HTML 编译为 PDF。

设计约束：
  - 依赖方向严格为 renderer → parser → siyuan_api → config，绝不反向依赖 siyuan_client；
  - 配置经 parser / siyuan_api 间接读取（config.get_config()），本模块不直接读写全局配置；
  - 共享 CSS（* / body / .header）抽为 _CSS_BASE 常量以减少重复，
    但**不改变任何 CSS 声明的内容、覆盖顺序与渲染效果**。
"""

from __future__ import annotations

import html
import math
import os
import urllib.parse
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Set, Tuple

from siyuan_api import map_image_path, _image_data_uri
from parser import (
    A4_HEIGHT_MM,
    GRID_GUTTER_MM,
    PAGE_MARGIN_MM,
    estimate_compact_height,
    format_question_title,
    image_natural_width_px,
    is_compact_item,
    parse_answer,
)

try:
    from weasyprint import HTML
    from weasyprint.text.fonts import FontConfiguration
    _HAS_WEASYPRINT = True
except ImportError:
    _HAS_WEASYPRINT = False
    print("⚠️  未安装 WeasyPrint，将无法生成 PDF。请运行: pip install weasyprint")


# ============================================================
#  页面几何常量（源自在 parser.py，与 @page margin 同源）
# ============================================================
# A4 内容区高度（CSS px，WeasyPrint 1in = 96px）
PAGE_CONTENT_HEIGHT_PX = (A4_HEIGHT_MM - 2 * PAGE_MARGIN_MM) * 96 / 25.4   # ≈ 1046.9
# 内容区下边缘相对页顶的 y 坐标，锚点 y 超过它即视为越出页面
PAGE_CONTENT_BOTTOM_PX = PAGE_MARGIN_MM * 96 / 25.4 + PAGE_CONTENT_HEIGHT_PX  # ≈ 1084.7
# 首轮图片高度上限（等效原 max-height: 80vh），与 CSS 兜底值 80vh 对齐
INITIAL_IMAGE_MAX_HEIGHT_PX = 0.8 * A4_HEIGHT_MM * 96 / 25.4              # ≈ 898.0
MIN_IMAGE_MAX_HEIGHT_PX = 60.0
IMAGE_CAP_SHRINK = 0.75
MAX_FIT_ITERATIONS = 10
# 半栏题卡内哨兵位于卡片 padding 之前：padding-bottom 2mm + 1px 边框即到卡底的距离。
# 取偏大值，宁可稍严也不让卡片越过内容区下边界。
CARD_BOTTOM_SLOP_PX = 2 * 96 / 25.4 + 1.0


@dataclass
class LayoutState:
    """一次排版适配过程中跨轮次累积的状态（同一 State 反复重建 HTML）。"""

    image_caps: Dict[str, float] = field(default_factory=dict)
    dense: Set[str] = field(default_factory=set)
    has_images: Dict[str, bool] = field(default_factory=dict)
    labels: Dict[str, str] = field(default_factory=dict)
    # 双栏等高：同排较矮题卡在标题后 / 正文后各补 pad 像素，使两卡等高、图片居中
    pads: Dict[str, float] = field(default_factory=dict)
    # 双栏行成员：(左 key, 右 key 或 None)，由 generate_html_practice 填充
    rows: List[Tuple[str, Optional[str]]] = field(default_factory=list)
    # 每题用于判定的结束哨兵 id 与哨兵到卡片下边缘的固定偏差（px）
    end_keys: Dict[str, str] = field(default_factory=dict)
    bottom_slop: Dict[str, float] = field(default_factory=dict)


class LayoutError(RuntimeError):
    """单道题在 A4 单页内无法完整排版。"""


# ============================================================
#  共享 CSS（练习卷 / 答案卷去重）
# ============================================================
# 说明：下列 * / body / .header 三条规则在原练习卷与答案卷中逐字相同，
#       现集中为单一常量。使用时始终紧随各自 @page 之后注入，以保持与旧代码
#       完全一致的声明顺序（@page → * → body → .header → 具体规则 → @media print）。
#       注：本常量首尾换行的处理，使 f-string 中 `}}{_CSS_BASE}` 的拼接结果
#       与原内联 CSS 逐字一致，不产生额外空行（仅去重，不改变渲染）。
_CSS_BASE = """
    * {
        box-sizing: border-box;
    }
    body {
        /* 浏览器默认 body 外边距 8px 会让内容区比 @page 内容区更窄、更内缩，
           导致双栏行与通栏卡左右边沿对不齐，因此显式清零。 */
        margin: 0;
        font-family: "Noto Sans CJK SC", "WenQuanYi Micro Hei", "Microsoft YaHei", "微软雅黑", "STHeiti", sans-serif;
        font-size: 11pt;
        line-height: 1.6;
        color: #222;
    }
    .header {
        text-align: center;
        font-size: 16pt;
        font-weight: bold;
        font-family: "Noto Sans CJK SC", "WenQuanYi Micro Hei", "Microsoft YaHei", "微软雅黑", "STHeiti", sans-serif;
        margin-bottom: 10mm;
        padding-bottom: 5mm;
        border-bottom: 2px solid #333;
    }"""


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

    图片高度上限走 CSS 变量 --img-max-h（由题卡内联样式按 LayoutState 注入），
    未注入时回退 80vh，保证单独使用该函数时行为不变。

    缩放规则：max-width = min(容器上限, 自然宽度)，自然宽度按截图采集倍率
    （SCREENSHOT_DPI）折算，因此图片只缩不放 —— 2x 截图的字号不会被放大成
    巨型字，也不会在窄栏里被强行撑满；height: auto 保证等比。
    """
    natural_px = image_natural_width_px(api_image_path)
    max_width_css = (f"min({max_width}, {natural_px:.0f}px)"
                     if natural_px else max_width)
    style = (f'max-width: {max_width_css}; height: auto; '
             f'max-height: var(--img-max-h, 80vh); '
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


def _html_build_question_body(item, idx, is_compact=False, pad_px=0.0, end_html=""):
    """
    构建单道题目的 HTML 内容字符串（不含外层容器标签）。
    返回 (html_content, has_images) 元组。

    pad_px：双栏等高用的垂直留白（像素）。在标题之后与正文末尾各插入同样高度的
    分隔块，使较矮题卡与同排较高题卡等高，并让图片落在剩余留白中间。
    end_html：插在正文与底部留白之间的标记（结束哨兵），保证测到的是正文下边缘，
    不受 pad / min-height 影响。

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

    pad_tag = (f'<div class="q-pad" style="height: {pad_px:.1f}px;"></div>'
               if pad_px > 0 else "")
    if pad_tag:
        parts.append(pad_tag)

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

    if end_html:
        parts.append(end_html)

    if pad_tag:
        parts.append(pad_tag)

    return "\n".join(parts), has_images


def _unit_key(prefix, idx):
    return f"{prefix}-{idx}"


def _unit_label(idx, title):
    return f"{idx}. {format_question_title(title)}"


def _end_sentinel(key):
    """题卡结束哨兵：零高度元素，其锚点 y 即该题的内容下边缘。"""
    return f'<div id="{key}-end" style="height: 0; line-height: 0;"></div>'


def _layout_order(selected_items):
    """
    返回最终视觉排版顺序 [(item, is_compact), ...]。

    顺序规则：半栏题聚在一起并按估算高度降序（高度接近者相邻，便于配对成行），
    通栏题保持原有相对顺序跟在后面。**卷面序号在调用方按本列表顺序重新生成**，
    因此序号始终是从上到下、从左到右连续的 1..N。
    """
    compacts = []
    fulls = []
    for item in selected_items:
        if is_compact_item(item):
            compacts.append(item)
        else:
            fulls.append(item)
    compacts.sort(key=estimate_compact_height, reverse=True)
    return ([(it, True) for it in compacts] +
            [(it, False) for it in fulls])


# ============================================================
#  HTML 源码生成器 —— 练习卷（重写版）
# ============================================================
def generate_html_practice(selected_items, current_time_str, target_folders_str,
                           layout_state=None):
    """
    生成 HTML 练习卷源码字符串。

    排版策略（顺序让位于空间利用率）：
    - 半栏题（图片自然宽度 ≤ 60% 页宽，或纯文字 ≤ 4 行）聚在一起，按估算高度
      降序两两配对成行；通栏题保持原有相对顺序排在所有双栏行之后；
    - **卷面序号在排版顺序确定后按视觉顺序重新编号**（1..N 连续），
      标题里的 DS0048: 16 等溯源信息来自文档标题，保持不变；
    - 同一行的两张题卡等高：较矮卡由 fit_document() 注入上下等量留白补齐
      （state.pads），使圆角底边对齐、图片落在剩余留白中间；
    - 双栏行用 inline-block + 精确宽度实现，行首/行尾与通栏卡左右边沿严格对齐；
    - 每道题带 id（q-N，N 为卷面序号）与结束哨兵（q-N-end），供 fit_document() 测量；
    - 题卡内联 --img-max-h，使图片高度上限可被适配循环逐轮收缩。
    """
    state = layout_state if layout_state is not None else LayoutState()
    date_str = current_time_str[:10]
    half_gutter_mm = GRID_GUTTER_MM / 2

    # 构建 CSS
    css = f"""
    @page {{
        size: A4;
        margin: {PAGE_MARGIN_MM}mm;
        @bottom-center {{
            content: "{_html_escape(target_folders_str)} | {_html_escape(current_time_str)}";
            font-size: 9pt;
            color: #666;
        }}
    }}{_CSS_BASE}
    /* 双栏行 — inline-block 卡片刻度，行首/行尾与通栏卡左右边沿对齐 */
    .compact-row {{
        margin-bottom: 4mm;
        break-inside: avoid;
        page-break-inside: avoid;
    }}
    .compact-row > .question-card {{
        display: inline-block;
        width: calc(50% - {half_gutter_mm}mm);
        vertical-align: top;
        margin-right: {GRID_GUTTER_MM}mm;
    }}
    .compact-row > .question-card:last-child {{
        margin-right: 0;
    }}
    /* 通栏题目包装块。
       注意：这里**禁止**加 break-inside / page-break-inside: avoid。
       WeasyPrint 69 在「外层包装块 + 内层 .question-card 同时 avoid」时，会把
       包装块高度算成整页内容高（实测 1046.9px），导致第一张通栏卡就放不下、
       被推到第 2 页，后续每张卡各自独占一页。防断裂只由 .question-card 承担。 */
    .full-width {{
        width: 100%;
        margin-bottom: 4mm;
    }}
    /* 单道题目的容器 */
    .question-card {{
        padding: 2mm 3mm;
        border: 1px solid #ddd;
        border-radius: 2mm;
        background: #fafafa;
        /* 用 min-height 确保卡片不会收缩到 0 高度 */
        min-height: 20mm;
        /* 显式声明，避免任何隐式裁剪导致正文不可见 */
        overflow: visible;
        break-inside: avoid;
        page-break-inside: avoid;
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
        height: auto;
        max-height: var(--img-max-h, 80vh);
        object-fit: contain;
    }}
    /* 紧凑字号 —— 图片缩到下限仍放不下时由适配循环启用 */
    .question-card.dense {{
        font-size: 8.5pt;
        line-height: 1.35;
        padding: 1mm 2mm;
    }}
    .question-card.dense .q-title {{ font-size: 10pt; }}
    .question-card.dense .q-spacer {{ height: 0 !important; }}
    @media print {{
        /* 通栏包装块不参与 avoid（见上方 .full-width 注释）；仅行与卡片防断裂。 */
        .compact-row,
        .question-card {{
            break-inside: avoid;
            page-break-inside: avoid;
        }}
    }}
    """

    # 构建 body
    body_parts = []
    body_parts.append(f'<div class="header">今日练习 {date_str}</div>')

    state.rows = []
    pending_cards = []
    pending_keys = []

    def flush_row():
        """输出一行双栏（单数时右格留空），并记录该行成员供等高测量使用。"""
        if not pending_cards:
            return
        body_parts.append('<div class="compact-row">'
                          + "".join(pending_cards) + '</div>')
        state.rows.append((pending_keys[0],
                           pending_keys[1] if len(pending_keys) > 1 else None))
        pending_cards.clear()
        pending_keys.clear()

    # 顺序策略：先紧凑拼版，再通栏；卷面序号在顺序确定后按视觉顺序重新编号。
    for vis, (item, is_compact) in enumerate(_layout_order(selected_items), 1):
        key = _unit_key("q", vis)
        cap = state.image_caps.setdefault(key, INITIAL_IMAGE_MAX_HEIGHT_PX)
        pad = state.pads.get(key, 0.0)
        # 半栏题的结束哨兵放正文末尾（不受 pad / min-height 影响）；通栏题放包装块之后
        end_html = _end_sentinel(key) if is_compact else ""
        html_body, has_images = _html_build_question_body(
            item, vis, is_compact=is_compact, pad_px=pad, end_html=end_html)
        state.has_images[key] = has_images
        state.labels[key] = _unit_label(vis, item[1])
        dense_cls = " dense" if key in state.dense else ""

        card_open = (f'<div class="question-card{dense_cls}" id="{key}" '
                     f'style="--img-max-h: {cap:.1f}px;">{html_body}')

        if is_compact:
            state.end_keys[key] = f"{key}-end"
            state.bottom_slop[key] = CARD_BOTTOM_SLOP_PX
            pending_cards.append(card_open + '</div>')
            pending_keys.append(key)
            if len(pending_cards) == 2:
                flush_row()
        else:
            state.end_keys[key] = f"{key}-out-end"
            state.bottom_slop[key] = 0.0
            flush_row()
            body_parts.append(
                f'<div class="full-width">{card_open}</div>'
                f'{_end_sentinel(key + "-out")}')

    flush_row()

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
def generate_html_answer(selected_items, current_time_str, target_folders_str,
                         layout_state=None):
    """
    生成 HTML 答案卷源码字符串。
    通栏排版，每道题显示序号 + 答案内容 + 答案图片。
    使用 break-inside: avoid 防止单个答案跨页。

    每张答案卡带 id（a-N）、结束哨兵（a-N-end）与 --img-max-h，
    由同一个 fit_document() 适配循环保证整卡落在单页内。

    升级 v2：如果 cat == "困难"，在标题前加 ⭐ 符号。
    """
    state = layout_state if layout_state is not None else LayoutState()
    date_str = current_time_str[:10]

    css = f"""
    @page {{
        size: A4;
        margin: {PAGE_MARGIN_MM}mm;
        @bottom-center {{
            content: "答案 | {_html_escape(target_folders_str)} | {_html_escape(current_time_str)}";
            font-size: 9pt;
            color: #666;
        }}
    }}{_CSS_BASE}
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
        height: auto;
        max-height: var(--img-max-h, 80vh);
        object-fit: contain;
    }}
    /* 紧凑字号 —— 图片缩到下限仍放不下时由适配循环启用 */
    .answer-card.dense {{
        font-size: 8.5pt;
        line-height: 1.35;
        padding: 1.5mm 2mm;
    }}
    .answer-card.dense .a-title {{ font-size: 10pt; }}
    @media print {{
        .answer-card {{
            break-inside: avoid;
            page-break-inside: avoid;
        }}
    }}
    """

    body_parts = []
    body_parts.append(f'<div class="header">【答案】今日练习 {date_str}</div>')

    for vis, (item, is_compact) in enumerate(_layout_order(selected_items), 1):
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
        card_parts.append(f'<div class="a-title">{vis}. 【答案】{_html_escape(display_title)}</div>')

        if answer_text:
            for para in answer_text.split("\n"):
                para = para.strip()
                if para:
                    card_parts.append(f'<div class="a-text">{_html_escape(para)}</div>')

        has_images = False
        for img_path in answer_images:
            tag = _html_image_tag(img_path, max_width="70%")
            if tag:
                card_parts.append(f'<div class="a-image">{tag}</div>')
                has_images = True

        key = _unit_key("a", vis)
        cap = state.image_caps.setdefault(key, INITIAL_IMAGE_MAX_HEIGHT_PX)
        state.has_images[key] = has_images
        state.labels[key] = _unit_label(vis, item[1])
        state.end_keys[key] = f"{key}-end"
        state.bottom_slop[key] = 0.0
        dense_cls = " dense" if key in state.dense else ""

        card = (
            f'<div class="answer-card{dense_cls}" id="{key}" '
            f'style="--img-max-h: {cap:.1f}px;">{"".join(card_parts)}</div>'
        )
        body_parts.append(f'{card}{_end_sentinel(key)}')

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
#  排版测量与适配
# ============================================================
def _collect_anchor_points(doc):
    """返回 {anchor_name: (page_index, y)}，同名锚点取首次出现页。"""
    points = {}
    for page_index, page in enumerate(doc.pages):
        for name, target in page.anchors.items():
            points.setdefault(name, (page_index, target[1]))
    return points


def _layout_problems(doc, state, points=None):
    """返回在单页内放不下的题目 key 列表（跨页 / 越出内容区底部 / 锚点缺失）。"""
    if points is None:
        points = _collect_anchor_points(doc)
    bad = []
    for key in state.has_images:
        start = points.get(key)
        end = points.get(state.end_keys.get(key, f"{key}-end"))
        if start is None or end is None:
            bad.append(key)
            continue
        slop = state.bottom_slop.get(key, 0.0) + state.pads.get(key, 0.0)
        if start[0] != end[0] or end[1] + slop > PAGE_CONTENT_BOTTOM_PX + 1.0:
            bad.append(key)
    return bad


def _update_row_pads(points, state):
    """
    让同一双栏行的两张卡等高：测量两卡当前高度差，把差值的一半各补到较矮卡的
    标题之后与正文之后，使圆角底边对齐、图片落在剩余留白中间。

    返回是否有 pad 变化（有则需再渲染一轮）。
    """
    changed = False
    for left, right in state.rows:
        if right is None:
            continue
        heights = {}
        for key in (left, right):
            start = points.get(key)
            end = points.get(f"{key}-end")
            if start is None or end is None or start[0] != end[0]:
                heights = {}
                break
            heights[key] = end[1] - start[1] - state.pads.get(key, 0.0)
        if not heights:
            continue
        target = max(heights.values())
        for key, height in heights.items():
            new_pad = max(0.0, (target - height) / 2.0)
            if abs(new_pad - state.pads.get(key, 0.0)) > 0.5:
                state.pads[key] = new_pad
                changed = True
    return changed


def _format_layout_error(state, keys):
    labels = [state.labels.get(k, k) for k in keys]
    return "无法在 A4 单页内完整排版：" + ("；".join(labels) or "未知题目")


def fit_document(build_html: Callable[[LayoutState], str]):
    """
    排版适配：反复渲染 → 锚点校验每题完整落在单页内 + 同排双栏卡等高。

    每轮流程：
      1. 无跨页/越底问题 → 计算双栏等高留白；留白稳定即返回结果；
      2. 仍有问题 → 有图片且上限未到下限的按 IMAGE_CAP_SHRINK 收缩，
         否则启用 dense（紧凑字号 + 隐藏作答留白）；
      3. 已 dense 仍不满足 → LayoutError（不输出被截断的 PDF）。

    参数：
      build_html: (LayoutState) -> str，用给定状态重建完整 HTML。
    返回：
      (weasyprint.document.Document, LayoutState)
    """
    state = LayoutState()
    for attempt in range(MAX_FIT_ITERATIONS + 1):
        doc = HTML(string=build_html(state)).render()
        points = _collect_anchor_points(doc)
        problems = _layout_problems(doc, state, points)
        if not problems:
            if not _update_row_pads(points, state):
                return doc, state
            continue
        if attempt == MAX_FIT_ITERATIONS:
            raise LayoutError(_format_layout_error(state, problems))
        for key in problems:
            if key in state.dense:
                raise LayoutError(_format_layout_error(state, [key]))
            if state.has_images.get(key):
                cap = state.image_caps.get(key, INITIAL_IMAGE_MAX_HEIGHT_PX)
                if cap > MIN_IMAGE_MAX_HEIGHT_PX:
                    state.image_caps[key] = max(
                        MIN_IMAGE_MAX_HEIGHT_PX, cap * IMAGE_CAP_SHRINK
                    )
                    continue
            state.dense.add(key)
    raise LayoutError(_format_layout_error(state, []))


# ============================================================
#  HTML → PDF 编译函数（使用 WeasyPrint）
# ============================================================
def compile_with_layout(build_html, output_pdf_path):
    """
    按「每题完整落在单页内」的要求编译 PDF。

    参数：
      build_html     : (LayoutState) -> str - 用适配状态重建完整 HTML 的回调
      output_pdf_path: str - 输出的 PDF 文件路径

    返回：
      (success: bool, message: str)；排版无解时不写出任何 PDF 文件。
    """
    if not _HAS_WEASYPRINT:
        return False, "❌ WeasyPrint 未安装。请运行: pip install weasyprint"

    try:
        doc, _state = fit_document(build_html)
        doc.write_pdf(output_pdf_path, font_config=FontConfiguration())
        return True, f"✅ PDF 生成成功: {output_pdf_path}（{len(doc.pages)} 页）"
    except LayoutError as e:
        return False, f"❌ 排版失败: {e}"
    except Exception as e:
        return False, f"❌ PDF 生成失败: {e}"
