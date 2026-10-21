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
| 🏷️ 智能分类 | 根据文档标题识别「基础」「易错」「困难」三类题目；困难题标题前自动添加 ⭐ 符号 |
| 📊 积分管理 | 解析内置积分表，**跳过**总积分 ≥ 阈值的已掌握题目（默认阈值 3）；各池内按积分升序排序，积分越低（错误越多）越优先 |
| 🎯 智能选题 | 按「基础 → 易错 → 困难补充」顺序入选；页数不足目标时从困难池按积分升序补充；支持 `MAX_PAGES` 上限控制 |
| 📄 双卷输出 | 同时生成 **练习卷**（留白作答）和 **答案卷**（带解答），均为 A4 PDF 格式 |
| 🖼️ 图片保留 | 自动映射思源笔记资产目录下的图片，嵌入到题文中 |
| 📐 页数估算 | 基于文字量和图片数估算最终页数，确保练习卷容量合理 |
| 🎨 双栏排版 | 先配对重排、再按视觉顺序连续编号 1..N；同排两卡等高（底边平齐、较矮题图片居中），双栏外沿与通栏卡左右对齐 |
| 🔢 题号溯源 | 卷面序号连续无跳号，标题保留 `DS0048: 16` 等原始来源；练习卷与答案卷顺序、编号一一对应 |
| 🖼️ 只缩不放 | 图片按 2x 截图倍率折算自然尺寸，`max-width: min(容器, 自然宽度)`，绝不放大字号；通栏题图片居中保持清晰 |
| 📏 分页完整性 | 两遍式渲染测量：用锚点校验每张题卡/答案卡是否跨页或越出页底，绝不输出被截断的 PDF |
| 🗓️ 近期排除 | 从 Markdown 表格「录入与练习日期」列提取真实练习日期，跳过最近 `EXCLUDE_RECENT_DAYS` 天内的题目（默认 2 天） |
| 📄 页数上限 | 支持 `MAX_PAGES` 配置（默认 10 页），防止练习卷过长 |
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
    "SIYUAN_HOST_MODE": "auto",
    "API_TOKEN": "YOUR_API_TOKEN",
    "NOTEBOOK_ID": "YOUR_NOTEBOOK_ID",
    "SIYUAN_DATA_PATH": "/path/to/siyuan/workspace/data",
    "TARGET_FOLDERS": ["DS0001"],
    "SCORE_THRESHOLD": 3,
    "MIN_PAGES": 2,
    "MAX_PAGES": 10,
    "EXCLUDE_RECENT_DAYS": 2,
    "CONNECT_TIMEOUT": 3,
    "READ_TIMEOUT": 30,
    "CONNECT_RETRIES": 2,
    "RETRY_BACKOFF": 0.5
}
```

各配置项说明：

| 参数 | 说明 |
|------|------|
| `SIYUAN_URL` | 思源笔记 API 地址（默认端口 6806）；写死的 IP 会作为「首选候选」参与自动探测 |
| `SIYUAN_HOST_MODE` | `auto`（默认）按「配置地址 → 127.0.0.1 → localhost → 默认网关」自动探测；`fixed` 则只用配置地址 |
| `API_TOKEN` | 从思源设置 → API Token 获取 |
| `NOTEBOOK_ID` | 目标笔记本 ID |
| `SIYUAN_DATA_PATH` | 思源 data 目录的**物理路径**，用于读取图片文件 |
| `TARGET_FOLDERS` | 扫描的目标目录名称列表（支持模糊匹配；未匹配时会列出可用范围与相近名称） |
| `SCORE_THRESHOLD` | 总积分 ≥ 此值时跳过该题（已掌握） |
| `MIN_PAGES` | 练习卷最少估算页数，不足时从困难池补充 |
| `MAX_PAGES` | 练习卷最大页数限制，超过此值时停止添加新题（默认 10） |
| `EXCLUDE_RECENT_DAYS` | 排除最近 N 天内录入/复习的题目，从 Markdown 表格日期列判断（默认 2） |
| `CONNECT_TIMEOUT` | TCP 建连超时秒数（默认 3）；超时会导致 `ConnectTimeout`，即 WSL 场景下的静默超时 |
| `READ_TIMEOUT` | 响应读取超时秒数（默认 30）；连接已建立但思源响应缓慢时触发 |
| `CONNECT_RETRIES` | 建连失败后的重试次数（默认 2），仅重试建连阶段 |
| `RETRY_BACKOFF` | 重试退避基数秒数（默认 0.5），实际等待 = 基数 × 2^重试序号 |

> ⚠️ **安全提醒**：`config.json` 包含你的 API Token 等敏感信息，已默认加入 `.gitignore`，请勿将其提交到代码仓库。

### 2. 运行

```bash
# 推荐：以 main.py 作为编排入口
python main.py

