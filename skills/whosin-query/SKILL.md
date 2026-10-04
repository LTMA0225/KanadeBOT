---
name: whosin-query
description: 查询“直流会馆”店内实时在店人员名单（玩家 / 管理员 / 士大夫），并渲染黑白赛博朋克风图片。当用户询问“店里有多少人”“谁在店”“几”，或发送“j”指令时使用。
---

# 直流会馆在店查询

面向 QQ 群聊的实时在店人员查询技能，由 `astrbot_plugin_dcs_whosin` 插件提供实现。

## 触发方式

- 指令：`j`（默认唤醒前缀下为 `/j`）。

## 数据来源

- 优先抓取 `https://dcstream.top/whosin`，失败时自动切换 `http://47.116.47.191/whosin`。
- 页面昵称元素带有 `nickname` class，附加 class 表示角色：

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
   - 图片内容：直流会馆 Logo、当前时间（UTC+8）、分类人员列表，黑白赛博朋克风格。

## 异常处理

- 网络失败或页面结构变化：回复 `查询失败：<原因>`，不崩溃。
- 页面要求登录：提示配置 Cookie 或联系站点管理员。

## 可用配置项（WebUI 插件配置页）

`target_urls`、`timeout_seconds`、`cookie`、`enable_image`、`cache_ttl_seconds`、`image_theme`
