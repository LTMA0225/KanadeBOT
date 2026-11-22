# 音游窝 QQ 机器人（AstrBot 插件）v3.17.0

> ⚠️ **使用声明**：本机器人仅供「音游窝」店内群内部使用。
> 本项目未获得任何其他组织、公司或个人的授权、许可或背书，亦与其不存在任何关联。

> 仓库：KanadeBOT · 群昵称：宵崎奏bot
>
> 基于 **AstrBot + NapCat** 的 QQ 群服务机器人：实时在店查询、进/离店自动播报、
> @bot 自助离店、QQ 绑定、时段价格查询、收藏表情、B50 查分、机台查询……
> 覆盖音游店日常运营的完整工具链。

## 运行方式（全部在本机）

整套机器人**全部跑在一台本机（Windows 桌面）上**：取数、渲染、缓存、解析、日志都不出本机，图片不经过任何第三方渲染站：

- 机器人框架：AstrBot 桌面版（≥ 4.13，当前生产环境 4.28.1），插件跑在同一个进程里；
- QQ 协议端：NapCat（OneBot v11，反向 WebSocket，只连 `127.0.0.1`）；
- 数据：站点统一 Bot API（`POST /api/botAction`，明钥鉴权、按 action 授权）；
- 图片：**本机渲染**——Jinja2 生成 HTML，Playwright 驱动本机浏览器内核截图
  （Windows 直接使用系统自带的 Edge），风格为**宵崎奏主题 + 萌感字体（站酷快乐体）**；
- 谱面与资料数据：水鱼查分器曲库（本地缓存 24 小时）+ 各 WIKI / 官网正文由本机抓取并解析；
- 可选接入 AWMC Bridge，直接使用发送者的 QQ 查分、同步成绩和开通 AWMC 档案；
- 机台查询（`/dx玩家`）使用本机保存的 maidx 证书（`.p12`）直连，证书只在本地解密；
- 电脑控制（shell / Python / OpenCode）同样是本机执行，只有授权操作员能触发。

会馆功能可在 Windows 本机运行。启用 AWMC 成绩功能时，共用 AWMC Bot 已有的服务端。

运行环境一览：