# 兼容：旧入口仍可用（内部转发到 main.py）
python siyuan_client.py
```

输出示例：

```
📋 思源笔记 —— 错题拼卷机 (HTML + WeasyPrint 版)
============================================================

🎯 当前选题范围: ['DS0001']
   积分阈值: < 3 (已掌握跳过)
   目标页数: 2 ~ 10 页
   排除最近 2 天内新增/复习的题目

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
| `SIYUAN_URL` | `http://YOUR_WINDOWS_IP:6806` | 思源笔记 API 地址（作为自动探测的首选候选） |
| `SIYUAN_HOST_MODE` | `auto` | 地址解析模式：`auto` 自动探测 / `fixed` 仅用配置地址 |
| `API_TOKEN` | — | 从思源设置 → API Token 获取 |
| `NOTEBOOK_ID` | — | 目标笔记本 ID，从思源 WebSocket 或文件树获取 |
| `SIYUAN_DATA_PATH` | — | 思源 data 目录的**物理路径**，用于读取图片文件 |
| `TARGET_FOLDERS` | `["DS0001"]` | 扫描的目标目录名称列表（支持模糊匹配；未匹配时会列出可用范围与相近名称） |
| `SCORE_THRESHOLD` | `3` | 总积分 ≥ 此值时跳过该题（已掌握） |
| `MIN_PAGES` | `2` | 练习卷最少估算页数，不足时从困难池补充 |
| `MAX_PAGES` | `10` | 练习卷最大页数限制，超过此值时停止添加新题 |
| `EXCLUDE_RECENT_DAYS` | `2` | 排除最近 N 天内录入/复习的题目，从表格日期列判断 |
| `CONNECT_TIMEOUT` | `3` | TCP 建连超时秒数 |
| `READ_TIMEOUT` | `30` | 响应读取超时秒数 |
| `CONNECT_RETRIES` | `2` | 建连失败重试次数（仅建连阶段） |
| `RETRY_BACKOFF` | `0.5` | 重试退避基数（秒），等待 = 基数 × 2^重试序号 |

---

## 🧠 选题引擎逻辑

1. **初筛**：遍历目标目录下所有文档，解析积分表，跳过 `总积分 ≥ SCORE_THRESHOLD` 的题目
2. **近期排除**：从 Markdown 表格「录入与练习日期」列提取最新练习日期，跳过距今 ≤ `EXCLUDE_RECENT_DAYS` 天的题目
3. **分类**：按标题关键词将剩余文档归入「基础」「易错」「困难」池
4. **池内排序**：各池均按**积分升序**排序（`score=None` 的全新题目视为最优先，排在最前）
5. **顺序入选**：先遍历「基础」池全部入选，再遍历「易错」池全部入选
6. **页数上限检查**：每添加一题前估算总页数，若已达 `MAX_PAGES` 则立即停止添加
7. **困难补充**：若页数 < `MIN_PAGES`，从「困难」池按积分升序依次补充，直到满足页数或达 `MAX_PAGES` 上限
8. **警告**：若困难池为空且页数仍不足，输出黄色警告提示

---

## 🖼️ 图片映射机制

脚本通过多种候选路径策略定位图片文件：

1. `{SIYUAN_DATA_PATH}/assets/{filename}`
2. `{SIYUAN_DATA_PATH}/{NOTEBOOK_ID}/assets/{filename}`
3. 去除 `NOTEBOOK_ID` 前缀后尝试
4. 在所有候选目录中按文件名搜索
5. 完整 `/data/...` 路径替换

> 🛟 **API 兜底（v2.4 新增）**：若以上本地路径全部失败（例如 `SIYUAN_DATA_PATH` 配置错误、目录不存在或与新版思源不一致），脚本会自动改用思源接口 `POST /api/file/getFile`（资源路径为 `/data/assets/{filename}`）直接拉取图片字节，并以内嵌 `data:image/...;base64,...` 的形式写入 HTML。这样即使本机没有挂载思源 data 目录，题目/答案卡片中的图片依然能正常渲染，不会再出现“卡片正文为空”的情况。

