# DcmGet Web 界面精修需求书（Base UI 版）

> 目的：作为 **Codex 执行 UI 重塑的需求与验收清单**。**强视觉重塑**——复用 React + Base UI 的行为与无障碍层，但换掉视觉外观，建立一套有性格、可信的临床控制台身份。
> 日期：2026-07-24
> 目标代码：`frontend/`（源码）→ 构建产物 `dcmget/webui-react/`
> 执行者：Codex（可调用 `lucide-animated` MCP 与 `@aicanvas/mcp`）
> 语言：界面中文为主，技术数据（IP、AE、检查号、计数、速率）继续用等宽字体

---

## 0. 现状与基线（Codex 必读）

当前前端栈已成熟，**不更换框架、不推翻结构**：

- React 19 + TypeScript（严格模式）+ `@base-ui/react` 1.6 + Tailwind 4 + `motion` 12 + `lucide-react` + `zod`。
- 视觉全部在 `frontend/src/styles.css`，用 CSS 变量维护 **light / dark 双主题**。
- 关键交互文件：`src/App.tsx`（外壳、header、连接态、菜单、目录选择、toast）、`src/components/TaskWorkspace.tsx`（任务编辑器 + 运行态）、`src/components/Primitives.tsx`（Button/StatusBadge/Field/Switch/Sheet/ConfirmDialog）、`ProfileRail.tsx`、`SettingsSheet.tsx`、`OperationsSheet.tsx`、`UpdateSheet.tsx`、`ProfileEditor.tsx`、`LogPanel.tsx`。
- 已有良好无障碍基础：`focus-visible` 焦点环、`forced-colors`（Windows 高对比）、`prefers-reduced-motion`、`aria-live`、全量 `aria-label`。**这些是红线，只能加强不能削弱。**
- 两种运行模式：`is-manager`（左侧 ProfileRail + 多 Profile）与 `is-profile`（单实例）。
- 数据热路径：SSE + 轮询高频刷新；≥ 200 条检查号走聚合视图，40,000 条不渲染明细。

### “大胆重塑”在这个产品里的定义（可信优先）

这是医院内使用的**临床影像下载工具**。**用户已选择"大胆·强视觉重塑"**：要一个有记忆点、看一眼就知道是专业医疗软件的**临床控制台（Mission Control）**身份——而不是又一个通用企业蓝。大胆 = **强烈而克制的个性**（航天/医疗控制台的精密感），不是营销页的花哨或玩具感。评判标准：

- 有清晰的视觉主张：主色/氛围/排版有性格，看得出是刻意设计而非默认。
- 层次分明（elevation / 排版层级 / 数据与标签强对比）。
- 动效有目的、短、可关闭；用来解释状态变化，也允许**每屏一个"编排时刻"**制造记忆点。
- 数字稳定、不跳动；实时数据读起来像仪表盘。
- 深色主题是一等公民，有真实空间深度，不是简单反色。
- **可信优先**：再大胆也不能让医院用户觉得"不稳/像玩具"。数据表、日志、进度这些干活的地方保持极度克制；个性放在外壳（boot、header、侧栏、空态、指标区）。

### 非目标（不要做）

- **可以重塑外观，但**不换 Base UI 行为层、不换框架、不引入第二套设计系统整体接管交互与无障碍。
- 不在 40k 明细、日志流、每秒刷新的进度数字上加动画。
- 不为了好看牺牲 `forced-colors` / 键盘可达 / 对比度（AA）。
- 不把任何**未确认授权**的付费组件（见 §4）打进正式发行包。
- 不做与医疗场景无关的玩具化交互（拖拽卡堆、贴纸墙、3D coverflow 等 aicanvas 娱乐组件一律不用）。

---

## 1. 设计原则与硬约束（Guardrails）

给整轮重塑定一个明确、可执行的身份：**Clinical Mission Control（临床控制台）**。要素（Codex 要真正落地，而不是停留在旧的企业蓝上）：

