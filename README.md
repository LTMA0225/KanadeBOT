# 直流会馆 QQ 查询机器人（AstrBot 插件）

基于 **AstrBot + NapCat** 的 QQ 群机器人：查询「直流会馆」店内实时在店人员，
以**黑白赛博朋克风图片 + 文本**返回，附带娱乐指令。

- 机器人框架：AstrBot（支持桌面客户端 / pip / Docker 部署）
- QQ 协议端：NapCat（OneBot v11，反向 WebSocket）
- 开发语言：Python 3.10+
- 图片渲染：AstrBot 内置 `html_render`（底层为 **Playwright 本地无头浏览器**，客户端渲染，不消耗服务器渲染性能）

## 功能

| 指令 | 说明 |
|------|------|
| `j`（默认 `/j`） | 抓取 `/whosin` 页面，整理玩家 / 管理员 / 士大夫名单，发送「图片 + 文本」 |
| `baka` | 回复 `bakabaka` |

图片与文本内容：

- 图片：黑白赛博朋克风，铺满整幅画布；**玩家**：每位一个信息小框（昵称、入店时间、已游玩分钟数）；**管理员 / 士大夫**：仅显示昵称与在店状态；
- 文本：`玩家：甲,乙`（英文逗号分隔）；有管理 / 士大夫时另起一行 `管理/STAFF：丙,丁`。

行为规则：

- **玩家数量为 0**（只有管理员 / 士大夫，或完全无人）时，回复固定文案：`店内无玩家，快来吧唧！`（不发送空消息 / null）；
- 玩家数量大于 0 时，文本格式：

  ```
  玩家：甲,乙,丙
  管理/STAFF：丁,戊
  ```

  （玩家之间用英文逗号分隔；无管理 / STAFF 时省略第二行）
- 抓取失败时返回友好错误提示，插件不会崩溃。

## 目录结构

```
astrbot_plugin_dcs_whosin/
├── main.py                   插件入口（Star 基类 + @filter.command）
├── dcs_api.py                页面抓取与解析（aiohttp + 正则）
├── dcs_render.py             图片渲染与缓存（调用 AstrBot html_render）
├── templates/whosin.html     黑白赛博朋克图片模板（HTML + CSS + Jinja2）
├── assets/logo.svg           直流会馆 Logo（渲染时以 base64 内联）
├── skills/                   随插件提供的 Skills
│   ├── whosin-query/SKILL.md
│   └── baka/SKILL.md
├── metadata.yaml             插件元数据
├── _conf_schema.json         可视化配置定义（WebUI 插件配置页）
├── requirements.txt          依赖清单
└── deploy/                   一键部署脚本与 Docker 编排
    ├── windows_install.bat
    ├── linux_install.sh
    └── docker-compose.yml
```

## 快速部署

### 方式 A：Windows 一键脚本

