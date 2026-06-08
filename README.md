# 📝 思源笔记 —— 错题拼卷机 (HTML+WeasyPrint 版)

> 基于 **思源笔记**（SiYuan Note）服务端 API 的自动化错题抽取 & PDF 生成工具。
> 专为孩子每日数学错题练习设计：扫描笔记本中结构化错题文档 → 筛选未掌握的题目 → 自动生成 **练习卷** 和 **答案卷** 两份 PDF 文件。
>
> ~~曾拥有过 16.6% 的屎山代码，今天终于完成了重构[doge]~~ 🎉

---

## ✨ 功能概览

| 功能 | 说明 |
|------|------|
| 📥 自动扫描 | 连接本地思源笔记 API，按指定目录（如 `DS0001`）扫描所有 `.sy` 文档 |
| 🏷️ 智能分类 | 根据文档标题识别「基础」「易错」「困难」三类题目 |
| 📊 积分管理 | 解析内置积分表，**跳过**总积分 ≥ 阈值的已掌握题目（默认阈值 3） |
| 🎯 智能选题 | 基础 + 易错题全部入选；页数不足目标时，从困难池按积分降序补充 |
| 📄 双卷输出 | 同时生成 **练习卷**（留白作答）和 **答案卷**（带解答），均为 A4 PDF 格式 |
| 🖼️ 图片保留 | 自动映射思源笔记资产目录下的图片，嵌入到题文中 |
| 📐 页数估算 | 基于文字量和图片数估算最终页数，确保练习卷容量合理 |
| 🎨 双栏排版 | 小尺寸题目自动并排显示，大幅节省纸张 |
| 🐧 Linux 友好 | CSS 字体回退优先使用 Linux 开源字体（Noto Sans CJK SC / WenQuanYi Micro Hei） |

---

## 🧩 前置依赖

- **Python 3.8+**
- **思源笔记** 已启动，且开启 **网络伺服**（`设置 → 关于 → 网络伺服`）
- 安装以下 Python 包：

```bash
pip install requests Pillow weasyprint
```