- **主表面偏深、以数据发光**：深色作为默认/主打主题，数据、状态、进度用发光的强调色"点亮"，像控制台屏幕；light 主题作为等价的第二皮肤，不能只是把深色反相。
- **一个有性格的展示字体 + 现代等宽**（见 §2.1），标题与大数字用展示字体建立识别度，正文/中文稳妥回退。
- **主色 + 一个信号强调色**：在克制的冷底色上，用**单一强调色**（青/水绿信号色）承担"活着/进行中/已连接/速度"这些生命体征表达；不要平均分布的多彩，要"暗底 + 少量高饱和信号"。
- **签名细节**：可复用一个贯穿全局的记忆元素（如四角定位角标 corner-markers、细网格底、单像素发光描边、扫描线式进度），在 boot / header / 指标卡 / 空态上重复出现，形成"这套软件"的辨识度。
- **深空间感**：elevation、发光边、极轻噪点/网格营造纵深；但只在外壳，不侵入数据区。

Codex 在每一处改动都必须同时满足：

1. **动效预算**：单次过渡 ≤ 200ms；除“真正在工作”的状态（连接中、预检中、下载中、导出中）外，**禁止无限循环动画**；`prefers-reduced-motion` 下所有非必要动画降级为无位移的即时状态。
2. **不抖动**：所有计数/进度/速率用 `font-variant-numeric: tabular-nums`；进度数字变化不改变元素宽度。
3. **无障碍不回退**：保留并覆盖 `focus-visible`、`forced-colors`、`aria-live`、键盘操作；装饰性图标 `aria-hidden`。
4. **性能**：动画只用 `transform`/`opacity`；高频刷新区域（进度块、指标、日志、结果表）**刷新期间不得触发进入动画、不得丢焦点、不得重排**（延续路线图 P2-6 教训）。
5. **拥有代码**：所有第三方图标/组件源码落到 `frontend/src/`，保持 TS 严格通过；`npm run typecheck`、`npm run test`（vitest）必须绿。
6. **双主题双模式**：每个改动都要在 light/dark × manager/profile × reduced-motion × forced-colors 下自检。

---

## 2. 视觉系统升级（设计令牌）

集中改 `styles.css` 的 `:root` / `:root[data-theme="dark"]`，令牌先行，后面各屏复用。

### 2.1 字体层级

- 引入一款**有性格的展示字体**用于 `h1/h2`、大号指标数字（如 `Space Grotesk` 之外的选择，避免与全网撞脸；候选：`Geist`、`Bricolage Grotesque`、`Hanken Grotesk`），正文与中文继续用 `PingFang SC / Microsoft YaHei UI / Segoe UI Variable`。
- 等宽（IP/AE/检查号/计数/速率）升级为带 tabular 数字的现代等宽（候选 `Geist Mono` / `JetBrains Mono`），并全局开启 `tabular-nums`。
- 展示字体只用于标题与大数字，正文不换，避免中文回退错乱。字体需本地内嵌或走可离线的方式（Windows 桌面壳，**不能依赖联网 CDN**）。

### 2.2 颜色与层次

- **重设主色关系（大胆档要真的换）**：确立"冷暗底色 + 单一高饱和信号强调色"。信号色（青/水绿/电蓝其一）承担"活着/进行中/已连接/速度"等生命体征；蓝可保留为次要动作色，但不再是唯一主角。避免多彩平均分布。
- **深色为 hero 主题**：优先打磨深色，light 主题做成等价第二皮肤（同样有层次与信号色，不是反相）。
- 建立 **elevation 阶梯**：`--surface-0/1/2` + 对应阴影 `--shadow-xs/sm/md/lg`，替换现在略平的单层卡片；深色下用发光边而非硬阴影。
- 精修焦点环（更细、更贴边、双色描边）与 `--ring`，保持可见但不笨重。
- 深色主题增加真实纵深：卡片有极轻的顶部高光边（`inset 0 1px 0 rgb(255 255 255 / 4%)`），背景可加极低强度渐变而非纯色。
- 状态语义色（running/starting/error/success/warning）已存在，抽为统一 token 供徽章、Profile 指示点、连接态、预检项复用。

### 2.3 形状 / 间距 / 纹理

- 收敛圆角为 `--radius-xs/sm/md/lg` 阶梯（当前 8/12 两档偏少）。
- **氛围只加在"非数据"表面**：boot 屏、空状态、ProfileRail 顶部、sheet 头部可用极轻的网格/噪点/径向渐变；卡片正文、表格、日志保持纯净。