---

## 🌐 WSL2 网络与宿主机地址解析（v2.5）

在 WSL2 默认 **NAT 模式**下，Windows 宿主机地址等于 WSL 的**默认网关**（例如 `172.20.0.1`）。
该网段会随 **Windows 重启、休眠恢复、Wi-Fi/有线/VPN 切换** 被重新分配，
因此把地址硬编码进 `config.json` 会周期性失效，表现为**连接静默超时**：

```log
File ".../urllib3/util/connection.py", line 73, in create_connection
  sock.connect(sa)          ← SYN 无人应答 → ConnectTimeout（而非 ConnectionRefused）
```

v2.5 起脚本在启动时按以下优先级自动探测可用地址，并对每个候选做 **1.5s 级 TCP 预检**：

| 优先级 | 候选地址 | 适用场景 |
|--------|----------|----------|
| 1 | `SIYUAN_URL` 中配置的地址 | 快速路径（地址仍有效时约 1.5s 内命中） |
| 2 | `127.0.0.1` / `localhost` | WSL1，或 `.wslconfig` 开启 `networkingMode=mirrored` |
| 3 | 默认网关 | WSL2 NAT 模式下的 Windows 宿主机 |

要点：

- 解析结果会缓存复用；**全部候选失败则 fail-fast 退出** 并打印排查清单，不会逐个文档重复等待超时
- 仅对 **建连阶段** 失败重试（指数退避）；**读取超时不重试**，避免重放可能已送达并执行的请求
- **不要**把 `/etc/resolv.conf` 的 `nameserver` 当作宿主机地址：新版 WSL 的 DNS 隧道会将其设为 `10.255.255.254`，那是 DNS 代理而非宿主机，连接必然超时
- **ping 不通宿主机属 WSL2 正常现象**，判断链路请一律使用 TCP 探测
- 若希望彻底摆脱网关漂移，可在 `%USERPROFILE%\.wslconfig` 增加 `[wsl2]` + `networkingMode=mirrored`，再执行 `wsl --shutdown`

---

## 🎨 排版特性

### 练习卷

- A4 页面，10mm 页边距（`body` 默认外边距已清零，内容区精确 190mm）
- **顺序让位于空间利用率**：半栏题集中在一起，按估算高度降序两两配对（高度接近者同行），
  通栏题排在所有双栏行之后，仍保持彼此原有相对顺序
- **卷面序号最后生成**：所有配对/重排确定后，按从上到下、从左到右的最终渲染顺序重新编号
  1..N（连续、无跳号），标题里的 `DS0048: 16` 等溯源信息保持不变；答案卷复用同一顺序与编号
- **半栏判定（宽度优先）**：图片自然宽度 ≤ 60% 页宽 → 半栏，**一律**不因行数改判；
  60%~65% 也留在半栏内自适应微缩；> 65% 才给通栏；纯文字题按 ≤ 4 行判半栏
- **自然宽度口径**：截图按 2x（HiDPI）采集，`SCREENSHOT_DPI = 192`，即一个图像像素 ≈ 0.5 CSS px；
  实测本库截图正文墨迹高 ≈23px（对应 16px 字号 @2x），故取 192
- **只缩不放**：`max-width: min(容器上限, 自然宽度)` + `height: auto`，图片不会把字号放大成巨型字
- **双栏等高对齐**：同一行的两张卡由适配循环注入上下等量留白（`.q-pad`）补齐高度差，
  圆角/边框底边严格平齐；较矮题的图片落在剩余留白正中
- **栅格严丝合缝**：双栏行用 inline-block 精确宽度（`calc(50% - 3mm)` + 6mm 间距），
  行首/行尾边沿与通栏卡完全一致（实测左 10.0mm、右 200.0mm）
- **单个紧凑题仍占半栏**：同行右侧留空，分类结果不随题目数量变化
- 分页单元是「行」而不是整张表：一行双栏整体不拆页，行与行之间可分页
- 页脚显示选题范围和生成时间

### 答案卷

- 通栏排版，每道答案独立卡片，与练习卷使用**同一视觉顺序与同一套卷面序号**
- 答案标题红色醒目
- 图片最大宽度 min(70%, 自然宽度)，防止溢出、也不放大
- 每张答案卡同样受单页完整性约束

