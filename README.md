# 直流会馆 QQ 查询机器人（AstrBot 插件）v2.2.0

基于 **AstrBot + NapCat** 的 QQ 群机器人：查询「直流会馆」店内实时在店人员，
以**赛博朋克风图片 + 文本**返回，附带娱乐指令。

- 机器人框架：AstrBot ≥ 4.13（迁移包内为 4.28.1 桌面版）
- QQ 协议端：NapCat（OneBot v11，反向 WebSocket）
- 开发语言：Python 3.10+
- 图片渲染：**本机渲染**——Jinja2 生成 HTML，Playwright 驱动本机浏览器内核截图
  （Windows 默认用系统自带的 Edge），不调用任何第三方或社区渲染服务

## 功能

| 指令 | 说明 | 频率限制 |
|------|------|----------|
| `j`（默认 `/j`） | 抓取 `/whosin` 页面，整理玩家 / 管理员 / 士大夫名单，发送「图片 + 文本」 | 同人同群 60 秒冷却；同群两次回复至少间隔 5 秒（并发查询依次排队） |
| `25h`（默认 `/25h`） | 回复 `25時、ナイトコードで` | 不设冷却 |
| `jg`（默认 `/jg`） | 查询本时段每小时游玩价格（含折扣与封顶说明，价目表随站点自动更新） | 不设冷却 |
| `mj`（默认 `/mj`） | 发送 bot 账号收藏的表情包（本地缓存，QQ 链接过期也不影响） | 不设冷却；受同群最小间隔约束 |
| `mjlist` | 列出 bot 收藏的表情（编号 + 图片），用于确定 `mj_sticker_index` | 一次性设置用 |
| `离店` | `@机器人 离店`：按 QQ 绑定自动离店结算（也支持 `/离店`）；**不支持机器人进店**（进店请在网站操作） | 每人 30 秒冷却 |

图片与文本内容：

- 图片：赛博朋克风；**玩家**每位一个信息小框（昵称、入店时间、已游玩分钟数）；**管理员 / 士大夫**仅显示昵称与在店状态；
- 文本：`玩家：甲,乙`（英文逗号分隔）；有管理 / 士大夫时另起一行 `管理/STAFF：丙,丁`。

行为规则：

- **玩家数量为 0**（只有管理员 / 士大夫，或完全无人）时，回复固定文案：`店内无玩家，快来吧唧！`；
- 抓取失败时只在群里给出通用提示（如「查询失败：站点暂时无法访问，请稍后再试。」），
  不暴露地址、IP 或异常细节；详细原因写入 AstrBot 日志，并通过状态推送告知维护者；
- 图片渲染失败或超时时降级为文字版，插件不会崩溃。

## 进/离店播报

开启 `presence_notify_enable` 后，插件按固定间隔轮询站点，**自动在群里播报**：

- 玩家进店：

  ```
  【Kanade】XXX进店了
  ```

- 玩家离店（竖向排版）：

  ```
  【Kanade】XXX离店了
  游玩XXX分钟
  扣费X.XX元（线上余额）
  ```

- 游玩时长**向上取整**（不足 1 分钟按 1 分钟计）；扣费按站点计费规则精确到分；前缀可在 `presence_prefix` 配置；

实现要点（三种通道，自动选择、统一去重）：

- **Webhook 实时通道（最快，可选）**：站点配置 `DCS_WEBHOOK_URLS` 后，进店/离店事件实时 POST
  到本插件内置的接收端口（默认 `0.0.0.0:8765/webhook`，令牌校验头 `X-DCS-Token`）；
  需要站点服务器能访问到本机（局域网 / 端口映射 / frp / SSH 反向隧道）。健康检查：`GET /health`。
- **访问记录流（推荐，无盲区兜底）**：读取站点 `/api/visits/recent` 接口（只读、工作人员会话可访问），
  数据库里每一次进离店都有记录，任意短的访问都不会漏报；扣费为站点真实结算金额。
- **在店快照模式（最终兜底）**：以上通道都不可用时，解析 `/whosin` 内嵌数据按用户差分判定。
- 三种通道共用同一张去重表，任何事件只播报一次；更高优先级的通道可用后自动切换，无需重启。
- 只播报玩家（管理员 / 士大夫不播报）；去重表保留 24 小时；
- 播报目标见 `presence_targets`，留空时自动发到 `allowed_groups` 中配置的群。

