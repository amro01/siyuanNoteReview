# AGENTS.md —— 错题拼卷机排版开发规范

本文件是本仓库的**排版/分页/图片缩放**约定与踩坑记录。改动 `renderer.py` / `parser.py` 的排版相关代码前必须读完；
其中标注 **不要拆** 的决策，拆除后会引起已修复的回归。

技术栈：`main.py`（编排）→ `renderer.py`（HTML + WeasyPrint PDF）→ `parser.py`（解析/判定/估算）→ `siyuan_api.py` → `config.py`。
依赖方向严格单向，**parser 不得导入 renderer**。

---

## 1. 业务原则：纸张利用率 > 原始题序

- 目标读者是每天刷错题的小学生，**卷面紧凑、少印纸** 优先于“题号与笔记顺序一致”。
- 允许且要求重排：半栏题聚在一起、按「估算高度降序」两两配对成行；通栏题保持彼此相对顺序，排在所有双栏行之后。
  实现见 `renderer._layout_order()`。
- 配对只影响**版面位置**，不影响题目内容；来源文档 ID 始终保留在标题里（见 §3）。
- 禁止为了让某题留在原位而放弃配对：相同高度的两题同行能省掉近半页空白。

取舍代价（已知、接受）：
- 卷面顺序与笔记中的顺序不再一致；
- 若半栏题为奇数个，最后一行右侧留空（不把剩余单题提成通栏，保持分类稳定）。

---

## 2. 视觉原则：图片只缩不放，双栏同行等高

### 2.1 严禁 `width: 100%` 暴力撑满

- 图片缩放走 `<img style="max-width: min(容器上限, 自然宽度); height: auto; max-height: var(--img-max-h, 80vh)">`。
  - `max-width` 用 `min(容器上限, 自然宽度)`：只缩不放，小图保持自然尺寸并水平居中（`.q-image` 已 `text-align:center`）。
  - **自然宽度** = 像素宽 × 96 / `SCREENSHOT_DPI`，`SCREENSHOT_DPI = 192`（见 §2.3）。
  - `height: auto` 保证等比；`max-height` 用于分页适配时逐轮收缩（§4.2）。
- 实测（WeasyPrint 盒树）：同一批 801×996 截图，旧写法会被拉到卡片内容宽度（≈183mm，等于放大近 2 倍，字变巨型）；
  现写法按自然宽度 400px = 106mm 渲染，仅在容器更窄时（双栏 92mm 列）进一步缩到 ≈86mm。

### 2.2 双栏同行等高

- 同一行的两张半栏卡**必须**等高：圆角背景/边框底边严格平齐；较矮题的图片落在剩余留白中间。
- 实现方式（**不是** CSS 拉伸，理由见 §4.1）：`fit_document()` 每轮测量同排两卡正文高度差，把差值一半各补到
  标题之后与正文之后，写入 `LayoutState.pads`，由 `_html_build_question_body(pad_px=...)` 渲染成 `.q-pad` 分隔块。
- 判定与调试：`state.pads[key]` 即该题单侧补白（px）；两侧永远等量，因此补白总量 = 2×pad。

### 2.3 半栏 / 通栏判定（宽度优先）

- 阈值常量为 `COMPACT_MAX_WIDTH_RATIO = 0.60`、`FULL_WIDTH_MIN_RATIO = 0.65`（占 A4 内容区宽度 190mm）。
  - ≤60% → 半栏，**一律**不因文本行数改判；
  - 60%~65% → 仍半栏（宁可在半栏内微缩，也不放通栏）；
  - >65% → 通栏；
  - 纯文字题（无图）另按 `COMPACT_MAX_TEXT_LINES = 4` 行判定；
  - 图片尺寸取不到 / Pillow 缺失 → 通栏（保守）。
- `SCREENSHOT_DPI = 192` 的依据：库内 288 张截图无 DPI 元数据，若按 96dpi 直算全部落在 91%~300% 页宽，阈值形同虚设；
  实测截图正文墨迹高 ≈23px（16px 字号 @2x），故按 2x 采集折算。换算后分布为 ≤60% 102 张 / 60–65% 19 张 / >65% 167 张。
