# CPA 导出

工作台 Team「更多 → 导出到 CPA」、勾选 Team 后的「批量导出到 CPA」，以及账号池表格中的 CPA 图标均上传到系统设置「CliproxyAPI 推送」配置的站点，使用该配置的管理密钥和系统代理。

批量导出逐个执行相同的母号导出流程，同一批次重复的 Team ID 只处理一次；某个 Team 失败后继续处理其他 Team，结果显示成功数、失败数和每个失败 Team 的原因。

Team 导出与 Sub2API 共用凭证刷新、缺失凭证自动登录、邮箱和工作区检查、母号授权同步流程。账号池导出与 Sub2API 共用后台任务、所选工作区验证、重新登录、凭证保存和登录期间凭证变更检查；失败原因会保存并显示在账号详情中。

CPA 收到扁平的 Codex 认证 JSON，包含 `type=codex`、`email`、`account_id`、`access_token`、`refresh_token`、`id_token`、`client_id`、`expired`、`last_refresh`。它使用 CPA 管理接口 `POST /v0/management/auth-files?name=...`，以 Bearer 管理密钥鉴权，不包含 Sub2API 分组或并发配置。

认证文件名为 `codex-邮箱-工作区ID.json`，同一邮箱的不同工作区分别保存，同一工作区再次导出时更新同名文件。CPA 导出任务保存于独立的 `account_pool_cpa_export_jobs` 表，应用启动时自动创建；不会写入 Sub2API 成功记录或标记已导出到 Sub2API。

缺少必要凭证、登录失败或 CPA 上传失败会明确报错。上传连接中断时提示结果未确认，不自动重试。
