# -*- coding: utf-8 -*-
"""
思源笔记 —— 渲染与产出层（步骤4 抽取）

职责：
  1. HTML 工具：图片标签（本地 file:// 优先 + API base64 兜底）、文本转义；
  2. 题目正文 HTML 片段组装（标题 / 文本 / 图片 / 作答留白）；
  3. 练习卷 / 答案卷 HTML 源码生成（CSS 打印排版）；
  4. 调用 WeasyPrint 将 HTML 编译为 PDF。

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

from siyuan_api import map_image_path, _image_data_uri
from parser import format_question_title, parse_answer, is_compact_item

try:
    from weasyprint import HTML
    from weasyprint.text.fonts import FontConfiguration
    _HAS_WEASYPRINT = True
except ImportError:
    _HAS_WEASYPRINT = False
    print("⚠️  未安装 WeasyPrint，将无法生成 PDF。请运行: pip install weasyprint")


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
    }}{_CSS_BASE}
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