### 分页完整性保证

- **两遍式测量**：把 HTML 渲染成 WeasyPrint 文档，读取每道题起止锚点（`#q-N` / `#q-N-end`）的页码与 y 坐标
- **判定**：起始页 ≠ 结束页（被拆到多页）或结束 y 超出内容区底部 → 该题不合格
- **收缩顺序**：先按 0.75 逐轮收缩该题图片高度上限（下限 60px），再退化为紧凑字号（8.5pt / 1.35 行高、隐藏作答留白）
- **兜底**：降到下限仍放不下 → 抛出 `LayoutError`，报错信息带题号，**不写出任何 PDF**，绝不输出被截断的成品
- 页面几何常量（`PAGE_MARGIN_MM` 等）与 CSS `@page margin` 同源，改边距必须同步改常量

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

### Q: 报错「请求超时」/ 连接超时（WSL → Windows 宿主机）？

脚本会自动探测宿主机地址并在启动时 fail-fast。若仍失败，按下面顺序排查：

- 确认 `SIYUAN_HOST_MODE` 为 `auto`（默认值），脚本会自动回退到 WSL 默认网关
- 思源「网络伺服」监听地址建议由 `127.0.0.1` 改为 `0.0.0.0`（改完需重启思源 Kernel 才生效）
- Windows 防火墙需放行入站 `6806/TCP`，并使用 `-Profile Any`（WSL 网卡常被判为 Public）：

  ```powershell
  New-NetFirewallRule -DisplayName "SiYuan 6806 (WSL)" -Direction Inbound `
    -Protocol TCP -LocalPort 6806 -Action Allow -Profile Any
  ```

- 休眠/唤醒后 WSL NAT 端点可能失效：Windows 侧执行 `wsl --shutdown` 后重开 WSL
- 看错误文案区分层次：「无法建立到 ... 的 TCP 连接」= 网络层问题（地址/防火墙）；
  「读取超时」= 连接已建立但思源响应慢（多为正在建索引或文档过大），可调大 `READ_TIMEOUT`
- 不要用 `ping` 判断连通性：WSL2 下 ping 不通宿主机属正常现象，请使用 TCP 探测

### Q: 图片无法显示 / 卡片正文为空？

- 优先确认 `SIYUAN_DATA_PATH` 是否正确指向思源 data 目录（新版思源资源统一位于 `{data}/assets/`）
- 若本机无法访问该目录也无需担心：v2.4 起会自动改用思源 API 拉取图片并以内嵌 `data:` URI 渲染
- 若仍为空，可设置环境变量 `DEBUG_KRAMDOWN=1` 运行一次，把生成的 `debug_kramdown.txt` 中的真实 Kramdown 提供出来排查

### Q: PDF 生成失败或中文显示方块？

- 确保已安装中文字体（见上方「Linux 字体渲染」章节）
- 检查 WeasyPrint 安装是否正确
- 查看终端输出的错误信息

### Q: TARGET_FOLDERS 找不到匹配的目录？

- 程序会打印「当前可用的选题范围」（笔记本名称 + 一级文档标题），并给出最接近的编号/名称
- `TARGET_FOLDERS` 按「笔记本名称 / 一级文档标题的子串」匹配，不会自动顺延编号
- 多项目标中只要有一项未匹配，未匹配项会被跳过并提示，其余已匹配项继续扫描

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

## 🔧 变更记录

| 版本 | 日期 | 变更 |
|------|------|------|
| v2.9 | 2026-10 | 卷面序号改为排版后按视觉顺序连续重排；双栏行等高对齐、外沿与通栏卡齐平；修复 body 默认外边距导致的内容区缩水 |
| v2.8 | 2026-10 | 半栏判定改为「截图自然宽度占页宽比例」（≤60% 半栏 / >65% 通栏），顺序让位于拼版利用率，图片只缩不放 |
| v2.7 | 2026-10 | 混合双栏重做为「顺序优先」+ 分页完整性保证：删除半栏题按高度重排，两遍式锚点测量确保每题完整落在单页内 |
| v2.6 | 2026-10 | 单文件（1844 行）模块化重构为扁平 5 模块架构，依赖单向、行为 100% 保持不变 |
| v2.5 | 2026-09 | 根治 WSL→Windows 宿主机「连接超时」：运行时地址解析、TCP 预检、超时拆分与建连重试、异常分类修正、fail-fast |
| v2.4 | 2026-09 | 修复题目/答案正文为空的 Bug：新增图片 API 兜底（data: URI），强化 Kramdown/IAL 解析 |
| v2.3 | 2026-06 | 升级错题抽取逻辑（排序、优先级、页数上限、近期排除） |
| v2.2 | 2026-06 | 修复日期过滤：从 Markdown 表格提取真实练习日期 |
| v2.1 | 2026-06 | 新增多项增强功能 |

### v2.9 变更详情

- **卷面序号最后生成**：新增 `renderer._layout_order()` 统一给出最终视觉顺序（半栏按估算高度降序配对，通栏随后），练习卷与答案卷都**在顺序确定后**从 1 连续编号；标题里的溯源信息（`DS0048: 16`）不变，两卷题号一一对应
- **双栏等高**：`LayoutState.pads` 记录每张半栏卡需补的留白，`fit_document()` 每轮测量同排两卡正文高度差并注入上下等量 `.q-pad`，使圆角与边框底边严格平齐；较矮题的图片因此落在剩余留白中间
- **哨兵口径调整**：半栏题的结束哨兵移到「正文之后、底部留白之前」，测到的是正文下沿，不受 pad 与 `min-height` 影响；`LayoutState.end_keys` / `bottom_slop` 记录每题判定用哨兵与固定偏差，通栏题哨兵仍在包装块之后
- **栅格严丝合缝**：双栏行由 `<table>` 改为 inline-block 行（`width: calc(50% - 3mm)` + 6mm 间距 + `:last-child` 去尾距），行首/行尾与通栏卡左右边沿严格一致
- **修复内容区缩水**：`_CSS_BASE` 的 `body` 增加 `margin: 0`。此前浏览器默认 8px 外边距让实际内容区只有 185.8mm（页面内缩 2.1mm），所有对齐比较都偏 2.1mm
- **验证**（真实 6 题）：两卷序号均为 1..6 连续且映射一致；行内卡底 132.6/132.6mm 与 262.4/262.4mm 平齐；卡片左右边沿 10.0/200.0mm 与通栏卡完全重合；页边距溢出墨点 0

### v2.8 变更详情

- **半栏判定改为宽度口径**：`is_compact_item()` 不再看图片宽高比，而是按「图片自然宽度 / A4 内容区宽度」判决 —— ≤60% 一律半栏（不再因文本行数改判）、60~65% 仍半栏（宁可在半栏内微缩）、>65% 才通栏；无图题仍按 ≤4 行判半栏
- **新增截图倍率常量**：`SCREENSHOT_DPI = 192`（2x HiDPI），自然宽度 = 像素宽 × 96/192；实测截图正文墨迹高 ≈23px 对应 16px 字号 @2x，故取 192
- **顺序让位于拼版利用率**：半栏题集中并按 `estimate_compact_height()` 降序两两配对（高度接近者同行），通栏题排在所有双栏行之后；`estimate_compact_height()` 以半栏可用宽度重写，新增按路径缓存的 `get_image_size()`
- **图片只缩不放**：`<img>` 内联 `max-width: min(容器上限, 自然宽度)` + `height: auto`，2x 截图不再被放大成巨型字号；通栏题图片按自然宽度居中，`.q-image`/`.a-image img` 同步加 `height: auto`
- **页面几何单一来源**：`A4_WIDTH_MM` / `A4_HEIGHT_MM` / `PAGE_MARGIN_MM` / `GRID_GUTTER_MM` 移到 `parser.py`，`renderer.py` 导入复用，CSS `@page margin` 与 `border-spacing` 由常量插值
- **真实数据验证**（DS0048 分数 6 题）：5 道半栏（自然宽度 48%~56%）+ 1 道通栏（100%）；练习卷 2 页，双栏行内两图分别落在 x=22mm / x=121mm；通栏题图片自然宽度 129mm 时居中于 x=40mm；全部图片渲染宽度 ≤ 自然宽度（无放大）；页边距内溢出墨点 0

### v2.7 变更详情

- **删除跨题重排**：`main.py` 不再把紧凑题按 `estimate_compact_height` 估算高度降序提前排列，题目顺序完全保持选题引擎输出；`estimate_compact_height()` 随之删除
- **紧凑判定确定化**：`is_compact_item()` 改为「文本 ≤ 4 行、任一图片宽高比 < 1.7」；Pillow 缺失或图片尺寸取不到时一律判为通栏（保守优先），新增常量 `COMPACT_MAX_TEXT_LINES` / `COMPACT_MAX_IMAGE_RATIO`
- **单个紧凑题保持半栏**：不再被提升为通栏，同行右侧留空
- **分片约束修正**：`.compact-grid` 允许跨页（`break-inside: auto`），分页单元下移到 `tr` / `td` / `.full-width` / `.question-card`，多行双栏表可在行间分页而单题不被拆开
- **图片高度可注入**：`<img>` 与 CSS 的 `max-height: 80vh` 改为 `max-height: var(--img-max-h, 80vh)`，题卡内联 `--img-max-h` 供适配循环逐轮收缩
- **新增分页完整性适配**：`renderer.fit_document()` 用 `#q-N` / `#q-N-end` 锚点测量每题起止页码与 y 坐标，跨页或越出 `PAGE_CONTENT_BOTTOM_PX` 时依次「缩图（×0.75，下限 60px）→ 紧凑字号（`.dense`）」，仍无解则抛 `LayoutError`
- **绝不输出截断 PDF**：`compile_with_layout()` 取代 `compile_html_to_pdf()`，排版失败直接返回错误、不写文件；成功信息附带页码数
- **页面几何同源**：新增 `A4_HEIGHT_MM` / `PAGE_MARGIN_MM` / `PAGE_CONTENT_BOTTOM_PX` 等常量，`@page margin` 由常量插值生成
- **答案卷同受约束**：答案卡带 `id="a-N"` 与结束哨兵，与练习卷共享同一套测量与收缩逻辑
- **验证**：离线合成测试（宽图/窄图/超高图/纯文字混排）确认双栏同行、通栏独占、无跨页、无越底；无解用例确认返回 `❌ 排版失败` 且不落盘

