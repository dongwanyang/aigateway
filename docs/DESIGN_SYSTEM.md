# AI Gateway 控制面板 — UI 设计规范

> 技术栈: React 18 + TypeScript + Vite + TailwindCSS
> 设计基准屏幕: 375px（iPhone SE / 移动端）
> 版本: 1.0
> 生成日期: 2026-06-27

---

## 设计原则

AI Gateway 控制面板是面向运维/DevOps 人员的内部工具。界面设计的首要目标是**信息密度高、可读性强、操作直观**。采用**深色主题为主**的设计语言，符合运维工具的通用审美，同时降低长时间监控的视觉疲劳。

---

## 一、颜色体系

### 品牌色 — 科技蓝

```css
--color-primary:         #3B82F6;   /* 主要操作按钮、选中状态、链接、活跃指示 */
--color-primary-hover:   #2563EB;   /* 悬停状态 */
--color-primary-active:  #1D4ED8;   /* 按下状态 */
--color-primary-light:   #EFF6FF;   /* 主色浅色背景、选中项底色 */
--color-primary-muted:   #BFDBFE;   /* 弱化的主色装饰线条 */
--color-primary-dark:    #1E40AF;   /* 深色文字中的主色变体 */
```

### 功能色 — 语义化

```css
--color-success:  #10B981;   /* 正常运行、成功、配额充足、插件已启用 */
--color-warning:  #F59E0B;   /* 警告、配额达到 80%、降级进行中 */
--color-danger:   #EF4444;   /* 错误、熔断开启、配额超限、危险操作 */
--color-info:     #3B82F6;   /* 信息提示、一般状态 */
--color-neutral:  #6B7280;   /* 中性状态、未知、idle */
```

### 中性色 — 深色主题（默认）

```css
/* 文字 */
--color-text-primary:   #F9FAFB;   /* 主要文字、标题、指标数值 */
--color-text-secondary: #D1D5DB;   /* 次要文字、描述、标签 */
--color-text-tertiary:  #9CA3AF;   /* 辅助文字、时间戳、单位 */
--color-text-quaternary:#6B7280;   /* 占位符、禁用文字、图表坐标轴 */
--color-text-disabled:  #4B5563;   /* 禁用状态文字 */
--color-text-inverse:   #111827;   /* 反色文字，用在主色背景上 */

/* 背景 */
--color-bg-base:        #111827;   /* 页面底色（最深） */
--color-bg-elevated:    #1F2937;   /* 卡片、弹窗底色 */
--color-bg-overlay:     #374151;   /* 悬浮层、popover 背景 */
--color-bg-input:       #374151;   /* 输入框背景 */
--color-bg-table-header:#1F2937;   /* 表头背景 */
--color-bg-table-row-hover:#374151;/* 表格悬停行 */
--color-bg-badge:       #374151;   /* 标签/徽章背景 */

/* 分割线 */
--color-border:         #374151;   /* 普通分割线 */
--color-border-light:   #1F2937;   /* 轻量分割线、边框 */
--color-border-focus:   #3B82F6;   /* 输入框聚焦边框 */
```

### 中性色 — 浅色主题

```css
/* 文字 */
--color-text-primary-light:   #111827;
--color-text-secondary-light: #4B5563;
--color-text-tertiary-light:  #9CA3AF;
--color-text-quaternary-light:#6B7280;
--color-text-disabled-light:  #D1D5DB;

/* 背景 */
--color-bg-base-light:        #F3F4F6;
--color-bg-elevated-light:    #FFFFFF;
--color-bg-overlay-light:     #F9FAFB;
--color-bg-input-light:       #FFFFFF;
--color-bg-table-header-light:#F9FAFB;
--color-bg-table-row-hover-light:#F3F4F6;
--color-bg-badge-light:       #E5E7EB;

/* 分割线 */
--color-border-light:         #E5E7EB;
--color-border-lighter:       #F3F4F6;
--color-border-focus-light:   #3B82F6;
```

