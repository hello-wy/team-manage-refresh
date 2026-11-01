# 后台常驻顶部框架

顶部导航、品牌 Logo、浏览器 favicon、主题按钮和个人资料由外层页面持有。后台内容在同源 iframe 中独立加载和滚动；切换页面不重新创建 Header，也不会重启外层通知轮询。每个内容页保留独立的脚本、事件、定时器和弹窗生命周期。

普通页面请求渲染顶部框架。浏览器的 `Sec-Fetch-Dest: iframe` 请求与现有表格刷新的 `X-Requested-With: XMLHttpRequest` 请求渲染内容布局。地址栏始终保留正常的后台地址和查询参数，可以直接打开、刷新或在新标签打开。

HTML 响应通过 `Vary: Sec-Fetch-Dest, X-Requested-With` 区分两种布局，避免缓存混用。

`admin-workspace-shell.js` 管理顶层地址栏、导航高亮和前进后退；`admin-workspace-content.js` 将内容页链接、GET 表单以及已有的 History API 更新同步到外层。内容加载失败会保留服务器的实际错误响应。登录失效进入登录页时，整个窗口返回登录界面。

共享主题通过同源 `storage` 事件同步，内容区和 Header 保持一致。全局通知和个人资料弹窗位于外层，业务弹窗位于内容区。

已移除“导出记录”和“额度轮转”的页面路由、桌面/手机入口及专用前端资源。导出、额度同步、轮转任务和已有业务 API 继续由各自的服务处理。

浏览器回归脚本：`tests/browser/admin-workspace.cjs`。配置 `WORKSPACE_TEST_PYTHON` 和 `WORKSPACE_TEST_CHROME` 为本机 Python 和 Chrome 可执行文件，再运行 `node tests/browser/admin-workspace.cjs`（Node 环境需提供 Playwright）。脚本使用临时数据库和随机测试密码启动独立服务器，完成后清理，不使用项目业务数据库。