> 如果需操作思源笔记物理数据目录（图片映射），请确保运行脚本的用户有读取权限。
> WeasyPrint 在 Linux 上可能需要额外系统库，详见：[WeasyPrint 安装文档](https://doc.courtbouillon.org/weasyprint/latest/first_steps.html)

---

## 🚀 快速开始

### 1. 安装与配置

复制示例配置文件并填写你的实际信息：

```bash
cp config.json.example config.json
```

然后编辑 `config.json`，填入你的思源笔记连接信息：

```json
{
    "SIYUAN_URL": "http://YOUR_WINDOWS_IP:6806",
    "API_TOKEN": "YOUR_API_TOKEN",
    "NOTEBOOK_ID": "YOUR_NOTEBOOK_ID",
    "SIYUAN_DATA_PATH": "/path/to/siyuan/workspace/data",
    "TARGET_FOLDERS": ["DS0001"],
    "SCORE_THRESHOLD": 3,
    "MIN_PAGES": 2
}
```

各配置项说明：

| 参数 | 说明 |
|------|------|
| `SIYUAN_URL` | 思源笔记 API 地址（默认端口 6806） |
| `API_TOKEN` | 从思源设置 → API Token 获取 |
| `NOTEBOOK_ID` | 目标笔记本 ID |
| `SIYUAN_DATA_PATH` | 思源 data 目录的**物理路径**，用于读取图片文件 |
| `TARGET_FOLDERS` | 扫描的目标目录名称列表（支持模糊匹配） |
| `SCORE_THRESHOLD` | 总积分 ≥ 此值时跳过该题（已掌握） |
| `MIN_PAGES` | 练习卷最少估算页数，不足时从困难池补充 |

> ⚠️ **安全提醒**：`config.json` 包含你的 API Token 等敏感信息，已默认加入 `.gitignore`，请勿将其提交到代码仓库。

### 2. 运行

```bash
python siyuan_client.py
```

输出示例：

```
📋 思源笔记 —— 错题拼卷机 (HTML + WeasyPrint 版)
============================================================

🎯 当前选题范围: ['DS0001']
   积分阈值: < 3 (已掌握跳过)
   目标页数: >= 2 页

📁 正在针对以下目录扫描：
   📂 DS0001  (/data/YOUR_NOTEBOOK_ID/xxxxx-ds0001id)

📂 [DS0001] 找到 12 个文档
   📄 xxxxx.sy → 01 基础 #分数比较  [基础]
       总积分: 1
   📄 yyyyy.sy → 02 易错 #图形分割  [易错]
       总积分: 2
   ...
```

生成的文件：

```
今日练习_20260608_2127.pdf       ← 练习卷（留白作答）
今日练习_答案_20260608_2127.pdf  ← 答案卷（带解答）
```

---

## 📝 文档模板规范

本项目配合**数学练习模板04.md**使用。每道错题是一个独立的 `.sy` 文档，结构如下：

### 积分表（文档开头）

| 录入与练习日期 | 对错 | 积分 | 总积分 |
| -------------- | ---- | ---- | ------ |
| 录入           |     |  -1  |   -1   |
| 练习           |     |      |        |
| 练习           |     |      |        |

- **总积分**列决定题目是否已掌握（`总积分 ≥ SCORE_THRESHOLD` 则跳过）
- 表格可包含多行练习记录

### 题目区

```markdown
# 题目

> （题目的文字描述，支持图片 `![描述](assets/xxx.png)`）
```

### 答案区

```markdown
## 答案

> （解答的文字描述，支持图片）
```

### 扩展题目（可选）

```markdown
## 扩展题目01

> 扩展题目01 内容
```

> 📌 **注意**：本项目目前只解析 `# 题目` / `## 答案` 主区块，扩展题目暂不纳入选题范围。

### 文档标题命名规则

文档标题用于分类和显示，建议格式：

```
<编号> <分类标签> #主题标签 #知识点
```

- 包含 **困难** → 归入「困难」池
- 包含 **易错** → 归入「易错」池
- 其余（含 **基础**）→ 归入「基础」池

示例：

```
01 基础 #分数比较 #分数大小
02 易错 #图形分割求分数
03 困难 #复杂分数应用题
```

---

## ⚙️ 配置详解

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `SIYUAN_URL` | `http://YOUR_WINDOWS_IP:6806` | 思源笔记 API 地址 |
| `API_TOKEN` | — | 从思源设置 → API Token 获取 |
| `NOTEBOOK_ID` | — | 目标笔记本 ID，从思源 WebSocket 或文件树获取 |
| `SIYUAN_DATA_PATH` | — | 思源 data 目录的**物理路径**，用于读取图片文件 |
| `TARGET_FOLDERS` | `["DS0001"]` | 扫描的目标目录名称列表（支持模糊匹配） |
| `SCORE_THRESHOLD` | `3` | 总积分 ≥ 此值时跳过该题（已掌握） |
| `MIN_PAGES` | `2` | 练习卷最少估算页数，不足时从困难池补充 |

---

## 🧠 选题引擎逻辑

1. **初筛**：遍历目标目录下所有文档，解析积分表，跳过 `总积分 ≥ SCORE_THRESHOLD` 的题目
2. **分类**：按标题关键词将剩余文档归入「基础」「易错」「困难」池
3. **基础入选**：「基础」「易错」池全部进入最终列表
4. **页数判断**：估算当前列表的打印页数
5. **困难补充**：若页数 < `MIN_PAGES`，从「困难」池按积分降序依次补充，直到满足页数
6. **警告**：若困难池为空且页数仍不足，输出黄色警告提示

---

## 🖼️ 图片映射机制

脚本通过多种候选路径策略定位图片文件：

1. `{SIYUAN_DATA_PATH}/assets/{filename}`
2. `{SIYUAN_DATA_PATH}/{NOTEBOOK_ID}/assets/{filename}`
3. 去除 `NOTEBOOK_ID` 前缀后尝试
4. 在所有候选目录中按文件名搜索
5. 完整 `/data/...` 路径替换

---

## 🎨 排版特性

### 练习卷

- A4 页面，10mm 页边距
- **CSS Grid 双栏布局**：紧凑型题目自动两两并排
- **通栏排版**：大型题目（含宽图或多行文字）独占一行
- `break-inside: avoid` 防止题目跨页截断
- 页脚显示选题范围和生成时间

### 答案卷

- 通栏排版，每道答案独立卡片
- 答案标题红色醒目
- 图片最大宽度 70%，防止溢出
- `break-inside: avoid` 防止答案跨页

### PDF 生成

- 使用 **WeasyPrint** 将 HTML 编译为 PDF
- 支持 `FontConfiguration` 自动查找系统字体
- CSS `@page` 控制页面尺寸、边距和页脚

---

## 🐧 Linux 字体渲染

CSS 字体回退策略优先使用 Linux 开源字体，避免 PDF 出现方块字：

```css
font-family: "Noto Sans CJK SC", "WenQuanYi Micro Hei",
             "Microsoft YaHei", "微软雅黑", "STHeiti", sans-serif;
```

安装推荐字体：

```bash
# Debian/Ubuntu
sudo apt install fonts-noto-cjk fonts-wqy-microhei

# Fedora
sudo dnf install google-noto-sans-cjk-fonts wqy-microhei-fonts
```

---

## 📂 输出文件

| 文件 | 内容 |
|------|------|
| `今日练习_YYYYMMDD_HHMM.pdf` | 练习卷：题目 + 留白（含图片），A4 页面 |
| `今日练习_答案_YYYYMMDD_HHMM.pdf` | 答案卷：题目标题 + 答案解析 + 图片，紧凑排版 |

---

## 🛠️ 常见问题

### Q: 连接不上思源笔记？

- 确认思源已开启网络伺服（`设置 → 关于 → 网络伺服`）
- 检查 `SIYUAN_URL` 和端口（默认 6806）
- 检查 `API_TOKEN` 是否正确

### Q: 图片无法显示？

- 确认 `SIYUAN_DATA_PATH` 指向正确的思源 data 目录
- 检查图片文件是否在 `assets/` 目录下
- 脚本会尝试多种路径策略，运行日志中有详细提示

### Q: PDF 生成失败或中文显示方块？

- 确保已安装中文字体（见上方「Linux 字体渲染」章节）
- 检查 WeasyPrint 安装是否正确
- 查看终端输出的错误信息

### Q: 没有选中任何题目？

- 检查 `TARGET_FOLDERS` 是否匹配实际目录名称
- 检查积分表格式是否正确，「总积分」列名需一致
- 查看运行日志中的积分解析结果

### Q: 生成的页数太少？

- 降低 `SCORE_THRESHOLD`（让更多题目进入候选）
- 扩大 `TARGET_FOLDERS` 范围
- 降低 `MIN_PAGES` 目标页数
- 增加更多「困难」类题目

### Q: config.json 丢失？

- 运行脚本时会提示 `未找到配置文件 config.json`
- 执行 `cp config.json.example config.json` 并填写实际信息即可

---

## 📋 项目结构

```
.
├── config.json              # 配置文件（已加入 .gitignore，勿上传）
├── config.json.example      # 示例配置文件（不含真实数据）
├── siyuan_client.py         # 主程序：扫描、选题、生成 HTML/PDF
├── 数学练习模板04.md         # 错题文档模板（参考用）
├── README.md                # 本文件
└── .gitignore               # Git 忽略规则
```

---

## 🔧 历史重构记录

| 版本 | 变更 |
|------|------|
| v1（原始） | 基于 python-docx 生成 Word `.docx`，含 `parse_full_content` 等死代码 |
| v2（当前） | **HTML + WeasyPrint** 生成 PDF，移除 Word 导出，清理死代码，修复 Linux 字体渲染 |

**重构摘要：**
- 🗑️ 移除 `generate_docx_report` 函数（~100 行）
- 🗑️ 移除 `_extract_block_text_and_images` + `parse_full_content` 死代码
- 🎨 CSS 字体回退策略优先 Linux 开源字体
- 🧹 清理 docx 依赖导入和 main() 中冗余逻辑

---

## 📄 许可

本项目仅供个人学习使用，请遵循思源笔记相关许可协议。