---

## 3. 动态图标（lucide-animated）——具体映射

### 3.1 接入方式（给 Codex）

- 站点：`https://lucide-animated.com`，MIT，基于 Lucide + Motion（**`motion` 已在依赖里，无需新增**）。
- 每个图标是**单文件 React 组件**，**hover 触发动画**。两种取用方式：
  - shadcn CLI：`npx shadcn@latest add "https://lucide-animated.com/r/<kebab-name>.json"`（会写入 `components/icons/`，需要项目有 `components.json`）。
  - 或用 `lucide-animated` MCP（`search_icons` / `list_icons` / `get_icon`）取源码，**手动落到 `frontend/src/components/icons/<name>.tsx`**（本项目没有 shadcn 目录约定，推荐这条，避免引入 shadcn 脚手架）。
- 命名：kebab → PascalCase（`refresh-cw` → `RefreshCw`），转发全部 SVG props。
- **统一封装**：新建 `src/components/icons/index.tsx`，包一层 `<AnimatedIcon>`，负责：`prefers-reduced-motion` 时回退为静态 `lucide-react` 图标；装饰性时补 `aria-hidden`；尺寸/描边与现有 `lucide-react`（size 14–20，stroke 一致）对齐，避免混用后粗细跳变。

### 3.2 图标替换表（只在有意义处替换，不全站铺开）

| 位置 / 文件 | 现图标 | 动画触发 | 说明 |
|---|---|---|---|
| 开始下载按钮 `TaskWorkspace` | `Download` | hover/press | 主 CTA，最值得做 |
| 导入 TXT/CSV/XLSX | `Upload` | hover | |
| 浏览目录 / 打开结果目录 | `FolderOpen` | hover | 文件夹开合 |
| 工作台菜单「设置」 | `Settings` | hover | 齿轮转动 |
| 任务控制 暂停/继续/取消/结束 | `Pause`/`Play`/`CircleStop` | hover/press | |
| 预检项 通过/失败 | `Check`/`X` | **状态解析时 draw-in（一次）** | 检查从 pending→ok 时描线动画，别循环 |
| 顶部「重新检查/刷新」 | `RefreshCw` | hover 转一圈 | 注意：**真正 loading 仍用现有 `.spin` 无限旋转**，二者区分 |
| 连接态 pill `App.tsx` | `Wifi`/`WifiOff` | **连接状态切换时播一次** | connecting 仍用 spin |
| Toast 成功 | `CheckCircle2` | 出现时 draw-in（一次） | 配合 toast 入场 |
| 全局错误条 | `AlertCircle` | 出现时轻微 attention（一次，非循环） | 克制 |
| 品牌标 / boot | `Activity` | **仅 boot 屏**做一次脉冲 | 常驻态保持静止 |

规则：hover/focus/状态切换触发，**默认不循环**；唯一允许的持续动画是既有 loading spinner 与真实"进行中"指示。

---

## 4. 组件升级（aicanvas.me）——具体建议

### 4.1 接入与授权（重要）

- Codex 用 `@aicanvas/mcp` 浏览/查看/安装，或 shadcn CLI；技术栈 TypeScript + Motion + Tailwind，与本项目一致。
- **授权红线**：站点分**免费 MIT** 与 **付费/专有（含 Andromeda 设计系统）** 两类。本产品是要**商业签名发布的医疗软件**，路线图强调 SBOM 与第三方许可清单。
  - **只允许直接打包 MIT 组件**；对任何付费/Andromeda 组件，**只借鉴视觉与交互思路、用自己的 Base UI + 令牌重实现**，不整段拷贝、不引入专有依赖，除非用户明确采购并确认授权（见 §9）。
- **定位**：aicanvas 作为**视觉与动效的灵感/来源**；**行为与无障碍继续由 Base UI 承担**（Dialog/Menu/Switch/Collapsible/Progress）。所有引入组件必须**重新套用 dcmget 令牌**，不得带入外部配色/字体。

### 4.2 dcmget 界面 → 候选组件映射