### v2.6 变更详情

- **扁平 5 模块架构**：将原 1844 行单文件 `siyuan_client.py` 平滑拆分为 `main.py` / `renderer.py` / `parser.py` / `siyuan_api.py` / `config.py`
- **严格单向依赖**：`main → renderer → parser → siyuan_api → config`，全程无循环导入
- **消除导入期副作用**：`config.py` 引入 `Config` 数据类与单例，配置由 `main()` 显式加载并注入
- **数据契约保持**：`QuestionDoc` / `QuestionItem` 采用 `NamedTuple`，完整保留元组下标契约（`item[4]`、7 元解包等旧写法继续可用）
- **渲染层共享 CSS**：练习卷与答案卷共用的字体 / `*` / `body` / `.header` 样式抽为 `renderer._CSS_BASE`，仅去重、不改变声明覆盖顺序与渲染效果
- **向后兼容**：`siyuan_client.py` 收敛为 3 行转发垫片（`from main import main`），旧调用点与旧习惯零感知
- **回归验证**：端到端 88 扫描 / 56 跳过 / 28 选题（半栏 6 · 通栏 22）/ ~9 页 / 两份 PDF，归一化日志与基线 **0 行差异**

### v2.5 变更详情

- **根治 WSL → Windows 宿主机「连接超时」**：WSL2 NAT 模式下宿主机地址等于 WSL 默认网关，该网段会随 Windows 重启、休眠恢复、Wi-Fi/VPN 切换而漂移；此前地址硬编码在 `config.json`，失效时表现为 `sock.connect` 静默超时（`ConnectTimeout`，而非 `ConnectionRefused`）
- **新增运行时地址解析**：新增 `SIYUAN_HOST_MODE`（`auto` / `fixed`），`auto` 模式按「配置地址 → 127.0.0.1 → localhost → 默认网关」顺序探测，结果缓存复用
- **新增默认网关探测**：`_detect_host_gateway()` 直接解析 `/proc/net/route`（纯文件读取、无子进程开销），并以 `ip route show default` 作为兜底
- **新增 1.5s TCP 预检**：`_tcp_probe()` 以极低成本快速筛掉失效候选，避免每个候选都白等完整的建连超时
- **超时拆分**：`timeout=10` 改为 `(CONNECT_TIMEOUT, READ_TIMEOUT)` 元组（默认 `3` / `30`，均可配置）
- **建连重试与退避**：建连失败按 `RETRY_BACKOFF × 2^n` 指数退避重试（默认 2 次）
- **重试安全性边界**：仅重试建连阶段失败（TCP 尚未建立、一个字节都未发出，故对 POST 也安全）；`ReadTimeout` 不重试，避免重放可能已送达并执行的请求
- **修正误导性异常分类**：`ConnectTimeout` 同时是 `Timeout` 与 `ConnectionError` 的子类，旧实现先捕获 `Timeout`，导致一律显示「请求超时」而掩盖真实原因；现按 `ConnectTimeout` → `ReadTimeout` → `ConnectionError` 顺序精确区分，并在文案中给出 `host:port` 与处置建议
- **启动 fail-fast**：`main()` 开头即做连通性预检，失败时打印排查清单并退出，不再逐个文档重复等待超时；运行中连续 3 次建连失败会打印一次汇总提示
- **修复图片静默吞错**：`get_asset_bytes()` 不再 `except Exception: continue`；网络层失败会告警且不写入负缓存（保留后续重试机会），仅确定性的「资源不存在」才做负缓存，并在收尾统计中汇总失败数量
- **Session 复用**：`call_api()` 改用共享 `requests.Session` + `HTTPAdapter` 连接池，避免每个请求重新三次握手
- **新增配置项**：`SIYUAN_HOST_MODE`、`CONNECT_TIMEOUT`、`READ_TIMEOUT`、`CONNECT_RETRIES`、`RETRY_BACKOFF`（均有默认值，旧配置文件可直接沿用）