### 渐变和装饰色（图表用）

```css
/* 柱状图/饼图/折线图的数据系列颜色（8色，互斥） */
--color-chart-1:  #3B82F6;   /* 主蓝 */
--color-chart-2:  #10B981;   /* 绿 */
--color-chart-3:  #F59E0B;   /* 黄 */
--color-chart-4:  #EF4444;   /* 红 */
--color-chart-5:  #8B5CF6;   /* 紫 */
--color-chart-6:  #EC4899;   /* 粉 */
--color-chart-7:  #06B6D4;   /* 青 */
--color-chart-8:  #F97316;   /* 橙 */

/* 渐变 */
--gradient-primary: linear-gradient(135deg, var(--color-primary) 0%, var(--color-primary-dark) 100%);
--gradient-success: linear-gradient(135deg, #10B981 0%, #059669 100%);
--gradient-danger:  linear-gradient(135deg, #EF4444 0%, #DC2626 100%);
--gradient-card:    linear-gradient(180deg, rgba(255,255,255,0.03) 0%, transparent 100%);
```

### 遮罩和透明度

```css
--opacity-mask:        rgba(0, 0, 0, 0.5);    /* 模态遮罩 */
--opacity-backdrop:    rgba(0, 0, 0, 0.3);    /* 毛玻璃遮罩 */
--opacity-icon-disabled: 0.4;                  /* 禁用图标 */
--opacity-card-border:  0.08;                  /* 卡片边框透明度 */
```

### 颜色使用规则

1. 状态色严格遵循功能色定义，不可自行发明
2. 图表配色固定使用 `--color-chart-*` 系列，最多展示 8 个数据系列
3. 链接和操作按钮一律使用品牌色
4. 深色/浅色主题通过 CSS 变量自动切换，不硬编码背景色
5. 同一屏幕上的功能色种类不超过 3 种（主色 + 最多 2 种状态色）

---

## 二、字体体系

### 字号梯度

| 变量名 | 默认值 (px) | 用途 | 示例场景 |
|---------|------------|------|---------|
| `--font-size-xs` | 11px | 极小文本 | trace_id 片段、时间戳、单位标签 |
| `--font-size-sm` | 12px | 小文本 | 描述文字、辅助说明、图例 |
| `--font-size-md` | 14px | 正文 | 表格文字、表单标签、按钮文字 |
| `--font-size-base` | 16px | 标准正文 | 段落文本、输入框占位符 |
| `--font-size-lg` | 18px | 强调 | 卡片数字指标、区块标题 |
| `--font-size-xl` | 24px | 大标题 | 页面副标题、图表标题 |
| `--font-size-2xl`| 32px | 特大数字 | QPS 数值、成本总额 |
| `--font-size-3xl`| 40px | 展示标题 | 空状态主标题 |

### 行高规则

```css
--line-height-tight:    1.2;    /* 标题、大数字指标 */
--line-height-normal:   1.5;    /* 正文、表格单元格 */
--line-height-relaxed:  1.75;   /* 多行描述文字 */
```

### 字重

```css
--font-weight-regular:    400;   /* 正文默认 */
--font-weight-medium:     500;   /* 按钮文字、表头、标签 */
--font-weight-semibold:   600;   /* 页面标题、区块标题、卡片标题 */
--font-weight-bold:       700;   /* 大数字指标、紧急信息 */
```

### 字体族

```css
--font-sans: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
--font-mono: 'JetBrains Mono', 'Fira Code', 'Cascadia Code', Menlo, monospace;
```

### 字体使用规则

1. 表格和代码类内容（trace_id、API Key 片段）使用等宽字体
2. 数值指标（QPS、成本、延迟）使用 bold 字重 + 大字号突出
3. 一个页面的字号种类不超过 5 种
4. 移动端最小可读字号 14px（表格里可降到 12px 但仅限辅助信息）

---

## 三、间距体系

### 基础单位：4px

