# KanadeBot × AWMC 接入说明

## 配置

AWMC Bot（`/www/bot/awmc/.env`）：

```env
MAIMAIDX_GROUP_CONSOLE_ENABLED=true
AWMC_XF_BRIDGE_ENABLED=true
AWMC_KANADE_BRIDGE_SECRET=<openssl rand -hex 32，不能与论坛 secret 相同>
AWMC_KANADE_BRIDGE_ALLOW_IPS=<KanadeBot 出口 IP，逗号分隔，可留空>
AWMC_KANADE_BRIDGE_AUTO_AGREE=true
AWMC_API_MODE=team
```

KanadeBot / AstrBot 插件配置：

```text
awmc_bridge_url = https://group.wmc.pub
awmc_bridge_secret = 与 AWMC_KANADE_BRIDGE_SECRET 相同
```

确认 NapCat → AstrBot 的 aiocqhttp 反向 WS 正常；私聊使用 `/maiu` 时打开 `allow_private_chat` 或把 QQ 加入 `private_chat_whitelist`。

## 基础指令

| 指令 | 作用 | 说明 |
|---|---|---|
| `/b50`、`b50` | 查询本人 B50 | AWMC 开启时只查发送者本人；不出图时按 `b50_enable_image` 返回文字 |
| `u`、`/u` | 免二维码更新成绩 | 需已绑定 `mai_uid`；更新 AWMCNET 及已授权水鱼/落雪；受 AWMC 冷却和配额限制 |
| `/maiu [SGWCMAID/官方链接/图片]` | 二维码完整传分 | `maiu` 上传 AWMCNET + 水鱼；首次使用会先建档；一次性码用完后需换新码 |
| `/mai绑定 [SGWCMAID/官方链接/图片]` | 仅绑定舞萌账号 | 首次绑定会消耗二维码；绑定后可直接 `/u`，或再发新码 `/maiu` |
| `/qbind`、`/mai状态`、`/mai账号` | 查看 AWMC 开通状态 | 首次使用默认同意统一协议并按当前 QQ 创建档案 |
| `/查进度`、`/查任务` | 查看任务与渠道同步状态 | 显示 AWMCNET、水鱼、落雪最近状态 |
| `/mai帮助`、`/awmc帮助` | 基础指令帮助 | 仅列基础功能 |
| `/用户协议` | 查看统一用户协议 | 默认同意时仅用于查看 |
| `/撤回用户协议` | 撤回协议同意 | 默认同意模式下下次使用会重新记录 |
| `/不同意共享我的数据`、`/同意共享我的数据`、`/数据共享状态` | 数据共享开关/状态 | 与 AWMC 现有规则一致 |

会馆原有 `/bd 验证码` 仍用于离店绑定，和 AWMC 的 `/qbind` 是两套用途。

## OneBot 检查点

- 只接受 `aiocqhttp` 平台的数字 QQ；跳过机器人自己发送的消息。
- 群聊遵守 `allowed_groups`；私聊遵守 `allow_private_chat` / `private_chat_whitelist`。
- 群聊二维码只提示自行撤回，不自动撤回；建议私聊提交。
- `u` 必须整条消息精确为 `u` / `导` / `更` / `更新`；群游戏进行中不触发。
- AWMC 模式下 `/b50` 不接受昵称、QQ、@ 或引用目标，避免绕过 QQ 身份查询他人。
- `/maiu` 的 `get-music` 会校验返回 `userId` 与当前绑定 `mai_uid` 一致；不一致拒绝上传。
- 首次绑定 `create_only`：不换绑、不跨 QQ 认领；一次性二维码不会在绑定后重复使用。
- 二维码只进入任务执行协程，不写入任务状态、接口响应或日志。
- 默认 AstrBot 唤醒前缀为 `/`；群聊需要 @ 机器人或唤醒词，私聊按 AstrBot 配置可能需要 `/`。

## 验证

```text
test_xf_bridge_api
test_kanade_bridge
test_awmc_v2_direct
test_account_machine_flow
test_qq_bind_app_namespace
test_one_time_upload_single_call
test_mysql_nonblocking
test_upload_fresh_qrcode
test_upload_bot_direct
test_upload_inline_billing
```