### v2.4 变更详情

- **修复“题目/答案卡片正文为空”**：经排查，DS0050 中每道题的 `# 题目` 区块为**纯图片**（正文即截图，`text` 本就为空），真正的空卡片原因是 `SIYUAN_DATA_PATH`（`/path/to/siyuan/workspace/data`）在本机不存在，导致 `map_image_path()` 全部返回 `None`、图片未被渲染
- **新增图片 API 兜底**：当本地路径映射失败时，自动调用思源 `POST /api/file/getFile`（资源路径 `/data/assets/{filename}`）拉取图片字节，并以内嵌 `data:image/...;base64,...` 形式写入 HTML，彻底摆脱对本地 data 目录的依赖
- **高度/宽高比估算同步增强**：新增 `_get_pil_image()`，`estimate_compact_height()` 与 `is_compact_item()` 改为“本地优先、API 兜底”，在无本地 data 目录时也能按真实图片尺寸排版
- **强化 Kramdown 解析**：新增 `_strip_ial()` / `_heading_text()` / `_is_question_heading()` / `_is_answer_heading()` / `_clean_md_line()` / `_extract_block()`，兼容思源新版在标题行尾附加 `{: id="..." updated="..."}`、独立成行的 IAL 块、行内 `>` 引用内的 IAL 行、以及孤立的 `>` 行
- **解析更稳健**：`parse_question()` / `parse_answer()` 改为基于标题语义匹配（自动排除“扩展题目XX”），图片路径仍在 IAL 清洗之前提取，并轻微放宽图片正则以兼容 `<>` 包裹的路径
- **调试开关**：新增环境变量 `DEBUG_KRAMDOWN=1`，启用后会把首个文档的原始 Kramdown 写入 `debug_kramdown.txt`（默认不写文件）