```css
--space-0:  0;
--space-0-5: 2px;   /* 极小微调 */
--space-1:  4px;    /* 微小间距 */
--space-2:  8px;    /* 紧凑间距，标签内元素 */
--space-3:  12px;   /* 小间距，列表项内部 */
--space-4:  16px;   /* 标准间距，卡片内边距 */
--space-5:  20px;   /* 中等间距 */
--space-6:  24px;   /* 大间距，区块间隔 */
--space-8:  32px;   /* 超大间距，页面分割 */
--space-10: 40px;   /* 超大 */
--space-12: 48px;   /* 最大间距 */
--space-16: 64px;   /* 极大间距，空状态插画间距 */
--space-20: 80px;
--space-24: 96px;
```

### 常用场景

```css
/* 页面内边距 */
--page-padding-sm:  16px;    /* 窄屏页面 */
--page-padding-md:  24px;    /* 平板/桌面页面 */
--page-padding-lg:  32px;    /* 大屏幕 */

/* 卡片内边距 */
--card-padding-sm:  16px;    /* 小卡片 */
--card-padding-md:  20px;    /* 标准卡片 */
--card-padding-lg:  24px;    /* 大卡片 */

/* 导航栏 */
--nav-height:       56px;    /* 顶部导航栏高度 */

/* 底部安全区 */
--safe-area-bottom: env(safe-area-inset-bottom);
```

---

## 四、圆角体系

```css
--radius-none:    0;         /* 不需要圆角（罕见） */
--radius-sm:      4px;       /* 小标签、input 元素 */
--radius-md:      8px;       /* 按钮、输入框、徽章 */
--radius-lg:      12px;      /* 卡片、对话框、面板 */
--radius-xl:      16px;      /* 大卡片、统计面板 */
--radius-2xl:     20px;      /* 弹窗、大型容器 */
--radius-full:    9999px;    /* 圆形头像、进度环、状态指示器 */
```

### 圆角使用规则

1. 卡片统一使用 `--radius-lg`（12px）
2. 按钮使用 `--radius-md`（8px）
3. 输入框和表格使用 `--radius-md`（8px）
4. 模态框/抽屉使用 `--radius-xl`（16px）
5. 全局保持一种风格（偏方），不混用偏圆风格

---

## 五、阴影体系

```css
/* 深色主题阴影（基于透明黑色叠加） */
--shadow-xs:  0 1px 2px rgba(0, 0, 0, 0.3);
--shadow-sm:  0 1px 3px rgba(0, 0, 0, 0.4), 0 1px 2px rgba(0, 0, 0, 0.2);
--shadow-md:  0 4px 6px rgba(0, 0, 0, 0.4), 0 2px 4px rgba(0, 0, 0, 0.2);
--shadow-lg:  0 10px 15px rgba(0, 0, 0, 0.4), 0 4px 6px rgba(0, 0, 0, 0.2);
--shadow-xl:  0 20px 25px rgba(0, 0, 0, 0.5), 0 10px 10px rgba(0, 0, 0, 0.2);
--shadow-2xl: 0 25px 50px rgba(0, 0, 0, 0.5);
--shadow-inner: inset 0 2px 4px rgba(0, 0, 0, 0.3);
```

### 阴影使用规则

1. 卡片默认 `--shadow-sm`
2. 悬浮提升效果使用 `--shadow-md`
3. 弹窗和模态框使用 `--shadow-xl`
4. 移动端阴影尽量轻薄，避免厚重感

---

## 六、表格边框与分割线样式

```css
/* 表格分割线风格 */
--table-border-width: 1px;
--table-border-style: solid;
--table-border-color: var(--color-border);
--table-stripe-opacity: 0.03;  /* 斑马纹透明度 */
```

---

## 七、组件规范

### 7.1 卡片（Card）

仪表盘的核心承载单元。

```
+--------------------------------------------------+
|  [icon]  指标名称              [trend arrow]      |  <- Header row
|                                                  |
|               1,234                              |  <- Value: --font-size-2xl bold
|          requests/sec                            |  <- Unit: --font-size-sm tertiary
|                                                  |
|  [#].......................[ ] 72%               |  <- Progress bar (optional)
+--------------------------------------------------+
```

