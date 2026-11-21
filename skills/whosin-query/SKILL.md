---
name: whosin-query
description: 查询“音游窝”店内实时在店人员名单（玩家 / 管理员 / 士大夫），并在本机渲染宵崎奏主题图片。当用户询问“店里有多少人”“谁在店”“几”，或发送“j”指令时使用。
---

# 音游窝在店查询

面向 QQ 群聊的实时在店人员查询技能，由 `astrbot_plugin_dcs_whosin` 插件提供实现。

## 触发方式

- 指令：`j`（默认唤醒前缀下为 `/j`）。
- 频率：同一用户在同一群 60 秒内只能查一次（冷却期内只提示一次）；同群两次回复至少间隔 5 秒。

## 数据来源

- 统一 Bot API：站点同一地址下的 `POST /api/botAction`，动作为 `getPresentUsers`，明钥鉴权、按 action 授权。
- 返回结构直接给出昵称与角色（`role`），不再解析页面 HTML：

| role | 角色 |
|------|------|
| `CUSTOMER` | 玩家 |
| `HELPER` | 士大夫（工作人员） |
| `ADMIN` | 管理员 |

- 域名访问失败时可直连备用 IP，但证书仍按域名校验，不会降级为明文 HTTP。
- 图片完全在本机渲染：Jinja2 模板 + 本机浏览器内核（Windows 用系统 Edge）截图，素材只用 `assets/` 里的本地文件。

## 输出规则

1. 玩家数量为 0（只有管理员 / 士大夫，或完全无人）时：
   - 回复固定文案 `店内无玩家，快来吧唧！`；
   - 不发送空消息、更不发送 null。
2. 玩家数量大于 0 时，发送「文本 + 图片」：
   - 文本第一行：`玩家：昵称1,昵称2`（英文逗号分隔）；
   - 文本第二行（存在管理 / STAFF 时）：`管理/STAFF：昵称1,昵称2`。
   - 图片：宵崎奏主题卡片（站酷快乐体），玩家每人一框（昵称 / 入店时间 / 游玩分钟），管理与 STAFF 单独一栏；在本机渲染，不经过任何外部渲染服务。

## 异常处理

- 网络失败、明钥无效、权限不足或返回结构异常：群内只回复通用提示（如 `查询失败：站点暂时无法访问，请稍后再试。`），
  不暴露地址或异常细节；详细原因写入日志。
- 图片渲染失败或超时：改发文字版，并附「图片渲染暂时失败」提示。

## 可用配置项（WebUI 插件配置页）

`bot_api_key`、`bot_qqid`、`target_urls`、`fallback_ips`、`timeout_seconds`、`cookie`、`login_username`、`login_password`、
`enable_image`、`cache_ttl_seconds`、`image_theme`、`render_browser`、`render_browser_path`、
`render_timeout_seconds`、`cooldown_seconds`、`group_interval_seconds`、`allowed_groups`
