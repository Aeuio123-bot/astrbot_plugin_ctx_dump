# 上下文时间线（AstrBot 插件）

把每一次发往 LLM 的 `ProviderRequest` 存下来，并回答三个问题：

1. **这次请求由什么组成？** 系统提示词 / 工具定义 / 用户消息 / 助手回复 / 工具调用 / 工具结果 / 推理，各自多少 token。
2. **它是怎么变成现在这样的？** 每个请求一根堆叠条，横向看增长，和上一轮做 diff。
3. **具体那句话是什么？** 任意消息可展开看原文，工具可看名称与开销，系统提示词全文可见。

灵感与产品形态对齐 DeepSeek Harness 的 [`dsh-context`](https://github.com/bowenliang123/dsh-context)，数据源换成了 AstrBot 的钩子，规模是精简版。

## 两个入口，同一份页面

| 入口 | 依赖 | 说明 |
|---|---|---|
| AstrBot 插件视图 | dashboard（桌面端=本机 6185） | 插件卡片 → 详情 → 「上下文时间线」 |
| 插件自带本地页面 | 无，只用 Python 标准库 | `http://127.0.0.1:6199`，直接开浏览器 |

页面是同一份 `views/context/`：在插件视图里走 bridge，在本地页面里自动降级成同源 `fetch("/api/*")`。

再加两个命令行入口：`/ctxdump_last [n]`、`/ctxdump_stats`。

## 安装

三种方式，任选：

**A. 从仓库安装（推荐，之后能一键更新）**

插件页 → 安装插件 → 从链接安装 → 填
`https://github.com/Aeuio123-bot/astrbot_plugin_ctx_dump`

**B. 从 zip 安装**

插件页 → 安装插件 → 从文件安装 → 选 `astrbot_plugin_ctx_dump-<version>.zip`。

**C. 命令行**

```powershell
cd E:\DeepSeekWorkSpace\Plan\astrbot_plugin_ctx_dump
.\scripts\install.ps1                       # 默认装到 %USERPROFILE%\.astrbot\data
.\scripts\install.ps1 -AstrBotData "D:\AstrBot\data" -Force
```

装完：插件 → 上下文时间线 → 重载；发一条消息，看 `data/plugin_data/ctx_dump/`。
浏览器打开 `http://127.0.0.1:6199` 可以直接看。

## 更新

AstrBot 的「检查更新」只认 `metadata.yaml` 里的 `repo` 字段——它拿仓库里的
`metadata.yaml` 与本地的 `version` 比对，然后 git pull。

- **从仓库/市场装的**：插件卡片右上角的更新图标点一下即可。前提是 `repo` 已填，
  且本地目录是 git 仓库（从链接安装时 AstrBot 会 clone，满足）。
- **从 zip 装的**：没有 git 元数据，更新图标只会提示未配置仓库。要更新就下载新 zip
  覆盖安装（安装器检测到同名插件会替换），或者改用方式 A 重装一次，之后就都能一键更新。

发版流程就是三步：改代码 → 把 `metadata.yaml` 的 `version` 加一 → push。市场侧看到新
版本号才会推更新。

## 卸载

```powershell
.\scripts\uninstall.ps1              # 只删插件，保留已记录的数据
.\scripts\uninstall.ps1 -PurgeData   # 连 data\plugin_data\ctx_dump 一起删
```

卸载干净的三件事，脚本/代码都覆盖了：

1. **钩子**：插件被卸载或重载时注册的 handler 随 Star 生命周期消失，不再写文件；
2. **本地端口**：`terminate()` 里 `shutdown()` + `server_close()`，线程是 daemon，进程退出也不会挂住；
3. **数据**：默认保留（你可能正在分析），`-PurgeData` 或 `/ctxdump_purge yes` 才删。

## 配置项

| 键 | 默认 | 说明 |
|---|---|---|
| `include_tools` | true | 记录工具名与参数 schema |
| `keep_latest` | true | 额外写 `latest.json` |
| `retention_days` | 7 | JSONL 保留天数，0 不清理 |
| `redact` | true | `sk-…` / `api_key` 掩码 |
| `local_server` | true | 是否启动本地页面 |
| `local_bind` | `127.0.0.1` | 改成 `0.0.0.0` 会暴露到局域网（不建议） |
| `local_port` | 6199 | 端口，0 表示不启动 |

## 页面怎么读

- **KPI**：当天请求数、估算 token 合计、单次最大、contexts 峰值。
- **左侧时间线**：每个 LLM 请求一行，条长按全体最大值归一，所以“哪一轮突然变大”一眼可见。
  agent 工具循环里一次对话会产生多条 —— 这正是“上下文过程”。
- **右侧详情**：组成条 + 图例（八类固定配色）、与上一轮的 diff（总增减 + 分类徽标）、
  系统提示词全文、工具逐个开销、消息逐条展开、本次输入。
- **消息列表的两个开关**（偏好记在 localStorage，刷新后保持）：
  - **排序**：`旧 → 新`（模型读取顺序）或 `新 → 旧`（最近发生的在最上面）；
  - **显示**：`全部` 或 `仅变更` —— 只列相对上一次请求**新增**（绿标 `新增`）和
    **被改写**（黄标 `改动`）的消息，其余折叠掉。
  分组标题上直接显示 `+新增 / ~改动 / -移除` 的条数与颜色标记。

## 后端接口

插件视图：`bridge.apiGet("list", {...})`；本地页面：`GET /api/list?...`。两边数据一致。

| 路由 | 说明 |
|---|---|
| `GET /api/sessions?date=YYYY-MM-DD` | 当天会话及请求数 |
| `GET /api/list?date=&umo=&limit=` | 时间线数据（含 breakdown） |
| `GET /api/detail?date=&seq=&full=1` | 单条明细：contexts、msg_items、tool_items、prev |

## 记录结构

```json
{
  "ts": 1763820000.1, "iso": "2026-11-22T21:20:00.123+08:00", "seq": 7,
  "trace_id": "…", "umo": "aiocqhttp:GroupMessage:123",
  "platform": "aiocqhttp", "sender": "某人", "session_id": "…",
  "model": "deepseek-chat",
  "system_prompt": "…", "prompt": "…",
  "n_contexts": 12, "contexts": [{"role": "user", "content": "…"}],
  "images": [], "audios": [],
  "tools": {"names": ["web_search"], "schemas": [...]},
  "chars": 8421
}
```

`kind: "response"` 的记录是模型返回（`completion_text` / `tool_calls` / `usage`），不计入时间线。

## 已知边界

- **token 是估算值**：CJK 1 字/token、其余 4 字符/token。只用于比较“哪块在膨胀”；真实用量看响应记录的 `usage`。
- **钩子在 provider 加工之前**：图片转 base64、连续消息合并、按上限裁剪都发生在之后。要“模型实际收到的字节”，得走代理抓包。
- **压缩事件没打标记**：目前只能从 diff 负值看出来。
- **消息级 diff 按下标对齐**：AstrBot 正常是把新消息追加在末尾，所以下标对齐是准的；
  但上下文被压缩/裁剪导致整体位移时，会退化成一批 `改动` 标记，这是近似结果。
- **本地页面无鉴权**：默认只绑 `127.0.0.1`，内容是你的聊天原文，别改成 `0.0.0.0` 除非你知道后果。