| dcmget 界面 | 参考组件（aicanvas） | 采用方式 |
|---|---|---|
| 运行态四指标（当前检查号/接收文件/实时速度/已用时间） | Stat Tile、Metric Chart | 重实现为精致指标卡，数字 tabular |
| **实时速度** | Trend Chart / Waveform（速度 sparkline） | 新增速度迷你折线/波形，最出彩的"精致"点 |
| 下载总进度 | Progress Bar | 现有 Base UI Progress 基础上，加**活动时轻微 shimmer/条纹**（reduced-motion 关闭） |
| 连接态 pill | Live Session Pill（在线指示） | 借鉴"呼吸点 + 状态"表达，重实现连接态 |
| 空状态（未建 Profile / 未启动） | Empty State | 更有氛围的空态插画/构图 |
| 日志过滤 / 详细开关 | Segmented Control | 替换现有 toggle，观感更整 |
| 任务结果表 | Data Table | 借鉴表头/行样式；**≤200 条才用；大批量保持聚合，不虚拟化即不上** |
| 状态徽章 / 标签 | Badge、Tag | 统一到 StatusBadge 令牌 |
| Gauge / Radar 等 | Gauge、Radar | **默认不用**，医疗工具避免花哨；仅作可选备选记录 |

---

## 5. 逐屏优化清单

按屏给出具体动作，Codex 可逐项落地并勾选。

### 5.1 Boot 屏（`App.tsx` `boot-screen`）
- 加一次性入场：品牌标 `Activity` 脉冲 + 文案 stagger 淡入；背景极轻径向渐变/网格。reduced-motion 直接显示。

### 5.2 App Header（`App.tsx`）
- 连接态 pill：三态（connecting/connected/disconnected）用**呼吸点 + 动态图标**表达，切换时过渡一次；文案不换行不跳。
- 主操作按钮（创建任务/启动/停止实例）：hover 微升起 + 动画图标；主 CTA 用 `--primary`，其余降噪。
- 菜单弹出（Base UI Menu）已有 scale 过渡，微调缓动与阴影到新 elevation。

### 5.3 ProfileRail 侧栏（`ProfileRail.tsx`）
- 运行中 Profile 的状态点做**极轻呼吸**（仅 running；starting 用 spin 点；error 静止红点）。
- 选中项：左侧 accent 竖条 + 卡片抬升；hover/selected 过渡顺滑。
- 顶部品牌区可加氛围底。

### 5.4 Context Strip（PACS / 接收端 / 版本）
- 分隔更精细，label 用 eyebrow 风格，值用 tabular 等宽；异常项按钮用动画 `AlertCircle`。

### 5.5 任务编辑器 Composer（`TaskWorkspace.tsx`）
- 检查号统计（有效/重复/空行/无效）：数字 tabular，变化时数字**淡入替换**（非逐位滚动，避免抖）。
- 预检面板：每个 check 从 pending→ok/fail 时播一次 `Check`/`X` draw-in；整体"可以开始"时 CTA 有一次到位的强调（非常克制）。
- 导入按钮动画图标；拖拽区（若加）保持简单。

### 5.6 运行态 Runtime（`TaskWorkspace.tsx`）
- **进度块**：进度条活动时轻微 shimmer；百分比与 `已处理/总数` tabular、宽度固定。
- **指标区**升级为 Stat Tile 观感；新增**实时速度 sparkline/波形**（近 N 个采样，reduced-motion 显示静态最新值）。
- 任务控制按钮动画图标；结果表（≤200）套 Data Table 样式，大批量保持聚合网格不变。
- PDI 折叠区：状态徽章 + 动作按钮观感统一。

### 5.7 日志面板（`LogPanel.tsx`）
- 「详细/清空/打开目录」工具条用 Segmented Control 观感；ERROR 行保留左红条。**日志列表本身不加逐行动画**（高频、可能很长）。

### 5.8 Sheets / Dialogs（`Primitives.tsx` 等）
- 右侧滑出 sheet 已有 translate 过渡，统一缓动/阴影；confirm/directory/column 弹窗统一到新 elevation 与圆角。
- 目录选择器列表项 hover/选中过渡更顺；`FolderOpen` 动画图标。

### 5.9 Toast / 全局错误
- Toast：入场 + `CheckCircle2` draw-in，自动消失；错误条：一次 attention，不循环，可关闭。

---

## 6. 微交互与动效规范

