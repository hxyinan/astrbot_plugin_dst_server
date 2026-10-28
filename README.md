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
| Python | 3.10+ |

LuaJIT 加速**不是必需**的，但如果面板装了 LuaJIT 并希望重启后保持加速，需要额外处理（见文末）。

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

> **注意**：`mcp_token` 从面板「外部 AI 接入」生成；token 文件路径在代码里是 `/root/.dst-admin-mcp-token-v3`，如果你的部署路径不同，需要改 `main.py` 顶部相关常量。

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

## 已知限制

- 代码里有部分本机定制路径（`/root/query_wiki.py`、`/root/dst/mods` 等），换环境需要自行调整
- 营地百科依赖一个本地导入的词条库，未包含在本仓库
- 长文本回复会转成图片发送（依赖 AstrBot 的 t2i 模板）
- 仅适配 OneBot v11（QQ）

---

## 许可

MIT
