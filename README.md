# astrbot_plugin_newapi

AstrBot 插件：在 QQ 群中使用 NewAPI 账号，支持 **账号绑定 / 每日签到 / 余额查询 / 退群自动删号**。

插件通过 NewAPI **超级管理员的系统访问令牌** 调用站点 Admin API 完成用户查询、加额度、删除账号等操作。

## 功能一览

| 命令 | 权限 | 说明 |
| --- | --- | --- |
| `/newapi绑定 <用户名> <密码>` | 用户 | 验证后绑定 QQ 与 NewAPI 账号（默认仅私聊可用） |
| `/绑定 <ID>` | 用户 | 两步验证绑定：发起后私聊回复"账号 密码"完成验证，通过后自动更换到配置的目标分组；`/取消绑定` 可放弃 |
| `/注册` | 用户 | 群内自助注册：QQ 号作为账号，随机 8 位密码私聊发送；若该 QQ 注册过则自动找回并重置密码；受最低群等级门槛限制 |
| `/newapi解绑` | 用户 | 解除自己的绑定 |
| `/签到`（`/打卡`） | 用户 | 每日签到，随机额度直接充入账号 |
| `/余额`（`/查询余额`） | 用户 | 查询剩余额度 / 累计消耗 / 调用次数 |
| `/newapi帮助` | 用户 | 查看命令列表 |
| `/newapi用户 <关键词>` | 管理员 | 搜索 NewAPI 用户，显示绑定状态 |
| `/newapi强制解绑 <QQ号>` | 管理员 | 强制解除某 QQ 的绑定 |
| 退群事件 | 自动 | 成员退群后自动解绑；可配置自动删除其 NewAPI 账号 |

## 安装

1. 将本文件夹放入 AstrBot 的 `data/plugins/` 目录（或通过插件市场/从 URL 安装本仓库）
2. 在 WebUI 中启用插件
3. 在插件配置中填写 `base_url` 与 `admin_token`

## 配置说明

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `base_url` | - | NewAPI 站点地址，如 `https://api.example.com` |
| `admin_token` | - | 超级管理员的**系统访问令牌**（NewAPI 个人设置 → 生成系统访问令牌） |
| `admin_user_id` | `1` | 生成令牌的管理员数字 ID。新版 new-api 校验请求头 `New-Api-User` 必须与令牌所属用户一致 |
| `quota_per_unit` | `500000` | 多少 quota 等于 1 美元 |
| `exchange_rate` | `7.2` | 余额展示用的人民币汇率 |
| `bind_verify_password` | `true` | 绑定时是否验证用户名+密码（走 `/api/user/login`）。关闭后仅凭用户名绑定，安全性低 |
| `allow_group_bind` | `false` | 是否允许群聊绑定。默认强制私聊，避免密码在群里泄露 |
| `checkin_enabled` | `true` | 签到开关 |
| `checkin_min_usd` / `checkin_max_usd` | `0.1` / `0.5` | 签到随机额度区间（美元） |
| `checkin_cooldown_hours` | `24` | 签到冷却时长 |
| `register_enabled` | `true` | 是否开启群内自助注册 |
| `register_min_level` | `0` | 注册所需最低 QQ 群聊等级（0 = 不限制，如填 5 则需 Lv.5） |
| `register_group` | `default` | 注册默认分组，新注册用户自动划入 |
| `bind_group` | 空 | `/绑定 <ID>` 成功后账号自动更换到该分组，留空不更换 |
| `bind_protect_admin` | `true` | 禁止通过 ID 绑定管理员账号（role≥10），防止被退群删号误删 |
| `watch_groups` | `[]` | 监听退群的群号列表，留空监听所有群 |
| `delete_on_leave` | `false` | 退群后是否**永久删除**其 NewAPI 账号。默认仅解除绑定，开启请三思 |

## 工作原理

- **绑定**：默认调用 `POST /api/user/login` 验证用户名密码，成功后本地保存 `QQ → user_id` 映射（JSON 存储）
- **签到**：`GET /api/user/{id}` 取出用户 → quota 加上随机额度 → `PUT /api/user/` 更新（管理员权限）；本地记录上次签到时间实现冷却
- **余额**：`GET /api/user/{id}`，`quota / quota_per_unit` 换算为美元
- **退群**：监听 OneBot（aiocqhttp / NapCat / Lagrange 等）的 `notice.group_decrease` 事件，匹配绑定关系后解绑/删号（`DELETE /api/user/{id}`）

所有管理员请求均携带：

```
Authorization: Bearer <admin_token>
New-Api-User: <admin_user_id>
```

## 注意事项

1. **令牌安全**：`admin_token` 是超级管理员权限，请勿泄露，也不要把配置文件发给别人
2. **退群删号有风险**：`delete_on_leave` 开启后无法恢复被删账号，建议保留默认关闭（只解绑）
3. **ID 绑定无验证**：`/绑定 <ID>` 不校验密码（凭 ID 即可绑定并更换分组），请确保站点用户 ID 不对外公开，或仅在小范围可信群使用；`bind_protect_admin` 默认保护管理员账号
4. **群等级门槛依赖适配端**：`register_min_level` 需要 OneBot 适配端（NapCat / Lagrange 等）支持 `get_group_member_info` 返回 `level` 字段；获取失败时注册会被拒绝
5. **Turnstile**：若站点开启了登录验证码（Turnstile），密码验证绑定会失败，可关闭 `bind_verify_password` 改用免密绑定
6. **Fork 差异**：本插件基于 [new-api](https://github.com/QuantumNous/new-api) 标准 Admin API 编写。Veloera、VoAPI 等 fork 若接口有出入（如自带签到接口），可能需要微调
7. **退群事件依赖**：需要 AstrBot + OneBot 适配端（NapCat 等）会把 notice 事件转发给插件；如遇不生效，请确认 AstrBot 版本较新，且可先用 `/newapi强制解绑` 手动处理

## 后续可扩展

- 签到排行榜
- 兑换码（redemption）模式签到
- 管理员远程创建/充值用户
- 用户调用用量日报推送

## License

MIT