- 更换题库（不同缩放比例的截图）时，`SCREENSHOT_DPI` 需要按同样的“墨迹高度”实验重新标定。

### 2.4 栅格对齐

- 双栏行用 inline-block 精确宽度：`width: calc(50% - 3mm)` + `margin-right: 6mm` + `:last-child { margin-right: 0 }`。
- 行首/行尾边沿必须与通栏卡（`.full-width`）完全重合：实测左 10.0mm、右 200.0mm。
- `_CSS_BASE` 的 `body` 必须保留 `margin: 0`：浏览器默认 8px 外边距会让实际内容区变成 185.8mm（页面内缩 2.1mm），
  所有对齐比较都会整体偏 2.1mm。

---

## 3. 编号机制：排版定稿后按视觉顺序统一编排

- 顺序链：`_layout_order()` 产出最终视觉顺序 → 练习卷 `enumerate(..., 1)` 编号 → 答案卷用**同一个**顺序与编号。
- 卷面序号必须是 1..N 连续、无跳号；序号即视觉位置（从上到下、从左到右）。
- 溯源信息（`DS0048: 16` 等）来自文档标题 `format_question_title(item[1])`，与序号解耦，**任何重排都不得改动**。
- 锚点 id `q-N` / `a-N` 中的 N 就是**卷面序号**（不是选题序号），`LayoutState` 的 `image_caps` / `pads` / `end_keys` 全部以它为键。
- 反面教材：曾用 `main.py` 先给题编号、再排序，卷面出现 “4 旁边是 2，后面是 5、3”。
  **禁止**在任何重排之前把序号写进标题/id/状态字典。

---

## 4. 已踩过的坑（按代价排序，勿重犯）

### 4.1 CSS 拉伸等高全部失效，只能靠测量注浆

| 尝试 | 结果（实测） |
|---|---|
| `td > .question-card { height: 100% }` | 不拉伸。百分比高度对 auto 高度的表格单元格退化为 auto，两卡 201.5mm vs 84.3mm |
| `td` 本身当卡片 + `border-spacing` 做间距 | 卡片背景能等高，但内容只能整体 `vertical-align: middle`（标题跟着下移），且外沿比内容区缩进 6mm |
| `display:flex; align-items:stretch` 做行 | **行高被撑到 336.9mm**（内容仅约 200mm）：WeasyPrint 69 的 flex 跨轴尺寸按图片**内在**高度（318mm）计算，忽略了 `max-width` 收缩 |
| 表格 + 负外边距把卡片外扩对齐 | 负 margin 未按预期压缩宽度，右边缘跑到 ≈215–218mm（越过 200mm 内容区右沿） |
| `.card + .card { margin-left: 6mm }` | 该相邻兄弟选择器在此版本不生效，两卡贴在一起；改成 `margin-right` + `:last-child` 才生效 |

结论：**双栏行用 inline-block，等高用“测量 + 注入上下等量留白”**。这套组合是唯一在 WeasyPrint 69 上实测通过的方案。

### 4.2 分页完整性只能靠锚点测量，不能靠 CSS 承诺

- `break-inside: avoid` 对“比整页还高”的内容无效：WeasyPrint 会照拆，`overflow: visible` 也不会裁剪。
- 现方案：每张卡带 `id` 与结束哨兵 `id-end`，渲染后读 `page.anchors`（值为 `(x1,y1,x2,y2)`，取 `y1`）：
  - 起始页 ≠ 结束页 → 被拆到多页；
  - 结束 y + 偏移 > `PAGE_CONTENT_BOTTOM_PX` → 越过内容区下沿；
  - 判不合格时：图片上限 ×0.75（下限 60px）→ 仍不行则 `.dense`（8.5pt/1.35、隐藏作答留白）→ 再无解抛 `LayoutError`，
    **不写出任何 PDF**。绝不输出被截断的成品。
