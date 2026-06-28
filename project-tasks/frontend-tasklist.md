# 前端任务清单

> 控制面板（control-panel）：React + TypeScript + Vite + TailwindCSS

### [x] TASK-F01：控制面板布局 + 顶部导航栏
- 对应契约：DESIGN_SYSTEM.md #Page Layout
- 验收标准：顶部导航栏 56px，侧边栏 200px（桌面），路由切换正常

### [x] TASK-F02：概览页面（Overview）
- 调用契约：GET /metrics, GET /health
- 验收标准：展示 QPS、延迟、成本、缓存命中率趋势图，使用 Recharts 折线图

### [x] TASK-F03：插件管理页面（Plugins）
- 调用契约：config.yaml schema（TECH_SPEC.md）
- 验收标准：展示所有插件列表，支持开关切换，编辑配置后即时生效（热重载）

### [x] TASK-F04：成本分析页面（Costs）
- 调用契约：GET /metrics（gateway_cost_by_model）
- 验收标准：饼图/柱状图展示各模型费用分布，支持时间范围筛选

### [x] TASK-F05：配额管理页面（Quotas）
- 调用契约：GET /admin/api-keys, POST /admin/api-keys, DELETE /admin/api-keys/{key_id}, GET /admin/quotas/{key_id}
- 验收标准：API Key 列表（分页），创建/撤销 Key，查看详情和配额使用情况

### [x] TASK-F06：缓存命中率页面（Cache）
- 调用契约：GET /metrics（gateway_cache_hits_total, gateway_cache_misses_total）
- 验收标准：L1/L2/L3 各级缓存命中率柱状图，命中/未命中趋势

### [x] TASK-F07：请求日志页面（Logs）
- 调用契约：结构化日志（TECH_SPEC.md）
- 验收标准：表格展示请求日志，含 trace_id/request_id/user_id/时间/状态码，支持按 trace_id 搜索

### [x] TASK-F08：暗色/亮色主题切换
- 对应契约：DESIGN_SYSTEM.md #颜色体系, src/styles/variables.css
- 验收标准：切换按钮，CSS 变量自动更新，默认暗色主题

### [x] TASK-F09：API 调用层封装
- 对应契约：API_CONTRACT.md 全部接口
- 验收标准：使用 VITE_API_BASE 环境变量拼接路径，禁止硬编码 /api/... 或 /admin/...，TypeScript 类型定义与契约一致

## 构建验证

| 检查项 | 结果 |
|--------|------|
| TypeScript 编译 | 0 错误 |
| Vite 构建 | 成功 (635KB JS + 14KB CSS) |
| CSS 变量主题 | 暗色/亮色切换正常 |
| API 路径拼接 | 全部使用 VITE_API_BASE |
