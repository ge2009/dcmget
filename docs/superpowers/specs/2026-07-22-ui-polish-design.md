# DcmGet Web 工作台 UI 完善方案

日期:2026-07-22 · 状态:已实现 · 范围:纯前端(`dcmget/nicegui_ui.py` 与 `dcmget/webui/`),不改后端与业务逻辑

## 背景

程序实际入口会重定向到 `/workspace/` 下的 NiceGUI 工作台；自定义 webui(`index.html` / `app.css` / `app.js`)作为无 NiceGUI 模式的备用页面。两套页面均已有完整设计令牌与响应式基础，本次保持结构不变，只补主题、反馈和无障碍空白点。

## 目标

1. **暗色模式 + 主题切换** —— 契合暗光阅片室;亮/暗/跟随系统,持久化。
2. **Toast 分类型反馈** —— success/error/warning/info 区分配色与图标,操作成败一眼可辨。
3. **细节打磨** —— 高对比度(forced-colors)兜底、初始加载态、克制的微交互。

## 非目标

- 不改后端、业务流程、任务/Profile 逻辑。
- 不引入前端框架或构建步骤(保持静态资源直出)。
- 不做整体视觉换肤——保留现有 teal 品牌与布局。

## 设计

### 1. 暗色模式

- **令牌化漏点**:CSS 中约 40 处写死颜色(顶栏玻璃 `rgb(255 255 255/94%)`、状态徽章、渐变、按钮文字)替换为语义令牌,新增 `--on-primary`、`--glass`、`--glass-border`、徽章色等,使暗色可统一覆盖。
- **单一暗色块**:两套 CSS 均只写一份 `:root[data-theme="dark"] { … }`,避免媒体查询重复。
- **JS 主题控制器**(`app.js`):
  - 启动读 `localStorage['dcmget-theme']`(`light`/`dark`);未设置则跟随 `matchMedia('(prefers-color-scheme: dark)')` 并监听系统变更。
  - 顶栏切换按钮在亮/暗间切换并写入 `localStorage`,同时更新 `<meta name="theme-color">` 与根元素 `color-scheme`。
  - 首帧主题脚本在页面绘制前设定 `data-theme`,避免暗色下白屏闪烁(FOUC)；NiceGUI 路径使用其已隔离的内联 CSP，备用静态页使用同源外部脚本。
- **切换按钮**:顶栏"连接状态"左侧,☀/☾ 图标按钮,复用现有 sprite 体系(新增 `icon-sun`/`icon-moon`)。
- **暗色配色方向**:深蓝黑底(`--bg:#0e1518`,`--surface:#18242a`)+ 提亮青色主色(`--primary:#37b4c2`),主色按钮改深色文字(`--on-primary:#052227`)保证对比度;success/danger/warning 用提亮版。
- **实际 NiceGUI 页面**:通过 `ui.add_head_html` 在首帧读取同一个本机主题偏好，顶栏按钮使用纯浏览器事件立即切换，避免等待服务端往返；Quasar 组件继续沿用现有行为，不引入第二套状态管理。

### 2. Toast 分类型

- `showToast(msg)` → `showToast(msg, { type })`,`type ∈ {info(默认), success, error, warning}`,向后兼容。
- 新增 `.toast--success/error/warning/info` 令牌化配色 + 左侧图标;`aria` 保持现有 live region。
- 更新语义明确的调用点:失败→`error`,保存/完成→`success`,受信内网提示→`warning`。

### 3. 细节打磨

- `@media (forced-colors: active)`:保留边框、改用系统色,保证 Windows 高对比度可用。
- 初始加载态:连接前显示带品牌标的轻量占位,`app-shell` 揭示后移除,替代白屏一闪。
- 微交互:主色按钮 `:active` 轻微下压、卡片 hover 阴影过渡(尊重现有克制风格)。

## 改动文件

- `dcmget/webui/app.css` —— 令牌化 + 暗色块 + forced-colors + 加载态 + toast 变体 + 微交互。
- `dcmget/webui/app.js` —— 主题控制器 + `showToast` 类型化 + 更新调用点。
- `dcmget/webui/index.html` —— 顶栏切换按钮、`icon-sun`/`icon-moon`、加载占位和外部首帧主题脚本入口。
- `dcmget/webui/theme.js` —— 在严格 CSP 下执行首帧主题初始化，不放宽备用页面的脚本策略。
- `dcmget/nicegui_ui.py` —— 将相同主题令牌、切换入口、高对比度与 reduced-motion 支持应用到实际 `/workspace/` 页面。

## 验证

- 在真实 Chromium 中分别以亮/暗系统偏好渲染实际 NiceGUI 工作台，人工核对文字对比度、顶栏、摘要卡片、按钮、表单、侧栏与加载态。
- 校验暗色下主色按钮文字对比度 ≥ 4.5:1(正文)/3:1(大字)。
- 确认 `prefers-reduced-motion` 与新微交互兼容;`forced-colors` 下无不可见元素。
- 在真实 Chromium 中分别以 1440×900 的浅色与深色首选项渲染 NiceGUI 工作台，确认管理信息、主操作、表单、侧栏、日志与禁用按钮均可读。

## 风险与回滚

- 风险主要在"遗漏某个写死颜色导致暗色下局部发白"。缓解:完整通读两套 CSS 的颜色清单逐项替换，并以真实浏览器覆盖关键组件。
- 纯前端改动,回滚 = 还原三个 webui 文件。