- 循环重建 HTML 最多 `MAX_FIT_ITERATIONS + 1` 轮；因此 `parser.get_image_size()` 必须保留**按路径缓存**，
  否则每轮都重新解码同一批 PNG。
- `.compact-grid` 时代的教训：把 `break-inside: avoid` 放在整张表上，多行双栏表会被当成不可分割整体从而溢页；
  分片单元必须是**行**。

### 4.3 哨兵位置决定测量口径（最容易写错）

- 半栏题：哨兵必须在**正文之后、底部留白之前**（`_html_build_question_body(end_html=...)`）。
  - 放在卡片之后不行：inline-block 行里插入块级哨兵会强制换行；
  - 放在卡片内最末不行：会包含 `.q-pad` 与 `min-height`，导致“用测量值减 pad 反推正文高度”在 `min-height` 生效时发散（pad 越算越大）。
- 通栏题 / 答案卷：哨兵放在卡片（或包装块）**之后**，可精确覆盖边框与 margin。
- 由此引入 `LayoutState.end_keys`（每题用哪个哨兵）与 `bottom_slop`（哨兵到卡片下沿的固定偏差），
  `_layout_problems()` 判定时用 `bottom_slop + pads` 补偿；改卡片 padding/border 必须同步 `CARD_BOTTOM_SLOP_PX`。

### 4.4 输出顺序的“缓冲 vs 直写”陷阱

- 半栏卡先缓冲、通栏卡直写 `body_parts`，若不在写通栏卡之前 `flush_row()`，通栏卡会跑到所有双栏行**前面**
  （曾出现：页首是通栏题，双栏行反而在后面，锚点 y 顺序与题号顺序不一致）。
- 自检断言：`html.index('class="full-width"')` 必须大于最后一个半栏卡 `id="q-N"` 的位置（即通栏题在双栏行之后）。

### 4.5 几何常量单一来源

- `A4_WIDTH_MM / A4_HEIGHT_MM / PAGE_MARGIN_MM / PAGE_CONTENT_WIDTH_MM / GRID_GUTTER_MM`
  定义在 `parser.py`，`renderer.py` 导入；CSS 的 `@page margin`、双栏宽度、间距都由常量插值。
- 改页边距必须只改 `PAGE_MARGIN_MM`；`PAGE_CONTENT_BOTTOM_PX` 由它推导，不要另写魔法数。

### 4.6 图片取值与降级

- 图片路径优先本地 `file://`，失败回退思源 API base64（`_html_image_tag`）。
- 自然宽度取不到（Pillow 缺失 / 资产拉取失败）时：`max-width` 回退为容器上限，判定回退为通栏。
- `max-height: var(--img-max-h, 80vh)` 的自定义属性来自题卡内联样式；**WeasyPrint 69 已验证自定义属性继承**，
  更换渲染引擎前必须重验，否则图片高度收缩失效、分页适配会误判。

---

## 5. 为什么不能轻易拆除这些决策（回归清单）

| 若拆除 | 立刻出现的回归（均为实测过的现象） |
|---|---|
| 恢复“严格原始题序，不配对” | 半栏题被通栏题隔断 → 大量单卡行（一行只放一张），页数上升且页底留白变大 |
| 把序号提前到排序前 | 卷面出现跳号/错位（4 旁边是 2，之后 5、3），答案卷与练习卷对不上 |
| 用 `width: 100%` 或去掉 `natural` 上限渲染图片 | 2x 截图字号被放大成巨型字；小图被拉伸模糊 |
| 改回 flex 行做等高 | 行高膨胀到 336.9mm（内容 200mm），每行独占一页且越界 |
| 用 `height: 100%` 代替 pad 注浆 | 卡片底边参差（201.5mm vs 84.3mm），等高与图片居中同时失效 |
| 去掉 `body { margin: 0 }` | 内容区变 185.8mm，双栏与通栏边沿整体内缩 2.1mm，对齐断言全部失败 |
| 把哨兵移回卡片末尾 | pad 计算在 `min-height` 生效时发散，最终 `LayoutError` 或卡片不等高 |
| 去掉 `get_image_size()` 缓存 | 适配最多 11 轮，每轮重建 HTML 都会重新解码同一批 PNG（本库资产 288 张），出卷无谓变慢 |
| 去掉 `LayoutError` 兜底（改为允许拆页） | 题目/图片会在页边界被切断，违背“绝不输出被截断 PDF”的目标 |
| 把 `break-inside: avoid` 放回整张双栏表 | 多行表被当作整体，溢出页面后仍被切开 |

