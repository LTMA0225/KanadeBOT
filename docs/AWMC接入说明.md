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

## 现网 AWMC Bot 核对（2026-11-22）

从现网 `milk-new:/www/bot/awmc/.env` 读取到的非敏感配置：

```text
AWMC_API_MODE=team
AWMC_API_BASE_URL=http://ipv4-beta.kxcym.top:25800
AWMCNET_SYNC_URL=http://net.wmc.pub:18780
MAIMAIDX_GROUP_CONSOLE_ENABLED=true
MAIMAIDX_GROUP_CONSOLE_PUBLIC_URL=https://group.wmc.pub/group-console
MAIMAIDX_GROUP_CONSOLE_HOST=0.0.0.0
MAIMAIDX_GROUP_CONSOLE_PORT=8100
AWMC_XF_BRIDGE_ENABLED=true
AWMC_XF_BRIDGE_SECRET=已设置（不要复用）
AWMC_KANADE_BRIDGE_SECRET=当前为空（需新增）
```

- 当前生产 HEAD：`5010eef4`（Kanade Bridge 在 PR #186，尚未部署）。
- 群后台本机 `http://127.0.0.1:8100/group-console/` 返回 200，`maimaidx-bot.service` active。
- 因此 Kanade 的 `awmc_bridge_url` 填 `https://group.wmc.pub`，不要带 `/group-console`。
- 如果 Kanade 主机无法访问域名，可在 Kanade 主机建立 SSH 隧道：`ssh -p 22002 -L 18100:127.0.0.1:8100 milk@ipv4-beta.kxcym.top`，然后临时填 `awmc_bridge_url=http://127.0.0.1:18100`。
- 如必须直接访问 `http://ipv4-beta.kxcym.top:8100`，还要显式打开 `awmc_bridge_allow_insecure_http=true`。这会明文传输二维码等凭据，只建议临时排查，排查完改回 HTTPS。

## 基础指令

| 指令 | 作用 | 说明 |
|---|---|---|
| `/b50`、`b50` | 查询本人 B50 | AWMC 开启时只查发送者本人；不出图时按 `b50_enable_image` 返回文字 |
| `u`、`/u` | 免二维码更新成绩 | 需已绑定 `mai_uid`；更新 AWMCNET 及已授权水鱼/落雪；受 AWMC 冷却和配额限制 |
| `/maiu [SGWCMAID/官方链接/图片]` | 二维码完整传分 | `maiu` 上传 AWMCNET + 水鱼；首次使用会先建档；一次性码用完后需换新码 |
| `/mai绑定 [SGWCMAID/官方链接/图片]` | 仅绑定舞萌账号 | 首次绑定会消耗二维码；绑定后可直接 `/u`，或再发新码 `/maiu` |
| `/签到`、`/我的AWMC` | 每日签到、余额/连签/签到状态 | 群号参与签到加成；沿用 AWMC 冷却 |
| `/日报`、`/周报`、`/月报`、`/年报` | 成绩报表 | 需先 `/开启存储数据`，且至少两次存档 |
| `/开启存储数据` | 开启每日存档 | 报表/趋势依赖，只开不关 |
| `/底力分析`、`/弱项处方`、`/b50风险预警`、`/含金量`、`/含水量`、`/我有多菜` | 成绩分析图 | 与 AWMC 同规则 |
| `/qbind`、`/mai状态`、`/mai账号` | 查看 AWMC 与 BBS 状态 | 首次使用默认同意协议、按 QQ 创建档案，并自动查找/创建 BBS 账号；不需要邮箱验证码 |
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
- 签到目前走 Bridge 预检签到，不弹图形验证码；群号会传入以保留群签到加成。
- 默认 AstrBot 唤醒前缀为 `/`；群聊需要 @ 机器人或唤醒词，私聊按 AstrBot 配置可能需要 `/`。
- BBS 自动建号使用 OneBot 已认证的 QQ 和 `QQ@qq.com`，用户名优先 QQ 昵称，否则 `maimai_<QQ>`；依赖 `AWMC_XF_API_KEY`，失败不影响 AWMC/AWMCNET。

## 配置步骤

1. 先合入并部署 AWMC Bot 的 PR #186；确认 `maimaidx-bot.service` 重启后群后台仍可用。
2. 在 AWMC 主机生成独立密钥：`openssl rand -hex 32`；写入 `.env` 的 `AWMC_KANADE_BRIDGE_SECRET`，**不要**复用 `AWMC_XF_BRIDGE_SECRET`。
3. 同文件确认 `AWMC_KANADE_BRIDGE_AUTO_AGREE=true`；`AWMC_KANADE_BRIDGE_ALLOW_IPS` 可先留空，稳定后填入 KanadeBot 出口 IP。
4. 重启 AWMC Bot，使群后台加载 Kanade 路由。
5. 在 AstrBot 插件配置填 `awmc_bridge_url=https://group.wmc.pub`，`awmc_bridge_secret` 填第 2 步生成的同一密钥；确认 `b50_enable=true`。
6. 在 AstrBot 插件页重载 Kanade 插件/安装 `Pillow`、`zxing-cpp` 依赖；NapCat 私聊需允许 `allow_private_chat` 或加入白名单。
7. 测试顺序：`/qbind` → `/b50` → `/mai绑定`（或 `/maiu`）→ `/u`。

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
