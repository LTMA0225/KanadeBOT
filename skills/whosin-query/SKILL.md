---
name: whosin-query
description: 查询“直流会馆”店内实时在店人员名单（玩家 / 管理员 / 士大夫），并在本机渲染赛博朋克风图片。当用户询问“店里有多少人”“谁在店”“几”，或发送“j”指令时使用。
---

# 直流会馆在店查询

面向 QQ 群聊的实时在店人员查询技能，由 `astrbot_plugin_dcs_whosin` 插件提供实现。

## 触发方式

- 指令：`j`（默认唤醒前缀下为 `/j`）。
- 频率：同一用户在同一群 60 秒内只能查一次（冷却期内只提示一次）；同群两次回复至少间隔 5 秒。

## 数据来源

- 抓取 `https://dcstream.top/whosin`（需要登录，账号密码与 Cookie 只经 HTTPS 发送）。
- 域名访问失败时直连备用 IP `47.116.47.191`，但证书仍按域名校验，不会降级为明文 HTTP。
- 页面昵称元素的 class 含独立的 `nickname` 词元，附加词元表示角色：

| class | 角色 |
|-------|------|
| `nickname` | 玩家 |
| `nickname sponsor` | 赞助者（计入玩家） |
| `nickname staff` | 士大夫（工作人员） |
| `nickname admin` | 管理员 |

## 输出规则

1. 玩家数量为 0（只有管理员 / 士大夫，或完全无人）时：
   - 回复固定文案 `店内无玩家，快来吧唧！`；
   - 不发送空消息、更不发送 null。
2. 玩家数量大于 0 时，发送「文本 + 图片」：
   - 文本第一行：`玩家：昵称1,昵称2`（英文逗号分隔）；
   - 文本第二行（存在管理 / STAFF 时）：`管理/STAFF：昵称1,昵称2`。
   - 图片：直流会馆 Logo、当前时间（UTC+8）、分类人员列表；在本机渲染，不经过第三方服务。

## 异常处理

- 网络失败、登录失败或页面结构变化：群内只回复通用提示（如 `查询失败：站点暂时无法访问，请稍后再试。`），
  不暴露地址或异常细节；详细原因写入日志。
- 图片渲染失败或超时：改发文字版，并附「图片渲染暂时失败」提示。

## 可用配置项（WebUI 插件配置页）

`target_urls`、`fallback_ips`、`timeout_seconds`、`cookie`、`login_username`、`login_password`、
`enable_image`、`cache_ttl_seconds`、`image_theme`、`render_browser`、`render_browser_path`、
`render_timeout_seconds`、`cooldown_seconds`、`group_interval_seconds`、`allowed_groups`