- 机器人框架：AstrBot 桌面版 ≥ 4.13（生产环境 4.28.1）
- QQ 协议端：NapCat（OneBot v11，反向 WebSocket，本机回环）
- 开发语言：Python 3.10+
- 配套网页端：[MioAoi/dcs-web](https://github.com/MioAoi/dcs-web)（音游窝网站，本机器人对接的业务系统）

## 功能总览

| 指令 | 说明 | 频率限制 |
|------|------|----------|
| `j`（默认 `/j`） | 通过统一 Bot API 获取在店人员，整理玩家 / 管理员 / 士大夫名单，发送「宵崎奏主题图片 + 文本」 | 同人同群 60 秒冷却；同群两次回复至少间隔 5 秒 |
| `25h`（默认 `/25h`） | 回复 `25時、ナイトコードで` | 不设冷却 |
| `jg`（默认 `/jg`） | 查询本时段每小时游玩价格（含折扣与封顶说明，价目表随站点自动更新） | 不设冷却 |
| `mj`（默认 `/mj`） | 发送 bot 账号收藏的表情包（本地缓存，QQ 链接过期也不影响） | 不设冷却；受同群最小间隔约束 |
| `mjlist` | 列出 bot 收藏的表情（编号 + 图片），用于确定 `mj_sticker_index` | 一次性设置用 |
| `bd`（默认 `/bd 验证码`） | QQ 绑定：把网站「个人信息」页的绑定验证码发到群里，机器人自动完成 QQ 绑定 | 每人 15 秒冷却 |
| `离店` | `@机器人 离店`：按 QQ 绑定自动离店结算（也支持 `/离店`）；**不支持机器人进店**（进店请在网站操作） | 每人 30 秒冷却 |
| `包场/包机` | `@机器人 包场 舞萌 10日14时~16时`：登记包场（群主/管理员）；包场时段群内 `/j` 只提示包场 | —— |
| `取消包场` | `@机器人 取消包场 [机台关键词]`：撤销包场登记（不带关键词=全部） | —— |
| `/b50`、`/B50` | 接入 AWMC Bridge 时查询本人 B50，沿用 AWMC 数据源和出图；未配置时使用本机水鱼 B50 | 每人默认 15 秒冷却 |
| `/qbind`、`/mai状态` | 用发送者 QQ 查看 AWMC 状态；首次默认同意协议、开通 AWMC 并直接创建 BBS 账号 | 需 AWMC Bridge / XF API Key |
| `u`、`/u` | 从已绑定的舞萌档案免码更新成绩 | 沿用 AWMC 冷却和配额 |
| `/mai绑定`、`/maiu`、`/查进度` | 首次绑定舞萌、使用二维码完整传分、查看各平台同步状态 | 同一 QQ 同时一个任务 |
| `/签到`、`/我的AWMC` | 每日签到、查看余额/连签/签到状态；群号会参与签到加成 | 签到沿用 AWMC 冷却与防重复 |
| `/日报`、`/周报`、`/月报`、`/年报` | 按存档生成成绩报表；需先开启数据存储 | 报表按 AWMC 计费规则 |
| `/开启存储数据` | 开启每日成绩存档（报表/趋势依赖） | 只开不关 |
| `/底力分析`、`/弱项处方`、`/b50风险预警`、`/含金量`、`/含水量`、`/我有多菜` | 现有 AWMC 分析图 | 与 AWMC 同计费 |
| `dx玩家`（默认 `/dx玩家`） | 用本机 maidx 证书查询机台在线人数（仅操作员私聊与指定群） | —— |
| `/bz QQ号 点数` | 为指定客户增点并显示余额（`bz_admins` 与群主可用） | —— |
| `/memory`、`/players` | 查看长期记忆 / 玩家档案；`/memory now` 立即汇总 | 管理员 |
| `/imgclean [now]` | 查看或立即执行图片缓存清理 | 管理员 |
| 过场台词 | `/j` 查询前会先说一句「等我吃完这口杯面帮你查哦…」等台词再出结果（台词池与停顿可配置） | —— |
| 待机语录 | 小奏本来只想发「……」时，按概率换成一句待机台词（可自定义/关闭） | 20% + 10 分钟冷却 |
| 联网搜索 | 需要最新信息（新闻、活动、价格、更新等）时自动联网搜索（按次计费） | —— |
| 萌娘百科 | 问到角色设定、作品背景、剧情或梗时自动查萌娘百科词条（搜索 + 本机解析正文） | —— |
| 音游资料 | CHUNITHM/中二节奏、maimai/舞萌DX 的角色与曲目资料：自动查对应 WIKI 与官网并抓正文 | —— |
| 谱面查询 | 难度、定数、物量、谱师：水鱼查分器曲库（本地缓存 24 小时），可附 v.wmc.pub 社区评分/标签 | —— |
| 夜间谱面问答 | 向指定群的 aquadx bot 逐条提问「XXX是什么歌？」，收集回复后在本机生成报告 | 夜间窗口内限速 |
| 长期记忆 | 自动记录群聊，每 12 小时在本机蒸馏一次（群规/常识/黑话），每次对话注入 | —— |
| 玩家档案库 | 群友的称呼/身份/常聊话题/性格画像（`players.json`）；`player_lookup` 按需查询，当前说话者档案自动注入 | —— |
| 电脑控制 | 授权操作员私聊可用本机 shell / Python / OpenCode；执行日志写在本机审计文件 | —— |
| （自动） | **进 / 离店播报**：玩家进出自动发到群里（离店含时长与扣费） | 见配置 |

图片与文本内容：

- 图片：**宵崎奏主题卡片**（背景为宵崎奏素材、字体为站酷快乐体）；
  **玩家**每位一个信息小框（昵称、入店时间、游玩分钟数）；**管理员 / 士大夫**单独一栏；
- 文本：`玩家：甲,乙`（英文逗号分隔）；有管理 / 士大夫时另起一行 `管理/STAFF：丙,丁`。

行为规则：

- **玩家数量为 0**（只有管理员 / 士大夫，或完全无人）时，回复固定文案：`店内无玩家，快来吧唧！`；
- 接口失败时只在群里给出通用提示（如「查询失败：站点暂时无法访问，请稍后再试。」），
  不暴露地址、明钥或异常细节；详细原因写入本机日志，并通过状态推送告知维护者；
- 图片渲染失败或超时时降级为文字版，插件不会崩溃。

## 统一 Bot API（数据来源）

站点在「高级管理 → Bot 管理」为机器人分配**明钥**与**权限**；机器人以

```
POST /api/botAction
Content-Type: application/json
{ "qqid": "<操作者QQ>", "key": "<Bot明钥>", "action": "<动作>", "payload": "<字符串>" }
```

调用实时数据。当前支持的动作：

| action | 用途 | 权限 |
|--------|------|------|
| `getPresentUsers` | 在店人员实时名单（查询与播报共用） | 必开 |
| `bindQq` | QQ 绑定校验 | 必开 |
| `purchase` / `getBalance` | 饮品购买 / 余额查询 | 站点侧筹备中（接口已预留） |

- 明钥只保存在本机插件配置（`bot_api_key`），不会出现在群聊或日志中；
- 站点返回 `{success:true,data}` 或 `{success:false,message}`，统一在插件内转为友好提示。

## 进 / 离店播报

开启 `presence_notify_enable` 后，插件按固定间隔轮询统一 Bot API 的在店名单并差分播报：

- 玩家进店：

  ```
  【Kanade】XXX进店了
  ```

- 玩家离店（竖向排版，时长为站点口径向上取整、扣费为站点计费规则金额）：

  ```
  【Kanade】XXX离店了
  游玩XXX分钟
  扣费X.XX元（线上余额）
  ```

实现要点：

- 轮询间隔默认 30 秒（`presence_check_interval_seconds`，范围 15-3600）；
- 离店结算时刻取「最后一次在店」与「本次发现」的中点（误差 ≤ 半个轮询周期）；
- 只播报玩家（管理员 / 士大夫不播报）；自助离店与轮询共用同一张去重表，不会重复播报；
- 播报目标见 `presence_targets`，留空时自动发到 `allowed_groups` 中配置的群；
- 站点计费表每 6 小时自动同步（`/chargecalc`），调价自动跟随；离店金额以网站实际扣费为准。

## @bot 自助离店（按 QQ 绑定自动结算）

玩家在群里发 **`@机器人 离店`**（或 `/离店`），机器人即用发送者的 QQ 完成离店结算：

- 在店名单来自统一 Bot API，按 **QQ 绑定（qqid）** 找到玩家账号；
- 调用站点现成的**工作人员代离店接口**完成结算，并从来店记录页读取**真实扣费金额**；
- **只支持离店**（不支持机器人进店，避免"人未到店就被远程进店"）；每人 30 秒冷却；
- 只影响发送者自己的账号；未绑定 QQ / 不在店 均有对应提示。

## 包场（包机）管理

群主 / 管理员（或配置 `booking_admins` 中的 QQ）在群里 **@机器人** 发送：

```
包场 舞萌 10日14时~16时
```

即可登记包场（也支持"包机"写法；日期可写 `X月X日` 或 `今天/明天/后天`，时间可写 `X时` 或 `X:XX`，支持跨零点如 `22时~2时`）。登记成功会回复确认。

- 在包场时间段内，群内 **`/j` 不再返回在店人数**，而是提示：

  ```
  【Kanade】本时段正有包场（包机）
  · 舞萌 10月10日 14:00~16:00
  （期间不提供在店人数查询）
  ```

- 另外开启 `site_booking_enable` 后，图片里也会画出站点包机页上正在或即将开始的时段；
- 误登记可撤销：`@机器人 取消包场`（全部）或 `@机器人 取消包场 舞萌`（按机台关键词）；
- 只允许群主 / 管理员操作；记录持久化保存，结束后自动过期清理。

## QQ 绑定（/bd 验证码）

1. 玩家在网站「个人信息」页填写要绑定的 QQ，页面会显示**绑定验证码**；
2. 玩家用该 QQ 在群里发送 `/bd 验证码`（也支持 `/bd验证码`，大小写不敏感）；
3. 机器人调用统一 Bot API 的 `bindQq` 动作自动完成绑定，并回复结果。

只使用发送者自己的 QQ 完成验证（只有 QQ 本人能绑定自己账号）；每人 15 秒冷却（`bind_cooldown_seconds`）。

## 时段价格查询（/jg）

发送 `/jg`，机器人回复**本时段每小时游玩价格**（价目表每 6 小时自动同步站点，站点调价自动跟随）：

```
【Kanade】当前时段价格
时段：10:00-22:00（白天4小时封顶）
每小时：¥10.50（原价 ¥15.00，7折）
```

## 收藏表情（/mj）

- `/mj`：发送 bot 账号收藏的表情包（自动读取 NapCat 收藏、下载到本地缓存，链接过期也不影响）；
- `/mjlist`：列出收藏表情与编号，用配置 `mj_sticker_index` 选择发送第几个。

## AWMC 查分与成绩同步

在 AWMC Bot 的群后台启用 Kanade Bridge 配置 `AWMC_KANADE_BRIDGE_SECRET`，并在 AstrBot 填入 `awmc_bridge_url`（群后台 HTTPS 地址）和同一把 `awmc_bridge_secret`。KanadeBot 通过现有论坛 Bridge 的签名和任务机制使用 AWMC Bot 的 B50、`u` 和 `maiu`。机台接口沿用服务端 `AWMC_API_MODE=team`。部署步骤见 AWMC Bot 仓库的 `docs/XF论坛桥接.md`。

- `/qbind` 或 `/mai状态` 查看状态。首次使用按默认同意记录 AWMC maiBot 统一用户协议、按当前 QQ 开通，并用 `QQ@qq.com` 自动查找/创建 BBS 账号（用户名优先取 QQ 昵称，取不到用 `maimai_<QQ>`）；不需要邮箱验证码。BBS 自动建号依赖 AWMC 的 `AWMC_XF_API_KEY`。发送 `/用户协议` 可查看文案。可通过 `/撤回用户协议` 撤回，或发送 `/不同意共享我的数据` 关闭脱敏共享。
- `/b50` 查询本人。出图、数据源偏好、BREAK 计费按 AWMC 规则执行。关闭 `b50_enable_image` 可查询文字摘要。
- `u` 或 `/u` 使用已绑定的舞萌档案免码更新 AWMCNET，以及已授权的水鱼、落雪。快速更新不包含 FC、AP、同步徽章和游玩次数。
- `/maiu` 使用最新舞萌二维码完整同步 AWMCNET 和已授权的水鱼。接受 SGWCMAID、官方二维码链接，或 QQ 原生图片。可在命令后附二维码，也可在同一会话按提示续发。未绑定舞萌账号时，首张码用于建档；之后可发 `/u` 或用一张新码发 `/maiu`。`/mai绑定` 可单独建档。二维码建议私聊提交，需允许该 QQ 的私聊。
- `/签到` 每日签到；`/我的AWMC` 查看余额、连签和签到状态。
- `/日报`、`/周报`、`/月报`、`/年报` 生成报表，先发 `/开启存储数据` 并在至少积累两次存档后使用。
- `/查进度` 或 `/查任务` 查看进度。已入队的上传渠道会显示同步状态。水鱼和落雪授权沿用 AWMC Bot 的 `dfbind` 和 `lxbind`。

会馆的 `/bd 验证码` 继续用于绑定离店账号。`awmc_bridge_url` 和 `awmc_bridge_secret` 都留空时，`/b50` 使用原有的水鱼接口和本机 Playwright 渲染，保留本机封面缓存及 `b50_allow_others` 查他人配置。

## 机台查询（/dx玩家）

- 用本机安装好的 maidx 证书（配置 `maidx_cert` 指向 `.p12` 绝对路径 + `maidx_cert_password`）直连查询；
- 证书只在本机解密成临时 pem，不外传；
- 只在操作员私聊与 `pc_control_group` 指定的群里响应，`dx_enable` 为总开关。

## 夜间谱面问答（aquiz）

- 只在 `aquiz_group` 指定群里运行、只在该群发言，其他群不受打扰；
- 在 `aquiz_start`~`aquiz_end` 时间窗内逐条向 aquadx bot 提问「XXX是什么歌？」；
- 收到回复后解析定数、物量等信息，结果、调试日志与报告都写在本机插件数据目录；
- 每条之间有最小间隔（`aquiz_interval_s`），首条无回复立即停止，避免刷屏。

## 安全设计

| 方面 | 做法 |
|------|------|
| Bot 明钥 | 仅保存在本机插件配置（密文项）；日志与群聊中绝不输出；权限按 action 由站点控制 |
| 站点凭据 | 账号密码与会话 Cookie **只经 HTTPS 发送**；明文 `http://` 地址只做匿名访问 |
| 备用线路 | 域名不通时直连备用 IP，但 SNI、Host 与证书校验仍使用域名，证书不符即放弃 |
| 重定向 | 手动跟随，只跟随同站点跳转，Cookie 不会被带到其他站点 |
| 图片渲染 | 模板自动转义 + 用户字段显式 `\|e`；页面禁用 JavaScript；拦截一切网络请求，只放行 `assets/` 下的本地素材 |
| 群锁 | 三层：AstrBot 白名单（`id_whitelist`）→ aiocqhttp 适配器补丁（群号 + 机器人号）→ 插件 `allowed_groups` |
| 账号保护 | 适配器补丁只接受机器人号的事件；适配器补丁发现登录的不是机器人号会自动停下 |
| 自助操作 | 离店 / 绑定只使用发送者自己的 QQ；冷却限频；只影响本人账号 |
| 管理接口 | AstrBot 面板与 NapCat WebUI 只监听 `127.0.0.1` |
| 密码保存 | 面板密码用 Windows DPAPI 加密保存在本机；站点密码在部署时输入；NapCat 令牌在部署时重新生成 |
| 状态推送 | 默认只在启动、站点异常 / 恢复时推送，外加每日心跳，降低协议号风控风险 |
| 电脑控制 | 白名单 QQ / 群 + 本机审计日志（`pc_audit.jsonl`）；执行前有超时上限 |

## 目录结构

```
astrbot_plugin_dcs_whosin/
├── main.py                   插件入口（指令、工具、静默策略、状态推送、依赖补装）
├── dcs_api.py                站点会话与线路辅助（登录缓存、HTTPS/备用 IP）
├── dcs_botapi.py             统一 Bot API 客户端（明钥鉴权、数据解析）
├── dcs_presence.py           进/离店播报与计费（差分、计费复刻、时段价格文案）
├── dcs_attend.py             @bot 自助离店（工作人员代离店 + 真实扣费读取）
├── dcs_bind.py               /bd QQ 绑定（验证码提交与结果文案）
├── dcs_mj.py                 /mj 收藏表情（收藏读取 + 本地缓存）
├── dcs_booking.py            包场（包机）登记与持久化
├── dcs_sitebooking.py        站点包机页读取（图片内嵌预告）
├── dcs_maidx.py              /dx玩家 机台在线查询（本机证书）
├── dcs_awmc.py               AWMC Bridge 客户端与舞萌二维码解析
├── dcs_b50.py                本机 /b50 查分（水鱼接口 + 本地封面缓存）
├── dcs_websearch.py          联网搜索与网页取文（多引擎回退）
├── dcs_vchart.py             v.wmc.pub 谱面只读客户端（社区评分/标签/评论）
├── dcs_render.py             本机图片渲染与缓存（Jinja2 + Playwright，独立线程）
├── templates/whosin.html     在店一览图片模板（HTML + CSS + Jinja2）
├── templates/b50.html        B50 成绩图模板
├── assets/                   本地素材：bg.jpg（宵崎奏）、logo.svg、fonts/（站酷快乐体，SIL OFL）
├── skills/                   随插件提供的 Skills（whosin-query、25h）
├── metadata.yaml             插件元数据
├── _conf_schema.json         可视化配置定义（WebUI 插件配置页）
├── requirements.txt          依赖清单（aiohttp、jinja2、playwright、cryptography）
└── deploy/                   仅作备选参考（Linux / Docker 想搬服务器时再看看）
```

运行时产生的所有数据（图片缓存、`b50_covers`、`players.json`、`bookings.json`、记忆文件、
`pc_audit.jsonl` 等）都在 AstrBot 的 `data/plugin_data/astrbot_plugin_dcs_whosin` 下，
插件目录本身可以只读。

## 部署（本机 Windows 桌面）

1. 安装 QQ 与 [AstrBot 桌面客户端](https://github.com/AstrBotDevs/AstrBot-desktop/releases)；
2. 把本插件文件夹复制到 AstrBot 数据目录的 `data/plugins/` 下；
3. 在 AstrBot WebUI「插件」页确认 `astrbot_plugin_dcs_whosin` 已加载并启用；
4. 在插件配置里填 **Bot 明钥**（`bot_api_key`）与机器人 QQ（`bot_qqid`），按需填群号白名单；
5. 插件首次启动会用 AstrBot 自带的 pip 安装器在本机补装 jinja2 / playwright（装好前只发文字版）；
6. 需要 B50 / 机台查询时，再补 `b50_*`、`maidx_*` 相关配置；
7. 群内发送 `/j` 测试。

全部步骤都在本机完成，不需要服务器；`deploy/` 下只剩可选的容器编排与 Linux 脚本，
本机桌面按上面步骤手动装好即可。

## NapCat 对接

1. 安装并启动 NapCat（[下载地址](https://github.com/NapNeko/NapCatQQ/releases)），登录机器人 QQ；
   把 NapCat `config/webui.json` 的 `host` 设为 `127.0.0.1`；
2. NapCat WebUI：**网络配置 → 新建 → WebSocket 反向**，URL 填 `ws://127.0.0.1:6199/ws`；
3. AstrBot WebUI：**消息平台 → 新增 → aiocqhttp（OneBot v11）**，反向 WS 端口 `6199`；
4. 群内发送 `/j` 测试。

## 配置项（本机 WebUI 插件配置页）

| 配置 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `bot_api_key` | string（密文） | 空 | 统一 Bot API 明钥（网站「高级管理 → Bot 管理」分配） |
| `bot_qqid` | string | 空 | 机器人 QQ 号（轮询调用统一接口时使用） |
| `target_urls` | list | `["https://dcstream.top/whosin"]` | 站点地址；统一 Bot API 使用同一站点的 `/api/botAction` |
| `fallback_ips` | list | `["47.116.47.191"]` | 域名访问失败时直连的备用 IP（证书仍按域名校验） |
| `timeout_seconds` | int | `10` | 单次请求超时（秒） |
| `cookie` | string（密文） | 空 | 站点 Cookie（计费表/来店记录页需要登录时使用） |
| `login_username` | string | 空 | 站点账号（读取计费表与代离店用） |
| `login_password` | string（密文） | 空 | 站点密码（自动登录用，会话失效自动重新登录） |
| `enable_image` | bool | `true` | 是否发送渲染图片（关闭后只发文本） |
| `cache_ttl_seconds` | int | `30` | 图片缓存时间（秒，0 = 不缓存） |
| `image_theme` | string | `cyberpunk-bw` | 图片模板主题（id 沿用历史名，实际为宵崎奏主题 + 站酷快乐体） |
| `render_browser` | string | `auto` | 浏览器内核：`auto`（Edge → Chrome → 自带 Chromium）/ `msedge` / `chrome` / `chromium` |
| `render_browser_path` | string | 空 | 自定义浏览器可执行文件路径（填写后优先使用） |
| `render_timeout_seconds` | int | `20` | 单次渲染超时（秒，含首次启动浏览器；超时改发文字版） |
| `cooldown_seconds` | int | `60` | `/j` 同人同群冷却（秒，0 = 不限制；`/25h` 不受限制） |
| `group_interval_seconds` | int | `5` | `/j` 同群回复最小间隔（秒，0 = 不限制） |
| `allowed_groups` | list | `[]` | 只响应的群号白名单（空 = 不限制） |
| `allow_private_chat` | bool | `false` | 是否响应私聊（适配器补丁会更早丢弃私聊） |
| `status_push_enable` | bool | `false` | 是否开启状态推送 |
| `status_push_mode` | string | `smart` | `smart`：启动、站点异常 / 恢复时推送 + 心跳；`interval`：每次检查都推送 |
| `status_push_interval_seconds` | int | `600` | 状态检查间隔（秒；`interval` 模式下即推送间隔） |
| `status_heartbeat_hours` | int | `24` | `smart` 模式的心跳间隔（小时，0 = 不发心跳） |
| `status_push_targets` | list | `[]` | 推送目标，格式 `napcat:FriendMessage:QQ号`（需是机器人号的好友） |
| `presence_notify_enable` | bool | `false` | 进/离店播报开关（只播报玩家） |
| `presence_check_interval_seconds` | int | `30` | 进/离店检查间隔（秒，范围 15-3600） |
| `presence_prefix` | string | `【Kanade】` | 播报消息前缀（第一条行首） |
| `presence_targets` | list | `[]` | 播报目标，格式 `napcat:GroupMessage:群号`；留空 = 自动用 `allowed_groups` |
| `presence_state_ttl_minutes` | int | `15` | 重启后快照保鲜时间（分钟内可续算，避免漏报） |
| `presence_min_gap_seconds` | int | `2` | 多条播报之间的最小发送间隔（秒） |
| `presence_pricing_url` | string | 空 | 计费表页面地址；留空 = 自动用站点的 `/chargecalc` |
| `presence_pricing_refresh_hours` | int | `6` | 计费表自动刷新间隔（小时） |
| `mj_enable` | bool | `true` | `/mj` 收藏表情指令开关 |
| `mj_sticker_index` | int | `0` | 使用收藏表情中的第几个（先用 `/mjlist` 查看编号） |
| `attend_enable` | bool | `true` | `@bot 离店` 自助离店开关（不支持机器人进店） |
| `attend_cooldown_seconds` | int | `30` | 自助离店冷却（秒/人） |
| `bind_cooldown_seconds` | int | `15` | `/bd` QQ绑定冷却（秒/人） |
| `booking_enable` | bool | `true` | 包场（包机）管理开关（包场时段 `/j` 改为包场提示） |
| `booking_admins` | list | `[]` | 允许登记/取消包场的额外 QQ（群主/管理员无需填写） |
| `query_quip_enable` | bool | `true` | 查询前的过场台词开关 |
| `query_quip_lines` | list | `[]` | 自定义过场台词（留空用内置台词池） |
| `query_quip_delay_ms` | int | `1500` | 台词与查询结果之间的停顿（毫秒，0=不停顿） |
| `bz_admins` | list | `["3565630243"]` | 可使用 `/bz` 增点的额外 QQ（本群群主自动可用） |
| `ltm_enable` | bool | `true` | 长期记忆开关（群聊记录 + 定时蒸馏 + 对话注入） |
| `ltm_interval_hours` | int | `12` | 长期记忆自动汇总间隔（小时） |
| `ltm_min_messages` | int | `30` | 至少积累多少条新消息才汇总 |
| `ltm_chunk_messages` | int | `300` | 分段汇总每段消息数（窗口内全部消息分段处理） |
| `ltm_max_items` | int | `120` | 长期记忆最多保留条数 |
| `ltm_inject_max_chars` | int | `3500` | 每次对话注入记忆的最大字符数 |
| `private_chat_whitelist` | list | `["2380600076"]` | 允许私聊使用机器人功能的 QQ（好友白名单） |
| `close_friends` | list | `["2380600076"]` | 管理员的亲密好友 QQ（对话中会识别并更亲切） |
| `player_db_inject` | bool | `true` | 把当前说话者的玩家档案注入对话 |
| `site_booking_enable` | bool | `true` | 在店一览图显示站点包机/包场预告 |
| `site_booking_ttl_seconds` | int | `120` | 包场预告缓存秒数 |
| `idle_quip_enable` | bool | `true` | 小奏只想发“……”时，偶尔换成一句待机语录（其余情况静默不发） |
| `idle_quip_lines` | list | `[]` | 自定义待机语录（留空使用内置语录池） |
| `idle_quip_rate` | int | `20` | 待机语录发送概率（%），其余情况静默不发送 |
| `idle_quip_cooldown_s` | int | `600` | 两条待机语录之间的最短间隔（秒） |
| `pc_control_enable` | bool | `true` | 电脑控制总开关（仅授权操作员私聊可用） |
| `pc_admin_qq` | string | `3565630243` | 允许使用电脑控制的 QQ（仅私聊） |
| `pc_control_group` | string | `1072977398` | 允许全员使用电脑控制的群号（只允许这个群） |
| `pc_control_group_admin` | string | `1030377150` | 仅限操作员本人使用的群号（此群其他人不可用） |
| `opencode_exe` | string | `C:\Users\Yoga-Slim-Pro-X\AppData\Roaming\npm\node_modules\opencode-ai\bin\opencode.exe` | OpenCode CLI 可执行文件路径 |
| `opencode_workdir` | string | 空 | OpenCode 默认工作目录（留空使用插件数据目录下的 opencode_workspace） |
| `opencode_timeout_s` | int | `300` | OpenCode 任务默认超时（秒） |
| `image_cleanup_enable` | bool | `true` | 自动清理旧图片缓存（QQ 原图 + AstrBot 临时图片） |
| `image_cleanup_days` | int | `7` | 保留最近多少天的图片缓存 |
| `image_cleanup_hour` | int | `4` | 每天几点执行清理（0-23，默认 4） |
| `image_cleanup_qq` | bool | `true` | 清理 QQ/NapCat 的旧原图缓存（Ori） |
| `image_cleanup_astrbot_temp` | bool | `true` | 清理 AstrBot 临时图片文件 |
| `aquiz_enable` | bool | `false` | 夜间谱面问答任务：向指定群的 aquadx bot 询问曲目别名与谱面信息（只在目标群发送） |
| `aquiz_group` | string | `1129978061` | 谱面问答目标群号 |
| `aquiz_start` | string | `23:10` | 夜间任务开始时间（HH:MM） |
| `aquiz_end` | string | `06:30` | 夜间任务结束时间（HH:MM） |
| `aquiz_interval_s` | int | `90` | 两次提问之间的最小间隔（秒） |
| `aquiz_reply_wait_s` | int | `40` | 等待 aquadx 回复的秒数 |
| `aquiz_max` | int | `30` | 本次任务最多提问数量 |
| `main_admin_qq` | string | `3565630243` | 唯一管理员/老板的 QQ（每次对话都会作为权威事实注入，避免认错人） |
| `ops_admin_qqs` | list | `["3565630243"]` | 管理员/维护者 QQ（不是店主；店主=群主，自动识别） |
| `maidx_cert` | string | 空 | maidx 机台管理证书文件（.p12）的绝对路径 |
| `maidx_cert_password` | string | 空 | maidx 证书的安装密码（下载页上给的那个） |
| `dx_enable` | bool | `false` | /dx玩家 机台信息查询总开关 |
| `awmc_bridge_url` | string | 空 | AWMC 群后台 HTTPS 地址 |
| `awmc_bridge_secret` | string（密文） | 空 | Kanade 专用桥接密钥，与 AWMC 端一致 |
| `awmc_bridge_allow_insecure_http` | bool | `false` | 允许公网 HTTP Bridge；二维码会明文传输，仅临时排查 |
| `b50_enable` | bool | `true` | /b50 查分总开关 |
| `b50_enable_image` | bool | `true` | /b50 是否出图片 |
| `b50_render_interval_seconds` | int | `15` | /b50 图片渲染最小间隔（秒/人，排队） |
| `b50_cooldown_seconds` | int | `15` | /b50 同一用户冷却（秒） |
| `b50_cache_ttl_seconds` | int | `120` | /b50 图片缓存秒数（同一分钟同成绩复用） |
| `b50_allow_others` | bool | `false` | 允许用 /b50 昵称 /QQ号 查别人的成绩 |
| `b50_qq_min_length` | int | `5` | 把参数当 QQ 号处理的最短位数 |
| `vchart_api_key` | string | 空 | v.wmc.pub 谱面平台只读 API Key（mk_live_ 开头，留空用内置兜底钥匙） |
| `vchart_enable` | bool | `true` | chart_lookup 时附带 v.wmc.pub 社区评分/标签/评论（只读） |
| `presence_confirm_seconds` | int | `45` | 进/离店二次确认时间（秒） |
| `presence_flap_merge_seconds` | int | `180` | 进店/离店重复播报合并窗口（秒） |
## 图片渲染说明

- 渲染在本机完成：Jinja2（`autoescape=True`）生成 HTML → Playwright 无头截图（视口宽 1080，高度随内容延展）；
- Playwright 运行在插件自己的线程与事件循环里，不受 AstrBot 主循环影响；浏览器空闲 5 分钟自动关闭；
- 页面里的背景、Logo、字体都经虚拟地址 `https://dcs-local.invalid/assets/` 从插件 `assets/` 目录读取，
  其余任何网络请求都会被拦截；
- 相同数据同一分钟内复用缓存；图片保存在 AstrBot 的 `data/plugin_data/astrbot_plugin_dcs_whosin/image_cache`
  （超过 15 分钟自动清理），插件目录可以只读。
## 版本历史

| 版本 | 里程碑 |
|------|--------|
| v2.0 | 本机渲染（Playwright + Edge）替换第三方 T2I 服务；Windows 本机部署 |
| v2.1 | 进/离店播报；竖向排版、时长向上取整；`/jg` 时段价格 |
| v2.2 | 访问记录流通道（无盲区、真实结算扣费） |
| v2.3 | Webhook 实时接收服务；统一去重表 |
| v2.4 | `/mj` 收藏表情（含本地缓存与预取） |
| v2.5 | `@bot` 自助离店（双通道；仅离店、防止远程进店） |
| v2.6 | 纯机器人兜底完善；通道自动切换 |
| v2.7 | `/bd` QQ 绑定（验证码提交自动完成绑定） |
| **v3.0** | **接入站点统一 Bot API（`getPresentUsers`/`bindQq`）；移除页面抓取、Webhook 与记录流旧通道；项目介绍更新** |
| **v3.1** | **包场（包机）管理：@bot 登记/取消，包场时段 `/j` 改为包场提示** |
| **v3.2** | **查询过场台词：`/j` 先发一句人设台词再出结果（台词池与停顿可配置）** |
| **v3.3** | **接入联网搜索（`web_search` 工具），可查询实时信息** |
| **v3.3.1** | **修复联网搜索取密钥失败（改用服务商实例读取）；完善搜索判断规则** |
| **v3.4** | **新增萌娘百科词条查询（`moegirl_lookup`：搜索 + 正文抓取）** |
| **v3.5** | **新增音游资料查询（`rhythm_lookup`）与谱面查询（`chart_lookup`）；提示词自然化、允许长回复、强化反幻觉** |
| **v3.6** | **新增 `/bz` 增点（仅管理员/群主）；小奏记忆管理员 QQ** |
| **v3.6.1** | **修复管理员认错人：每次请求注入权威管理员事实与当前对话者身份** |
| **v3.7** | **长期记忆：群聊记录 + 定时蒸馏 + 每对话注入；`/memory` 管理** |
| **v3.7.1** | **长期记忆改为分段汇总：窗口内所有消息均分段处理，不再只取最新 300 条** |
| **v3.8** | **私聊白名单（好友可用私聊指令）；亲密好友识别注入；记忆容量提升（120 条 / 注入 3500 字）** |
| **v3.8.1** | **长期记忆合并规则收紧（非明显过时不再删除旧条目）** |
| **v3.8.2** | **修复私信无法到达：适配器补丁放行私信白名单好友；新增收到私聊日志** |
| **v3.9** | **玩家档案库：群友画像（称呼/话题/性格）；`player_lookup` 工具、当前说话者档案注入、`/players` 查询** |
| **v3.9.1** | **强化说话者身份绑定：群名片+QQ 明确当前对话者，「我是谁」只按该身份回答** |
| **v3.9.2** | **长期记忆干净化：只保留全群通用知识，个人称呼/性格等不再入库（改由玩家档案库负责）** |
| **v3.9.3** | **说话者身份写入用户消息（[说话者：群名片(QQ)] 前缀），并重置被错误历史污染的群会话** |
| **v3.10** | **玩家档案补充站点身份；自动识别群主为店主并写回配置；区分管理员/维护者与店主** |
| **v3.11** | **店员/店主名单注入对话；`player_lookup` 支持按身份查询；新增 `remember` 工具** |
| **v3.11.1** | **身份问题改为当轮消息注入名单/档案，重置群会话，修复长历史导致沿用旧回答** |
| **v3.12.0** | **待机语录：小奏只想发「……」时自动换成一句待机台词（可自定义）** |
| **v3.12.1** | **待机语录降低频率：默认 20% 概率发送、10 分钟冷却，其余静默不在群里刷屏** |
| **v3.13.0** | **私聊注入长期记忆与对方档案；新增群聊记录检索工具；记忆汇总保留游戏/角色知识** |
| **v3.14.0** | **夜间谱面问答：向指定群的 aquadx bot 发送「XXX是什么歌？」并收集回复，生成报告** |
| **v3.14.1** | **谱面问答加固：插件重载取消任务、首条无回立即停、修复定数读取、群消息调试日志** |
| **v3.15.0** | **读图修复：适配器优先使用 NapCat 本地图片文件，解决 QQ 图片链接过期导致读图空白** |
| **v3.15.1** | **图片缓存自动清理：默认保留 7 天、每天 4:00 清理 QQ 原图与 AstrBot 临时图片，支持 `/imgclean now`** |
| **v3.16.0** | **电脑控制：授权操作员私聊可用本机 shell/Python 与 OpenCode 桥接；执行日志审计** |
| **v3.16.1** | **电脑控制扩展到指定群 1072977398（全员可用），其他群/私聊仍禁止** |
| **v3.16.2** | **会馆群 1030377150 仅操作员本人可用电脑控制；该群其他人调用一律拒绝** |
| **v3.17.0** | **本地化收口：`/b50` 查分图、`/dx玩家` 机台查询、v.wmc.pub 谱面社区评分全部改由本机渲染与直连；README 全篇改为本机部署口径；删掉 Windows 一键脚本 / watchdog / 打包辅助，`deploy/` 只留可选容器与 Linux 脚本** |

## 常见问题

1. **查询提示「机器人密钥无效 / 未配置」？**
   在插件配置中填写 `bot_api_key`（网站「高级管理 → Bot 管理」分配，注意重置后旧明钥立即失效）。
2. **查询提示「机器人缺少该功能权限」？**
   到网站的 Bot 管理页勾选对应权限（`getPresentUsers`、`bindQq`）后保存。
3. **只发文字、不出图？**
   看 AstrBot 日志：提示缺少依赖时，在插件页为本插件安装依赖后重载；提示找不到浏览器时，
   在 `render_browser_path` 填写 Edge / Chrome 的路径；渲染超时可调大 `render_timeout_seconds`。
4. **图片里的中文或日文显示为方块？**
   本机已自带字体，一般不会方块；真出现就在 `assets/fonts/` 里补字体文件。
5. **发送 `j` 没反应？**
   AstrBot 默认唤醒前缀为 `/`，请发送 `/j`；并确认群号在 AstrBot 白名单与 `allowed_groups` 中。
6. **`jg` 价格不对或取不到？**
   时段价格来自站点 `/chargecalc`，需要 `login_username` / `login_password`（或 Cookie）可登录；每 6 小时自动刷新。
7. **升级 AstrBot 后，状态推送提示「适配器补丁未检测到」？**
   按补丁步骤在本机重新打一次适配器补丁（打完后插件启动会自检），再重启 AstrBot。
   在此之前 AstrBot 白名单仍只放行目标群，其他人进不来。

## 开发与调试

- 修改代码后，在 AstrBot WebUI 的「插件」页点击插件卡片的刷新图标（热重载）；
- 插件运行日志请在 AstrBot 日志中查看，关键字：`[dcs_whosin]`；
- 统一 API 数据解析可离线测试：`python -c "from dcs_botapi import parse_present_users; print(parse_present_users([{'id':1,'nickname':'测试','role':'CUSTOMER','enteredAt':'2026-10-05T04:29:21.554Z'}]))"`。

## 灵感与素材来源

- 业务数据与视觉资产来自音游窝网页端 [MioAoi/dcs-web](https://github.com/MioAoi/dcs-web)；
- 字体：站酷快乐体（ZCOOL KuaiLe），SIL Open Font License 1.1，见 `assets/fonts/OFL.txt`；
- 插件开发遵循 [AstrBot 官方插件开发文档](https://docs.astrbot.app/dev/star/plugin-new.html)，
  Skill 采用 [Anthropic Skills](https://code.claude.com/docs/zh-CN/skills) 规范。