> 站点端接口文件：`app/api/visits/recent/route.ts`（随站点下次构建部署即可生效）。

## 时段价格查询（/jg）

发送 `/jg`，机器人回复**本时段每小时游玩价格**（价目表每 6 小时自动同步站点 `/chargecalc`，站点调价自动跟随）：

```
【Kanade】当前时段价格
时段：10:00-22:00（白天4小时封顶）
每小时：¥10.50（原价 ¥15.00，7折）
```

## @bot 自助离店（纯机器人实现，无需站点更新）

玩家在群里发 **`@机器人 离店`**（或 `/离店`），机器人即用发送者的 QQ 完成离店结算：

- 原理：机器人从 `/whosin` 在店数据里按 **QQ 绑定（qqid）** 找到玩家账号，
  调用站点已有的**工作人员代离店接口**完成结算，并从来店记录页读取**真实扣费金额**；
- **纯机器人实现**：网站不做任何改动即可使用（进店仍需在网站上操作）；
- 若网站后续部署了机器人接口（见交付包，可选），会自动作为兜底通道启用（错误提示更精确）；
- 只支持离店（不支持机器人进店）；每人 30 秒冷却；只影响发送者自己的账号；
- 未绑定 QQ / 不在店 都会给出对应提示；自助离店写入统一去重表，播报不会重复。

## 安全设计

| 方面 | 做法 |
|------|------|
| 站点凭据 | 账号密码与会话 Cookie **只经 HTTPS 发送**；明文 `http://` 地址只做匿名访问 |
| 备用线路 | 域名不通时直连备用 IP，但 SNI、Host 与证书校验仍使用域名，证书不符即放弃 |
| 重定向 | 手动跟随，只跟随同站点跳转，Cookie 不会被带到其他站点 |
| 图片渲染 | 模板自动转义 + 用户字段显式 `\|e`；页面禁用 JavaScript；拦截一切网络请求，只放行 `assets/` 下的本地素材 |
| 群锁 | 三层：AstrBot 白名单（`id_whitelist`）→ aiocqhttp 适配器补丁（群号 + 机器人号）→ 插件 `allowed_groups` |
| 账号保护 | 适配器补丁只接受机器人号的事件；控制台发现登录的不是机器人号会自动停下 |
| 管理接口 | AstrBot 面板与 NapCat WebUI 只监听 `127.0.0.1` |
| 密码保存 | 迁移包不含密码：面板密码由控制台用 Windows DPAPI 加密保存在本机；站点密码在部署时输入；NapCat 令牌在部署时重新生成 |
| 状态推送 | 默认只在启动、站点异常 / 恢复时推送，外加每日心跳，降低协议号风控风险 |

## 目录结构

```
astrbot_plugin_dcs_whosin/
├── main.py                   插件入口（指令、静默策略、状态推送、依赖补装）
├── dcs_api.py                页面抓取与解析（aiohttp + 正则；HTTPS 与备用 IP 线路）
├── dcs_render.py             本地渲染与缓存（Jinja2 + Playwright，独立线程运行）
├── dcs_presence.py           进/离店播报（内嵌数据解析、计费复刻、状态差分）
├── templates/whosin.html     图片模板（HTML + CSS + Jinja2）
├── assets/                   本地素材：bg.jpg、logo.svg、fonts/（站酷快乐体，SIL OFL）
├── skills/                   随插件提供的 Skills（whosin-query、25h）
├── metadata.yaml             插件元数据
├── _conf_schema.json         可视化配置定义（WebUI 插件配置页）
├── requirements.txt          依赖清单（aiohttp、jinja2、playwright）
└── deploy/                   Linux 一键脚本与 Docker 编排
    ├── linux_install.sh
    ├── docker-compose.yml
    └── Dockerfile.astrbot
```

Windows 运维脚本（一键部署、控制台、看门狗、适配器补丁）在迁移包的「控制台\DCS机器人开关」目录。

## 部署