| 属性 | 值 |
|------|-----|
| 圆角 | `--radius-lg`（12px） |
| 内边距 | `--card-padding-lg`（24px） |
| 背景 | `--color-bg-elevated` |
| 边框 | 1px solid `--color-border` |
| 阴影 | `--shadow-sm` |
| 悬停 | 阴影加深到 `--shadow-md`，边框变为 `--color-primary-muted` |

### 7.2 统计卡片区格（Stat Grid）

- 网格布局：4列（桌面）/ 2列（平板）/ 1列（手机）
- 每个格子即一张 Card
- 数值列宽度相等

### 7.3 按钮（Button）

#### 主按钮（Primary）

| 属性 | 值 |
|------|-----|
| 背景 | `--color-primary` |
| 文字 | `--color-text-inverse` |
| 圆角 | `--radius-md` |
| 内边距 | 10px 20px |
| 最小高度 | 40px（手机 44px） |
| 字号 | `--font-size-md` |
| 字重 | `--font-weight-medium` |
| 悬停 | `--color-primary-hover` + `--shadow-sm` |

#### 次要按钮（Secondary）

| 属性 | 值 |
|------|-----|
| 背景 | transparent |
| 边框 | 1px solid `--color-border` |
| 文字 | `--color-text-secondary` |
| 悬停 | 背景 `--color-bg-overlay`，边框 `--color-text-tertiary` |

#### 危险按钮（Danger）

| 属性 | 值 |
|------|-----|
| 背景 | transparent |
| 边框 | 1px solid `--color-danger` |
| 文字 | `--color-danger` |
| 悬停 | 背景 `--color-danger` + 20% 透明度 |

#### 图标按钮（Icon Button）

| 属性 | 值 |
|------|-----|
| 尺寸 | 28x28px |
| 圆角 | 6px |
| 悬停 | `--color-bg-overlay` |

### 7.4 表格（Table）

| 属性 | 值 |
|------|-----|
| 表头背景 | `--color-bg-table-header` |
| 表头文字 | `--font-size-sm`, `--font-weight-medium`, `--color-text-secondary` |
| 单元格文字 | `--font-size-md`, `--font-weight-regular`, `--color-text-primary` |
| 单元格高度 | 44px（满足手指点击） |
| 行间距 | 1px 底部 `--color-border`（无边框风格时用） |
| 斑马纹 | 偶数行背景 + `--table-stripe-opacity` |
| 悬停行 | `--color-bg-table-row-hover` |
| 固定列 | 表头固定 `position: sticky; top: 0` |

#### 表格内状态标签

| 状态 | 背景 | 文字 |
|------|------|------|
| 正常/已启用 | `rgba(16,185,129,0.15)` | `--color-success` |
| 告警/即将超限 | `rgba(245,158,11,0.15)` | `--color-warning` |
| 禁用/已停用 | `rgba(107,114,128,0.15)` | `--color-text-tertiary` |
| 错误/熔断 | `rgba(239,68,68,0.15)` | `--color-danger` |

### 7.5 图表容器（Chart Container）

| 属性 | 值 |
|------|-----|
| 外框 | 同 Card 样式（圆角 12px、边框 1px、背景 elevated、阴影 sm） |
| 标题 | `--font-size-md`, `--font-weight-semibold`, `--color-text-primary` |
| 图表区域 | 最小高度 200px（手机）/ 300px（桌面） |
| 坐标轴文字 | `--font-size-xs`, `--color-text-quaternary` |
| 图例 | `--font-size-sm`, `--color-text-secondary` |

#### 图表交互

- Tooltip：背景 `--color-bg-overlay`，圆角 `--radius-md`，边框 1px `--color-border`
- 十字准线：虚线，1px `--color-border` 50% 透明度

### 7.6 开关/切换（Toggle / Switch）

