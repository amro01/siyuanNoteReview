# -*- coding: utf-8 -*-
"""
思源笔记 —— 错题拼卷机 · 顶层编排入口（步骤5 收敛）

职责：把「扫描 → 过滤 → 选题 → 重排 → 出卷 → 统计」串成单一流程，
      自身不承载任何解析 / 网络 / 渲染细节。

依赖方向（严格单向）：
    main → renderer → parser → siyuan_api → config
"""

from __future__ import annotations

import os
import sys
from datetime import datetime

import config as config_module

from siyuan_api import (
    # 地址解析与连通性
    reset_resolved_base,
    _split_siyuan_url,
    _candidate_hosts,
    resolve_siyuan_base,
    # 思源仓储
    find_target_dirs,
    list_sy_files_in_dir,
    get_doc_title,
    get_block_kramdown,
    # 运行态计数器（共享可变对象，main 读取时拿到的即最新值）
    _ASSET_FETCH_FAILED,
)
from parser import (
    # 数据模型
    QuestionDoc,
    # 解析
    parse_latest_date,
    parse_score_table,
    # 分类 / 标题
    classify_document,
    shorten_title,
    # 版式估算
    estimate_total_pages,
    estimate_compact_height,
    is_compact_item,
    # 选题引擎
    select_questions,
)
from renderer import (
    generate_html_practice,
    generate_html_answer,
    compile_html_to_pdf,
)


def collect_documents(cfg):
    """
    连通性预检 + 扫描目标目录 + 过滤（排除最近练习），返回候选文档列表。

    参数：
      cfg: config.Config —— 已加载并注入单例的配置对象。

    返回：
      List[QuestionDoc] —— 通过日期过滤、待进入选题引擎的候选文档；
      若无法连通（进程退出）/ 未找到目录 / 未找到文档，则返回空列表，
      并在过程中打印与旧实现完全一致的提示信息。
    """
    # 0) 连通性预检（fail-fast）
    #    先确认能连上思源，避免一路跑到 SQL 查询阶段才因超时失败，
    #    也避免宿主机不可达时对每个文档重复等待完整的建连超时。
    if resolve_siyuan_base() is None:
        _scheme, _host, _port = _split_siyuan_url(cfg.siyuan_url)
        _probed = ", ".join(f"{h}:{_port}" for h in _candidate_hosts(_host))
        print("❌ 无法连接到思源笔记 API，已提前终止。")
        print(f"   配置地址: {cfg.siyuan_url}（SIYUAN_HOST_MODE={cfg.siyuan_host_mode}）")
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

    print(f"🔗 链路已就绪，超时配置: 建连 {cfg.connect_timeout}s / 读取 {cfg.read_timeout}s，"
          f"建连重试 {cfg.connect_retries} 次\n")

    # 1) 查找目录
    target_dirs = find_target_dirs()
    if not target_dirs:
        print(f"\n❌ 未找到匹配的目录: {cfg.target_folders}")
        print(f"   请确认笔记本名称或其下的文档标题包含: {cfg.target_folders}。")
        return []

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
                if days_diff <= cfg.exclude_recent_days:
                    short = shorten_title(title)
                    print(f"   [跳过] 距上次练习/录入不足 {cfg.exclude_recent_days} 天：{short} (距今 {days_diff} 天)")
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
        return []

    return all_docs


def main():
    # 步骤1：配置改为显式加载，消除模块导入期副作用；
    #        注入单例后供 siyuan_api / parser 内部按需读取。
    cfg = config_module.load_config()
    config_module.set_config(cfg)
    reset_resolved_base()

    now = datetime.now()
    current_time_str = now.strftime('%Y-%m-%d %H:%M')
    file_time_str = now.strftime('%Y%m%d_%H%M')

    practice_pdf = f"今日练习_{file_time_str}.pdf"
    answer_pdf = f"今日练习_答案_{file_time_str}.pdf"

    target_folders_str = ", ".join(cfg.target_folders)

    print("=" * 60)
    print("📋 思源笔记 —— 错题拼卷机 (HTML + WeasyPrint 版)")
    print("=" * 60)

    print(f"\n🎯 当前选题范围: {cfg.target_folders}")
    print(f"   积分阈值: < {cfg.score_threshold} (已掌握跳过)")
    print(f"   目标页数: {cfg.min_pages} ~ {cfg.max_pages} 页")
    print(f"   排除最近 {cfg.exclude_recent_days} 天内新增/复习的题目")
    print()

    # 0~2) 连通性预检 + 扫描 + 过滤
    all_docs = collect_documents(cfg)
    if not all_docs:
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