### 方式 A：Windows 迁移包（推荐，当前生产环境）

按迁移包根目录「0.迁移指南（先看我）.txt」操作：安装 QQ 与 AstrBot → 双击「1.一键部署」→
桌面「DCS机器人开关」选 [1] 上线。一键部署会把 NapCat 与控制台装到 `%LOCALAPPDATA%\DCSBot`，
并完成插件复制、AstrBot 补丁、看门狗计划任务与桌面快捷方式。

### 方式 B：Linux / macOS 一键脚本

```bash
chmod +x deploy/linux_install.sh
./deploy/linux_install.sh
```

### 方式 C：Docker Compose（服务器）

```bash
cd deploy
docker compose up -d --build
```

- 管理面板 `6185`、NapCat WebUI `6099` 只绑定宿主机 `127.0.0.1`，远程管理请用 SSH 隧道；
- `6199` 只在容器网络内开放，NapCat 反向 WS 填 `ws://astrbot:6199/ws`；
- `Dockerfile.astrbot` 在官方镜像上加装 Playwright Chromium 与中日文字体（插件配置 `render_browser` 选 `chromium`）。

### 方式 D：手动部署（桌面客户端）

1. 安装 [AstrBot 桌面客户端](https://github.com/AstrBotDevs/AstrBot-desktop/releases)；
2. 将本插件文件夹复制到 AstrBot 数据目录的 `data/plugins/` 下；
3. 在 AstrBot WebUI「插件」页确认 `astrbot_plugin_dcs_whosin` 已加载并启用；
4. 插件首次启动会用 AstrBot 自带的 pip 安装器在后台补装 jinja2 / playwright（装好前只发文字版）。

## NapCat 对接（手动部署时）

1. 安装并启动 NapCat（[下载地址](https://github.com/NapNeko/NapCatQQ/releases)），登录机器人 QQ；
   建议把 NapCat `config/webui.json` 的 `host` 设为 `127.0.0.1`；
2. NapCat WebUI：**网络配置 → 新建 → WebSocket 反向**，URL 填 `ws://127.0.0.1:6199/ws`；
3. AstrBot WebUI：**消息平台 → 新增 → aiocqhttp（OneBot v11）**，反向 WS 端口 `6199`；
4. 群内发送 `/j` 测试。

## 配置项（WebUI 插件配置页）

| 配置 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `target_urls` | list | `["https://dcstream.top/whosin"]` | 目标地址，按顺序尝试；明文地址不会携带凭据 |
| `fallback_ips` | list | `["47.116.47.191"]` | 域名访问失败时直连的备用 IP（证书仍按域名校验） |
| `timeout_seconds` | int | `10` | 单次请求超时（秒） |
| `cookie` | string（密文） | 空 | 站点 Cookie（优先于账号密码方式） |
| `login_username` | string | 空 | 站点账号（自动登录用） |
| `login_password` | string（密文） | 空 | 站点密码（自动登录用，会话失效自动重新登录） |
| `enable_image` | bool | `true` | 是否发送渲染图片（关闭后只发文本） |
| `cache_ttl_seconds` | int | `30` | 图片缓存时间（秒，0 = 不缓存） |
| `image_theme` | string | `cyberpunk-bw` | 图片模板主题 |
| `render_browser` | string | `auto` | 浏览器内核：`auto`（Edge → Chrome → 自带 Chromium）/ `msedge` / `chrome` / `chromium` |
| `render_browser_path` | string | 空 | 自定义浏览器可执行文件路径（填写后优先使用） |
| `render_timeout_seconds` | int | `20` | 单次渲染超时（秒，含首次启动浏览器；超时改发文字版） |
| `cooldown_seconds` | int | `60` | `/j` 同人同群冷却（秒，0 = 不限制；`/25h` 不受限制） |
| `group_interval_seconds` | int | `5` | `/j` 同群回复最小间隔（秒，0 = 不限制） |
| `allowed_groups` | list | `[]` | 只响应的群号白名单（空 = 不限制） |
| `allow_private_chat` | bool | `false` | 是否响应私聊（与群白名单互不影响；注意适配器补丁会更早丢弃私聊） |
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
| `presence_visits_url` | string | 空 | 访问记录接口地址；留空 = 自动用站点的 `/api/visits/recent` |
| `presence_record_fresh_minutes` | int | `10` | 记录流播报保鲜时间（分钟；更早的记录只去重不播报） |
| `webhook_enable` | bool | `false` | Webhook 接收服务开关 |
| `webhook_bind` | string | `0.0.0.0` | 监听地址（隧道/端口映射用 `0.0.0.0`；仅本机用 `127.0.0.1`） |
| `webhook_port` | int | `8765` | 监听端口 |
| `webhook_secret` | string（密文） | 空 | 令牌；校验请求头 `X-DCS-Token`（与站点 `.env` 的 `DCS_WEBHOOK_TOKEN` 一致） |
| `mj_enable` | bool | `true` | `/mj` 收藏表情指令开关 |
| `mj_sticker_index` | int | `0` | 使用收藏表情中的第几个（先用 `/mjlist` 查看编号） |
| `attend_enable` | bool | `true` | `@bot 离店` 自助离店开关（不支持机器人进店） |
| `attend_cooldown_seconds` | int | `30` | 自助离店冷却（秒/人） |

## 图片渲染说明

- 渲染在本机完成：Jinja2（`autoescape=True`）生成 HTML → Playwright 无头截图（视口宽 1080，高度随内容延展）；
- Playwright 运行在插件自己的线程与事件循环里，不受 AstrBot 主循环影响；浏览器空闲 5 分钟自动关闭；
- 页面里的背景、Logo、字体都经虚拟地址 `https://dcs-local.invalid/assets/` 从插件 `assets/` 目录读取，
  其余任何网络请求都会被拦截；
- 相同数据同一分钟内复用缓存；图片保存在 AstrBot 的 `data/plugin_data/astrbot_plugin_dcs_whosin/image_cache`
  （超过 15 分钟自动清理），插件目录可以只读。

## 常见问题

1. **查询提示「站点需要登录」或「站点登录失败」？**
   在插件配置中填写 `login_username` / `login_password`（或浏览器里的完整 Cookie）。详细原因见 AstrBot 日志（关键字 `[dcs_whosin]`）。
2. **只发文字、不出图？**
   看 AstrBot 日志：提示缺少依赖时，在插件页为本插件安装依赖后重载；提示找不到浏览器时，
   在 `render_browser_path` 填写 Edge / Chrome 的路径；渲染超时可调大 `render_timeout_seconds`。
3. **图片里的中文或日文显示为方块？**
   Linux / Docker 需安装中文字体（如 `fonts-noto-cjk`）；Windows 自带字体即可。
4. **发送 `j` 没反应？**
   AstrBot 默认唤醒前缀为 `/`，请发送 `/j`；并确认群号在 AstrBot 白名单与 `allowed_groups` 中。
5. **抓取到的人数为 0 但实际有人？**
   站点页面结构可能已变化。解析基于 class 中独立的 `nickname` 词元与角色词元
   （`staff` / `admin` / `sponsor`），如站点改版请参照 `dcs_api.py` 调整。
6. **升级 AstrBot 后，状态推送提示「适配器补丁未检测到」？**
   在控制台选 [4] 重新打补丁，再重启 AstrBot。在此期间 AstrBot 白名单仍只放行会馆群。

## 开发与调试

- 修改代码后，在 AstrBot WebUI 的「插件」页点击插件卡片的刷新图标（热重载）；
- 插件运行日志请在 AstrBot 日志中查看，关键字：`[dcs_whosin]`；
- 解析逻辑可离线测试：`python -c "from dcs_api import parse_whosin_html; print(parse_whosin_html('<span class=\"nickname staff\">测试</span>'))"`。

## 灵感与素材来源

- 业务数据与视觉资产来自「直流会馆」项目（dc-web）；
- 字体：站酷快乐体（ZCOOL KuaiLe），SIL Open Font License 1.1，见 `assets/fonts/OFL.txt`；
- 插件开发遵循 [AstrBot 官方插件开发文档](https://docs.astrbot.app/dev/star/plugin-new.html)，
  Skill 采用 [Anthropic Skills](https://code.claude.com/docs/zh-CN/skills) 规范。
