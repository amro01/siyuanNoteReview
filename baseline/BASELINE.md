# 重构基线（步骤 0 安全网）

> 本文件是「错题拼卷机」模块化重构的回归基准。
> 后续每一步拆分完成后，均以本文件的「基线签名」验收，确保行为不变。

## 元信息

| 项 | 值 |
|----|----|
| 采集时间 | 2026-10-03 21:33（Asia/Shanghai） |
| 采集命令 | `python siyuan_client.py`（conda 环境 `note`） |
| 源码版本 | git `HEAD = 35289e6`（v2.5），工作区干净 |
| 解释器 | Python 3.10.20 |
| 依赖版本 | requests 2.34.2 / Pillow 12.2.0 / weasyprint 69.0 |
| 思源地址 | `http://172.19.192.1:6806`（auto 模式经默认网关探测命中） |

## 运行配置（来自本地 config.json）

| 配置项 | 值 |
|--------|----|
| `TARGET_FOLDERS` | `DS0050, DS0049, JH0051, DS0048, JH0046` |
| `SCORE_THRESHOLD` | `0` |
| `MIN_PAGES` / `MAX_PAGES` | `1` / `999` |
| `EXCLUDE_RECENT_DAYS` | `1` |

## 基线签名（回归验收标准）

| 指标 | 基线值 |
|------|--------|
| 扫描文档总数 | **88**（DS0050=20, DS0049=6, JH0051=6, DS0048=17, JH0046=39） |
| 因积分达标而跳过 | **56** |
| 最终选题数 | **28** |
| 半栏题 / 通栏题 | **6 / 22** |
| 估算页数 | **~9 页（A4）** |
| 进程退出码 | **0** |
| PDF 编译 | 练习卷、答案卷 **两份均成功** |

## 产物清单

| 文件 | 大小 (bytes) | SHA-256 |
|------|--------------|---------|
| `baseline/今日练习_20261003_2133.pdf` | 6,916,561 | `0cd6a793fa3ea5fa0f304fec80023596010340092ecf5a541ebacdfa717b9066` |
| `baseline/今日练习_答案_20261003_2133.pdf` | 2,911,702 | `c841e3cbc7e42f665dcde645fefb5402b60263dfe87595391930b9df64427b07` |
| `baseline/run.log`（1416 行，完整终端输出） | 80,303 | 见本地文件 |

> `baseline/run.log` 与 PDF 为本地留存产物（`run.log` 已加入 .gitignore），不入库。

## 回归判定规则

后续每次拆分的端到端验证，必须同时满足：

1. 进程退出码为 `0`；
2. 扫描文档总数 = 88，各目录文档数一致；
3. 因积分达标跳过 = 56；
4. 最终选题数 = 28，半栏/通栏 = 6/22，估算 ≈ 9 页；
5. 练习卷与答案卷两份 PDF 均生成成功。

> ⚠️ 注意：PDF/HTML 内含生成日期与时间戳，且 `EXCLUDE_RECENT_DAYS` 依赖运行当日日期，因此**字节级哈希无法跨次一致**。上表哈希仅用于「同一次产物」的一致性参考，跨次回归一律以「基线签名」指标为准。

## 复现命令

```bash
conda activate note
python siyuan_client.py > baseline/run.log 2>&1
```

## 环境提示（步骤 0 发现）

- 项目解释器是 **conda 环境 `note`**（`/home/deep/miniconda3/envs/note/bin/python`，Python 3.10.20），**不是** base 环境的 `python3`（base 缺少 weasyprint）。
- 系统已具备 WeasyPrint 依赖库（`libpango-1.0` / `libcairo2`）与中文字体（Noto Sans CJK SC、WenQuanYi Micro Hei、Microsoft YaHei）。
