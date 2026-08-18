# QQ 群聊管理

面向 AstrBot `qq_official` / `qq_official_webhook` 适配器的 QQ 官方群聊管理插件。覆盖 QQ 机器人官方文档当前提供的 12 个群聊管理 HTTP 接口：

- 查询群基本信息与机器人群内状态
- 拉取、同意、拒绝及拒绝并拉黑入群申请
- 查询成员禁言，新增、更新及解除成员禁言
- 查询、创建、修改、删除及执行入群自动审批策略
- 增删策略关联群和白名单 QQ 号码

插件直接复用 AstrBot 已认证的 QQ 客户端，不读取、保存或输出 AppID、Secret、Access Token。

## 要求

- AstrBot `4.27.3 <= version < 5`
- QQ 官方机器人 WebSocket 或 Webhook 适配器
- 调用者必须是 AstrBot 管理员
- 机器人需要申请 QQ 群聊管理白名单权限；审批和禁言还要求机器人是目标群管理员

> QQ 官方机器人群和 QQ 频道在 AstrBot 中都属于群消息。本插件会进一步检查原始 `group_openid`，不会把频道 ID 当作群 OpenID。

## 安装

在 AstrBot 插件管理页使用仓库地址安装：

```text
https://github.com/mgz0227/QQGroup-admin
```

插件没有额外 Python 依赖。安装后在 QQ 群中发送 `/群管 帮助` 查看命令。

## 命令

所有命令也可用根命令 `/qqgroup` 或 `/groupadmin`。多个 ID 或 QQ 号码使用英文逗号分隔。

| 命令 | 作用 |
| --- | --- |
| `/群管 标识` | 查看当前群 OpenID 和自己的成员 OpenID |
| `/群管 信息` | 查询群名称、简介、标签和成员数 |
| `/群管 机器人` | 查询机器人角色、消息接收设置和主动消息状态 |
| `/群管 申请 列表 [limit] [cursor]` | 分页拉取申请，`limit` 为 1-100 |
| `/群管 申请 同意 <成员OpenID\|@成员> <申请ID>` | 同意申请 |
| `/群管 申请 拒绝 <成员OpenID\|@成员> <申请ID> <理由\|->` | 拒绝申请，`-` 表示无理由 |
| `/群管 申请 拒绝拉黑 <成员OpenID\|@成员> <申请ID> 确认 <理由\|->` | 拒绝并加入群黑名单 |
| `/群管 禁言 状态` | 查询全员规则和当前成员禁言 |
| `/群管 禁言 添加 <成员OpenID\|@成员> <时长>` | 新增禁言，时长如 `30m`、`2h`、`7d` |
| `/群管 禁言 更新 <成员OpenID\|@成员> <时长>` | 更新禁言到期时间 |
| `/群管 禁言 解除 <成员OpenID\|@成员>` | 解除禁言 |
| `/群管 策略 列表 [limit] [cursor]` | 分页查询自动审批策略 |
| `/群管 策略 创建当前 <on\|off> <RFC3339\|-> <备注\|->` | 为当前群创建策略 |
| `/群管 策略 创建OpenID <OpenID,...> <on\|off> <RFC3339\|-> <备注\|->` | 按群 OpenID 创建策略 |
| `/群管 策略 创建群号 <群号,...> <on\|off> <RFC3339\|-> <备注\|->` | 按 QQ 群号创建策略 |
| `/群管 策略 开关 <策略ID> <on\|off>` | 启停策略 |
| `/群管 策略 到期 <策略ID> <RFC3339>` | 修改策略到期时间 |
| `/群管 策略 备注 <策略ID> <备注>` | 修改备注，`-` 可清空 |
| `/群管 策略 关联OpenID <策略ID> <add\|del> <OpenID,...>` | 增删关联群 OpenID |
| `/群管 策略 关联群号 <策略ID> <add\|del> <群号,...>` | 增删关联 QQ 群号 |
| `/群管 策略 白名单 <策略ID> <add\|del> <QQ号,...>` | 增删白名单号码 |
| `/群管 策略 删除 <策略ID> 确认` | 永久删除策略 |
| `/群管 策略 执行 <策略ID> 确认` | 异步全量扫描并审批白名单申请 |

禁言最长 30 天；单次成员禁言最多 10 人（命令每次操作一人）；策略最多关联 100 个群；白名单单次最多 10,000 个 QQ 号码。执行策略后 QQ 官方只承诺约 10 分钟完成，目前没有任务进度查询接口。

## 官方能力边界

QQ 当前没有开放成员列表、踢人、管理员设置、修改群资料、查询或解除群黑名单、写入全员/定时禁言规则等接口。本插件不会用非官方协议补齐这些能力。

QQ 文档还列出了 7 个群管理事件，但 AstrBot `4.27.3` 的 QQ 官方适配器尚未把这些原生事件转发到插件事件总线，因此本插件只实现完整的 12 个 HTTP 管理接口，不侵入或猴补平台适配器。

参考：

- [QQ 机器人群聊管理接口](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_groups_group_openid_info.get.html)
- [QQ OpenAPI 调用指南](https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/api-call-guide.html)
- [AstrBot 插件开发指南](https://docs.astrbot.app/dev/star/plugin-new.html)

## 测试

```bash
python -m unittest discover -s tests -v
python -m py_compile main.py qq_api.py
```