- **每屏只做一个"编排时刻"**：如 boot 的 stagger、runtime 首次出现的指标依次点亮——比到处撒小动画更高级。
- 列表/卡片进入：`opacity + translateY(≤6px)`，≤180ms，`ease-out`；退出更快。
- 悬停：颜色/边框/阴影过渡 120–160ms；避免大位移。
- 进度/数字：只做淡入替换与条宽过渡，绝不逐帧跳动。
- 全部动效集中定义 motion token（时长/缓动/位移），各处引用，便于统一与在 reduced-motion 下集中降级。

---

## 7. 无障碍与性能红线（复述，必须自检）

- `forced-colors: active` 下所有新组件仍有可见边界与状态区分（不能只靠颜色/阴影）。
- 键盘可完整操作新加的分段控件、指标交互、图标按钮；焦点环可见。
- `aria-live` 区域（导入统计、连接态、toast）语义不变。
- 高频刷新区刷新时：无进入动画、无重排、不丢焦点/滚动/展开态。
- 动画只用 `transform`/`opacity`；长列表与 40k 聚合路径零回归。
- 无联网字体/资源依赖（桌面离线壳）。

---

## 8. 交付与验收

Codex 完成后需满足：

1. `npm run typecheck` 与 `npm run test` 全绿；新增组件补必要测试（沿用 `@testing-library/react` + vitest）。
2. `npm run build` 成功产出，并同步更新 `dcmget/webui-react/` 产物（现构建落点）。
3. 人工过一遍：light/dark × manager/profile × reduced-motion × Windows 高对比，四象限无破面。
4. 40k 检查号仍走聚合、不渲染明细，UI 保持可操作；日志长列表滚动流畅。
5. 引入的每个第三方图标/组件：源码在仓库内、许可为 MIT 或已确认授权、记入第三方许可清单（对接 `THIRD_PARTY_NOTICES.md`）。
6. 无新增联网运行时依赖。

---

## 9. 实施批次（建议顺序，可分次提交）

- **批次 A — 令牌与动效地基**：字体、颜色 elevation、圆角/阴影阶梯、tabular 数字、motion token、`<AnimatedIcon>` 封装与 reduced-motion 降级。（先不换具体组件，先立地基）
- **批次 B — 动态图标**：按 §3.2 表逐个替换，验证 hover/状态触发与降级。
- **批次 C — 关键组件**：进度 shimmer、指标 Stat Tile、**实时速度 sparkline**、连接态 pill、Segmented Control、空状态。（授权按 §4.1）
- **批次 D — 逐屏收尾**：编排时刻、sheet/dialog 统一、结果表与 PDI、四象限自检与验收。

每批次结束跑一次 §8 验收，避免把地基、图标、组件混进一次大提交。

---

## 10. 已确认决策（Codex 按此执行）

已与用户确认，**Codex 直接照此执行**：

1. **重塑幅度：大胆·强视觉重塑（已定）** — 建立"临床控制台（Mission Control）"身份，复用 Base UI 行为层但换掉外观；个性放外壳，数据区保持克制、可信。
2. **aicanvas 付费/Andromeda：只借鉴，用 Base UI 重实现（已定）** — 不直接打包付费/专有组件，不引入专有依赖；只借鉴视觉/交互，用现有 Base UI + 令牌重做。仅 MIT 组件可直接采用并记入许可清单。
3. **实时速度 sparkline：新增，前端自采样画折线（已定）** — 用 runtime 已有的 `speed_bytes_per_second`（含 `speed_bps`/`current_speed` 兜底）即时值，前端保留最近 N 个采样画迷你折线/波形；reduced-motion 下显示静态最新值。
4. **信号强调色：采用（已定）** — 在冷暗底色上用单一高饱和信号色（青/水绿/电蓝其一，Codex 选定后固化为令牌）表达生命体征，克制不铺满。

### 仍建议用户补充（不阻塞，Codex 可先用默认推进）

- **展示字体 + 等宽的具体选型**：是否有品牌指定字体？否则 Codex 从 §2.1 候选里选一款**可本地内嵌、含良好 CJK 回退**的组合并固化。
- **信号色的确切色值**：Codex 先选定并出样，用户可在批次 A 评审时替换为品牌色。
