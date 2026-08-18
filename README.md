# QQ 群聊管理

面向 AstrBot `qq_official` / `qq_official_webhook` 适配器的 QQ 官方群聊管理插件。插件复用 AstrBot 已认证的 QQ 客户端，覆盖 QQ 官方当前提供的 12 个群管理 HTTP 接口，不读取、保存或输出机器人凭据。

## 要求

- AstrBot `4.27.3 <= version < 5`
- QQ 官方机器人 WebSocket 或 Webhook 适配器
- 调用者是 AstrBot 管理员
- 机器人已获得 QQ 群管理接口白名单权限
- 入群审批、禁言和自动审核要求机器人是目标群管理员
- 按钮审批还需要 QQ Markdown、自定义按钮和互动事件权限

## 安装

在 AstrBot 插件管理页使用仓库地址安装：

```text
https://github.com/mgz0227/QQGroup-admin
```

插件没有额外 Python 依赖。

## 命令前缀

插件使用 AstrBot 自带的唤醒前缀，不再设置 `/群管` 根命令。将 AstrBot 的 `wake_prefix` 配置为 `/` 后，可以直接发送：

```text
/群信息
/申请列表
/禁言 @成员 2h
/自动审核状态
```

如果使用其他唤醒前缀，只需替换命令开头的 `/`，命令名称不变。

## 命令

多个 QQ 号码使用英文逗号分隔。成员优先使用 `@成员`，也可以填写成员 OpenID。

| 命令 | 作用 |
| --- | --- |
| `/群帮助` | 显示命令帮助 |
| `/群信息` | 查询群信息，并显示当前群和自己的 OpenID |
| `/机器人状态` | 查询机器人群角色和消息接收状态 |
| `/申请列表 [游标]` | 每页查询 5 条待审申请，并显示同意/拒绝按钮 |
| `/禁言状态` | 查询全员规则和当前成员禁言 |
| `/禁言 <成员OpenID\|@成员> <时长>` | 新增或更新禁言，如 `30m`、`2h`、`7d` |
| `/解禁 <成员OpenID\|@成员>` | 解除成员禁言 |
| `/自动审核状态` | 查询当前群自动审核状态 |
| `/自动审核开启 <QQ号,...>` | 创建或启用当前群策略，并添加白名单 |
| `/自动审核添加 <QQ号,...>` | 添加白名单，并扫描已有待审申请 |
| `/自动审核移除 <QQ号,...>` | 移除白名单 |
| `/自动审核同步 确认` | 将当前群的 WebUI 配置同步到 QQ 官方 |
| `/自动审核关闭 确认` | 删除当前群策略及其白名单 |

## WebUI 配置

插件目录中的 `_conf_schema.json` 会在 AstrBot WebUI 生成“QQ群自动审核配置”。每个群可以独立配置：

- 群 OpenID
- 是否启用 QQ 号码白名单策略
- 是否启用 B 站 UID 审核
- 白名单 QQ 号码或验证消息拒绝关键词
- 同步后是否扫描已有待审申请
- 审批按钮的默认拒绝理由
- UID 审核轮询间隔

使用步骤：

1. 在目标群发送 `/群信息`，取得群 OpenID。
2. 打开 AstrBot WebUI 的插件配置，添加“QQ群自动审核”条目。
3. 选择 QQ 号码白名单或 B 站 UID 审核，填写对应设置后保存。
4. 回到该群发送 `/自动审核同步 确认`。

两种自动审核不能在同一群同时启用。QQ 原生白名单可能在插件读取验证消息前直接放行，因此同步命令会拒绝这种配置。

WebUI 保存的是期望状态。首次配置或切换模式必须在目标群执行同步，以绑定正确的 QQ 适配器；绑定后修改 UID 关键词和轮询间隔会在下个周期生效。插件隐藏记录策略 ID、平台 ID 和上次成功应用的白名单，QQ 白名单后续同步只增删差异。

## 自动审核

`/自动审核开启 123456,789012` 会为当前群创建 QQ 官方白名单自动审核策略。白名单中的后续入群申请由 QQ 平台自动通过，不依赖 AstrBot 入群事件，也不需要插件后台轮询。

开启策略或添加白名单后，插件还会调用官方执行接口，异步扫描当前待审申请。QQ 官方预计约 10 分钟完成，暂未提供任务进度查询接口。

插件只操作当前群的单群 OpenID 策略，并隐藏策略 ID、群 OpenID 和开关等技术参数。如果检测到多个、跨群或按 QQ 群号关联而无法安全匹配的策略，会停止操作并要求先在 QQ 官方后台整理，避免影响其他群。

如果当前群已经存在未由 WebUI 配置管理的策略，WebUI 同步会拒绝接管。确认旧策略可以删除后，先发送 `/自动审核关闭 确认`，再重新同步。

白名单单次最多提交 10,000 个 QQ 号码，单个策略最多 100,000 人。官方返回的白名单人数是估算值。

B 站 UID 模式会读取用户主动入群申请的验证内容，先匹配拒绝关键词，再接受 `UID:188144093`、`UID：188144093` 或纯数字格式，并通过 B 站用户名片接口核对返回的 UID。UID 不存在或格式不符会拒绝；B 站限流、网络错误或异常返回只会保留待审，避免误拒。机器人邀请产生的申请不自动处理，可用 `/申请列表` 的按钮审核。

## 审批按钮

`/申请列表` 使用 QQ 原生回调按钮，只有 QQ 群主或管理员能点击，没有文字审批命令回退。按钮凭据 15 分钟后失效，AstrBot 重启后旧按钮也会失效，重新查询即可。

AstrBot `4.27.3` 尚未公开按钮组件和互动事件插件接口，本插件在 QQ 适配器启动前启用互动 intent，并复用其 botpy 客户端发送键盘。首次安装、热重载插件或修改平台后请重启 AstrBot（或重载 QQ 适配器）；Webhook 模式还需在 QQ 开放平台订阅 `INTERACTION_CREATE`。该兼容桥已锁定 AstrBot `4.27.3 <= version < 5`，升级 AstrBot 后应重新验证。

## 能力边界

QQ 当前没有开放成员列表、踢人、管理员设置、修改群资料、查询或解除群黑名单、写入全员或定时禁言规则等接口，本插件不会使用非官方协议补齐。

QQ 文档列出的入群申请事件尚未由 AstrBot `4.27.3` QQ 适配器转发到插件事件总线。QQ 号码白名单使用官方持久化策略；需要读取验证文字的 UID 模式则以 30 QPM 限额内的错峰轮询实现。

参考：

- [QQ 机器人群聊管理接口](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_groups_group_openid_info.get.html)
- [QQ 入群自动审批策略](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_groups_join_approval_strategy.post.html)
- [QQ 群消息与按钮](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_groups_group_openid_messages.post.html)
- [QQ 互动事件](https://bot.q.qq.com/wiki/develop/api-v2/autogen/event/interaction_create.html)
- [Bilibili API Collect：用户名片信息](https://github.com/Goooler/bilibili-API-collect/blob/trunk/docs/user/info.md#用户名片信息)
- [AstrBot 插件开发指南](https://docs.astrbot.app/dev/star/plugin-new.html)
- [AstrBot 插件配置指南](https://docs.astrbot.app/dev/star/guides/plugin-config.html)

## 测试

```bash
python -m unittest discover -s tests -v
python -m py_compile main.py qq_api.py review.py
```