### v2.3 变更详情

- **积分升序排序**：基础/易错/困难各池均按积分升序排序，积分越低（错误次数越多）越优先入选；全新题目（无积分表）视为最优先
- **入选顺序调整**：先基础 → 再易错 → 最后困难补充，保证基础薄弱环节优先巩固
- **新增 `MAX_PAGES` 配置**：默认 10 页，防止练习卷过长；每添加一题前检查上限
- **新增 `EXCLUDE_RECENT_DAYS` 配置**：默认 2 天，从 Markdown 表格「录入与练习日期」列提取真实练习日期，跳过近期刚练习过的题目
- **困难题标注**：困难题在练习卷和答案卷标题前自动添加 ⭐ 符号，便于识别
- **半栏题高度配对**：`select_questions` 返回后，半栏题按 `estimate_compact_height` 估算高度降序排列，使高度相近的题目自动配对到同一排，减少页面底部留白

### v2.2 变更详情

- **修复日期过滤不可靠问题**：删除了通过 SQL 查询 `blocks` 表 `created`/`updated` 字段来判定"最近练习时间"的逻辑。思源笔记的 `updated` 字段会因后台建索引、同步、微小排版修改等操作频繁更新，导致几乎所有题目被误判为"刚练习"而跳过
- **新增 `parse_latest_date(md_source)` 函数**：改为从文档正文 Markdown 表格的"录入与练习日期"列提取真实练习日期，只认可用户亲手记录在表格中的数据
- **支持的日期格式**：`2026年4月12日`、`2026-04-12`、`2026/4/12` 等常见格式均可解析
- **无日期表格不过滤**：如果文档中没有录入与练习日期表格、或表格中所有日期列为空，则不跳过该题，正常进入选题池