---

## 6. 改动后的验证配方（必须真跑，不靠推断）

```bash
# 0) 环境
/home/deep/miniconda3/envs/note/bin/python -V      # 3.10 / weasyprint 69.0 / Pillow 12.2

# 1) 语法与死符号
python -m py_compile main.py parser.py renderer.py
grep -Rn "compile_html_to_pdf" --include='*.py' .   # 期望无输出（已由 compile_with_layout 取代）
# 注意：estimate_compact_height 目前仍存在，但只用于“配对排序”，不是版面真值

# 2) 真实出卷（需思源可连；不可连时用离线合成数据）
python main.py

# 3) 版面几何白盒检查（等比、等高、对齐、页码）
#    - 卡片/图片盒：遍历 doc.pages[i]._page_box，读 position_x/border_width/position_y/border_height
#    - 锚点：renderer._collect_anchor_points(doc) → {id: (page, y)}
#    - 断言：同排卡底相等(<0.2mm)、行首尾 10.0/200.0mm、每题 start/end 同页且 end+slop ≤ 1084.7px

# 4) 成品 PDF 复核（不依赖渲染器内部）
#    pypdfium2 读页面文本序号、图片对象尺寸、路径对象边界；渲染成位图检查页边距内是否有墨点溢出
```

离线合成数据的必备覆盖：宽图（通栏）、窄图（半栏）、超高图（触发图片上限收缩）、纯文字题、60~65% 边界图（仍半栏）、
65% 以上边界图（通栏）、奇数个半栏题（末行留空）、无解用例（400 行文字 → `❌ 排版失败` 且不落盘）。

### 测试脚手架本身的坑

- 跨行正则必须带 `re.S`：卡片 HTML 内部含换行，`<tr>(.*?)</tr>`、`<div class="compact-row">(.*?)</div>` 这类写法不加 `re.S` 会匹配不到，
  而“匹配不到”会静默通过 `all(...)` 之类断言（踩过一次：`rows` 为空仍判为通过）；
- 统计每行卡数应按 `<div class="compact-row">` / `<div class="full-width">` 切分，不要用 `.*?</div>`（会停在标题 div）；
- `parse_answer(md)` 在 `answer_md` 为空时根本不会被调用，mock 前要确保传入非空 answer；
- 断言取字典字段用 `.get()`：`state.pads` 只对“被补白”的卡有键，`state.pads[k]` 会 KeyError 误报为测量失败。

---

## 7. 关键代码锚点

- `parser.py`：几何常量、`SCREENSHOT_DPI`、`get_image_size()`（带缓存）、`image_natural_width_px()`、
  `image_width_ratio()`、`is_compact_item()`（宽度优先判定）、`estimate_compact_height()`（**仅用于配对排序**，非版面真值）。
- `renderer.py`：`_CSS_BASE`（含 `body{margin:0}`）、`_html_image_tag()`（只缩不放）、
  `_html_build_question_body(pad_px, end_html)`、`_layout_order()`（唯一顺序来源）、
  `generate_html_practice()`（inline-block 行 / 连续编号 / `.compact-row` 成员写入 `state.rows`）、
  `generate_html_answer()`（复用同一顺序与编号）、`_collect_anchor_points()`、`_layout_problems()`、
  `_update_row_pads()`（等高注浆）、`fit_document()`（收缩循环）、`compile_with_layout()`（唯一出口，失败不落盘）。