| 属性 | 值 |
|------|-----|
| 尺寸 | 40x22px |
| 未开启 | 背景 `--color-text-tertiary`，滑块 `--color-bg-base` |
| 已开启 | 背景 `--color-primary`，滑块 `--color-bg-elevated` + `--shadow-sm` |
| 圆角 | 滑块 `--radius-full`，轨道 `--radius-full` |
| 文字标签 | `--font-size-sm`, `--color-text-secondary` |
| 最小触控区 | 44px 高度（轨道 + 文字整体） |

### 7.7 表单（Form）

| 属性 | 值 |
|------|-----|
| 输入框高度 | 40px |
| 输入框圆角 | `--radius-md` |
| 输入框边框 | 1px solid `--color-border` |
| 输入框背景 | `--color-bg-input` |
| 输入框文字 | `--font-size-md`, `--color-text-primary` |
| 聚焦状态 | 边框 `--color-primary`，外发光 `box-shadow: 0 0 0 2px rgba(59,130,246,0.3)` |
| 禁用状态 | 背景 `--color-bg-overlay`，文字 `--color-text-disabled` |
| 标签文字 | `--font-size-sm`, `--font-weight-medium`, `--color-text-secondary`，上方 |
| 帮助文字 | `--font-size-xs`, `--color-text-tertiary`，下方 |
| 错误状态 | 边框 `--color-danger`，帮助文字 `--color-danger` |
| 必填标记 | 红色 `*`，`--font-size-sm` |

#### 选择器（Select / Dropdown）

| 属性 | 值 |
|------|-----|
| 样式 | 同输入框，右侧添加下拉箭头 |
| 选项 | 下拉面板背景 `--color-bg-elevated`，选中项 `--color-primary-light` |

### 7.8 空状态（Empty State）

| 属性 | 值 |
|------|-----|
| 插画高度 | 120px |
| 插画颜色 | `--color-text-tertiary`，opacity 0.4 |
| 标题 | `--font-size-lg`, `--font-weight-medium`, `--color-text-secondary` |
| 描述 | `--font-size-sm`, `--color-text-tertiary` |
| 间距 | 插画与标题 `--space-4`，标题与描述 `--space-2` |
| CTA 按钮 | 可选，描述下方 `--space-6` |

### 7.9 面包屑导航

| 属性 | 值 |
|------|-----|
| 文字 | `--font-size-sm`, `--color-text-tertiary` |
| 分割符 | `/`，`--color-text-quaternary` |
| 当前页 | `--color-text-primary`, `--font-weight-medium` |

### 7.10 Toast 通知

| 属性 | 值 |
|------|-----|
| 高度 | 48px |
| 圆角 | `--radius-md` |
| 背景 | `--color-bg-elevated` |
| 边框 | 左侧 3px 状态色指示条 |
| 位置 | 右上角，距顶部 `--space-4`，距右侧 `--space-4` |
| 自动消失 | 3 秒渐隐（成功/警告），错误需手动关闭 |

---

## 八、页面布局

### 8.1 整体布局

```
+--------------------------------------------------------+
|  [==  AI Gateway Control Panel  ==]                    |  <- 顶部导航栏 56px
+--------------------------------------------------------+
|  +----------+------------------------------------------+|
|  |          |                                          ||
|  |  侧边栏  |             页面内容区域                    ||
|  |  200px   |                                          ||
|  |          |                                          ||
|  +----------+------------------------------------------+|
+--------------------------------------------------------+
```

| 区域 | 桌面 | 平板 | 手机 |
|------|------|------|------|
| 顶部导航栏 | 固定 56px | 固定 56px | 固定 56px |
| 侧边栏宽度 | 200px / 64px 折叠 | 固定 180px | 隐藏（汉堡菜单） |
| 内容区 | flex:1, padding 24px | flex:1, padding 20px | flex:1, padding 16px |
| 页面最大宽度 | 1440px | 100% | 100% |

### 8.2 移动端适配规则