### v2.1 变更详情

- **去除 Kramdown 转义符**：在 `parse_question` 和 `parse_answer` 中使用 `re.sub(r'\\(.)', r'\1', text)` 全局清除反斜杠转义（`\_`、`\=`、`\*` 等），避免题目中残留多余反斜杠
- **预留手写空间**：`_html_build_question_body` 新增 `is_compact` 参数，半栏题尾部预留 50px、通栏题预留 90px 空白 div，方便学生作答
- **优化双栏排版**：移除 `.grid-row` 的 `break-inside: avoid` 规则，改用 `align-items: start` 避免 Grid 等高拉伸，减少底部大面积留白
- **放宽紧凑型阈值**：`is_compact_item` 中的图片宽高比阈值从 1.3 提升至 1.7，使宽高比在 1.6 左右的图片正常使用半栏排版
- **修复积分表解析**：重构 `parse_score_table`，改用 `parts[-2]` 定位积分列，跳过空行继续向上追溯，正则支持负数提取（如 `-1`）

---

## 📋 项目结构

```text
note/
├── main.py                # 顶层编排：扫描 → 过滤 → 选题 → 出卷 → 统计
├── renderer.py            # 渲染层：CSS + HTML 拼装 + 分页完整性适配 + WeasyPrint PDF 编译
├── parser.py              # 解析与规则：数据模型 + Markdown 解析 + 标题分类 + 版式估算 + 选题引擎
├── siyuan_api.py          # 网络与仓储：地址探测 + HTTP 传输 + 思源领域端点 + 图片资源
├── config.py              # 配置：Config 数据类 + config.json 加载 + 单例
├── siyuan_client.py       # 兼容转发垫片（3 行，转发到 main.py）
├── requirements.txt       # 显式依赖
├── config.json.example    # 示例配置文件（不含真实数据）
├── 数学练习模板04.md       # 错题文档模板（参考用）
├── baseline/BASELINE.md   # 重构回归基线签名
├── AGENTS.md              # 排版/分页/图片缩放开发规范与踩坑记录（改排版前必读）
├── README.md              # 本文件
└── .gitignore             # Git 忽略规则
```

> `config.json`（含敏感 Token）与实际产物 PDF / 日志已加入 `.gitignore`，不入库。

### 依赖方向（严格单向，无循环导入）

```text
main → renderer → parser → siyuan_api → config
```

### 各模块职责

| 模块 | 职责 | 依赖 |
|------|------|------|
| [`main.py`](main.py:1) | 顶层编排；[`collect_documents()`](main.py:61) 负责连通性预检、目录扫描与日期过滤 | 全部 |
| [`renderer.py`](renderer.py:1) | 一切产出物：HTML 工具、练习卷/答案卷 HTML 生成、PDF 编译（含共享 CSS 常量） | `parser`、`siyuan_api` |
| [`parser.py`](parser.py:1) | 纯数据与规则：`QuestionDoc`/`QuestionItem`、Markdown 解析、标题分类、版式估算、选题引擎 | `config`、`siyuan_api` |
| [`siyuan_api.py`](siyuan_api.py:1) | 一切与思源通信：地址解析、HTTP 传输、仓储端点、图片字节获取与 base64 兜底 | `config` |
| [`config.py`](config.py:1) | 配置定义、加载、校验与单例（导入期零副作用） | 无 |
| [`siyuan_client.py`](siyuan_client.py:1) | 兼容转发垫片，保证旧入口/旧调用点仍可用 | `main` |

---

## 🏗️ 历史重构记录

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

## ⚖️ 许可

本项目仅供个人学习使用，请遵循思源笔记相关许可协议。