1. 安装 [Python 3.10+](https://www.python.org/downloads/)（勾选 Add to PATH）；
2. 双击 `deploy/windows_install.bat`，脚本自动：
   - 安装 / 更新 AstrBot（pip 方式，失败自动切换清华镜像）；
   - 将插件部署到 `deploy/runtime/data/plugins/`；
   - 从 GitHub 下载 AstrBot 桌面客户端安装包到 `deploy/installers/`（可选，失败不影响）；
   - 输出 NapCat 与适配器配置步骤，并可直接启动 AstrBot。

### 日常启动（安装完成后）

双击项目根目录的 `快速启动.bat`：

- 自动检查 Python / AstrBot（未安装时引导一键安装）；
- 自动部署 / 更新插件到运行时目录；
- 启动 AstrBot，并尝试自动启动常见位置的 NapCat（如项目根目录 `NapCat/`）；
- 自动打开 AstrBot 管理面板，并打印配置清单。

### 方式 B：Linux / macOS 一键脚本

```bash
chmod +x deploy/linux_install.sh
./deploy/linux_install.sh
```

### 方式 C：Docker Compose（服务器推荐）

```bash
cd deploy
docker compose up -d
```

说明：

- `astrbot`：管理面板 `6185`，OneBot v11 反向 WS 端口 `6199`；
- `napcat`：WebUI 端口 `6099`；
- 插件目录已通过挂载自动进入 AstrBot 插件目录；
- 中国大陆拉取镜像失败时，按 `docker-compose.yml` 内注释给镜像名加 `m.daocloud.io/docker.io/` 前缀。

### 方式 D：手动部署（桌面客户端）

1. 下载并安装 [AstrBot 桌面客户端](https://github.com/AstrBotDevs/AstrBot-desktop/releases)；
2. 将本插件整个文件夹复制到 AstrBot 数据目录的 `data/plugins/` 下；
3. 在 AstrBot WebUI 的「插件」页确认 `astrbot_plugin_dcs_whosin` 已加载并启用；
4. 若依赖缺失，点击插件卡片的安装依赖（AstrBot 会自动读取 `requirements.txt`）。

## 持续运行（7×24 值守）

Windows 环境下已提供并默认配置好以下自愈机制（脚本位于 `deploy/`）：

1. **开机自启**：启动文件夹中已创建 `AstrBot` 与 `NapCat` 快捷方式，开机登录后自动拉起；
2. **看门狗**（计划任务 `DCStreamBotWatchdog`，每 5 分钟执行一次）：
   - AstrBot 掉线 → 自动启动桌面客户端；
   - NapCat 掉线 → 自动启动启动器；
   - NapCat 的 QQ 未登录 → 自动尝试快速登录（`autoLoginAccount` 已在 `webui.json` 中设为机器人账号）；
   - 日志：`deploy/watchdog.log`（仅异常时写入）。
3. **NapCat 自动登录**：`webui.json` 的 `autoLoginAccount` 已配置，NapCat 重启后自动恢复 QQ 登录；
4. **电源设置**：已设置交流电下**永不睡眠/永不休眠**（`powercfg`）。
   - 合盖动作：Modern Standby 机型无法用脚本修改，请手动在
     「设置 → 系统 → 电源和电池 → 合上盖子时 → 不采取任何操作」中设置；
   - 建议保持接通电源，以保证机器人全天在线。

## NapCat 对接（关键步骤）

1. 安装并启动 NapCat（[下载地址](https://github.com/NapNeko/NapCatQQ/releases)），登录机器人 QQ；
2. 打开 NapCat WebUI（默认 `http://localhost:6099`）：
   **网络配置 → 新建 → WebSocket 反向**，URL 填写：

   ```
   ws://127.0.0.1:6199/ws
   ```

   （Docker 部署时填写 `ws://astrbot:6199/ws`）
3. 打开 AstrBot WebUI（默认 `http://localhost:6185`）：
   **消息平台 → 新增 → aiocqhttp（OneBot v11）**，保持反向 WS 端口 `6199`；
4. 群内发送 `/j` 测试（AstrBot 默认唤醒前缀为 `/`，可在配置中调整；设为空前缀后可以直接发送 `j`）。

## 配置项（WebUI 插件配置页）

| 配置 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `target_urls` | list | `["https://dcstream.top/whosin", "http://47.116.47.191/whosin"]` | 目标地址，按顺序尝试，失败自动切换 |
| `timeout_seconds` | int | `10` | 单次爬取超时时间（秒） |
| `cookie` | string（密文） | 空 | 站点 Cookie（优先于账号密码方式） |
| `login_username` | string | 空 | 站点账号（自动登录用） |
| `login_password` | string（密文） | 空 | 站点密码（自动登录用，失效自动重新登录） |
| `enable_image` | bool | `true` | 是否发送渲染图片（关闭后仅发文本） |
| `cache_ttl_seconds` | int | `30` | 图片缓存时间（秒，0 = 不缓存） |
| `image_theme` | string | `cyberpunk-bw` | 图片模板主题 |

> 说明：站点 `/whosin` 需要登录。推荐在插件配置中填写 `login_username` / `login_password`，
> 插件会自动登录并在会话失效时自动重新登录，无需手动维护 Cookie。

## 图片渲染说明

- 渲染调用 AstrBot 的 `self.html_render(template, data)`：模板为 Jinja2，渲染选项对应
  Playwright 截图参数（`type=png`、`full_page` 等），**在本地客户端完成渲染**；
- 模板为 760px 宽的黑白赛博朋克风格：深色底、网格线、扫描线、四角装饰括号；
  内容包含 Logo、当前时间（UTC+8）、分类人员列表、在店总数；
- Logo 渲染时转换为 base64 Data URI 内联，避免无头浏览器访问本地文件受限；
- 内置图片缓存：相同数据且同一分钟内直接复用，避免重复渲染。

## 常见问题

1. **查询提示“目标页面要求登录”？**
   站点最新版本 `/whosin` 可能要求登录（匿名访问会被重定向到 `/login`）。
   解决方式：登录站点后复制浏览器请求头中的完整 `Cookie` 填入插件配置的 `cookie` 项；
   或联系站点管理员开放该页面的匿名访问。
2. **图片里的中文显示为方块？**
   渲染机缺少中文字体。建议安装 `Noto Sans CJK`（Linux）或使用自带微软雅黑的 Windows。
3. **图片发送失败 / 看不到图？**
   确认 AstrBot 与 NapCat 之间的文件传输可用（同机部署最稳妥，或使用 AstrBot 的图床配置）。
4. **发送 `j` 没反应？**
   AstrBot 默认唤醒前缀为 `/`，请发送 `/j`；也可在 AstrBot 配置中将唤醒前缀设为空。
5. **抓取到的人数为 0 但实际有人？**
   目标站点页面结构可能已变化。解析基于昵称元素的 `nickname` class 与角色附加 class
   （`staff` / `admin` / `sponsor`），如站点改版请参照 `dcs_api.py` 中的正则调整。

## 开发与调试

- 修改代码后，在 AstrBot WebUI 的「插件」页点击插件卡片的刷新图标（热重载）；
- 插件运行日志请在 AstrBot 日志中查看，关键字：`[dcs_whosin]`；
- 解析逻辑可离线测试：`python -c "from dcs_api import parse_whosin_html; print(parse_whosin_html('<span class=\"nickname staff\">测试</span>'))"`。

## 灵感与素材来源

- 业务数据与视觉资产来自「直流会馆」项目（dc-web）；
- 插件开发遵循 [AstrBot 官方插件开发文档](https://docs.astrbot.app/dev/star/plugin-new.html)，
  Skill 采用 [Anthropic Skills](https://code.claude.com/docs/zh-CN/skills) 规范。