- 侧边栏折叠为汉堡菜单 + 抽屉式面板
- 统计卡片 4列 -> 2列 -> 1列
- 表格在小屏幕转为卡片列表视图或横向滚动
- 所有可点击区域最小 44x44px（手指触控标准）
- 图表最小高度 200px（手机）

---

## 九、动效规范

### 过渡时长

```css
--transition-fast:     100ms;   /* 按钮状态切换、图标变色 */
--transition-normal:   200ms;   /* hover 效果、下拉展开 */
--transition-slow:     300ms;   /* 模态框出现、抽屉滑入 */
--transition-slower:   400ms;   /* 页面切换、路由过渡 */
```

### 缓动函数

```css
--ease-standard: cubic-bezier(0.4, 0, 0.2, 1);   /* Material Design 标准 */
--ease-out:      cubic-bezier(0.0, 0, 0.2, 1);    /* 元素进入 */
--ease-in:       cubic-bezier(0.4, 0, 1, 1);      /* 元素退出 */
```

### 数据刷新

- 图表数据更新：旧值缩小淡出，新值从底部放大淡入（`--transition-normal`）
- 表格数据排序/筛选：500ms loading 骨架屏

---

## 十、暗色模式（可选）

当前规范以**深色主题**为默认。浅色主题通过 CSS 变量覆盖实现。

```css
/* 在 <html> 上添加 class="light" 即可切换 */
html.light {
  /* 覆盖以下变量 */
  --color-bg-base:        var(--color-bg-base-light);
  --color-bg-elevated:    var(--color-bg-elevated-light);
  --color-bg-overlay:     var(--color-bg-overlay-light);
  --color-bg-input:       var(--color-bg-input-light);
  --color-bg-table-header:var(--color-bg-table-header-light);
  --color-bg-table-row-hover:var(--color-bg-table-row-hover-light);
  --color-bg-badge:       var(--color-bg-badge-light);
  --color-text-primary:   var(--color-text-primary-light);
  --color-text-secondary: var(--color-text-secondary-light);
  /* ... 其余同理 */
}
```

---

## 十一、图标规范

| 属性 | 值 |
|------|-----|
| 图标大小 | 16px（文本内）/ 20px（导航栏）/ 24px（卡片头部）/ 32px（空状态） |
| 图标库 | Lucide React（推荐，与 Tailwind 风格一致） |
| 图标描边 | 1.5px |

### 状态图标

| 状态 | 图标 | 颜色 |
|------|------|------|
| 成功 | Check | `--color-success` |
| 警告 | AlertTriangle | `--color-warning` |
| 错误 | X | `--color-danger` |
| 信息 | Info | `--color-info` |

### 导航栏图标映射

| 页面 | 图标 |
|------|------|
| 概览（Overview） | LayoutDashboard |
| 插件管理（Plugins） | Puzzle |
| 成本分析（Costs） | DollarSign |
| 配额管理（Quotas） | Shield |
| 缓存监控（Cache） | Database |
| 日志查看（Logs） | FileText |
| 设置（Settings） | Settings |

---

## 十二、响应式断点

```css
/* TailwindCSS 预设断点 */
--breakpoint-sm:  640px;   /* 手机横屏及以上 */
--breakpoint-md:  768px;   /* 平板及以上 */
--breakpoint-lg:  1024px;  /* 笔记本及以上 */
--breakpoint-xl:  1280px;  /* 桌面及以上 */
--breakpoint-2xl: 1536px;  /* 大屏桌面 */
```

### 各断点下的布局变化

| 特性 | <640px | 640-768px | 768-1024px | >1024px |
|------|--------|-----------|------------|---------|
| 侧边栏 | 抽屉式 | 180px | 180px | 200px/64px |
| 统计卡片 | 1列 | 2列 | 2-3列 | 4列 |
| 图表高度 | 200px | 250px | 280px | 320px |
| 页面内边距 | 16px | 20px | 20px | 24px |
| 表格 | 卡片列表或横滚 | 横向滚动 | 完整表格 | 完整表格 |
