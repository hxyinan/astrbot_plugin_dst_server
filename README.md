# astrbot_plugin_dst_server

饥荒联机版（Don't Starve Together）服务器的 **QQ 群 AI 管家** —— 一个 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 插件。

通过 MCP 协议对接 [dst-admin-go](https://github.com/lcy0828/dst-admin-go) 面板，让群友在 QQ 里用自然语言问服务器状态、查背包、看排行榜、管理世界，也可以由管理员直接在群里执行运维操作。

> 本仓库是脱敏后的公开版本，去掉了服务器地址、群号、QQ 号等私有配置。使用前需要按下方「配置」一节填好自己的信息。

---

## 功能

**查询类（群友可用）**

- 服务器状态：天数、在线人数、季节、TPS、各分片运行情况
- 玩家背包：在线实时读取（含装备、容器嵌套、耐久/新鲜度百分比），离线读存档快照
- 玩家位置：区分地面 / 洞穴 / 离线
- 排行榜：在线时长、击杀、生存天数等
- 营地百科：物品、机制、Boss 的官方词条查询
- 模组信息：已装模组、创意工坊 ID、版本

**管理类（主人 / 群管理）**

- 启停服务器、重启分片
- 踢人、封禁、白名单管理
- 世界回档、暂停、重置
- 模组启用 / 禁用
- 备份管理
- 自定义 Lua 指令执行

**主动推送**

- 玩家进服 / 退服播报
- 服务器开关机播报
- 满员提醒
- 定时摘要

---

## 依赖

| 组件 | 要求 |
|---|---|
| AstrBot | v4.x |
| dst-admin-go 面板 | **v1.3.7 及以上**（需要 MCP 接口与分片级玩家存在性） |
| 面板 Runtime | 2.4.16+ |
| 操作系统 | Linux / WSL2（插件使用 `grep`、`tmux`、`/proc` 等） |
| Python | **3.12+** |

LuaJIT 加速**不是必需**的，但如果面板装了 LuaJIT 并希望重启后保持加速，需要额外处理（见文末）。

### 关于 WSL 部署假设

插件的本地能力（读日志、读存档、读 `cluster.ini`、跑辅助脚本）集中在一个函数 `_run_wsl_cmd()` 里，它的实现是：

```python
subprocess.run(["wsl.exe", "-d", "Ubuntu", "-e", "bash", "-c", cmd], ...)
```

也就是说，**它假设 AstrBot 跑在 Windows 上、游戏服务在 WSL2 里**。如果你的部署不是这个形态，改这一个函数即可，调用它的 20 多处代码不用动：

| 你的部署 | `_run_wsl_cmd` 应改成 |
|---|---|
| AstrBot 与游戏都在同一台 Linux | `subprocess.run(["bash", "-c", cmd], ...)` |
| AstrBot 本地、游戏在远程 Linux | `subprocess.run(["ssh", "user@host", cmd], ...)` |
| AstrBot 与游戏都在 Windows（无 WSL） | 改用 `pwsh -Command`，或直接走面板 MCP 接口 |

注意：发行版名写死成 `Ubuntu` 是个已知缺陷，判断不到时会静默返回空字符串（功能降级但不报错）。如果你要多发行版兼容，建议改成从配置读或先跑 `wsl.exe -l -q` 探测。

---

## 安装

```bash
cd AstrBot/data/plugins
git clone https://github.com/hxyinan/astrbot_plugin_dst_server.git
```

重启 AstrBot，或在 WebUI 插件管理里重载。

首次加载后插件会从面板拉取 MCP 工具目录并缓存到 `tool_index.json`（约 300 个工具，首次约 10 秒）。

---

## 配置

在 AstrBot WebUI → 插件管理 → 本插件 → 配置 中填写：

| 配置项 | 必填 | 说明 |
|---|---|---|
| `target_group_id` | ✅ | 饥荒专属 QQ 群号。只有这个群会加载饥荒人设和全部工具 |
| `master_qq` | ✅ | 机器人主人 QQ，私聊管理权限依据 |
| `mcp_endpoint` | ✅ | 面板 MCP 地址，默认 `http://127.0.0.1:8888/mcp` |
| `mcp_token` | ✅ | 面板「外部 AI 接入」里生成的 Bearer Token |
| `mcp_room_id_1` | ✅ | 1 服 roomId（面板内 base64，如 `Cluster_1` → `Q2x1c3Rlcl8x`） |
| `mcp_room_id_2` | ⬜ | 2 服 roomId，单服部署可留空 |
| `server_name_1` / `server_name_2` | ⬜ | 群内展示用的服务器名称 |
| `embed_api_key` | ⬜ | 向量检索密钥（硅基流动等 OpenAI 兼容服务）。留空则读环境变量 `DST_EMBED_API_KEY`；都没有就关闭语义检索、退回关键词匹配 |
| `wiki_dir` | ⬜ | 百科词条库目录。留空则按 `DST_WIKI_DIR` → `~/vault/饥荒联机版` 顺序探测 |

`mcp_token` 从面板「外部 AI 接入」生成后直接填进配置即可，插件不再读取固定路径的 token 文件。

---

## 实现要点（写给面板开发者）

以下是与 dst-admin-go Issue [#25](https://github.com/lcy0828/dst-admin-go/issues/25) 相关的实现思路，也是这个插件跑了大半年的经验。

### 玩家分片定位：`AllPlayers` vs `GetClientTable()`

这是最关键的一条：

| API | 作用域 | 用途 |
|---|---|---|
| `AllPlayers` | **分片级** | 判断"这个人在不在我这个分片"，以及拿实体读背包 |
| `TheNet:GetClientTable()` | **集群级** | 只用来判断"这个人到底在不在线" |

**错误做法**：用 `GetClientTable()` 做名单探针 → 每个分片都会列出全部在线玩家 → 两个分片都说"这人在这" → 位置判定失败。

**正确做法**：一次问一个分片，三步定位：

1. 脚本发到 **Master**，命中 `AllPlayers` → 在地面，背包已拿到
2. 输出 `INV|OFF` 但 UID 在 `CLU|` 集群名单里 → 人在洞穴，同一段脚本发给 **Caves**
3. `INV|OFF` 且不在名单里 → 真离线，去读存档快照

这样每个分片只回答"我这儿有没有这个人"，不存在两边都命中。

### 背包读取

- **在线**：读 `player.components.inventory`，遍历 `GetItemInSlot`，装备栏递归容器（背包、熊獾桶、野餐盒、钓具箱），礼物包装还要再拆一层
- **离线**：读存档快照，只有绝对值（剩 43 次、护甲 720），存档里没有上限字段，**不要硬算百分比**
- 耐久 / 护甲 / 新鲜度 / 燃料 / 充能统一按百分比输出，前端好展示

### 几个踩过的坑

- `AllPlayers` 是**数组不是函数**，写 `AllPlayers()` 会报 `attempt to call global 'AllPlayers'`
- 不要用 `Ents` 遍历找玩家，本服实测该表恒为空
- 装备栏里的容器必须递归，只调 `inventory:GetItemInSlot` 会漏掉里面所有东西
- 同一容器内名称和状态完全相同的堆叠物品要合并成一行，否则一个熊獾桶能刷半屏

---

## 与 LuaJIT 的关系

如果你在面板里装了 LuaJIT 并希望保持加速，注意一个坑：**面板的 `room.restart` 动作不接受任何参数**，创建自动化任务时带 `runtimeMode` 会返回：

```
HTTP 422 INVALID_AUTOMATION_INPUT
details: { "parameters": "此动作不接受额外参数" }
```

所以本插件在重启时**不读面板的默认模式**，而是先查 `api_getRoomRuntimeModes`，房间支持 `luajit` 就用 `luajit`：

```python
modes = _mcp_call("api_getRoomRuntimeModes", {"path": {"roomId": room_id}}) or {}
available = modes.get("modes") or []
fallback = modes.get("defaultMode") or "game"
payload["runtimeMode"] = "luajit" if "luajit" in available else fallback
```

面板侧的建议见 Issue [#26](https://github.com/lcy0828/dst-admin-go/issues/26)。

---

## 辅助脚本与词条库

仓库带两个可选辅助脚本，放在 `scripts/` 下。插件按自身目录定位它们，不再依赖 `/root` 下的绝对路径。

### `scripts/get_dst_cpu.py` — 分片单核占用

读 `/proc/stat` 与 `/proc/<pid>/cmdline`，输出每个分片的单核 CPU 占用、内存、绑定核心。纯标准库，无第三方依赖，Linux 专用。

集群名默认 `Cluster_1,Cluster_2`，可用环境变量覆盖：

```bash
DST_CLUSTERS=Cluster_A,Cluster_B python3 scripts/get_dst_cpu.py
```

### `scripts/query_wiki.py` — 营地百科查询

按别名映射和文件名模糊匹配，从词条目录取一篇 `.md` 返回。目录由 `wiki_dir` 配置项或 `DST_WIKI_DIR` 环境变量指定。

**词条库本身不在仓库里**，两个原因：体积（约 1678 篇词条 + 配图，近 300MB），以及版权（内容镜像自灰机 Wiki，遵循 CC BY-NC-SA）。需要的话可以自己从 [灰机 Wiki 饥荒联机版](https://dontstarve.huijiwiki.com/) 导出 Markdown —— 一篇一个文件，文件名就是词条名（如 `独眼巨鹿.md`）。

可选配图：插件会在回复里附 1–3 张主立绘，图片目录由 `WIN_ASSETS_DIR` 指定，留空则不带图。

### 向量检索（可选）

工具语义检索依赖 Embedding 服务。不填 `embed_api_key` 时自动退化为关键词匹配，功能仍可用，召回率低一些。

---

## 已知限制

- `_run_wsl_cmd()` 硬编码 `wsl.exe -d Ubuntu`（见上文「关于 WSL 部署假设」），非 WSL 部署需改这一个函数
- 营地百科依赖外部词条库，仓库只带查询脚本
- 长文本回复会转成图片发送（依赖 AstrBot 的 t2i 模板）
- 仅适配 OneBot v11（QQ）
- 语义检索默认用硅基流动的 `Qwen3-Embedding-0.6B`，换服务需改 `_EMBED_MODEL` / `_EMBED_URL`

---

## 安全说明

两处已修复的历史问题，供审阅参考：

- **shell 命令注入**：`query_dst_wiki` 曾经用 f-string 把用户关键词拼进 `bash -c` 命令，只过滤了引号，`$(...)`、反引号、分号仍可执行。现在改用 `shlex.quote()` 逐个转义参数，关键词里带什么都只会当普通字符串传参。
- **失败回执误报成功**：面板把 HTTP 状态码放在 `data.httpStatus` 字段里，MCP 层的 `isError` 不会置位。旧版 `_mcp_call` 直接返回 `data.data`，导致 4xx/5xx 被调用方当成成功。现在 `httpStatus >= 400` 会直接抛异常并带上面板返回的错误详情。

需要说明的是：插件自身不做二次鉴权，权限判断完全依赖 AstrBot 传来的群成员角色（`_is_group_admin`）。部署时请确认 `target_group_id` 只指向自己的群，不要把管理工具暴露到公开群。

---

## 许可

MIT
