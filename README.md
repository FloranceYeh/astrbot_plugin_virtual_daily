# astrbot_plugin_virtual_daily

虚拟日常插件会按配置的间隔生成一段模拟见闻或经历，再把经历和近期聊天上下文交给 LLM 判断：

- 是否发送主动消息
- 延迟多久发送
- 发到群聊还是私聊
- 发送什么内容

## 命令

- `虚拟日常状态`：查看最近一次经历、决策和插件状态
- `虚拟日常触发 [主题]`：管理员手动触发一次，并立即按 LLM 决策发送

## 关键配置

- `target_groups` / `target_users`：主动消息候选目标。建议至少配置一个群号或用户号。
- `allow_recent_sessions`：开启后会把插件运行期间记录到的群聊或私聊也作为候选目标。
- `interval_minutes`：每次生成经历并决策的基础间隔。
- `min_delay_seconds` / `max_delay_seconds`：LLM 决定发送时允许的发送延迟区间。
- `experience_send_policy`：见闻发送策略，`always` 总是提供见闻，`never` 不提供见闻只询问用户近况，`probability` 按概率提供见闻。
- `experience_send_probability`：`experience_send_policy` 为 `probability` 时的见闻发送概率，范围 0-100。
- `persona_document` / `persona_document_path`：生成见闻时使用的角色人格文档。配置路径时会优先读取文件，路径相对插件目录。
- `split_messages_enabled`：启用后会把主动消息按长度和标点拆成多条发送。
- `split_message_max_chars` / `split_message_interval_ms`：控制每条分割消息的最大字数和发送间隔。
- `split_message_regex`：自定义分割正则，填写后优先使用，例如 `[。！？\n]+`。
- `split_message_words`：自定义分割词列表，正则为空时生效；为空则回退到兼容字段 `split_message_separators`。
- `experience_prompt`：控制虚拟经历的风格。
- `decision_prompt`：控制 LLM 如何判断是否主动发言，默认要求只输出 JSON。

LLM 决策 JSON 示例：

```json
{
  "should_send": true,
  "delay_seconds": 300,
  "target_type": "group",
  "target_id": "123456789",
  "content": "刚才路过楼下闻到烤面包味，突然有点想吃夜宵。",
  "reason": "群里正在聊吃的，顺势分享比较自然"
}
```
