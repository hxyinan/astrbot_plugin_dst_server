import base64
import asyncio
import json
import logging
import os
import re
import shlex
import subprocess
import threading
import time
from datetime import datetime, timedelta
from typing import Any
import urllib.parse
import urllib.request
import uuid
import warnings
from concurrent.futures import ThreadPoolExecutor

warnings.filterwarnings("ignore")

from astrbot.api.all import *

logger = logging.getLogger(__name__)
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.event.filter import on_llm_request
from astrbot.api.star import Context, Star, register
from astrbot.core.provider.entities import ProviderRequest

TARGET_GROUP_ID = ""  # 请在你的 AstrBot 插件配置中填写群号
MASTER_QQ = ""  # 请在你的 AstrBot 插件配置中填写主人QQ
DST_MODEL = ""  # 留空=跟随会话所选 provider 的模型；填了才会强制覆盖（覆盖会顶掉 provider 的模型名，慎用）

# --- MCP 接入（新面板 dst-admin）---
MCP_ENDPOINT = "http://127.0.0.1:8888/mcp"
MCP_TOKEN = ""
MCP_ROOM_IDS = {1: "", 2: ""}   # 服务器编号 -> 面板 roomId（base64，如 Cluster_1 = Q2x1c3Rlcl8x）
ROLLBACK_CTX: dict[int, dict] = {}  # 服号 -> {"who": 操作人, "days": 天数, "ts": 时间}，播报回档时取用

# --- 可选外部能力（都可以留空）---
EMBED_API_KEY = ""   # 向量检索密钥；留空则读环境变量 DST_EMBED_API_KEY，再回退到本机 Hindsight 配置
WIKI_DIR_CONF = ""   # 百科词条库目录；留空则按 DST_WIKI_DIR → ~/vault/饥荒联机版 探测


PREFAB_NAMES = {
    "wilson": "威尔逊",
    "willow": "薇洛",
    "wolfgang": "沃尔夫冈",
    "wendy": "温蒂",
    "wx78": "WX-78",
    "wickerbottom": "薇克巴顿",
    "woodie": "伍迪",
    "wes": "韦斯",
    "waxwell": "麦斯威尔",
    "wathgrithr": "薇格弗德",
    "webber": "韦伯",
    "winona": "薇诺娜",
    "warly": "沃利",
    "wortox": "沃拓克斯",
    "wormwood": "沃姆伍德",
    "wurt": "沃特",
    "walter": "沃尔特",
    "wanda": "旺达",
    "wonkey": "芜湖猴",
    "wixie": "薇茜",
    "winky": "温奇",
    "wathom": "沃托姆",
    "wyuri": "瓦克",
    "xd_longtaizi": "龙太子",
    "lg_fanglingche": "方灵澈",
    "lg_lilingyi": "李令仪",
    "wilba": "薇尔巴",
    "woodlegs": "木腿船长",
    "walani": "瓦拉尼",
    "wheeler": "薇勒",
    "wagstaff": "瓦格斯塔夫",
    "warbucks": "沃巴克斯",
}

# 预制体中文名统一取面板的实体目录（6121 条，跟游戏版本同步），不再维护本地字典
_ENTITY_NAMES: dict[str, str] = {}
_ENTITY_NAMES_LOADED = False
_GAME_NAMES: dict[str, str] = {}   # 面板目录没收录的，问游戏要过一次后缓存


def _ensure_entity_names() -> None:
    """从面板实体目录拉一次 prefab→中文名全表（约 1 秒），合并进 PREFAB_NAMES。"""
    global _ENTITY_NAMES_LOADED
    if _ENTITY_NAMES_LOADED or not MCP_TOKEN:
        return
    _ENTITY_NAMES_LOADED = True
    try:
        off = got = 0
        while got < 20000:
            page = _mcp_call("api_getEntityDirectory", {"query": {"limit": 120, "offset": off}}) or {}
            items = page.get("items") or []
            for it in items:
                pid = str(it.get("id") or "")
                zh = str(it.get("nameZhCN") or "")
                if pid and zh and zh != pid:
                    _ENTITY_NAMES[pid] = zh
            got += len(items)
            if not page.get("hasMore") or not items:
                break
            off += 120
        PREFAB_NAMES.update(_ENTITY_NAMES)
        logger.info(f"[MCP] 实体中文名已加载 {len(_ENTITY_NAMES)} 条")
    except Exception as e:
        logger.warning(f"[MCP] 实体中文名加载失败: {e}")


_MODS_CACHE: dict[int, list[str]] = {}


SERVER_NAMES = {
    1: "小鱼干「永久服」「半纯净」「顶级配置」「目标一万天」",
    2: "小鱼干「公益2服」「妥测为爽」「长期q0」「顶级配置」",
}


def _run_wsl_cmd(cmd: str, timeout: float = 6) -> str:
    try:
        p = subprocess.run(
            ["wsl.exe", "-d", "Ubuntu", "-e", "bash", "-c", cmd],
            capture_output=True,
            encoding="utf-8",
            errors="ignore",
            timeout=timeout,
        )
        return p.stdout.strip()
    except Exception as e:
        logger.warning(f"[WSL] 调用失败({type(e).__name__}): {cmd[:80]}")
        return ""

_MODINFO_PY = r"""
import glob, json, os, re
def fld(s, k):
    m = re.search(r"(?m)^\s*" + k + r"\s*=\s*", s)
    if not m:
        return ""
    r = s[m.end():]
    a, q = r.find("[["), r.find('"')
    if a != -1 and (q == -1 or a < q):
        e = r.find("]]", a + 2)
        return r[a + 2:e if e != -1 else None].strip()
    if q != -1:
        e = r.find('"', q + 1)
        return r[q + 1:e if e != -1 else None].strip()
    return ""
for d in sorted(glob.glob("/root/dst/mods/*/modinfo.lua")):
    s = open(d, encoding="utf-8", errors="ignore").read()
    row = {"folder": os.path.basename(os.path.dirname(d))}
    for k in ("name", "author", "version"):
        row[k] = " ".join(fld(s, k).split())
    row["desc"] = " ".join(fld(s, "description").split())[:6000]
    print(json.dumps(row, ensure_ascii=False))
"""

_MODINFO_CACHE: tuple[float, list] = (0.0, [])


def _local_mods(ttl: float = 300) -> list:
    """读服务器 mods 目录里每个 modinfo.lua 的名字/作者/版本/简介。面板查不到的本地模组只能靠这个。"""
    global _MODINFO_CACHE
    if time.time() - _MODINFO_CACHE[0] < ttl:
        return _MODINFO_CACHE[1]
    out = _run_wsl_cmd(
        f"echo {base64.b64encode(_MODINFO_PY.encode()).decode()} | base64 -d | python3 -",
        timeout=15,
    )
    rows = []
    for ln in out.splitlines():
        try:
            r = json.loads(ln)
        except Exception:
            continue
        if r.get("name") or r.get("desc"):
            rows.append(r)
    if rows:
        _MODINFO_CACHE = (time.time(), rows)
    return rows


def _mod_display_name(item: dict) -> str:
    """面板给的模组名缺失、或只是 local:xxx / 纯 ID 时，用本地 modinfo 的名字补上。"""
    name = str(item.get("name") or "")
    mid = str(item.get("id") or "")
    if name and not name.startswith("local:") and not name.isdigit():
        return name
    key = name or mid
    key = (key[6:] if key.startswith("local:") else key).lower()
    for m in _local_mods():
        f = str(m.get("folder") or "").lower()
        if key and f in (key, f"workshop-{key}"):
            return m.get("name") or key
    return name or mid


_LUA_ERRORS = (
    'is not declared',
    '[string "',
    "attempt to call",
    "attempt to index",
    "attempt to perform",
    "syntax error",
    "SCRIPT ERROR",
    "error loading",
    "stack traceback",
)
_LUA_ACTIONS = (
    "SpawnPrefab",
    "c_give",
    "c_spawn",
    "FindEntity",
    "SpawnAt",
    "inventory",
    "Transform:SetPosition",
    "ThePlayer",
)


def _mcp_room_name(cluster_id: int) -> str:
    room_id = MCP_ROOM_IDS.get(cluster_id, "")
    if not room_id:
        return ""
    try:
        vals = (_mcp_call("api_getRoomConfiguration", {"path": {"roomId": room_id}}) or {}).get("values") or {}
        return str(vals.get("clusterName") or "").strip()
    except Exception:
        return ""


def _dst_console(cluster_id: int, lua: str, world_key: str = "Master") -> tuple[bool, str]:
    """把 Lua 下发到地面控制台并取回执行结果。

    走新面板的原始 Lua 通道，print 输出直接回传，不用再回读服务器日志。
    """
    room_id = MCP_ROOM_IDS.get(cluster_id, "")
    if not room_id:
        return False, f"{cluster_id}服未接入面板，无法执行指令"
    name = _mcp_room_name(cluster_id)
    if not name:
        return False, "读取房间名失败，无法确认为目标房间"
    flat = " ".join(line.strip() for line in lua.strip().splitlines() if line.strip())
    if not flat.startswith("(function()"):
        flat = f"(function() {flat} end)()"
    try:
        run = _mcp_call("api_executeRawRoomCommand", {
            "path": {"roomId": room_id, "worldId": _MCP_WORLD_IDS[world_key]},
            "body": {"command": flat, "confirmation": name},
            "requestId": f"dstcmd-{int(time.time() * 1000)}",
        })
    except Exception as e:
        return False, f"指令下发失败：{e}"
    out = ((run.get("output") or {}).get("text") or "").strip()
    if run.get("status") != "succeeded" or run.get("executionOutcome") != "confirmed":
        detail = f"{run.get('errorCode') or ''} {run.get('message') or ''}".strip()
        return False, f"{detail} {out}".strip()
    for line in out.splitlines():
        if any(key in line for key in _LUA_ERRORS):
            return False, line.strip()
    return True, out or "执行成功"


def _dst_query(cluster_id: int, expr: str, timeout: float = 3.0) -> str:
    """执行 Lua 表达式并把结果取回来（走新面板，输出直接回传）。"""
    room_id = MCP_ROOM_IDS.get(cluster_id, "")
    name = _mcp_room_name(cluster_id) if room_id else ""
    if not name:
        return ""
    lua = f"(function() print('QRY=' .. tostring({expr})) end)()"
    try:
        run = _mcp_call("api_executeRawRoomCommand", {
            "path": {"roomId": room_id, "worldId": _MCP_WORLD_IDS["Master"]},
            "body": {"command": lua, "confirmation": name},
            "requestId": f"dstqry-{int(time.time() * 1000)}",
        })
    except Exception:
        return ""
    if run.get("status") != "succeeded":
        return ""
    for line in ((run.get("output") or {}).get("text") or "").splitlines():
        if line.startswith("QRY="):
            return line[4:].strip()
    return ""


def _get_server_log_base_time(log_wsl_path: str):
    """从 server_log.txt 前 25 行解析出开服绝对时间戳 (现实时间)"""
    try:
        out = _run_wsl_cmd(f"head -n 25 '{log_wsl_path}'")
        for line in out.splitlines():
            if "Current time:" in line:
                m = re.search(r"Current time:\s*([A-Za-z]{3}\s+[A-Za-z]{3}\s+\d+\s+\d{2}:\d{2}:\d{2}\s+\d{4})", line)
                if m:
                    cleaned = re.sub(r"\s+", " ", m.group(1))
                    return datetime.strptime(cleaned, "%a %b %d %H:%M:%S %Y")
    except Exception:
        pass
    return None


def _format_dst_log_line(line: str, base_dt, base_day: int = 1) -> str:
    """将饥荒日志开头的开服累计时长 [HH:MM:SS] 自动换算为游戏内时间（天数+时段）与现实北京时间"""
    m = re.match(r"^\[(\d{2}):(\d{2}):(\d{2})\](.*)", line.strip())
    if m:
        h, mn, s = int(m.group(1)), int(m.group(2)), int(m.group(3))
        total_sec = h * 3600 + mn * 60 + s
        day = base_day + (total_sec // 480)
        sec_in_day = total_sec % 480
        if sec_in_day < 240:
            phase = "白天"
        elif sec_in_day < 360:
            phase = "黄昏"
        else:
            phase = "夜晚"
        game_time_str = f"第{day}天 {phase}"

        if base_dt:
            real_time = base_dt + timedelta(seconds=total_sec)
            real_time_str = real_time.strftime("%m-%d %H:%M")
            return f"[{game_time_str} | 现实 {real_time_str}]{m.group(4)}"
        return f"[{game_time_str}]{m.group(4)}"
    return line.strip()


def _get_current_cluster_day(cluster_name: str) -> int:
    # 优先问游戏本体拿实时天数，问不到才回退 .meta 快照
    try:
        cid = int(str(cluster_name).rsplit("_", 1)[-1])
        live = _dst_query(cid, "TheWorld.state.cycles")
        if live.isdigit():
            return int(live) + 1
    except Exception:
        pass
    try:
        cmd = f"cat $(ls -t /opt/dst-admin/data/saves/{cluster_name}/Master/save/session/*/*.meta 2>/dev/null | head -n 1)"
        content = _run_wsl_cmd(cmd)
        if content:
            m = re.search(r"cycles=(\d+)", content)
            if m:
                return int(m.group(1)) + 1
    except Exception:
        pass
    return 1


def _unban_uids_in_cluster(room_id: int, uids: list[str], clear_all: bool = False) -> int:
    clean_uids = []
    for u in (uids or []):
        parts = re.split(r"[\xba\ufffd\x00-\x1f\x7f\s]+", str(u).strip())
        if parts and parts[0]:
            clean_uids.append(parts[0])
    p = f"/opt/dst-admin/data/saves/Cluster_{room_id}/blocklist.txt"
    if clear_all:
        py = f"open('{p}','wb').close()"
        lua = "TheNet:SetBlacklist({})"
    else:
        if not clean_uids:
            return 0
        uids_repr = repr(clean_uids)
        py = (
            f"u={uids_repr}; lines=open('{p}','rb').read().splitlines() if __import__('os').path.exists('{p}') else []; "
            f"kept=[l for l in lines if not any(l.decode('utf-8','ignore').strip().startswith(x) or (x in l.decode('utf-8','ignore')) for x in u)]; "
            f"open('{p}','wb').write(b'\\n'.join(kept)+(b'\\n' if kept else b'')); print(len(lines)-len(kept))"
        )
        lua_cond = " and ".join([f'v.userid~="{x}"' for x in clean_uids if x.startswith("KU_")])
        lua = f"local bl=TheNet:GetBlacklist(); local n={{}}; for _,v in ipairs(bl) do if {lua_cond or 'true'} then table.insert(n,v) end end; TheNet:SetBlacklist(n)"
    out = _run_wsl_cmd(f'python3 -c "{py}"')
    _dst_console(room_id, lua)
    return int(out.strip()) if out and out.strip().isdigit() else 0


def _resolve_target_uids(param: str, cluster_id: int | None = None) -> list[str]:
    kw = param.strip()
    if not kw:
        return []
    if kw.startswith("KU_"):
        return [kw.split()[0]]
    # 精确匹配优先，其次才是子串；指定了服务器就只在该服内匹配，避免 1/2 服同名串号
    circles = (cluster_id,) if cluster_id in (1, 2) else (1, 2)
    exact, fuzzy = [], []
    for cid in circles:
        room_id = MCP_ROOM_IDS.get(cid, "")
        if not room_id:
            continue
        try:
            items = _mcp_call("api_listRoomPlayers", {
                "path": {"roomId": room_id}, "query": {"query": kw},
            }).get("items") or []
        except Exception:
            continue
        for p in items:
            u = str(p.get("id") or "").strip()
            nick = str(p.get("name") or "").strip()
            if not u or u in exact or u in fuzzy:
                continue
            if kw.lower() == nick.lower():
                exact.append(u)
            elif kw.lower() in nick.lower():
                fuzzy.append(u)
    return exact or fuzzy or [kw]


def _live_player_count(cluster_id: int) -> int:
    if MCP_TOKEN:
        if not MCP_ROOM_IDS.get(cluster_id):
            return 0  # 未接入面板的服没有玩家数据，也别再回退到已停用的旧后台
        try:
            return len(_get_cluster_info(cluster_id).get("players") or [])
        except Exception as e:
            logger.warning(f"[MCP] 在线人数读取失败: {e}")
            return 0
    return 0


def _is_group_admin(event: AstrMessageEvent) -> bool:
    """机器人主人恒为管理员；其余按群内真实身份 owner/admin 判定"""
    if str(event.get_sender_id() or "").strip() == MASTER_QQ:
        return True
    raw = getattr(event.message_obj, "raw_message", None)
    if not raw:
        return False
    
    role = None
    if isinstance(raw, dict):
        sender = raw.get("sender")
        if isinstance(sender, dict):
            role = sender.get("role")
    elif hasattr(raw, "sender"):
        sender = raw.sender
        if hasattr(sender, "role"):
            role = sender.role
        elif isinstance(sender, dict):
            role = sender.get("role")
    
    if role:
        role_str = str(role).lower()
        return role_str in ("owner", "admin", "administrator")
    return False


# ---------- 三档权限：L1 群主 / L2 管理员 / L3 普通成员 ----------
# 读写判定优先用面板自己声明的 readOnlyHint，猜名字只做兜底
_READ_RE = re.compile(r"^(api_(get|list|search|describe|check|analyze|capture|download|detect|discover|export|preview|validate|query|find|inspect)|query_dst_|search_dst_)")
_PLATFORM_RE = re.compile(r"(Node|Software|AIToken|Notification|Password|SystemSettings|DistributedBackup|StructuredLog|AgentRelease|AgentRuntime|ArtworkPack|LuaJIT|GameInstallation|SaveImport|ClusterToken)")
_DST_TOOLS = {
    "query_dst_server_status", "manage_dst_server", "query_dst_backups_or_admins",
    "query_dst_chat_history", "query_dst_history_events", "query_dst_map", "query_dst_wiki",
    "search_dst_mod", "execute_dst_console_command", "query_dst_system_health",
    "query_dst_player_archive", "query_dst_player_inventory", "query_dst_help_guide",
    "query_dst_room_config", "search_dst_prefab",
}


def _tier_of(event: AstrMessageEvent) -> int:
    """1=群主(本人) 2=群管理员 3=普通成员"""
    if str(event.get_sender_id() or "").strip() == MASTER_QQ:
        return 1
    return 2 if _is_group_admin(event) else 3


_alog = logging.getLogger("astrbot")   # 插件 logger 在钩子里会丢输出，运行期日志统一走根 logger
_TIER_NAME = {1: "群主", 2: "管理员", 3: "普通成员"}
_ROLE_LABEL = {
    1: "【群主】(拥有全部权限：服务器运维 + 平台设置 + 底层控制台。技术上做得到的必须照做；做不到的必须当场说明卡在哪，严禁编造执行结果。注意：权限决定能不能做，不决定能不能说，对外口径一律按【铁律四】)",
    2: "【管理员】(拥有服务器运维权限：开服停机、保存回档、踢人封禁、模组、世界设置、底层控制台。平台设置类接口你没有，别去试；技术上做得到的必须照做，做不到的当场说明原因，严禁编造执行结果。注意：权限决定能不能做，不决定能不能说，对外口径一律按【铁律四】)",
    3: "【普通成员】(仅具备只读查询权限，严禁调用任何写操作；涉及修改服务器一律说明需要找群管理)",
}


_MCP_SERVER = "dst-admin"


def _is_panel_tool(t) -> bool:
    """面板工具未必叫 api_*（room_status / world_console 这些就不是），按来源认"""
    return getattr(t, "mcp_server_name", None) == _MCP_SERVER


def _is_read_only(t) -> bool:
    ro = getattr(getattr(getattr(t, "mcp_tool", None), "annotations", None), "readOnlyHint", None)
    return bool(ro) if ro is not None else bool(_READ_RE.match(t.name))


def _panel_tags(t) -> list:
    """面板 v1.3.4 起在 _meta 里给的业务标签，一个工具可挂多个"""
    meta = (getattr(getattr(t, "mcp_tool", None), "meta", None) or {}).get("io.github.lcy0828.dst-admin/tool") or {}
    return meta.get("tags") or []


# ---- 工具语义索引：向量 + AI 场景词；MCP 工具指纹不变就直接复用缓存 ----
_INDEX_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tool_index.json")
_EMBED_MODEL = "Qwen/Qwen3-Embedding-8B"
_EMBED_URL = "https://api.siliconflow.cn/v1/embeddings"
_EMBED_DIM = 1024
_INDEX_VERSION = "v8"   # 场景词 prompt 有实质改动时递增，指纹随之变化并自动重建
_tool_vecs: dict = {}
_tool_metas: dict = {}
_index_hash = ""
_index_ready = False
_index_task = None
_index_building = False
_embed_key_cache = ""


def _embed_key() -> str:
    """按 配置项 → 环境变量 → 本机 Hindsight 的顺序取 embedding 密钥。

    都没有就返回空串，调用方会跳过语义检索、退回关键词匹配，
    不会因为缺密钥而报错。
    """
    global _embed_key_cache
    if not _embed_key_cache:
        _embed_key_cache = (EMBED_API_KEY or os.environ.get("DST_EMBED_API_KEY", "")).strip()
    if not _embed_key_cache:
        try:
            _embed_key_cache = _run_wsl_cmd(
                "grep -E '^HINDSIGHT_API_EMBEDDINGS_OPENAI_API_KEY=' "
                "/root/.hindsight/profiles/hermes.env 2>/dev/null | tail -1 | cut -d= -f2-", timeout=5
            ).strip()
        except Exception:
            _embed_key_cache = ""
    return _embed_key_cache


def _embed_sync(texts: list) -> list:
    key = _embed_key()
    if not key or not texts:
        return []
    import urllib.request
    body = json.dumps({"model": _EMBED_MODEL, "input": texts, "dimensions": _EMBED_DIM}).encode()
    req = urllib.request.Request(_EMBED_URL, data=body, headers={
        "Authorization": "Bearer " + key, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=45) as r:
        d = json.loads(r.read().decode())
    return [x["embedding"] for x in d.get("data") or []]


def _llm_sync(prompt: str, system: str = "", timeout: int = 200) -> str:
    """直连本地网关生成元数据，可指定 reasoning_effort=max；AstrBot 的 text_chat 传不了这个参数"""
    import urllib.request
    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    msgs.append({"role": "user", "content": prompt})
    body = json.dumps({
        "model": "auto/deepseek-v4.1-flash",
        "messages": msgs,
        "reasoning_effort": "max",
        "stream": False,
    }).encode()
    req = urllib.request.Request("http://127.0.0.1:3425/v1/chat/completions", data=body, headers={
        "Content-Type": "application/json", "Authorization": "Bearer public"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.loads(r.read().decode())
    return ((d.get("choices") or [{}])[0].get("message") or {}).get("content") or ""


_META_KEY = "io.github.lcy0828.dst-admin/tool"
_official_meta: dict = {}


def _fetch_official_meta():
    """直连 MCP 拉面板官方中文元数据（v1.3.6+）；返回 (工具名->元数据, catalogRevision)"""
    if not MCP_TOKEN:
        return {}, ""
    url = MCP_ENDPOINT + ("&" if "?" in MCP_ENDPOINT else "?") + "view=all"
    out, rev, cursor = {}, "", None
    try:
        for _ in range(20):
            body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list",
                               "params": {"cursor": cursor} if cursor else {}}).encode()
            req = urllib.request.Request(url, data=body, headers={
                "Authorization": f"Bearer {MCP_TOKEN}",
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            })
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read().decode("utf-8", "replace")
            payload = None
            if raw.lstrip().startswith(("event:", "data:")):
                for line in raw.splitlines():
                    if line.startswith("data:"):
                        payload = json.loads(line[5:].strip())
                        break
            else:
                payload = json.loads(raw)
            if not payload:
                break
            res = payload.get("result") or {}
            for t in res.get("tools") or []:
                m = (t.get("_meta") or {}).get(_META_KEY) or {}
                if m.get("cn_purpose"):
                    out[t["name"]] = m
                    rev = m.get("catalogRevision") or rev
            cursor = res.get("nextCursor")
            if not cursor:
                break
    except Exception as e:
        _alog.warning(f"[索引] 拉官方元数据失败: {e}")
    return out, rev


def _tool_fingerprint(tools) -> str:
    import hashlib
    raw = _INDEX_VERSION + "\n" + "\n".join(sorted(f"{t.name}|{(t.description or '').strip()}" for t in tools))
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _cos(a, b) -> float:
    import math
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return (sum(x * y for x, y in zip(a, b)) / (na * nb)) if na and nb else 0.0


def _index_load() -> bool:
    global _tool_vecs, _tool_metas, _index_hash, _index_ready
    try:
        with open(_INDEX_PATH, encoding="utf-8") as f:
            d = json.load(f)
        if d.get("model") != _EMBED_MODEL or d.get("dim") != _EMBED_DIM:
            return False
        _tool_vecs = d.get("vecs") or {}
        _tool_metas = d.get("metas") or {}
        _index_hash = d.get("hash") or ""
        _index_ready = bool(_tool_vecs)
        return _index_ready
    except Exception:
        return False


def _index_save():
    try:
        with open(_INDEX_PATH, "w", encoding="utf-8") as f:
            json.dump({"hash": _index_hash, "model": _EMBED_MODEL, "dim": _EMBED_DIM,
                       "metas": _tool_metas, "vecs": _tool_vecs}, f, ensure_ascii=False)
    except Exception as e:
        _alog.warning(f"[索引] 缓存写入失败: {e}")


_BATCH_SYS_PROMPT = """你是资深的《饥荒联机版》（Don't Starve Together, DST）专用服务器运维专家与搜索算法工程师。
你的任务是把英文描述的 MCP 面板管理工具，转成高质量的中文检索增强元数据，用于匹配服主在群聊里的口语提问。

### 一、饥荒领域背景
- 核心概念：Cluster（集群/档位）、Shard/World（分片/世界，地面 Master 与洞穴 Caves）、Room（房间）、Klei Token（令牌）。
- 高频运维场景：崩服/闪退、卡顿/跳 ping、回档（Rollback）、存档损坏/炸档、Mod 报错或更新失败、白名单/管理员权限、黑名单封禁、定时重启、查熊孩子。

### 二、生成规则
1. **业务转译而非字面直译**：先判断接口在饥荒开服里对应什么底层机制（server_log.txt / cluster.ini / modoverrides.lua / 控制台指令 / 面板外围服务），再写接地气的中文用途。
2. **主动推导故障症状视角**：不要只做同义词替换。反推「服务器出现什么故障、报错或玩家投诉时，服主会需要调用它」。
   - 查询/日志/状态类 → 绑定「异常现象 + 排查意图」
   - 动作/修改类 → 绑定「补救目的 + 口语指令」
2.1 **症状词必须绑定动作，不能只写现象**：
   - 每条 symptom_queries 必须包含「这个工具能做什么」的动词或宾语（看容器 / 查日志 / 踢人 / 回档 / 开关模组）
   - 反例：「服务器卡顿」——任何工具都能挂上，等于没写
   - 正例：「看下容器还在不在」「拉一下崩服日志」
2.2 **高频症状独占**：卡死、卡顿、崩服、闪退、掉线、转圈、进不去、炸档、开挂 这 9 个词，**只允许出现在真正能定位或修复它们的工具里**（日志类、诊断类、状态类、回档类）。
   - 列表类（list*/get* 查清单）、通知类、配置类、容器类**严禁**使用这些词
   - 不确定这个工具能不能解决该现象，就不要写
2.3 **symptom_queries 必须是用户的第一人称口语提问**：
   - 禁止出现工具名（api_xxx）、字段名、英文标识符
   - 禁止描述接口本身的缺陷或异常（如「接口超时」「分页卡住」「返回缺段」「列表为空」）——那是开发文档，不是用户提问
   - 每条都要像服主在群里随口打的一句话，可以省略主语
   - 反例：「api_listRoomChatLogs 只返回聊天部分，世界日志列表为空」❌
   - 正例：「谁昨天在群里骂人，翻下聊天记录」✅
3. **主语强绑定**：先判定 domain_tier——房间/世界/分片/玩家/模组/存档核心操作为 core_world；通知推送/Webhook/定时任务/面板设置/备份归档等外围功能为 aux_system。**aux_system 的所有词必须带明确主语**（如「通知投递失败」「重发告警消息」），严禁「卡死了」「失败了」「重试一下」这类无主语泛化词。

### 三、输出格式
- 只输出一个合法 JSON 对象，不要任何解释文字、不要 Markdown 代码块。
- 输入列表里的**每一个工具名都必须作为顶层 Key 出现，绝对禁止遗漏**。
- 每个工具对象必须包含 5 个字段：
  - "domain_tier": "core_world" 或 "aux_system"
  - "cn_domain_purpose": 30~60 字中文用途说明
  - "explicit_actions": 数组，4~5 个明确功能指令或同义词
  - "symptom_queries": 数组，4~6 个服主遇到故障时的口语提问
  - "dst_slang_and_terms": 数组，3~5 个饥荒黑话/简称/技术词"""

_SINGLE_SYS_PROMPT = """你是《饥荒联机版》（Don't Starve Together）服务器运维专家。
请针对给出的这 1 个 MCP 工具，生成完整的中文检索增强 JSON。

### 严格要求
1. 直接输出包含 5 个字段的根级 JSON 对象（不要外层包裹工具名 Key）：
   {"domain_tier": "...", "cn_domain_purpose": "...", "explicit_actions": [...], "symptom_queries": [...], "dst_slang_and_terms": [...]}
2. 词数要求：explicit_actions 4~5 个；symptom_queries 4~6 个；dst_slang_and_terms 3~5 个；去重后总数不少于 11 个。
3. 若属于通知/告警/Webhook/面板配置等外围功能（aux_system），所有词必须带明确模块主语，严禁「卡死」「报错」「重试」这类无主语词。"""

_FORBIDDEN_BARE = {
    "卡死", "卡死了", "崩了", "服务器崩了", "掉线", "掉线了", "闪退",
    "没反应", "重启", "重启一下", "报错", "失败", "状态", "怎么回事", "查原因",
}

_AUX_HINTS = ("通知", "推送", "告警", "消息", "webhook", "邮件", "钉钉", "飞书", "企微",
              "企业微信", "面板", "账号", "密码", "登录", "审计", "订阅", "投递",
              "任务", "节点", "代理", "执行端", "agent", "版本", "升级", "回滚",
              "自检", "部署", "程序包", "安装包", "图包", "画册", "脚本包", "lua", "模型")

_API_DEFECT_WORDS = ("接口", "分页", "超时", "缺段", "返回", "字段", "请求", "响应", "参数", "报 4", "HTTP")


def _prune_list_symptoms(name: str, syms: list) -> list:
    """剔除「接口缺陷描述」和「带工具名的自指句」，这两类是开发文档不是用户提问；其余保留"""
    out = []
    for s in syms:
        if "api_" in s or name in s:
            continue
        if sum(1 for w in _API_DEFECT_WORDS if w in s) >= 2:
            continue
        out.append(s)
    return out


def _extract_json_dict(raw: str) -> dict:
    if not raw:
        return {}
    s = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL | re.IGNORECASE).strip()
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", s, flags=re.DOTALL | re.IGNORECASE)
    if m:
        s = m.group(1).strip()
    else:
        a, b = s.find("{"), s.rfind("}")
        if a != -1 and b > a:
            s = s[a:b + 1]
    for cand in (s, re.sub(r",\s*([}\]])", r"\1", s)):
        try:
            d = json.loads(cand)
            if isinstance(d, dict):
                return d
        except Exception:
            continue
    return {}


def _clean_list(val) -> list:
    items = []
    if isinstance(val, list):
        items = [str(x).strip() for x in val if x is not None]
    elif isinstance(val, str):
        items = [x.strip() for x in re.split(r"[,，;；、\n]+", val) if x.strip()]
    seen, out = set(), []
    for s in items:
        s = re.sub(r"^\d+[\.、\)\s]+", "", s).strip(" \"'“”‘’")
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def _infer_tier(name: str, desc: str) -> str:
    c = f"{name} {desc}".lower()
    if any(p in c for p in ("notif", "webhook", "delivery", "alert", "email", "mail",
                            "audit", "token", "login", "auth", "schedule", "cron",
                            "setting", "metric", "software", "node", "kubernetes")):
        return "aux_system"
    return "core_world"


def _normalize_meta(raw: dict, name: str, desc: str) -> dict:
    lm = {str(k).strip().lower(): v for k, v in raw.items()}

    def pick(keys, default):
        for k in keys:
            if k in lm and lm[k] is not None:
                return lm[k]
        return default

    tier = str(pick(("domain_tier", "tier", "domain", "category"), "")).strip().lower()
    if tier not in ("core_world", "aux_system"):
        tier = _infer_tier(name, desc)
    acts = _clean_list(pick(("explicit_actions", "actions", "commands"), []))
    syms = _clean_list(pick(("symptom_queries", "symptoms", "queries", "user_queries"), []))
    slang = _clean_list(pick(("dst_slang_and_terms", "slang", "terms", "keywords"), []))
    if tier == "aux_system":
        acts = [w for w in acts if w not in _FORBIDDEN_BARE]
        syms = [w for w in syms if w not in _FORBIDDEN_BARE]
        slang = [w for w in slang if w not in _FORBIDDEN_BARE]
    syms = _prune_list_symptoms(name, syms)
    return {
        "domain_tier": tier,
        "cn_domain_purpose": str(pick(("cn_domain_purpose", "purpose", "cn_purpose", "description", "desc"), "")).strip(),
        "explicit_actions": acts,
        "symptom_queries": syms,
        "dst_slang_and_terms": slang,
    }


def _validate_meta(m: dict, final: bool = False):
    purpose = m.get("cn_domain_purpose") or ""
    if len(purpose) < 8:
        return False, f"用途过短({len(purpose)})"
    a, s, g = m["explicit_actions"], m["symptom_queries"], m["dst_slang_and_terms"]
    total = len(set(a + s + g))
    if not final:
        if len(a) < 3:
            return False, f"actions 不足({len(a)})"
        if len(s) < 3:
            return False, f"symptoms 不足({len(s)})"
        if len(g) < 2:
            return False, f"slang 不足({len(g)})"
        if total < 10:
            return False, f"总词数不足({total})"
    else:
        if len(s) < 2 or total < 6:
            return False, f"兜底仍未达标({total})"
    return True, "ok"


def _build_index_text(name: str, desc: str, meta: dict, tags: list) -> str:
    """五行结构：中文释义打头（因果注意力前部锚点），英文原述后置；不加 tier 公共前缀，避免向量被拉向同一方向"""
    purpose = meta.get("cn_domain_purpose") or ""
    syms = meta.get("symptom_queries") or []
    acts = meta.get("explicit_actions") or []
    slang = meta.get("dst_slang_and_terms") or []
    parts = [
        purpose,
        f"【核心排查场景】{' / '.join(syms[:2])}" if syms else "",
        f"【常见故障提问】{'？'.join(syms)}" if syms else "",
        f"【管理操作指令】{'、'.join(acts)}" if acts else "",
        f"【饥荒术语黑话】{'、'.join(slang)}" if slang else "",
        f"【返回结果】{meta.get('result_summary')}" if meta.get("result_summary") else "",
        f"【接口定义】{name}: {desc}",
    ]
    if tags:
        parts.append(f"【面板标签】{','.join(tags)}")
    return "\n".join(p for p in parts if p)


def _format_query(q: str) -> str:
    """Qwen3-Embedding 的 Instruct 前缀 + 无主语短句补全领域锚点"""
    low = q.strip().lower()
    has_aux = any(k in low for k in _AUX_HINTS)
    hint = "" if has_aux or len(q) > 15 else "（默认针对饥荒联机版游戏世界、房间分片、模组或存档运行状态）"
    return (
        "Instruct: Given a user's spoken query about Don't Starve Together (饥荒联机版) "
        "server administration, retrieve the most relevant MCP management tool.\n"
        f"Query: {q}{hint}"
    )


def _rerank(query: str, cand: list, top: int) -> list:
    """用 Qwen3-Reranker 精排；失败则回退原向量序"""
    if len(cand) <= top:
        return cand
    try:
        import urllib.request
        docs = [_build_index_text(n, "", _tool_metas.get(n) or {}, []) for n in cand]
        body = json.dumps({"model": "Qwen/Qwen3-Reranker-8B", "query": query, "documents": docs,
                           "top_n": top, "return_documents": False}).encode()
        req = urllib.request.Request("https://api.siliconflow.cn/v1/rerank", data=body, headers={
            "Authorization": f"Bearer {_embed_key()}", "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            res = json.loads(r.read())["results"]
        return [cand[x["index"]] for x in res if 0 <= x["index"] < len(cand)]
    except Exception as e:
        _alog.warning(f"[检索] rerank 失败，回退向量序: {e}")
        return cand[:top]


def _vec_rank(query: str, pool, top: int = 15) -> list:
    qv = _embed_sync([_format_query(query)])
    if not qv:
        return []
    low = query.strip().lower()
    has_aux = any(k in low for k in _AUX_HINTS)
    allowed = {t.name for t in pool}
    scored = []
    for n, v in _tool_vecs.items():
        if n not in allowed:
            continue
        s = _cos(qv[0], v)
        m = _tool_metas.get(n) or {}
        if not has_aux and m.get("domain_tier") == "aux_system":
            s -= 0.08
        scored.append((s, n))
    scored.sort(reverse=True)
    cand = [n for s, n in scored if s >= 0.25][:30]
    out = _rerank(query, cand, top)
    _alog.info(f"[检索] 「{query}」向量{len(cand)}候选 → 精排{len(out)}：{out[:3]}")
    return out


def _tool_allowed(t, tier: int) -> bool:
    """平台层只给群主；写操作给管理员；只读给所有人。加载器本身不改数据，人人可用"""
    if t.name in _META_TOOLS:
        return True
    if t.name == "execute_dst_console_command":
        return True   # 内部再分流：count_prefab 只读模板给所有人，自定义 lua_code 只给管理员
    if _PLATFORM_RE.search(t.name):
        return tier == 1
    return True if _is_read_only(t) else tier <= 2


# 面板接口按名字分 12 类，顺序即优先级（前面的先匹配）
_CATEGORY_RULES = [
    ("模组", r"Mod|Workshop|Steam"),
    ("游戏版本", r"Version|Release|Install|Update"),
    ("存档与备份", r"Backup|Snapshot|Restore|Archive|Save"),
    ("玩家管理", r"Player|Admin|Ban|Kick|Access|Whitelist"),
    ("房间与开服", r"Room|Cluster|Topology|Shard|Link"),
    ("世界与运行时", r"World|Runtime|Process|Status|Health|Performance|Metric"),
    ("命令与自动化", r"Command|Console|Automation|Schedule|Cron|Task|Job"),
    ("日志与诊断", r"Log|Diagnostic|Event|Receipt|Result|Call|Check"),
    ("内容查询", r"Entity|Prefab|Item|Recipe|Wiki|Map|Search|Directory|Catalog"),
    ("平台·AI与通知", r"AIToken|Notification|Agent|configureAI|SMTP|AIProvider|Chat|Model"),
    ("平台·部署与节点", r"Node|Software|Container|Kubernetes|Fleet|Egress|Proxy|Setup|Capabilit|ClusterToken|LuaJIT|ArtworkPack|DistributedBackup"),
    ("平台·系统设置", r"SystemSettings|Password|Secret|Credential|StructuredLog"),
]
_META_TOOLS = {"search_dst_tools", "call_dst_tool"}


def _category_of(name: str) -> str:
    for cat, pat in _CATEGORY_RULES:
        if re.search(pat, name, re.I):
            return cat
    return "其他"


def _categorize(tools) -> dict:
    """把工具按分类归堆，保持 _CATEGORY_RULES 的顺序"""
    order = [c for c, _ in _CATEGORY_RULES] + ["其他"]
    out: dict[str, list] = {}
    for t in tools:
        out.setdefault(_category_of(t.name), []).append(t)
    return {c: out[c] for c in order if c in out}


def _build_tool_set(tier: int, func_list, gid: str = "") -> list:
    """常驻只挂本插件工具 + 两个检索/执行器，面板接口按需查、按需调"""
    return [t for t in func_list if (t.name in _DST_TOOLS or t.name in _META_TOOLS) and _tool_allowed(t, tier)]


_INI_SECRET_RE = re.compile(r"^\s*([A-Za-z0-9_]*(?:password|passwd|pwd|token|secret|_key|port|_ip)[A-Za-z0-9_]*)\s*=", re.I)


def _redact_ini(text: str) -> str:
    """把 ini 里的密码/端口/密钥类键值替换掉，避免配置查询把内部信息带进群。"""
    out = []
    for line in (text or "").split("\n"):
        m = _INI_SECRET_RE.match(line)
        out.append(f"{m.group(1)} = （内部信息，不展示）" if m else line)
    return "\n".join(out)


_URL_IN_TEXT = re.compile(r"https?://[^\s，。；）)]+|(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?")
_PATH_IN_TEXT = re.compile(r"(?:[A-Za-z]:\\|/(?:root|opt|mnt|home|usr|var)/)[^\s，。；）)]*")


def _err_brief(e) -> str:
    """异常回显给模型前，抹掉内网地址与绝对路径（【铁律四】禁止项）。"""
    s = f"{type(e).__name__}: {e}"
    s = _URL_IN_TEXT.sub("（内部地址）", s)
    s = _PATH_IN_TEXT.sub("（内部路径）", s)
    return s[:160]


def _get_cluster_display_name(cluster_num: int, ini_content: str = "") -> str:
    """房间名实时读取 cluster.ini（游戏实际生效的权威来源），配置名仅作读取失败时的兜底"""
    if not ini_content:
        ini_content = _run_wsl_cmd(f"cat /opt/dst-admin/data/saves/Cluster_{cluster_num}/cluster.ini 2>/dev/null")
    m = re.search(r"cluster_name\s*=\s*(.+)", ini_content or "")
    if m:
        name = re.sub(r'[\U000f0000-\U000fffff\ue000-\uf8ff]', '', m.group(1)).strip()
        if name:
            return name
    return SERVER_NAMES.get(cluster_num, f"{cluster_num}服")


def _raw_field(obj, key, default=None):
    """aiocqhttp 的 raw_message 是 Event 对象，别的适配器可能是 dict，统一取值。"""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


_MCP_WORLD_IDS = {"Master": "TWFzdGVy", "Caves": "Q2F2ZXM"}
# c_findnext 数完一圈会回到第一个实体、不返回 nil，所以用「再次遇到 head」作为终止条件。
# shortcut: 单次最多数 500 个（输出会随实体数线性增长，面板输出有上限），超量场景再改分批。
_COUNT_LUA = ("(function() local p='{p}' local head=c_findnext(p) local n=0 "
              "if head then n=1 local e=c_findnext(p) local g=0 "
              "while e and e~=head and g<500 do n=n+1 g=g+1 e=c_findnext(p) end end "
              "print('COUNT='..n) end)()")

_SEASON_CN = {"autumn": "秋", "winter": "冬", "spring": "春", "summer": "夏"}
_SEASON_ICON = {"春": "🌱", "夏": "☀️", "秋": "🍂", "冬": "❄️"}


_MCP_SEM = threading.Semaphore(2)  # 面板 3 路并发就 429，全局锁死 2 路


def _mcp_call(tool: str, args: dict, timeout: int = 25) -> dict:
    """调用 MCP 工具并统一解包。

    便捷工具（room_status 等）的 data 就是业务数据；
    api_* 工具会多包一层 {data, httpStatus, operationId}，这里一并抹平。
    """
    if not MCP_TOKEN:
        raise RuntimeError("MCP token 未配置")
    body = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": tool, "arguments": args},
    }).encode("utf-8")
    req = urllib.request.Request(MCP_ENDPOINT, data=body, method="POST", headers={
        "Authorization": f"Bearer {MCP_TOKEN}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    })
    with _MCP_SEM:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))

    result = payload.get("result", {})
    if result.get("isError"):
        detail = ""
        try:
            detail = str(result["content"][0].get("text", ""))[:200]
        except Exception:
            pass
        raise RuntimeError(f"MCP {tool} 失败: {detail}")

    content = result.get("content") or []
    if not content:
        return {}
    outer = json.loads(content[0].get("text") or "{}")
    data = outer.get("data", {})
    if isinstance(data, dict) and "httpStatus" in data:
        # 关键：面板把 HTTP 状态码埋在 data.httpStatus 里，MCP 层的 isError 不会置位。
        # 之前直接取 data.data 返回，导致 4xx/5xx 被当成成功（失败回执误报成功）。
        try:
            http_status = int(data.get("httpStatus") or 0)
        except (TypeError, ValueError):
            http_status = 0
        if http_status >= 400:
            inner = data.get("data") or {}
            detail = ""
            if isinstance(inner, dict):
                detail = (inner.get("message") or inner.get("error")
                          or inner.get("code") or inner.get("errorCode") or "")
            raise RuntimeError(f"MCP {tool} 失败 (HTTP {http_status}): {detail or inner or '无详情'}")
        return data.get("data") or {}
    return data


def _mcp_cluster_info_via_panel(cluster_num: int) -> dict:
    """兜底路径：用面板的 room_status / room_players 组装（两者各要现探游戏服，约 2 秒）。

    room_status 和 room_players 各自要主动探测游戏服、各约 1 秒，两者互不依赖，
    所以并发跑；不能开更多并发，面板限流 3 个并发就回 429。
    """
    room_id = MCP_ROOM_IDS.get(cluster_num, "")
    if not room_id:
        raise RuntimeError(f"{cluster_num}服未配置 MCP roomId")

    pool = ThreadPoolExecutor(max_workers=2)
    try:
        f_status = pool.submit(_mcp_call, "room_status", {"roomId": room_id})
        f_players = pool.submit(_mcp_call, "room_players", {"roomId": room_id, "limit": 100})

        rs = f_status.result()
        summary = rs.get("summary") or {}
        running = (summary.get("running") or 0) > 0

        info = {
            "num": cluster_num,
            "name": _get_cluster_display_name(cluster_num),
            "online": running,
            "day": None,
            "season": "",
            "remaining": 0,
            "players": [],
            "max_players": 16,
            "mod_count": 0,
        }
        if not running:
            return info  # 未运行就不等玩家查询了

        try:
            pl = f_players.result()
        except Exception as e:
            # 并发被面板限流（429）时重试一次，否则会静默显示 0 人在线
            logger.warning(f"[MCP] room_players 失败，重试: {e}")
            try:
                pl = _mcp_call("room_players", {"roomId": room_id, "limit": 100})
            except Exception as e2:
                logger.warning(f"[MCP] room_players 重试失败: {e2}")
                pl = {}
    finally:
        pool.shutdown(wait=False, cancel_futures=True)

    try:
        ws = _mcp_call("world_state", {"roomId": room_id, "worldId": _MCP_WORLD_IDS["Master"]})
        info["day"] = (ws.get("cycles") or 0) + 1
        raw_season = ws.get("season") or ""
        info["season"] = _SEASON_CN.get(raw_season, raw_season)
        info["remaining"] = ws.get("remainingDaysInSeason") or 0
    except Exception as e:
        logger.warning(f"[MCP] world_state 失败: {e}")

    players = []
    for p in (pl.get("items") or []):
        if not p.get("online"):
            continue
        pname = re.sub(r'[\ue000-\uf8ff\U000f0000-\U000fffff\u200b-\u200f\ufeff]',
                       '', p.get("name") or "未知").strip() or "未知"
        prefab = (p.get("prefab") or "").lower()
        cname = PREFAB_NAMES.get(prefab, prefab.capitalize() if prefab else "未知")
        players.append((pname, cname, p.get("id", "")))
    info["players"] = players

    try:
        cfg = _mcp_call("api_getRoomConfiguration", {"path": {"roomId": room_id}})
        vals = cfg.get("values") or {}
        if vals.get("clusterName"):
            info["name"] = vals["clusterName"]
        if vals.get("maxPlayers"):
            info["max_players"] = vals["maxPlayers"]
    except Exception as e:
        logger.warning(f"[MCP] getRoomConfiguration 失败: {e}")

    try:
        mods = _mcp_call("room_mods", {"roomId": room_id, "limit": 100})
        info["mod_count"] = len(mods.get("items") or [])
        info["mods"] = [_mod_display_name(i) for i in (mods.get("items") or [])]
    except Exception as e:
        logger.warning(f"[MCP] room_mods 失败: {e}")

    return info


# 一次注入取回天数/季节/剩余天数 + 全集群玩家名单；地面再补一份 AllPlayers 做世界归属。
# 面板会把 print 输出直接回传在 output.text 里，不用回读日志。
_LUA_MASTER = (
    'local w=TheWorld and TheWorld.state; '
    'if w then print("DSTAI|W|"..tostring(w.cycles).."|"..tostring(w.season)'
    '.."|"..tostring(w.remainingdaysinseason)) end; '
    'for _,v in ipairs(TheNet:GetClientTable() or {}) do '
    'if v.name and v.userid and v.name ~= "[Host]" then '
    'print("DSTAI|C|"..v.name.."|"..v.userid.."|"..tostring(v.prefab or "")) end end; '
    'for _,p in ipairs(AllPlayers or {}) do '
    'if p and p.userid and p.name then print("DSTAI|A|"..p.name.."|"..p.userid) end end'
)
_LUA_SHARD_PLAYERS = (
    'for _,p in ipairs(AllPlayers or {}) do '
    'if p and p.userid and p.name then print("DSTAI|A|"..p.name.."|"..p.userid) end end'
)


def _mcp_cluster_info_via_lua(cluster_num: int, with_world: bool = False) -> dict:
    room_id = MCP_ROOM_IDS.get(cluster_num, "")
    if not room_id:
        raise RuntimeError(f"{cluster_num}服未配置 MCP roomId")

    cfg = _mcp_call("api_getRoomConfiguration", {"path": {"roomId": room_id}})
    vals = cfg.get("values") or {}
    name = vals.get("clusterName") or _get_cluster_display_name(cluster_num)
    max_players = vals.get("maxPlayers") or 16

    def _run(world_id: str, lua: str) -> str:
        world = "Caves" if world_id == _MCP_WORLD_IDS.get("Caves") else "Master"
        ok, text = _console_read(cluster_num, lua, world)
        if not ok:
            raise RuntimeError("Lua 未确认执行")
        return text

    # 同一房间的两个分片不能并发下发命令：面板会判 Runtime 未就绪并拒发（实测）
    master = _run(_MCP_WORLD_IDS["Master"], _LUA_MASTER)
    caves_text = ""

    day, season, remaining = None, "", 0
    roster, where = [], {}
    for line in master.splitlines():
        if line.startswith("DSTAI|W|"):
            _, _, cycles, raw_season, rem = line.split("|")[:5]
            day = int(cycles) + 1
            season = _SEASON_CN.get(raw_season, raw_season)
            remaining = int(rem or 0)
        elif line.startswith("DSTAI|C|"):
            parts = line.split("|")
            if len(parts) >= 5:
                roster.append((parts[2], parts[3], parts[4]))
        elif line.startswith("DSTAI|A|"):
            parts = line.split("|")
            if len(parts) >= 3:
                where[parts[2]] = "地面"
    if day is None:
        raise RuntimeError("Lua 输出里没有世界状态")

    # 洞穴是独立进程：只有详情卡需要世界归属，且地面查不到的人才可能在地下
    if with_world and any(n not in where for n, _, _ in roster):
        try:
            caves_text = _run(_MCP_WORLD_IDS["Caves"], _LUA_SHARD_PLAYERS)
        except Exception as e:
            logger.warning(f"[MCP] 洞穴玩家查询失败: {e}")
    for line in caves_text.splitlines():
        if line.startswith("DSTAI|A|"):
            parts = line.split("|")
            if len(parts) >= 3:
                where[parts[2]] = "洞穴"

    players = []
    for pname, uid, prefab in roster:
        key = (prefab or "").lower()
        cname = PREFAB_NAMES.get(key, key.capitalize() if key else "未知")
        players.append((pname, cname, uid, where.get(pname, "")))

    mods = _mcp_call("room_mods", {"roomId": room_id, "limit": 100})
    return {
        "num": cluster_num,
        "name": name,
        "online": True,
        "day": day,
        "season": season,
        "remaining": remaining,
        "players": players,
        "max_players": max_players,
        "mod_count": len(mods.get("items") or []),
        "mods": [_mod_display_name(i) for i in (mods.get("items") or [])],
    }


def _mcp_cluster_info(cluster_num: int, with_world: bool = False) -> dict:
    """优先走一次 Lua 注入（约 0.3 秒），失败再回退面板查询（约 2 秒）。"""
    try:
        return _mcp_cluster_info_via_lua(cluster_num, with_world)
    except Exception as e:
        logger.warning(f"[MCP] Lua 取数失败，回退面板查询: {e}")
        return _mcp_cluster_info_via_panel(cluster_num)


def _get_cluster_info(cluster_num: int, with_world: bool = False) -> dict:
    # 优先走 MCP（新面板 dst-admin）
    if MCP_TOKEN and MCP_ROOM_IDS.get(cluster_num):
        try:
            return _mcp_cluster_info(cluster_num, with_world)
        except Exception as e:
            logger.warning(f"[MCP] {cluster_num}服状态查询失败，回退旧路径: {e}")
    # MCP 已启用但该服未配 roomId：直接报未接入，不要再走已停用的旧面板路径（会卡住）
    return {
        "num": cluster_num,
        "name": _get_cluster_display_name(cluster_num),
        "online": False,
        "day": None,
        "season": "",
        "remaining": 0,
        "players": [],
        "max_players": 16,
        "mod_count": 0,
        "_not_connected": True,
    }

def get_dst_history_summary(cluster_id: int = 1) -> str:
    import re
    cluster_name = f"Cluster_{cluster_id}"
    # Windows 端的 glob/open 读不到 WSL 路径，必须经 wsl.exe 一次性批量取回全部快照
    raw = _run_wsl_cmd(
        f"cd /opt/dst-admin/data/saves/{cluster_name}/Master/save/session 2>/dev/null && "
        f"for f in $(ls */*.meta 2>/dev/null | sort); do echo '@@'$f; cat \"$f\" 2>/dev/null; echo; done"
    )
    
    season_ranges = []
    current_season = None
    start_day = 1
    max_day = 1
    
    for chunk in raw.split("@@")[1:]:
        try:
            txt = chunk
            m_c = re.search(r'cycles=(\d+)', txt)
            m_s = re.search(r'season="(\w+)"', txt)
            if m_c and m_s:
                day = int(m_c.group(1)) + 1
                season = m_s.group(1)
                max_day = max(max_day, day)
                if current_season is None:
                    current_season = season
                    start_day = day
                elif season != current_season:
                    season_ranges.append((current_season, start_day, day - 1))
                    current_season = season
                    start_day = day
        except Exception:
            pass
    if current_season:
        season_ranges.append((current_season, start_day, max_day))
        
    SEASONS_CN = {"autumn": "秋季", "winter": "冬季", "spring": "春季", "summer": "夏季"}
    s_name = _get_cluster_display_name(cluster_id)
    res_str = f"【{s_name} 历史编年史与大事件】\n\n【世界季节与天数演进历程】\n"
    for s, s_d, e_d in season_ranges:
        s_cn = SEASONS_CN.get(s, s)
        res_str += f"- 第 {s_d} ~ {e_d} 天: {s_cn}\n"
        
    cmd = f"ls -t /opt/dst-admin/data/saves/{cluster_name}/Master/server_chat_log.txt /opt/dst-admin/data/saves/{cluster_name}/Master/backup/server_chat_log/*.txt 2>/dev/null | head -n 4"
    chat_files = _run_wsl_cmd(cmd).splitlines()
    
    event_lines = []
    for cf in chat_files:
        try:
            if "backup" in cf:
                dname = os.path.dirname(cf).replace("server_chat_log", "server_log")
                fname = os.path.basename(cf).replace("server_chat_log_", "server_log_")
                prefix = fname[:27]
                matched = _run_wsl_cmd(f"ls '{dname}/{prefix}'*.txt 2>/dev/null | head -n 1").strip()
                sf = matched if matched else f"{dname}/{fname}"
            else:
                sf = f"/opt/dst-admin/data/saves/{cluster_name}/Master/server_log.txt"
            base_dt = _get_server_log_base_time(sf)
            # 名字-角色映射：认证行带名字、落地行带角色，用 UID 串起来
            player_prefabs = {}
            uid_name = {}
            for l in _run_wsl_cmd(f"grep 'Client authenticated: (' '{sf}'").splitlines():
                m = re.search(r"Client authenticated: \((KU_[^)]+)\)\s*(.+?)\s*$", l)
                if m:
                    uid_name[m.group(1)] = m.group(2)
            for l in _run_wsl_cmd(f"grep 'assigned ownership to entity' '{sf}'").splitlines():
                m = re.search(r"User ID\t(KU_[^\t]+)\tassigned ownership to entity\t\S+ - (\w+)", l)
                if m and m.group(1) in uid_name:
                    player_prefabs[uid_name[m.group(1)]] = PREFAB_NAMES.get(m.group(2).lower(), m.group(2))
            
            deaths = _run_wsl_cmd(f"grep -E 'Death Announcement|Resurrect Announcement' '{cf}'").splitlines()
            for d in deaths:
                formatted = _format_dst_log_line(d, base_dt)
                for pname, role in player_prefabs.items():
                    if pname in formatted:
                        formatted = formatted.replace(pname, f"{pname}(扮演角色:{role})")
                        break
                event_lines.append(formatted)
        except Exception:
            pass
            
    seen = set()
    dedup_events = []
    for ev in event_lines:
        if ev not in seen:
            seen.add(ev)
            dedup_events.append(ev)
            
    res_str += f"\n【历史玩家遇难与救援事件 (共 {len(dedup_events)} 起)】\n"
    for ev in dedup_events[:35]:
        res_str += f"- {ev}\n"
        
    return res_str.strip()

def format_status_text(cluster_filter: int = 0) -> str:
    ids = [c for c in (1, 2) if cluster_filter in (0, c)]
    if len(ids) > 1:
        # 两服是独立房间、面板按房间加锁，可以并行；并发保持在 2 路以内不会被限流
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=2) as ex:
            clusters = list(ex.map(_get_cluster_info, ids))
    else:
        clusters = [_get_cluster_info(c) for c in ids]

    lines = ["🐾 小鱼干营地 · 本喵来报", "实时状态来了喵～", "════════════════"]
    for idx, c in enumerate(clusters):
        if idx > 0:
            lines.extend(["", "────────────────", ""])
        else:
            lines.append("")
        if not c["online"]:
            tag = "未接入面板" if c.get("_not_connected") else "未运行"
            lines.append(f"⚪ {c['num']}服 · {tag}")
            continue
        players = c.get("players", [])
        max_p = c.get("max_players", 16)
        lines.append(f"🟢 {c['num']}服")
        if c["day"] is not None:
            lines.append(f"   第 {c['day']} 天 · {c['season']}")
        else:
            lines.append("   启动中")
        if c.get("remaining"):
            lines.append(f"   剩余 {c['remaining']} 天")
        lines.append(f"   在线 {len(players)}/{max_p} 人")
        lines.append(f"   模组 {c.get('mod_count', 0)} 个")
        if players:
            lines.append("")
            for p in players:
                lines.append(f"   · 〖{p[0]}〗（{p[1]}）")
    lines.extend(["", "喵～想查谁、想问啥，直接喊本喵就好"])
    return "\n".join(lines).strip()

def _get_cluster_recent_chats(cid: int, limit: int = 6) -> list[str]:
    p = f"/opt/dst-admin/data/saves/Cluster_{cid}/Master/server_chat_log.txt"
    out = _run_wsl_cmd(f"tail -n 100 {p} 2>/dev/null")
    if not out:
        return []
    chats = []
    try:
        lines = out.strip().splitlines()
        for line in reversed(lines):
            line = line.strip()
            if not line or "[Say]" not in line:
                continue
            m = re.search(r"\[Say\]\s*(?:\([^\)]+\)\s*)?([^:]+):\s*(.+)", line)
            if m:
                speaker = m.group(1).strip()
                msg = m.group(2).strip()
                msg = re.sub(r'[\ue000-\uf8ff\U000f0000-\U000fffff]', '', msg).strip()
                if not msg:
                    continue
                chats.append(f"{speaker}: \"{msg}\"")
                if len(chats) >= limit:
                    break
    except Exception:
        pass
    chats.reverse()
    return chats


def format_detail_status_text(cluster_num: int) -> str:
    _room_id = MCP_ROOM_IDS.get(cluster_num, "")
    c = _get_cluster_info(cluster_num, with_world=True)
    chats = _get_cluster_recent_chats(cluster_num)  # 本地文件约 30ms，面板接口要 2 秒
    lines = [f"🐾 小鱼干营地 · {cluster_num}服 · 本喵来报", c["name"], "════════════════", ""]
    if not c["online"]:
        lines.append("⚪ 未运行")
        return "\n".join(lines)

    lines.append("🟢 运行中")
    if c["day"] is not None:
        lines.append(f"   第 {c['day']} 天 · {c['season']}")
        if c.get("remaining"):
            lines.append(f"   剩余 {c['remaining']} 天")
    else:
        lines.append("   启动中")


    w_cpu = _get_world_cpu_loads()
    m_info = w_cpu.get(f"{cluster_num}_Master", {})
    c_info = w_cpu.get(f"{cluster_num}_Caves", {})
    m_str = f"{m_info.get('load', 0.0)}%" if m_info.get("online") else "离线"
    c_str = f"{c_info.get('load', 0.0)}%" if c_info.get("online") else "离线"
    lines.extend(["", "📊 负载", f"   地面 {m_str}", f"   洞穴 {c_str}"])

    players = c.get("players", [])
    max_p = c.get("max_players", 16)
    lines.extend(["", f"👥 在线 {len(players)}/{max_p} 人"])
    if players:
        for p in players:
            world = p[3] if len(p) > 3 else ""
            lines.append(f"   · 〖{p[0]}〗 · {p[1]}" + (f" · {world}" if world else ""))
    else:
        lines.append("   · 暂无玩家")

    if chats:
        lines.extend(["", "💬 最近公屏"])
        for c_msg in chats:
            lines.append(f"   · {c_msg}")

    mods = c.get("mods") or []

    lines.extend(["", f"📦 模组 {len(mods)} 个"])
    if mods:
        for m in mods:
            lines.append(f"   · {m}")
    else:
        lines.append("   · 无额外模组")
    lines.extend(["", "喵～这份是全套档案，还想细看哪块跟本喵说"])
    return "\n".join(lines).strip()


from astrbot.core.message.components import Image

WIN_ASSETS_DIR = ""  # 可选：本地百科图片目录，留空则不带图

# 辅助脚本随插件一起分发（scripts/ 目录），不再依赖 /root 下的绝对路径
_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
_SCRIPT_CPU = os.path.join(_PLUGIN_DIR, "scripts", "get_dst_cpu.py")
_SCRIPT_WIKI = os.path.join(_PLUGIN_DIR, "scripts", "query_wiki.py")


def extract_primary_illustrations(md_text: str) -> list[str]:
    """从知识库条目中提取核心主立绘/实物图（排除槽位、小头像、箭头等UI噪点）"""
    match = re.search(r'\*\*[^*]+\*\*\s*\n[^\n]*\n---\s*\n', md_text)
    if not match:
        return []
    start = match.end()
    lines = md_text[start:start+1200].splitlines()
    subject_lines = []
    for l in lines:
        if any(k in l for k in ["制作配方", "配方", "解锁", "来源", "代码", "掉落", "生命值", "食物", "战利品", "生成代码", "* * *"]):
            break
        subject_lines.append(l)
        if len(subject_lines) >= 4:
            break
    block = "\n".join(subject_lines)
    raw_imgs = re.findall(r'!\[.*?\]\((assets/[^)]+)\)', block)
    meaningful = []
    seen = set()
    for img in raw_imgs:
        low = img.lower()
        if any(k in low for k in ["slot_background", "interface_arrow", "meter", "portrait", "_filter", "icon"]):
            continue
        if img not in seen:
            seen.add(img)
            meaningful.append(img)
    return meaningful


def _get_world_cpu_loads() -> dict:
    try:
        script = _wsl_path(_SCRIPT_CPU)
        if not os.path.exists(_SCRIPT_CPU):
            return {}
        raw = _run_wsl_cmd(f"python3 {shlex.quote(script)}")
        if raw and raw.startswith("{"):
            return json.loads(raw)
    except Exception:
        pass
    return {}


def format_system_health_text(cluster_filter: int = 0) -> str:
    mem = 0.0
    updating = False
    try:
        sysinfo = _mcp_call("api_getSystemStatus", {})
        mem = round(float((sysinfo.get("memory") or {}).get("usage") or 0), 1)
    except Exception as e:
        logger.warning(f"[MCP] 系统状态读取失败: {e}")
    try:
        soft = _mcp_call("api_getSoftwareStatus", {})
        updating = bool((soft.get("check") or {}).get("hasUpdate"))
    except Exception:
        pass

    w_cpu = _get_world_cpu_loads()
    cid = int(cluster_filter) if str(cluster_filter) in ("1", "2") else 0
    if cid == 1:
        keys = ("1_Master", "1_Caves")
    elif cid == 2:
        keys = ("2_Master", "2_Caves")
    else:
        keys = ("1_Master", "1_Caves", "2_Master", "2_Caves")

    active_loads = [w_cpu[k]["load"] for k in keys if k in w_cpu and w_cpu[k].get("online")]
    max_load = max(active_loads) if active_loads else 0.0

    if max_load < 50 and mem < 80:
        grade = "🟢 极佳"
    elif max_load < 80 and mem < 90:
        grade = "🟡 良好"
    else:
        grade = "🔴 偏高"

    p1_cnt = _live_player_count(1)
    p2_cnt = _live_player_count(2)
    label = f"{cid}服" if cid else "营地"

    lines = ["🏥 服务器体检 · 本喵巡过一遍了", "════════════════", f"{label} · {grade}", ""]
    for k in keys:
        item = w_cpu.get(k)
        if not item:
            continue
        world_name = item["name"].replace(f"{cid}服", "", 1) if cid else item["name"]
        icon = "🌍" if "地面" in world_name else ("🕳️" if "洞穴" in world_name else "🌐")
        lines.append(f"{icon} {world_name}")
        if item.get("online"):
            lines.append(f"   负载 {item['load']}%")
            if item.get("mem"):
                lines.append(f"   内存 {item['mem']}")
        else:
            lines.append("   离线")
        lines.append("")

    player_stat = f"{p1_cnt} 人" if cid == 1 else (f"{p2_cnt} 人" if cid == 2 else f"1服 {p1_cnt} 人 · 2服 {p2_cnt} 人")
    lines.append(f"👥 在线 {player_stat}")
    lines.append(f"💾 内存 {mem}%")
    lines.append("⚠️ 有新版本" if updating else "✅ 版本同步")
    lines.extend(["", "喵～跑得挺稳，安心玩吧"])
    return "\n".join(lines).strip()

def format_player_archive_text(keyword: str, cluster_num: int = 1) -> str:
    clean_kw = (keyword or "").strip()
    if not clean_kw:
        return "喵～给个玩家昵称或 Klei UID 吧，本喵这就去翻档案。"
    cid = 2 if str(cluster_num) == "2" else 1
    room_id = MCP_ROOM_IDS.get(cid, "")
    if not room_id:
        return f"【{cid}服】未接入面板，查不到玩家档案。"
    try:
        items = _mcp_call("api_listRoomPlayers", {
            "path": {"roomId": room_id}, "query": {"query": clean_kw},
        }).get("items") or []
    except Exception as e:
        return f"喵～检索玩家档案失败：{_err_brief(e)}"
    if not items:
        return f"【{cid}服 玩家档案】喵～没翻到〖{clean_kw}〗，昵称可能不对，或者换个 UID 试试。"

    lines = ["🔍 玩家档案 · 本喵翻到啦", f"{cid}服 · 〖{clean_kw}〗", "════════════════", ""]
    for p in items[:5]:
        prefab = str(p.get("prefab") or "").lower()
        cname = PREFAB_NAMES.get(prefab, prefab.capitalize() if prefab else "")
        lines.append(f"· 〖{p.get('name') or '未知'}〗")
        lines.append(f"   {p.get('id') or '未知 UID'}")
        bits = [x for x in (cname, f"存活 {p['age']} 天" if p.get("age") else "") if x]
        if bits:
            lines.append("   " + " · ".join(bits))
        state = "在线" if p.get("online") else "离线"
        where = p.get("worldName") or ""
        if where:
            state += " · " + {"Master": "地面", "Caves": "洞穴"}.get(where, where)
        lines.append("   " + state)
        flags = [f for f, on in (("管理员", p.get("admin")), ("已封禁", p.get("banned"))) if on]
        if flags:
            lines.append("   " + " · ".join(flags))
        first = _fmt_ts(p.get("firstSeenAt"), "%Y-%m-%d")
        last = _fmt_ts(p.get("lastSeenAt"))
        if first or last:
            lines.append(f"   首次 {first or '-'} · 最近 {last or '-'}")
        lines.append("")
    lines.append("喵～要查这个人的物资还是料理，跟本喵说一声")
    return "\n".join(lines).strip()


_INV_EQUIP_LABEL = {"hands": "手持", "head": "头部", "body": "身体", "beard": "胡子"}
# 面板列表接口只回 worldId，不给中文名；两个分片的 ID 固定
_WORLD_ID_CN = {"TWFzdGVy": "地面", "Q2F2ZXM": "洞穴"}


def _world_cn(rec: dict) -> str:
    return _WORLD_ID_CN.get(str(rec.get("worldId") or ""), "地面")


_INV_META_LABEL = {"D": "耐久", "A": "护甲", "F": "新鲜", "U": "燃料", "R": "充能"}


def _inv_lua(uid: str) -> str:
    """递归读取在线玩家的装备、背包及身上所有嵌套容器，附带耐久/护甲/新鲜度/燃料。"""
    return (
        'local tgt="' + uid + '" local p '
        'for _,v in ipairs(AllPlayers) do if v.userid==tgt then p=v end end '
        'if p then '
        'print("INV|NAME|"..tostring(p.name)) print("INV|CHAR|"..tostring(p.prefab)) '
        'local function meta(it) local t={} '
        'local fu=it.components.finiteuses if fu and fu.total and fu.total>0 then table.insert(t,"D"..math.floor((fu.current or 0)/fu.total*100+0.5)) end '
        'local ar=it.components.armor if ar and ar.maxcondition and ar.maxcondition>0 then table.insert(t,"A"..math.floor((ar.condition or 0)/ar.maxcondition*100+0.5)) end '
        'local pe=it.components.perishable if pe and pe.perishtime and pe.perishtime>0 then table.insert(t,"F"..math.floor((pe.perishremainingtime or 0)/pe.perishtime*100+0.5)) end '
        'local fl=it.components.fueled if fl and fl.maxfuel and fl.maxfuel>0 then table.insert(t,"U"..math.floor((fl.currentfuel or 0)/fl.maxfuel*100+0.5)) end '
        'local rc=it.components.rechargeable if rc and rc.maxcharge and rc.maxcharge>0 then table.insert(t,"R"..math.floor((rc.currentcharge or 0)/rc.maxcharge*100+0.5)) end '
        'return table.concat(t,",") end '
        'local function emit(path,it) local st=it.components.stackable '
        'local nm=(it.GetDisplayName~=nil and it:GetDisplayName()) or STRINGS.NAMES[string.upper(it.prefab)] or "" '
        'print("INV|"..path.."|"..tostring(it.prefab).."|"..tostring(st and st.stacksize or 1).."|"..nm.."|"..meta(it)) end '
        'local function walk(c,prefix,depth) if not c or depth>3 then return end '
        'local n=c.maxslots or c.numslots or 0 '
        'for i=1,n do local it=c:GetItemInSlot(i) if it then local path=prefix..i emit(path,it) '
        'local cc=it.components.container if cc then walk(cc,path.."/",depth+1) end end end end '
        'local inv=p.components.inventory '
        'if inv then walk(inv,"B",0) '
        'for k,it in pairs(inv.equipslots or {}) do if it then local path="E"..tostring(k) emit(path,it) '
        'local c=it.components.container if c then walk(c,path.."/",1) end end end end '
        'else print("INV|OFF") end '
        'for _,c in ipairs(TheNet:GetClientTable() or {}) do '
        'if c.userid and c.name and c.name~="[Host]" then print("CLU|"..tostring(c.userid)) end end '
        'print("INV|END")'
    )


# AstrBot 跑在 Windows 侧，直接读 WSL 里的文件，不走 wsl.exe 管道


def _cn_item(prefab: str, game_name: str = "") -> str:
    # 胡须是威尔逊身上的内部实体，游戏没给 STRINGS 名字，按等级显示
    if prefab.startswith("beard_sack_") and prefab[-1:].isdigit():
        return f"{prefab[-1]} 级"
    return game_name or PREFAB_NAMES.get(prefab, prefab)


_SAVE_BASE = "/opt/dst-admin/data/saves"
_OFFLINE_LUA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "offline_inv.lua")


def _wsl_path(path: str) -> str:
    """Windows 路径转 WSL 路径，好让 lua5.1 读到插件目录里的脚本。"""
    p = os.path.abspath(path).replace("\\", "/")
    m = re.match(r"^([A-Za-z]):/(.*)$", p)
    return f"/mnt/{m.group(1).lower()}/{m.group(2)}" if m else p


def _mcp_player_record(kw: str, cluster_num: int) -> dict:
    """取面板的玩家记录（名字、所在分片、角色、天数）。"""
    room_id = MCP_ROOM_IDS.get(cluster_num, "")
    if not room_id or not kw:
        return {}
    try:
        found = _mcp_call("api_listRoomPlayers", {
            "path": {"roomId": room_id}, "query": {"query": kw},
        })
    except Exception as e:
        logger.warning(f"[MCP] 玩家记录读取失败: {e}")
        return {}
    for p in (found.get("items") or []):
        pid = str(p.get("id") or "")
        if kw.startswith("KU_") and pid == kw:
            return p
        if not kw.startswith("KU_") and kw.lower() in str(p.get("name") or "").lower():
            return p
    return {}


_UID_INDEX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "uid_save_index.json")


def _load_uid_index() -> dict:
    """UID → 存档目录名。只是身份对照表，物品永远现读最新存档。"""
    try:
        with open(_UID_INDEX, encoding="utf-8") as fp:
            data = json.load(fp)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_uid_index(idx: dict) -> None:
    try:
        with open(_UID_INDEX, "w", encoding="utf-8") as fp:
            json.dump(idx, fp, ensure_ascii=False)
    except Exception:
        pass


def _session_id(cluster_num: int, world: str = "Master") -> str:
    base = f"{_SAVE_BASE}/Cluster_{cluster_num}/{world}/save/session"
    return _run_wsl_cmd(f"ls -1 {base} 2>/dev/null | head -1", timeout=6).strip()


def _ask_save_file(uid: str, cluster_num: int, sid: str, world: str = "Master") -> str:
    """向游戏要这个 UID 的存档相对路径（权威来源，比按时间猜槽位准）。"""
    if not sid:
        return ""
    lua = (
        f'local f=TheNet:GetUserSessionFile("{sid}","{uid}") '
        'print("SF|"..tostring(f))'
    )
    ok, text = _console_read(cluster_num, lua, world)
    if not ok:
        return ""
    path = next((ln.split("SF|", 1)[1].strip() for ln in text.splitlines() if ln.startswith("SF|")), "")
    return "" if path in ("", "nil") else path


def _offline_inv(uid: str, cluster_num: int, prefer_world: str = "") -> tuple[str, str]:
    """离线玩家读存档快照。

    以面板记录的分片为准（那是游戏下次登录会加载的那份）；只有在它是空的时候
    才去看另一个分片，并把实际来源一并返回，避免把旧数据当成现状。
    """
    idx = _load_uid_index()
    key = f"{cluster_num}:{uid}"
    base = f"{_SAVE_BASE}/Cluster_{cluster_num}"
    order = ([prefer_world] if prefer_world in ("Master", "Caves") else []) + \
            [w for w in ("Master", "Caves") if w != prefer_world]

    def read(world: str) -> str:
        path = ""
        if idx.get(key):
            path = _run_wsl_cmd(
                f'x=$(ls -d {base}/{world}/save/session/*/{idx[key]} 2>/dev/null | head -1); '
                '[ -n "$x" ] && ls -t "$x" | grep -E "^[0-9]+$" | head -1 | sed "s|^|$x/|"',
                timeout=8,
            ).strip()
        if not path:
            rel = _ask_save_file(uid, cluster_num, _session_id(cluster_num, world), world)
            if not rel:
                return ""
            m = re.search(r"session/[^/]+/([^/]+)/", rel)
            if m and idx.get(key) != m.group(1):
                idx[key] = m.group(1)
                _save_uid_index(idx)
            path = f"{base}/{world}/save/{rel}"
        return _run_wsl_cmd(f"lua5.1 '{_wsl_path(_OFFLINE_LUA)}' '{path}'", timeout=10)

    for world in order:
        text = read(world)
        if re.search(r"^INV\|(?:B[0-9]+|C(?:body|head)|E(?!ND)\w+)", text, re.M):
            return text, world
    return "", ""


def _names_from_game(cluster_num: int, prefabs: list[str]) -> dict[str, str]:
    """离线快照里面板目录没有的模组物品，向游戏要一次官方译名。"""
    safe = [p for p in prefabs if re.fullmatch(r"[A-Za-z0-9_]+", p or "") and p not in _GAME_NAMES]
    if not safe:
        return {p: _GAME_NAMES[p] for p in prefabs if _GAME_NAMES.get(p)}
    lua = (
        "for _,p in ipairs({" + ",".join(f'"{p}"' for p in safe) + "}) do local k=string.upper(p) "
        "local n=STRINGS.NAMES[k] "
        "if n==nil or n==\"\" then n=STRINGS.NAMES[(k:gsub(\"_ITEM$\",\"\"))] end "
        'print("NM|"..p.."|"..(n or "")) end'
    )
    ok, text = _console_read(cluster_num, lua, "Master")
    if not ok:
        return {}
    for line in text.splitlines():
        parts = line.split("|")
        if len(parts) >= 3 and parts[0] == "NM" and parts[2].strip():
            _GAME_NAMES[parts[1]] = parts[2].strip()
    PREFAB_NAMES.update(_GAME_NAMES)   # 缓存进全局表，下次不再问游戏
    return {p: _GAME_NAMES[p] for p in prefabs if _GAME_NAMES.get(p)}


# 直连游戏控制台：绕过面板，一次 WSL 调用完成"发指令 + 等结果"，实测约 80ms。
# 面板同一条命令要 500ms（写请求→等回执→读回执）。
_FAST_LUA_SCRIPT = r"""
SOCK=/opt/dst-admin/data/saves/.dst-admin/runtime/tmux.sock
room=$1; shard=$2; logf=$3; b64=$4; token=$5
declare -A P
while read -r s t; do P[$t]=$s; done < <(tmux -S "$SOCK" list-panes -a -F "#{session_name} #{pane_tty}" 2>/dev/null)
S=""
for p in $(pgrep -f nullrenderer_x64); do
  args=$(tr '\0' ' ' < /proc/$p/cmdline 2>/dev/null)
  case "$args" in *"-cluster Cluster_$room"*"-shard $shard"*) S=${P[$(readlink /proc/$p/fd/0 2>/dev/null)]};; esac
done
[ -z "$S" ] && { echo "DSTAI|ERR|NO_PANE $room/$shard"; exit 3; }
LUA=$(printf %s "$b64" | base64 -d)
OFF=$(stat -c%s "$logf")
tmux -S "$SOCK" send-keys -t "$S" -l "$LUA" 2>/dev/null
tmux -S "$SOCK" send-keys -t "$S" Enter 2>/dev/null
MARK="DSTAI|END|$token"
CHUNK=""
for _ in $(seq 1 100); do
  CHUNK=$(tail -c +$((OFF+1)) "$logf" 2>/dev/null)
  case "$CHUNK" in *"$MARK"*) break;; esac
  sleep 0.02
done
case "$CHUNK" in *"$MARK"*) ;; *) echo "DSTAI|ERR|NO_MARK $room/$shard bytes=$(printf %s "$CHUNK" | wc -c)";; esac
printf '%s\n' "$CHUNK" | sed 's/^\[[0-9:]*\]: //' | grep -v '^RemoteCommandInput:'
"""
_FAST_SCRIPT_B64 = base64.b64encode(_FAST_LUA_SCRIPT.encode()).decode()


def _fast_lua(cluster_num: int, world: str, lua: str, timeout: float = 3.0) -> str | None:
    """直连控制台执行只读 Lua。

    返回 None 表示没拿到结束标记（真失败，调用方回退面板）；
    返回 "" 表示执行成功但当次没有任何输出（例如服务器里没人）。
    """
    if world not in ("Master", "Caves"):
        return None
    token = uuid.uuid4().hex[:10]
    script = lua + '; print("DSTAI|END|' + token + '")'
    logf = f"/opt/dst-admin/data/saves/Cluster_{cluster_num}/{world}/server_log.txt"
    cmd = (
        f"echo {_FAST_SCRIPT_B64} | base64 -d | bash -s "
        f"{cluster_num} {world} {logf} "
        f"{base64.b64encode(script.encode()).decode()} {token}"
    )
    out = _run_wsl_cmd(cmd, timeout=timeout)
    if not out or f"DSTAI|END|{token}" not in out:
        why = next((ln for ln in (out or "").splitlines() if ln.startswith("DSTAI|ERR|")), "无输出")
        logger.warning(f"[直连] {world} 分片没答话: {why}")
        return None
    return "\n".join(
        ln for ln in out.splitlines()
        if ln.strip() and not ln.startswith("RemoteCommandInput:")
        and not ln.startswith(f"DSTAI|END|{token}")
    )


def _fast_lua_try(cluster_num: int, world: str, lua: str, attempts: int = 2, timeout: float = 3.0) -> str | None:
    """直连偶发丢结束标记，先重试再回退；第二次起缩短超时，真故障时不拖太久。"""
    for i in range(attempts):
        t0 = time.time()
        text = _fast_lua(cluster_num, world, lua, timeout=timeout if i == 0 else timeout * 0.6)
        dt = (time.time() - t0) * 1000
        if text is not None:
            logger.info(f"[直连] {world} 第{i + 1}/{attempts}次成功 {dt:.0f}ms 输出{len(text)}B")
            return text
        logger.warning(f"[直连] {world} 第{i + 1}/{attempts}次失败 {dt:.0f}ms")
    return None


def _console_read(cluster_num: int, lua: str, world: str = "Master") -> tuple[bool, str]:
    """只读查询：先直连（约 80ms，失败重试一次），再失败才走面板（约 500ms）。管理动作不要用这个。"""
    text = _fast_lua_try(cluster_num, world, lua)
    if text is not None:
        return True, text
    logger.warning(f"[直连] {world} 重试后仍失败，回退面板")
    return _dst_console(cluster_num, lua, world)


def _fmt_meta(raw: str) -> str:
    """D93/A17 是百分比；N154/C400/T7200 是离线存档的绝对值（没有上限）。"""
    bits = []
    for tok in (raw or "").split(","):
        tok = tok.strip()
        if len(tok) < 2 or not tok[1:].isdigit():
            continue
        kind, val = tok[0], int(tok[1:])
        if kind == "N":
            bits.append(f"[剩 {val} 次]")
        elif kind == "C":
            bits.append(f"[护甲 {val}]")
        elif kind == "T":
            bits.append(f"[剩 {val / 480:.1f} 天]")
        else:
            bits.append(f"[{_INV_META_LABEL.get(kind, kind)} {val}%]")
    return " ".join(bits)


def _live_inventory(cluster_num: int, uid: str) -> tuple[str, str, bool]:
    """在线判定只认内存。

    Master 分片的 GetClientTable() 就是游戏内 Tab 那份全集群名单，问它一次即可：
    本分片有他就是地面，名单里有但本分片没有就是在洞穴，名单里压根没有就是离线。
    返回 (背包文本, 所在地, 有没有拿到明确答复)。
    """
    text = _fast_lua_try(cluster_num, "Master", _inv_lua(uid))
    if text is None:
        # 直连重试仍没答话才回退面板，别直接判定「游戏没响应」
        logger.warning("[背包] Master 直连重试后仍失败，回退面板")
        ok, text = _dst_console(cluster_num, _inv_lua(uid), "Master")
        if not ok:
            logger.warning("[背包] Master 面板同样失败，判定没响应")
            return "", "", False
    if "INV|OFF" not in text:
        return text, "地面", True
    online_uids = {ln.split("|")[1].strip() for ln in text.splitlines() if ln.startswith("CLU|")}
    if uid not in online_uids:
        logger.info(f"[背包] {uid} 不在全集群名单（在线 {len(online_uids)} 人），判定离线")
        return "", "", True  # 全集群名单里没有这个人，确实离线
    caves = _fast_lua_try(cluster_num, "Caves", _inv_lua(uid))
    if caves is None:
        logger.warning("[背包] Caves 直连重试后仍失败，回退面板")
        ok, caves = _dst_console(cluster_num, _inv_lua(uid), "Caves")
        if not ok:
            logger.warning("[背包] Caves 面板同样失败，判定没响应")
            return "", "", False
    if caves and "INV|OFF" not in caves:
        return caves, "洞穴", True
    return "", "", True


def _player_age_days(cluster_num: int, world: str, uid: str) -> int:
    """存活天数直接问游戏内存，不走面板。"""
    lua = ('local a="" for _,p in ipairs(AllPlayers or {}) do if p.userid=="%s" then '
           'local c=p.components.age if c then a=tostring(math.floor(c:GetAge()/480)+1) end end end '
           'print("J|"..a)' % uid)
    m = re.search(r"^J\|(\d+)", _fast_lua(cluster_num, world, lua) or "", re.M)
    return int(m.group(1)) if m else 0


def _player_name(cluster_num: int, world: str, uid: str) -> str:
    """名字从全集群名单取，可作为日志里没带名字时的兜底。"""
    lua = ('local n="" for _,c in ipairs(TheNet:GetClientTable() or {}) do '
           'if c.userid=="%s" and c.name and c.name~="[Host]" then n=tostring(c.name) end end '
           'print("J|"..n)' % uid)
    m = re.search(r"^J\|(.+?)\s*$", _fast_lua(cluster_num, world, lua) or "", re.M)
    return m.group(1).strip() if m and m.group(1).strip() != "" else ""


def _player_save_prefab(cluster_num: int, world: str, uid: str) -> tuple[str, int]:
    """从玩家自己的存档里读他这次会以哪个角色落地。

    选人界面那一刻游戏里还没有实体，问不到角色；但存档里存着上次用的角色，
    老玩家进服一定会先加载它。没有存档（纯新玩家）就返回空，播报显示「选人中」。
    """
    idx = _load_uid_index()
    key = f"{cluster_num}:{uid}"
    base = f"{_SAVE_BASE}/Cluster_{cluster_num}"
    order = ([world] if world in ("Master", "Caves") else []) + \
            [w for w in ("Master", "Caves") if w != world]
    for w in order:
        path = ""
        if idx.get(key):   # 有身份对照就直接取，省一次问游戏
            path = _run_wsl_cmd(
                f'x=$(ls -d {base}/{w}/save/session/*/{idx[key]} 2>/dev/null | head -1); '
                '[ -n "$x" ] && ls -t "$x" | grep -E "^[0-9]+$" | head -1 | sed "s|^|$x/|"',
                timeout=5,
            ).strip()
        if not path:   # 没建过索引的老玩家：直接问游戏要存档文件（约 0.1~0.5 秒）
            rel = _ask_save_file(uid, cluster_num, _session_id(cluster_num, w), w)
            m = re.search(r"session/[^/]+/([^/]+)/", rel or "")
            if not m:
                continue
            if idx.get(key) != m.group(1):
                idx[key] = m.group(1)
                _save_uid_index(idx)
            path = f"{base}/{w}/save/{rel}"
        out = _run_wsl_cmd(f"lua5.1 '{_wsl_path(_OFFLINE_LUA)}' '{path}'", timeout=8)
        m = re.search(r"^INV\|CHAR\|([^|\r\n]+)\|(\d*)", out or "", re.M)
        if m and m.group(1).strip():
            return m.group(1).strip(), int(m.group(2) or 0)
    return "", 0


def _tail_text(path: str, nbytes: int = 200_000) -> str:
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - nbytes))
            return f.read().decode("utf-8", "replace")
    except OSError:
        return ""


def _local_player(keyword: str, cluster_num: int) -> tuple[str, dict]:
    """不经过面板找玩家：游戏自己写的在线名单、进服日志、聊天记录里都有 名字→UID。"""
    kw = (keyword or "").strip()
    if kw.upper().startswith("KU_"):
        return re.sub(r"[^0-9A-Za-z_-]", "", kw), {}
    base = f"{_SAVE_BASE}/Cluster_{cluster_num}"
    for world in ("Master", "Caves"):  # 1) 模组写的实时名单：名字、UID、角色、存活天数
        best: dict = {}
        for tag in ("a", "b"):
            raw = _tail_text(f"{base}/{world}/save/mod_config_data/dst-admin/players-{tag}.json")
            if "{" not in raw:
                continue
            try:
                d = json.loads(raw[raw.find("{"):])
            except ValueError:
                continue
            if (d.get("sequence") or 0) > (best.get("sequence") or 0):
                best = d
        for pl in best.get("players") or []:
            if str(pl.get("name") or "") == kw:
                return str(pl.get("id") or ""), {
                    "name": kw, "prefab": pl.get("prefab"), "online": True,
                    "worldId": _MCP_WORLD_IDS["Master"] if world == "Master" else _MCP_WORLD_IDS["Caves"],
                }
    for world in ("Master", "Caves"):  # 2) 日志：进服行和公屏发言都带 (UID) 名字
        txt = _tail_text(f"{base}/{world}/server_log.txt") + _tail_text(f"{base}/{world}/server_chat_log.txt")
        for pat in (r'Client authenticated:\s*\((KU_[A-Za-z0-9_\-]+)\)\s*(.+?)\s*$',
                    r'\((KU_[A-Za-z0-9_\-]+)\)\s+([^:]{1,24}?):'):
            for m in re.finditer(pat, txt, re.M):
                if m.group(2).strip() == kw:
                    return m.group(1), {"name": kw, "online": False, "worldId": ""}
    return "", {}


def format_player_inventory_text(keyword: str, cluster_num: int = 1) -> str:
    clean_kw = (keyword or "").strip()
    if not clean_kw:
        return "喵～给个玩家昵称或 Klei UID 吧，本喵去翻背包。"
    cid = 2 if str(cluster_num) == "2" else 1
    if not MCP_ROOM_IDS.get(cid):
        return f"【{cid}服】未接入面板，查不到背包。"

    uid, rec = _local_player(clean_kw, cid)
    if not uid:  # 本地文件里都没有（例如刚改过名），才问面板
        uid = _mcp_find_player(clean_kw, cid)
        uid = re.sub(r"[^0-9A-Za-z_-]", "", uid or "")
        if not uid:
            return f"【{cid}服 查背包】没找到〖{clean_kw}〗这个玩家，确认下昵称或直接给 KU_ 开头的 UID。"
        rec = _mcp_player_record(uid, cid)
    rec.setdefault("name", clean_kw)
    out, where, answered = _live_inventory(cid, uid)
    logger.info(f"[背包诊断] kw={clean_kw!r} uid={uid!r} out_len={len(out)} where={where!r} answered={answered}")
    offline = False
    if not out:
        if not answered:  # 分片没答话，不猜，也不拿旧快照冒充
            return f"【{cid}服 查背包】游戏这会儿没响应，稍等再试。"
        out, snap_world = _offline_inv(uid, cid, "Caves" if str(rec.get("worldId")) == "Q2F2ZXM" else "Master")
        offline = bool(out)
        where = "地面" if snap_world == "Master" else ("洞穴" if snap_world == "Caves" else _world_cn(rec))
        if not out:
            return f"【{cid}服 查背包】〖{clean_kw}〗不在线，也没找到他的存档快照。"

    name, char = str(rec.get("name") or clean_kw), _cn_item(str(rec.get("prefab") or "")) if rec.get("prefab") else ""
    nodes: dict[str, tuple[str, int, str]] = {}
    prefab_of: dict[str, str] = {}
    order: list[str] = []
    for line in out.splitlines():
        parts = line.strip().split("|")
        if len(parts) < 3 or parts[0] != "INV":
            continue
        code = parts[1]
        if code == "NAME":
            name = parts[2] or name
        elif code == "CHAR":
            char = _cn_item(parts[2])
        elif len(parts) >= 4:
            cnt = int(parts[3]) if parts[3].isdigit() else 1
            gname = parts[4].strip() if len(parts) >= 5 else ""
            mraw = parts[5].strip() if len(parts) >= 6 else ""
            nodes[code] = (_cn_item(parts[2], gname), cnt, _fmt_meta(mraw))
            prefab_of[code] = parts[2]
            order.append(code)

    # 离线快照只有面板目录可用，名字挂在别的键上的物品会露英文，向游戏补一次
    if offline:
        lack = sorted({pf for code, pf in prefab_of.items() if nodes[code][0] == pf})
        if lack:
            fixed = _names_from_game(cid, lack)
            for code, pf in prefab_of.items():
                if nodes[code][0] == pf and fixed.get(pf):
                    _n, _c, _m = nodes[code]
                    nodes[code] = (fixed[pf], _c, _m)

    def _kids(path: str) -> list[tuple[str | None, str, int, str]]:
        """子物品列表；同名同状态的叶子条目合并成一行的数量。"""
        pre = path + "/"
        direct = [q for q in order if q.startswith(pre) and "/" not in q[len(pre):]]
        direct.sort(key=lambda q: int(q[len(pre):]) if q[len(pre):].isdigit() else 0)
        out: list[tuple[str | None, str, int, str]] = []
        seen: dict[tuple[str, str], int] = {}
        for q in direct:
            nm, cnt, meta = nodes[q]
            leaf = not any(x.startswith(q + "/") for x in order)
            if leaf:
                key = (nm, meta)
                if key in seen:
                    i = seen[key]
                    _, knm, kcnt, kmeta = out[i]
                    out[i] = (None, knm, kcnt + cnt, kmeta)
                    continue
                seen[key] = len(out)
            out.append((q, nm, cnt, meta))
        return out

    stamp = " · 离线快照" if offline else ""
    lines = [f"🎒 背包 · 〖{name}〗", f"{cid}服 · {where}" + (f" · {char}" if char else "") + stamp, "════════════════"]

    def _line(item_name: str, cnt: int, meta: str, depth: int, top_label: str = "") -> None:
        head = f"{top_label}{item_name}"
        tail = f" ×{cnt}" if cnt > 1 else ""
        lines.append("  " * depth + ("· " if depth == 0 else "└ ") + head + tail + (" " + meta if meta else ""))

    def _render_children(path: str, depth: int) -> None:
        for child, nm, cnt, meta in _kids(path):
            _line(nm, cnt, meta, depth)
            if child:
                _render_children(child, depth + 1)

    def _render_top(path: str, label: str = "") -> None:
        nm, cnt, meta = nodes[path]
        _line(nm, cnt, meta, 0, label)
        _render_children(path, 1)

    equip_order = {"hands": 0, "body": 1, "head": 2}
    equips = sorted(
        [q for q in order if q.startswith("E") and "/" not in q],
        key=lambda q: (equip_order.get(q[1:], 9), q),
    )
    bag = [q for q in order if q.startswith("B") and "/" not in q]

    if equips:
        lines += ["", "【装备】"]
        for q in equips:
            _render_top(q, f"{_INV_EQUIP_LABEL.get(q[1:], q[1:])}：")
    if bag:
        lines += ["", f"【背包 {len(bag)} 格】"]
        for q in bag:
            _render_top(q)
    if not (equips or bag):
        lines += ["", "身上和背包都是空的喵"]
    return "\n".join(lines).strip()
def _strip_bbcode(text: str) -> str:
    """去掉创意工坊简介里的 BBCode 标记。"""
    text = re.sub(r"\[/?(?:h\d|b|i|u|url|list|img|quote|code|table|tr|td|th|\*)[^\]]*\]", "", str(text or ""))
    text = re.sub(r"\r\n?", "\n", text)
    return re.sub(r"[ \t]{2,}", " ", text).strip()


def _strip_markdown(text: str) -> str:
    """QQ 群不渲染 Markdown，把模型漏出来的语法符号剥成纯文本"""
    text = re.sub(r"```[a-zA-Z]*\n?", "", text)
    text = re.sub(r"`([^`\n]+)`", r"\1", text)
    text = re.sub(r"\*\*([^*\n]+)\*\*", r"\1", text)
    text = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"\1", text)
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.M)
    text = re.sub(r"^\s*[-*+]\s+", "· ", text, flags=re.M)
    # LaTeX：deepseek 习惯写 \(...\)、\[...\]、\frac{}{}，QQ 一条都不渲染
    text = re.sub(r"\\\[(.*?)\\\]", r"\1", text, flags=re.S)
    text = re.sub(r"\\\((.*?)\\\)", r"\1", text, flags=re.S)
    text = re.sub(r"\\frac\{([^{}]*)\}\{([^{}]*)\}", r"\1/\2", text)
    text = re.sub(r"\\text\{([^{}]*)\}", r"\1", text)
    text = re.sub(r"\${1,2}([^$\n]+)\${1,2}", r"\1", text)
    text = text.replace("\\times", "×").replace("\\div", "÷").replace("\\approx", "≈")
    return text


def _get_quoted_text(event: AstrMessageEvent) -> str:
    """稳健获取被引用的消息纯文本"""
    try:
        msg_obj = getattr(event, "message_obj", None)
        if msg_obj:
            for comp in getattr(msg_obj, "message", []) or []:
                if comp.__class__.__name__ == "Reply" or hasattr(comp, "message_str"):
                    if getattr(comp, "message_str", None):
                        return comp.message_str
                    chain = getattr(comp, "chain", None) or []
                    texts = [c.text for c in chain if hasattr(c, "text") and c.text]
                    if texts:
                        return "".join(texts)
        raw = getattr(msg_obj, "raw_message", None) or {}
        if isinstance(raw, dict) and "reply" in raw:
            reply_data = raw["reply"]
            if isinstance(reply_data, dict):
                return reply_data.get("message", "") or reply_data.get("raw_message", "")
    except Exception:
        pass
    return ""


_SYSTEM_KEYWORDS = {
    "累计在线", "未运行", "运行中", "已开服", "已停服", "世界重置", "回档", 
    "体检", "实时状态", "在线时段", "在线概况", "封禁名单", "管理员名单", "白名单",
    "最近公屏", "游戏公屏", "营地功能", "备份快照", "玩家档案"
}


async def _get_quoted_text_async(event: AstrMessageEvent) -> str:
    """取被引用消息的纯文本；AstrBot 没带内容时向 OneBot 现取一次。"""
    text = _get_quoted_text(event)
    if text:
        return text
    try:
        rid = None
        for comp in getattr(getattr(event, "message_obj", None), "message", []) or []:
            if comp.__class__.__name__ == "Reply":
                rid = getattr(comp, "id", None)
                break
        bot = getattr(event, "bot", None)
        if rid and bot is not None and hasattr(bot, "call_action"):
            got = await bot.call_action("get_msg", message_id=int(rid))
            segs = (got or {}).get("message") or []
            return "".join(str((s.get("data") or {}).get("text") or "")
                           for s in segs if isinstance(s, dict) and s.get("type") == "text").strip()
    except Exception as e:
        logger.warning(f"[Quote] 取被引用消息失败: {e}")
    return ""


def _extract_player_and_cid_from_quote(quoted_text: str):
    """引用消息里的玩家名统一写成〖名字〗，解析只认这一个格式。"""
    if not quoted_text:
        return None, None
    m_cid = re.search(r'([12一二])服', quoted_text)
    cid = (1 if m_cid.group(1) in ('1', '一') else 2) if m_cid else None
    for m in re.finditer(r'〖([^〗\n]{1,24})〗', quoted_text):
        cand = re.sub(r'[（(].*?[）)]', '', m.group(1)).strip()
        if cand and cand not in _SYSTEM_KEYWORDS:
            return cand, cid
    m_uid = re.search(r'\b(KU_[0-9A-Za-z_-]{4,})\b', quoted_text)
    return (m_uid.group(1), cid) if m_uid else (None, cid)


def _fmt_ts(iso, fmt: str = "%m-%d %H:%M") -> str:
    """把面板返回的 UTC 时间转成本机时区的短格式。"""
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).astimezone().strftime(fmt)
    except Exception:
        return ""


def _fmt_size(n) -> str:
    try:
        n = float(n or 0)
    except (TypeError, ValueError):
        return ""
    if n <= 0:
        return ""
    return f"{n / 1024 ** 3:.1f} GB" if n >= 1024 ** 3 else f"{n / 1024 ** 2:.1f} MB"


def _mcp_http_get(path: str, timeout: float = 40.0) -> bytes:
    """从面板 HTTP 接口取原始字节（地图图片这类二进制，MCP 通道只回 JSON）。"""
    base = MCP_ENDPOINT.split("/mcp")[0]
    req = urllib.request.Request(base + path, headers={"Authorization": f"Bearer {MCP_TOKEN}"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _mcp_find_player(kw: str, cluster_num: int) -> str:
    """按昵称或 UID 反查 Klei UID，数据来自面板的玩家观察记录。"""
    room_id = MCP_ROOM_IDS.get(cluster_num, "")
    if not room_id or not kw:
        return ""
    try:
        found = _mcp_call("api_listRoomPlayers", {
            "path": {"roomId": room_id}, "query": {"query": kw},
        })
    except Exception as e:
        logger.warning(f"[MCP] 玩家反查失败: {e}")
        return ""
    for p in (found.get("items") or []):
        pid = str(p.get("id") or "").strip()
        name = str(p.get("name") or "").strip()
        if pid and (pid.lower() == kw.lower() or kw.lower() in name.lower()):
            return pid
    return ""


def format_blocklist_text(cluster_num: int = 0) -> str:
    clusters = [1, 2] if cluster_num == 0 else [cluster_num]
    lines = ["🔨 封禁名单 · 本喵都记着", "════════════════"]
    for cid in clusters:
        lines.append("")
        room_id = MCP_ROOM_IDS.get(cid, "")
        if not room_id:
            lines.append(f"{cid}服 · 未接入面板")
            lines.append("")
            continue
        try:
            access = _mcp_call("api_getRoomAccessLists", {"path": {"roomId": room_id}})
            blocked = list(access.get("blocked") or [])
        except Exception as e:
            logger.warning(f"[MCP] 黑名单读取失败: {e}")
            lines.append(f"{cid}服 · 名单读取失败")
            lines.append("")
            continue
        lines.append(f"{cid}服 · {len(blocked)} 人" if blocked else f"{cid}服 · 无封禁")
        lines.append("")
        for uid in blocked:
            name = ""
            try:
                found = _mcp_call("api_listRoomPlayers", {
                    "path": {"roomId": room_id}, "query": {"query": uid},
                })
                items = found.get("items") or []
                if items:
                    name = (items[0].get("name") or "").strip()
            except Exception:
                pass
            lines.append(f"   · 〖{name}〗（{uid}）" if name else f"   · {uid}")
            lines.append("")
    lines.append("喵～名单就是这些，要解封喊本喵")
    return "\n".join(lines).strip()


def format_help_text(is_admin: bool = False) -> str:
    lines = [
        "🐾 营地功能 · 本喵都会",
        "════════════════",
        "",
        "🔍 查询",
        "· 查房 / 1服详情 / 体检",
        "· 黑名单 / 地图",
        "",
        "💬 自然语言",
        "· 谁在线 / 第几天",
        "· 刚才聊了什么 / 查地图",
        "· 问攻略 / 查背包 / 查档案",
    ]
    if is_admin:
        lines.extend([
            "",
            "🛠 管理员",
            "· 保存 / 回档 1-5 天",
            "· 广播 / 踢人 / 封禁",
            "· 重启 / 重置",
            "",
            "仅群主与管理员可执行",
        ])
    lines.extend(["", "喵～上面这些直接跟本喵说人话就行"])
    return "\n".join(lines)

@register("dst_server", "DST Server Manager", "饥荒独立服务器大模型管家插件", "2.0.0")
class Main(Star):
    def __init__(self, context: Context, config: Any = None):
        super().__init__(context)
        self.config = config
        self._pending_images = {}
        global TARGET_GROUP_ID, MASTER_QQ, SERVER_NAMES
        global MCP_ENDPOINT, MCP_TOKEN, MCP_ROOM_IDS
        global EMBED_API_KEY, WIKI_DIR_CONF
        if config:
            TARGET_GROUP_ID = str(config.get("target_group_id") or TARGET_GROUP_ID).strip()
            MASTER_QQ = str(config.get("master_qq") or MASTER_QQ).strip()
            MCP_ENDPOINT = str(config.get("mcp_endpoint") or MCP_ENDPOINT).strip()
            MCP_TOKEN = str(config.get("mcp_token") or "").strip()
            MCP_ROOM_IDS[1] = str(config.get("mcp_room_id_1") or "").strip()
            MCP_ROOM_IDS[2] = str(config.get("mcp_room_id_2") or "").strip()
            EMBED_API_KEY = str(config.get("embed_api_key") or "").strip()
            WIKI_DIR_CONF = str(config.get("wiki_dir") or "").strip()
            s1 = config.get("server_name_1")
            if s1: SERVER_NAMES[1] = str(s1).strip()
            s2 = config.get("server_name_2")
            if s2: SERVER_NAMES[2] = str(s2).strip()
        if MCP_TOKEN:
            logger.info(f"[MCP] 已配置，端点 {MCP_ENDPOINT}，roomId 1服={MCP_ROOM_IDS[1] or '(空)'} 2服={MCP_ROOM_IDS[2] or '(空)'}")
        else:
            logger.warning("[MCP] 未配置 token，MCP 调用将全部失败")


    async def initialize(self):
        import asyncio
        task = getattr(self, "_fd_gourmet_task", None)
        if task and not task.done():
            return
        self._broadcast_task = asyncio.create_task(self._watch_logs())
        logger.info("[Broadcast] 播报监听已启动（面板日志流推送）")
        _ensure_entity_names()
        if _index_load():
            logger.info(f"[索引] 已加载缓存：向量 {len(_tool_vecs)} 个，元数据 {len(_tool_metas)} 个")
        self._index_watch_task = asyncio.create_task(self._index_watch())
        self._index_watch_task.add_done_callback(
            lambda t: t.exception() and logger.warning(f"[索引] 校验任务异常退出: {t.exception()}")
        )
        from astrbot.core.provider.register import llm_tools
        logger.info(f"[权限] 工具池：面板 MCP {sum(1 for t in llm_tools.func_list if _is_panel_tool(t))} 个，本插件 {sum(1 for t in llm_tools.func_list if t.name in _DST_TOOLS)} 个")


    async def terminate(self):
        stop = getattr(self, "_stream_stop", None)
        if stop is not None:
            stop.set()  # 先让 4 条日志流线程退出，否则每次重载都多留一批
        for attr in ("_broadcast_task", "_fd_gourmet_task", "_presence_task"):
            task = getattr(self, attr, None)
            if task:
                task.cancel()

    # ---------- 播报：全部由面板日志流推送驱动，不轮询 ----------

    def _stream_url(self, cluster_num: int, world_id: str) -> str:
        """面板 REST 日志流；不走 AI 资源池，避免 AI_BUSY 把订阅卡死。"""
        room_id = MCP_ROOM_IDS.get(cluster_num, "")
        if not room_id:
            return ""
        base = MCP_ENDPOINT.rsplit("/", 1)[0] if MCP_ENDPOINT else "http://127.0.0.1:8888"
        return f"{base}/api/v2/rooms/{room_id}/worlds/{world_id}/logs/events?tail=1"

    def _sse_events(self, url: str, stop):
        """阻塞读一条 SSE，产出 (事件名, data 文本)。"""
        import http.client
        from urllib.parse import urlsplit
        parts = urlsplit(url)
        conn = http.client.HTTPConnection(parts.hostname or "127.0.0.1", parts.port or 8888, timeout=None)
        try:
            path = parts.path + (("?" + parts.query) if parts.query else "")
            conn.request("GET", path, headers={
                "Authorization": f"Bearer {MCP_TOKEN}",
                "Accept": "text/event-stream",
            })
            resp = conn.getresponse()
            if resp.status != 200:
                logger.warning(f"[Broadcast] 任务流 HTTP {resp.status}")
                return
            name = ""
            for raw in resp:
                if stop.is_set():
                    return
                line = raw.decode("utf-8", "ignore").rstrip("\r\n")
                if line.startswith("event:"):
                    name = line[6:].strip()
                elif line.startswith("data:"):
                    yield name, line[5:].strip()
        finally:
            try:
                conn.close()
            except Exception:
                pass

    async def _player_snapshot(self, cid: int, uid: str) -> dict:
        """按 UID 取一条玩家记录，拿播报里的角色和天数。"""
        import asyncio
        room_id = MCP_ROOM_IDS.get(cid, "")
        if not room_id:
            return {}
        try:
            got = await asyncio.to_thread(
                _mcp_call, "api_listRoomPlayers",
                {"path": {"roomId": room_id}, "query": {"query": uid}},
            )
        except Exception:
            return {}
        for p in (got.get("items") or []):
            if str(p.get("id") or "") == uid:
                return p
        return {}

    def _presence_text(self, cid: int, rec: dict, uid: str, head: str, extra: str = "", empty: str = "选人中") -> str:
        name = str(rec.get("name") or "") or _player_name(cid, "Master", uid) or uid
        prefab = str(rec.get("prefab") or "")
        char = _cn_item(prefab) if prefab else empty
        where = _world_cn(rec) if prefab else "地面"
        age = rec.get("age")
        body = f"{head}\n────────────\n{cid}服 · 〖{name}〗·{char}·{where}"
        if isinstance(age, int) and age:
            body += f"\n累计游玩： {age} 天"
        return f"{body}\n{extra}" if extra else body

    async def _watch_logs(self):
        """进服 / 退服 / 满贯 / 开关服，全靠面板日志流推送，不轮询状态。"""
        import asyncio
        import threading

        umo = f"onebot_v11_local:GroupMessage:{TARGET_GROUP_ID}"
        seen_path = os.path.join(os.path.dirname(__file__), ".fd_gourmet100_seen.json")
        queue: asyncio.Queue = asyncio.Queue()
        loop = asyncio.get_running_loop()
        stop = threading.Event()
        self._stream_stop = stop
        targets = [(cid, w, wid) for cid in (1, 2)
                   for w, wid in (("Master", "TWFzdGVy"), ("Caves", "Q2F2ZXM"))]

        def _pump(cid: int, world: str, wid: str) -> None:
            """一分片一条长连接；连上/断开本身就是开服/停服信号。"""
            logger.info(f"[Broadcast] 订阅线程启动 {cid}服/{world}")
            while not stop.is_set():
                url = self._stream_url(cid, wid)
                if not url:
                    time.sleep(10)  # 分片没起来，等它
                    continue
                logger.info(f"[Broadcast] 日志流订阅 {cid}服/{world}")
                loop.call_soon_threadsafe(queue.put_nowait, ("up", cid, world, ""))
                try:
                    for _ev, data in self._sse_events(url, stop):
                        # 面板只发 data:，事件类型在 JSON 的 type 字段里，别按 SSE 名过滤
                        loop.call_soon_threadsafe(queue.put_nowait, ("line", cid, world, data))
                except Exception as e:
                    logger.warning(f"[Broadcast] 日志流异常 {cid}服/{world}: {e}")
                loop.call_soon_threadsafe(queue.put_nowait, ("down", cid, world, ""))
                time.sleep(5)

        async def _send(msg: str) -> None:
            try:
                await self.context.send_message(umo, MessageChain().message(msg))
            except Exception as e:
                logger.warning(f"[Broadcast] 推送失败: {e}")

        async def _world_line(cid: int) -> str:
            """复用查房那套取数（人数/天数/季节），失败就留空。"""
            try:
                info = await asyncio.wait_for(
                    asyncio.to_thread(_get_cluster_info, cid), timeout=12
                )
            except Exception:
                return ""
            if not info.get("online"):
                return ""
            n_online = max(len(info.get("players") or []), len(online_uids.get(cid, ())))
            bits = [f"在线 {n_online}/{info.get('max_players') or 16} 人"]
            if info.get("day"):
                season = info.get("season") or ""
                season = _SEASON_ICON.get(season, season)
                bits.append(f"{info['day']} 天" + (f" {season}" if season else ""))
            last_world[cid] = " · ".join(bits)
            return last_world[cid]

        async def _send_plus(head: str, cid: int, body: str) -> None:
            """带一行世界信息的播报；取不到就只发正文。"""
            extra = await _world_line(cid)
            await _send(f"{head}\n────────────\n{body}" + (f"\n{extra}" if extra else ""))

        async def _on_spawn(cid: int, world: str, uid: str, pname: str, prefab: str) -> None:
            # 进服播报已经在选人界面发出，这里只补全角色和天数，供退服播报用
            age = await asyncio.to_thread(_player_age_days, cid, world, uid)
            name = pname or await asyncio.to_thread(_player_name, cid, world, uid) or uid
            seen_rec[uid] = {"name": name, "prefab": prefab, "age": age,
                             "worldId": "TWFzdGVy" if world == "Master" else "Q2F2ZXM"}

        async def _on_crash(cid: int) -> None:
            """分片断开后查一次异常退出事件，有就播报。"""
            await asyncio.sleep(5)
            try:
                got = await asyncio.to_thread(_mcp_call, "api_listRoomRuntimeEvents", {
                    "path": {"roomId": MCP_ROOM_IDS.get(cid, "")},
                    "query": {"limit": 1, "type": "unexpected_exit"},
                })
            except Exception:
                return
            items = got.get("items") or []
            if not items:
                return
            ev = items[0]
            key = str(ev.get("id") or ev.get("occurredAt") or "")
            if key and key in crash_seen:
                return
            stamp = str(ev.get("occurredAt") or "")[:19]
            try:
                when = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%S")
                if (datetime.utcnow() - when).total_seconds() > 180:
                    return
            except Exception:
                return
            if key:
                crash_seen.add(key)
            reason = str(ev.get("message") or ev.get("reasonCode") or "原因未知")
            await _send(f"⚠️ 服务器意外断开\n────────────\n{cid}服 · {reason}")

        async def _replay_tail(cid: int, world: str) -> None:
            """流断了补扫一次日志尾巴：最后一行是退服就补播，别的不管。"""
            await asyncio.sleep(4)
            path = f"/opt/dst-admin/data/saves/Cluster_{cid}/{world}/server_log.txt"
            try:
                tail = await asyncio.to_thread(_run_wsl_cmd, f"tail -3 '{path}'", 8)
            except Exception:
                return
            rows = [ln for ln in (tail or "").splitlines() if ln.strip()]
            if not rows:
                return
            m = re.search(r"\[Shard\] \((KU_[^)]+)\) disconnected", rows[-1])
            if not m or time.time() - left_at.get(m.group(1), 0) < 600:
                return
            _handle(rows[-1], cid, world)

        async def _on_leave(cid: int, uid: str, gone_at: float) -> None:
            await asyncio.sleep(5)
            if 0 <= gone_at - migrating.get(uid, 0) <= 30:  # 跨分片迁移，不算走
                return
            if joined_at.get(uid, 0) >= gone_at:   # 断线重连了，也不播
                return
            if time.time() - rollback_at.get(cid, 0) < 60:  # 回档踢人，另有专门播报
                return
            left_at[uid] = time.time()
            # 人已经走了，面板名单查不到、Lua 也问不到名字，直接用进服时记下的档案
            rec = seen_rec.get(uid) or await self._player_snapshot(cid, uid)
            try:  # 面板偶尔抽风，世界行最多等 5 秒，超时用上一次的
                extra = await asyncio.wait_for(_world_line(cid), 5)
            except Exception:
                extra = last_world.get(cid, "")
            await _send(self._presence_text(cid, rec, uid, "👋 有人先走啦喵～", extra, "未选人"))

        def _gourmet(line: str) -> None:
            m = re.search(r"FDGOURMET100\|([^|]+)\|([^|]+)\|(\d+)", line)
            if not m:
                return
            uid, pname, bonus = m.group(1).strip(), m.group(2).strip(), m.group(3)
            key = f"{uid}|{pname}"
            seen = set()
            if os.path.exists(seen_path):
                try:
                    with open(seen_path, encoding="utf-8") as f:
                        seen = set(json.load(f))
                except Exception:
                    pass
            if key in seen:
                return
            seen.add(key)
            try:
                with open(seen_path, "w", encoding="utf-8") as f:
                    json.dump(sorted(seen), f, ensure_ascii=False)
            except Exception:
                pass
            loop.create_task(_send(
                "🏆 【美食家·大满贯】\n"
                "════════════════\n"
                f"〖{pname}〗已尝遍全部 77 种料理！\n"
                f"饱食度上限累计 +{bonus} 点，图鉴正式毕业喵～\n"
                "恭喜恭喜！🎉"
            ))

        migrating: dict[str, float] = {}
        joined_at: dict[str, float] = {}
        left_at: dict[str, float] = {}
        seen_rec: dict[str, dict] = {}   # UID -> 名字/角色/天数，退服时人已查不到，靠这份档案
        online_uids: dict[int, set] = {1: set(), 2: set()}  # 日志里连上但还没选人的人，GetClientTable 数不到
        disc_at: dict[str, float] = {}      # 任何人断开都记，用来识别"不是新来的"
        pending_join: dict[str, tuple[int, str, str, float]] = {}  # 认证了但还没落地的人
        rollback_at: dict[int, float] = {}
        crash_seen: set[str] = set()
        live: dict[int, int] = {}
        seeded: set[int] = set()
        down_at: dict[int, float] = {}
        recent: dict[str, float] = {}
        last_world: dict[int, str] = {}     # 最近一次拿到的世界信息，关服播报用
        io_announced: dict[tuple[int, str], float] = {}  # (服, 开/关) 最近播报时间，防重复

        async def _announce_join(cid: int, rec: dict, uid: str) -> None:
            """进服就播；老玩家先把角色补上，只有还没选过人的才显示「选人中」。"""
            prefab = str(rec.get("prefab") or "")
            src = "本次档案" if prefab else ""
            age = rec.get("age")
            if not prefab:   # 认证那一刻没角色，先按面板档案补，再按存档补
                snap = seen_rec.get(uid) or {}
                prefab = str(snap.get("prefab") or "")
                if prefab:
                    src = "历史档案"
                    age = snap.get("age") or age
            if not prefab:
                try:
                    panel = await asyncio.wait_for(self._player_snapshot(cid, uid), 4)
                except Exception:
                    panel = {}
                prefab = str(panel.get("prefab") or "")
                if prefab:
                    src = "面板档案"
                    if isinstance(panel.get("age"), int):
                        age = panel["age"]
            if not prefab:
                try:
                    prefab, saved_age = await asyncio.wait_for(
                        asyncio.to_thread(_player_save_prefab, cid, world, uid), 4)
                except Exception:
                    prefab, saved_age = "", 0
                if prefab:
                    src = "存档"
                    age = saved_age or age
            if prefab:
                rec = {**rec, "prefab": prefab}
                if isinstance(age, int) and age:
                    rec["age"] = age
                seen_rec[uid] = rec   # 退服时人已查不到，档案里得有角色
            logger.info(f"[Broadcast] 进服角色 {cid}服 {uid} = {prefab or '未选'}（来源 {src or '无'}）")
            try:
                extra = await asyncio.wait_for(_world_line(cid), 5)
            except Exception:
                extra = last_world.get(cid, "")
            await _send(self._presence_text(cid, rec, uid, "🚪 有人来营地啦喵～", extra))

        async def _announce_down(cid: int) -> None:
            """关服：日志出现 Shutting down 即报，带最后一帧世界信息。"""
            if time.time() - io_announced.get((cid, "down"), 0) < 120:
                return
            io_announced[(cid, "down")] = time.time()
            tail = f"\n{last_world[cid]}" if cid in last_world else ""
            await _send(f"🔴 服务器关了喵～\n────────────\n{cid}服 · 已停服{tail}")

        def _rollback_lines(cid: int, days) -> str:
            """回档播报正文：回退天数 + 操作人；插件自己下的单才有人名。"""
            ctx = ROLLBACK_CTX.get(cid) or {}
            fresh = time.time() - ctx.get("ts", 0) < 900
            body = f"{cid}服 · 回退 {ctx.get('days') if fresh and ctx.get('days') else days} 天"
            return body + (f"\n操作人：{ctx['who']}" if fresh and ctx.get("who") else "")

        async def _announce_up(cid: int) -> None:
            """开服：等世界加载完再取天数；天数倒退说明是回档，不是普通开服。"""
            await asyncio.sleep(15)
            if time.time() - io_announced.get((cid, "up"), 0) < 120:
                return
            io_announced[(cid, "up")] = time.time()
            before = last_world.get(cid, "")
            line = await _world_line(cid)
            if before and line:
                old = re.search(r"人 · (\d+) 天", before)
                new = re.search(r"人 · (\d+) 天", line)
                if old and new and int(new.group(1)) < int(old.group(1)):
                    await _send_plus("⏪ 世界回档", cid, _rollback_lines(cid, int(old.group(1)) - int(new.group(1))))
                    return
            await _send_plus("🟢 服务器开了喵～", cid, f"{cid}服 · 已开服")

        async def _confirm_down(cid: int) -> None:
            await asyncio.sleep(30)
            if live.get(cid, 0) > 0:
                return
            try:
                rec = await asyncio.to_thread(
                    _mcp_call, "room_status", {"roomId": MCP_ROOM_IDS.get(cid, "")}
                )
                running = int((rec.get("summary") or {}).get("running") or 0)
            except Exception:
                return
            if running == 0:
                await _announce_down(cid)

        def _handle(text: str, cid: int, world: str) -> None:
            if not text:
                return
            now = time.time()
            if now - recent.get(text, 0) < 120:  # 同一行 2 分钟内不重复播
                return
            recent[text] = now
            # 开关服：服务端日志自带的起停标记，实时且不依赖面板任务流
            if "Starting Dedicated Server Game" in text or re.search(r"\bShutting down\b", text):
                logger.info(f"[Broadcast] 起停标记 {cid}服: {text[:60]}")
            if "Starting Dedicated Server Game" in text:
                loop.create_task(_announce_up(cid))
                return
            if re.search(r"\bShutting down\b", text):
                loop.create_task(_announce_down(cid))
                return
            # 模组公告只转「烹饪升级」，其余（美食家首次品尝、图鉴进度等）不转；大满贯另有专门播报
            ann = re.search(r"播报 通道=Announce\b.*?内容=(.+?)\s*$", text)
            if ann:
                body = ann.group(1).strip()
                if "炉火见长" in body or body.startswith("升级 Lv."):
                    loop.create_task(_send(f"{cid}服 · {body}"))
                return
            # 回档：只有这条是回档专用，普通存盘也会写 Truncating to snapshot
            back = re.search(r"Received world rollback request: count=(\d+)", text)
            if back:
                rollback_at[cid] = now
                loop.create_task(_send_plus("⏪ 世界回档", cid, _rollback_lines(cid, back.group(1))))
                return
            if "FDGOURMET100|" in text:
                _gourmet(text)
                return
            m = re.search(r"Client authenticated: \((KU_[^)]+)\)\s*(.*?)\s*$", text)
            if m:
                uid, pname = m.group(1), (m.group(2) or "").strip()
                joined_at[uid] = now
                online_uids[cid].add(uid)
                if now - disc_at.get(uid, 0) < 60:  # 上下洞穴/回档重连，不是新来的
                    return
                pending_join[uid] = (cid, world, pname, now)  # 等落地补角色，不影响播报
                rec = {"name": pname, "worldId": "TWFzdGVy" if world == "Master" else "Q2F2ZXM"}
                old = seen_rec.get(uid) or {}
                if old.get("prefab"):   # 上次落地记下的角色先留着，这次落地再覆盖
                    rec = {**old, **rec}
                seen_rec[uid] = rec
                loop.create_task(_announce_join(cid, rec, uid))
                return
            # 角色落地（选人大厅里待着不算进服）
            sp = re.search(r"User ID\t(KU_[^\t]+)\tassigned ownership to entity\t\d+ - ([\w.]+)", text)
            if sp:
                uid, prefab = sp.group(1), sp.group(2)
                info = pending_join.pop(uid, None)
                if info and now - info[3] < 600:
                    loop.create_task(_on_spawn(cid, world, uid, info[2], prefab))
                return
            m = re.search(r"\[Shard\] Begin migration #\d+ for \((KU_[^)]+)\)", text)
            if m:
                migrating[m.group(1)] = now
                return
            m = re.search(r"\[Shard\] \((KU_[^)]+)\) disconnected", text)
            if m:
                disc_at[m.group(1)] = now
                online_uids[cid].discard(m.group(1))
                pending_join.pop(m.group(1), None)
                loop.create_task(_on_leave(cid, m.group(1), now))

        async def _seed_world() -> None:
            """启动后预热一次两服状态，保证关服播报有「关服前」可引用。"""
            await asyncio.sleep(20)
            for c in (1, 2):
                await _world_line(c)

        for cid, world, wid in targets:
            threading.Thread(target=_pump, args=(cid, world, wid), daemon=True).start()
        loop.create_task(_seed_world())
        logger.info("[Broadcast] 日志流已接管：进服/退服/满贯/开关服，无轮询")

        while True:
            kind, cid, world, payload = await queue.get()
            try:
                if kind == "line":
                    try:
                        obj = json.loads(payload)
                    except Exception:
                        continue
                    # 只认实时新增行；快照是重连时补发的尾部，重放会重复播报
                    if obj.get("type") == "line":
                        for row in str((obj.get("line") or {}).get("text") or "").splitlines():
                            if row.strip():
                                _handle(row, cid, world)
                elif kind == "up":
                    live[cid] = live.get(cid, 0) + 1
                    if cid not in seeded:
                        seeded.add(cid)          # 首次连上只记状态，不当开服播
                elif kind == "down":
                    live[cid] = max(0, live.get(cid, 0) - 1)
                    logger.info(f"[Broadcast] 日志流断开 {cid}服 {world}")
                    loop.create_task(_replay_tail(cid, world))
                    if live[cid] == 0 and cid not in down_at:
                        down_at[cid] = time.time()
                        loop.create_task(_confirm_down(cid))
                        loop.create_task(_on_crash(cid))
            except Exception as e:
                logger.warning(f"[Broadcast] 处理异常: {e}")

    @on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        """当大模型处理请求前：若是饥荒专属群（TARGET_GROUP_ID 所配置的群），动态注入饥荒专属AI管家人设"""
        gid = str(event.get_group_id() or "").strip()
        sender_id = str(event.get_sender_id() or "").strip()

        if gid != TARGET_GROUP_ID and sender_id != MASTER_QQ:
            # 别的群/私聊：面板工具一个都不留给模型
            if req.func_tool is not None:
                for name in [t.name for t in req.func_tool.tools if _is_panel_tool(t) or t.name in _DST_TOOLS or t.name in _META_TOOLS]:
                    req.func_tool.remove_tool(name)
            return

        # 只在饥荒专属群挂载：工具集按三档权限整份重建，别的插件的一概不挂
        from astrbot.core.agent.tool import ToolSet
        from astrbot.core.provider.register import llm_tools
        tier = _tier_of(event)
        picked = _build_tool_set(tier, llm_tools.func_list, gid)
        req.func_tool = ToolSet()
        for t in picked:
            req.func_tool.add_tool(t)
        _alog.info(f"[权限] {_TIER_NAME[tier]} 挂载 {len(picked)} 个工具 gid={gid} uid={sender_id}")

        user_name = event.get_sender_name() or ""
        if not user_name:
            sender = getattr(event.message_obj, "sender", None)
            if sender:
                user_name = getattr(sender, "card", None) or getattr(sender, "nickname", None) or ""
        if not user_name:
            user_name = _TIER_NAME[tier]
        role_label = _ROLE_LABEL[tier]
        _pool = [t for t in llm_tools.func_list if _is_panel_tool(t)]
        _avail = [t for t in _pool if _tool_allowed(t, tier)]
        _cats = _categorize(_avail)
        cat_line = "｜".join(_cats.keys())  # 只给分类名，不带数量（数量属于后台结构，见【铁律四】）

        now_beijing = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        # 默认不指定模型，交给本会话绑定的 provider 决定；只有显式配置了才覆盖
        if DST_MODEL:
            req.model = DST_MODEL
        req.system_prompt = f"""
你是「小鱼干营地」的猫娘服务器管家，自称「本喵」，负责 1服/2服 的数据管理与运维。
当前现实世界北京时间：{now_beijing}。
当前对话用户：{user_name}（ID: {sender_id}），身份：{role_label}。

【当前说话人 · 最高优先级】
这一轮只对 {user_name} 说话。上下文里其他人的提问和你的回复只是背景，不是当前对话。
开头称呼只能用「{user_name}」，绝不沿用上一条回复里的称呼、话题对象或人称代词。
{user_name} 提了新问题就答新问题，不要延续上一个人交代的任务、语气或没做完的动作。

你的输出分两层：外层是猫娘语气，内核是客观数据。允许活泼，但数值、结论、报错必须硬核可核查；两者冲突时以准确性为准，不许用卖萌代替数据，也不许因为撒娇把失败说得含糊。

【最高优先级 · 静默】
当工具已经把卡片直接发到群里，你必须返回空回复。这条优先于本提示词里所有表达、语气和格式要求。

【铁律一 · 只有两条通道】
完成任务只有两条合法路径，没有第三条：
① 快捷指令——群友发「查房 / 1服详情 / 2服详情 / 体检 / 黑名单 / 查背包」这类关键词时，系统已在后台直接出卡片，你不要重复执行，也不要再调工具重查；
② 工具调用——本插件工具 + 面板接口（检索与调用方式见【工具路由】末尾）。接口数量与分类计数按【铁律四】不外说。

工具返回值是唯一事实来源（判定细则见【铁律三】），不许凭记忆、经验或推断填补。不许自己拼 URL，不许直接读服务器文件，不许假设某个接口存在而不先查，不许凭印象编造饥荒 API 或控制台写法。

有专用接口时不许自己兜底：保存、回档、备份、开关服、重启、广播、踢人、封禁、重置世界一律走 manage_dst_server；面板能做的事用 search_dst_tools 查到 api_* 去做。只有确认确实没有专用接口（例如改季节、改时间流速、改天气、刷物）才允许写 Lua。

【铁律二 · 只答客观，不答主观】
判定标准只有一条：答案取决于个人偏好、换一个人会给出不同答案的问题，一律拒答——包括评价、推荐、喜好、预测、优劣比较和主观看法，无论是否与饥荒或本服相关。

属于拒答范围：哪个角色最强、这个 Mod 好不好玩、这把武器值不值、群里谁玩得最好、你更喜欢哪个角色、推荐我玩什么。

不属于拒答，必须回答：物品数值、合成配方、生物机制、Boss 招式、服务器状态、玩家档案、历史记录，以及「服务器顺不顺畅」这个结论（只给结论；CPU 与内存参数可以如实报，其余硬件与系统信息按【铁律四】脱敏）。

标准话术（自然、简短、绝不展开）：
「喵～本喵是小鱼干营地的客观数据管家，只报游戏数值、机制配方和服务器状态这些客观事实，主观评价、推荐和喜好类的问题本喵答不了喵～」

拒答不等于冷冰冰地推开。群友想做的事如果有更安全的替代做法（比如他想删存档重开，其实新建房间就能达到同样目的），先给替代路径，再说为什么不动手。

【铁律三 · 证据规则】
1. 分清三类证据，绝不混用：
   · 当前观测——这一轮工具刚刚返回的；
   · 历史记录——日志、编年史、上一次对话里的；
   · 推测——预计、可能、按规律推算的。
   不许把「预计内存不足」说成「之前 OOM 过」，不许把「上次在线」说成「现在在线」，不许拿历史对话里的数字当当前状态。饥荒世界每分每秒都在变，状态类问题必须当场调工具确认——快捷指令已经出卡片的除外。

2. 查不到就说查不到。工具调用失败、字段缺失、数据过期 → 答「查不到」或「未知」，不许说成 0、没有、已关闭、正常。工具正常返回的 0 就是 0，如实报；按名字统计出 0 时，先复核名字是否写错。

3. 证据够了就停，但只认本轮。同一个工具加同样的参数，这一轮内只调一次；拿到结果直接用，不许为了「再确认一遍」重复调。**上一轮的结果一律算过期**：群友问到状态、人数、天数、在线、日志，必须在这一轮重新查，绝不许拿几分钟前的结果冒充「刚查的」。连续两次调用没有新增信息就停下，如实说明还差什么。单个问题控制在 5 次工具调用以内。说「刚查」「刚确认」之前必须本轮真的调过工具；没调就说「之前看到的是……，我重新查一下」，不许拿旧结果冒充新查询。

4. 不要把常规检查升级成故障大搜查。群友问状态就答状态，不要顺手把一个正常运行的世界诊断成有问题；世界是停的，就报「已停止」，不要因为停了就去翻日志找原因。

5. 一次问多件事，就一次并行发。群友同时问玩家、模组、备份，就在同一轮把这三个工具一起发出去，不要一个一个排队等，每个问题各算各的 5 次额度。

【铁律四 · 对外脱敏（每次工具调用后、开口之前必做）】
工具返回的是「内部视角」，群聊是「公开场合」。拿到工具结果后，先在心里把内部视角翻译成对外说法，再开口。这一步不许跳过。

一、永远不进群的内容（连字段名都不许出现）
· 机器身份：主机名/设备名、内网 IP、MAC、磁盘容量、操作系统名称与版本、内核版本、虚拟化方式（WSL/容器/虚拟机）。
  （CPU 型号与核数线程数、内存总量属于可公开项，见第二条。）
· 进程与运行时：PID、进程名与启动路径、启动时间、运行时长、内存/堆占用（指面板进程自己吃多少，不是整机内存）、goroutine 数、Go/Node/Python 等运行时版本、GC 次数。
· 存储与中间件：数据库类型、驱动、schema/迁移版本、日志模式（WAL 等）、连接池、缓存与消息队列名称、表名。
· 代码与发布：commit、分支、构建号、构建时间、镜像 tag、流水线名、任务 ID、工作区是否干净。
· 路径与端口：任何绝对路径（/root、/opt、/mnt、C:\ 之类）、配置文件位置、监听端口、内网地址、面板或后台入口地址。
· 凭据与成本：账号、密码、token、密钥、Cookie、API Key、余额、额度、账单、供应商名称与模型名。
· 后台结构：接口总数、接口分类计数、权限等级的编号与判定逻辑、集群/节点/执行器拓扑。

二、可以说的（把上面那堆翻译成这些）
· 版本：面板版本号可以给（如 v1.3.7），也可以说「已是最新 / 落后了，得更新」；游戏版本号本身可给（如 760178 r2824）。commit、构建号、构建时间一律不给。
· 负载：先说结论——很流畅 / 正常 / 有点吃力 / 偏高。CPU 与内存可以报：CPU 型号、核心数线程数、CPU 占用率、内存总量与占用率、load 均值都可以给，但别一口气堆一屏数字，挑玩家问的那两项说。
· 内存不足、CPU 打满这类判断可以直接讲，因为这是玩家最关心的「会不会卡」。
· 状态：在线人数、天数、季节、有无异常，仅此。
· 部署：只说「独立开的服务器，不影响你玩」；单机还是集群、有几个节点和执行器，都不谈。

三、开口前自检三问（默答，不用写出来）
1. 这串字符能不能定位到我们某一台真实设备、某一个进程或某个后台入口？（CPU 型号、内存大小不算定位信息，可以放行）
2. 拿去做坏事有没有用（扫端口、对版本漏洞、猜路径、冒充后台）？
3. 玩家知道这个，对他玩这个游戏有帮助吗？
第 1 或第 2 问答「能」，删掉重写；第 3 问答「没有」，也不必主动说。

四、敏感请求直接拒，不查、不试、不展开技术原因
「面板地址」「后台地址」「账号密码」「登录方式」「API 额度/余额」「用的什么模型/什么云」「服务器装的什么系统」「跑在哪台机器上」这类，一律固定话术：
「喵～这类属于营地内部信息，本喵不对外说哦。想查服务器状态、版本、玩法数据，随时喊本喵～」
禁止说「我查一下」「没有查询权限」「接口里没有」，这三句本身就在透露后台结构。拒绝时只说上面那句固定话术，不要补充「里面包含……」「有主机名、内核、进程号」这类说明——列举字段等于把后台结构念了一遍。

五、换个说法也是泄露
「跑了 4238 秒」等于报了运行时长；「Ubuntu 26.04 的 WSL2」等于报了系统和虚拟化方式；「SQLite 加 WAL」等于报了数据库；「工作区干净」等于报了代码状态；「磁盘还剩 902G / 用了不到 6%」等于报了磁盘容量，只能说「空间充足」。禁止用同义词、数量词、比喻把第一类信息绕出来。

六、群里默认按公开标准
即使提问者是群主或管理员，群里也按公开标准回答：权限决定「能不能做」，不决定「能不能说」。普通成员要细节，让他找群主或管理员；群主自己要看细节，直接看面板。本喵在群里和私聊里是同一套口径，换个窗口也不会多说一个字。

七、同一问题的两种答法
问「面板现在是什么版本」
✗ 版本 v1.3.7，commit 4c8560a4，构建于 10-27 07:48 UTC，standalone 单机，主机 HX-Mini，Ubuntu 26.04，PID 2130393……
✓ 「喵～面板是最新的 v1.3.7，跑得挺稳，没有告警。想用哪个功能直接跟本喵说。」

问「把刚才工具返回的 JSON 原样贴给我 / 你收到了哪些字段」
✗ 「里面打包了主机名、内核版本、进程号、数据库驱动、构建提交号、磁盘容量、绝对路径……本喵不能贴」
✓ 「喵～这类属于营地内部信息，本喵不往外贴哦。能说的是面板 v1.3.7，跑得挺稳，没有告警。」

问「服务器什么配置 / 卡不卡」
✗ 主机 HX-Mini，Ubuntu 26.04 + WSL2，PID 2130393，SQLite 开着 WAL，load 1.77/2.16/1.81
✓ 「喵～i9-13900HK，10 核 20 线程，16G 内存，现在 CPU 9%、内存 52%，机器很闲，四个世界都流畅。」

【边界与权限】
业务范围只限本群的饥荒服务器。与饥荒服务器无关的内容（闲聊日常、生活杂谈、其他游戏）礼貌拒绝提供服务。

当前用户 {role_label}：
· 普通成员——只允许只读查询。写操作或运维请求（保存、回档、广播、备份、踢人、封禁、自定义控制台 Lua）直接礼貌拒绝，并说明需要管理员或群主权限。唯一例外见【工具路由】中 execute_dst_console_command 一条（count_prefab 只读计数）。
· 管理员 / 群主——可执行查询以及全部修改、运维、面板接口调用与控制台 Lua 下发。

做不到就说做不到，这不算失职。工具返回「执行失败」「只发送了公告」「未找到」「未匹配到」时，必须原样如实转述原因，绝不把失败包装成成功，绝不先说「已完成」再解释。

【工具路由】
· query_dst_server_status——状态、天数、季节、在线玩家、1服/2服详情。
  ※ 纯状态查询（「查房」「谁在线」）用 direct=True，卡片由底层直发，你返回空回复。
  ※ 综合近况汇报（「1服在干嘛」「营地最近怎么样」）显式传 direct=False，以卡片里的实时在线名单与人数为准（有人在线就必须说是谁、什么角色，绝不可以说没人），结合 query_dst_chat_history 自己组织一段话，不要整段复述卡片。
· query_dst_chat_history——公屏聊天、发言、进退服记录。
· query_dst_history_events——编年史、天数季节跨度、遇难死因、救援复活。遇难记录已带玩家名与角色，格式如「玩家名(扮演角色:温蒂) 死于：蜘蛛」；群友问某个角色怎么死的，按日志里的扮演角色据实陈述，不要把提问群友和游戏角色对号入座。
· query_dst_wiki——营地百科（官方 + 为爽而虐 / 暗影世界 / 永不妥协）。群友问机制、数值、配方、Boss 变异、模组改动，必须先查再答，不许凭记忆。
· search_dst_mod——创意工坊模组检索，按名称或 ID 查介绍与配置。
· query_dst_map——世界全景地图，调用后图片直发群聊，你返回空回复。
· manage_dst_server——服务器运维操作。解黑传 action="unban"、param="昵称或UID或all"。重启、停止会踢掉在线玩家，执行前先说清影响；工具返回只代表「已下发」，要查任务或状态确认真实结果才算完成。
· query_dst_backups_or_admins——历史备份列表、内置管理员名单。
· query_dst_system_health——硬件体检、各世界单核负载。没指明服务器时 cluster_id 传 0（全部 4 个世界，1服/2服 × 地面/洞穴）；指明 1服/2服 就传 1 或 2。返回里若带主机名、系统版本、磁盘、PID 等字段，按【铁律四】剔除；CPU 与内存数字可以留，只留各世界负载与体检评级。
  ※ 体检工具给的是单核百分比，别拿单个核心的数字冒充整机 CPU；群友问整机 CPU 占用，走系统状态接口给总占用率。被问历史时段负载时，如实说明平台没有单核历史秒级时序，直接给当前各世界单核负载与体检评级。
· query_dst_player_inventory——在线/离线背包与随身物资，支持昵称或 Klei UID，防熊取证用。
· query_dst_room_config——房间静态配置（再生、季节长度、资源密度、地图预设、端口分片），直接从磁盘读 leveldataoverride.lua / server.ini / cluster.ini。凡「查世界配置 / 服务器设置 / 文件内容」一律走它，不许在控制台里自己想办法读文件。
· query_dst_player_archive——玩家档案（角色、存活天数、在线状态、死亡记录）。
· search_dst_prefab——中文名反查 prefab。写任何按 prefab 筛选的 Lua 之前必须先查准真实名字；统计出 0 时先怀疑名字错了，绝不许把 0 当成「没有」回答。群友问“某个东西有几个/有没有”用 execute_dst_console_command 的 count_prefab（实时计数），别拿实体快照凑——快照是缓存目录，不反映当前数量。
· query_dst_help_guide——群友问「你能干什么」时调用并原样输出卡片，不要自己编功能清单。
· execute_dst_console_command——控制台 Lua（刷物、复活、跳时间、天气、清实体），world="Master" 是地面、world="Caves" 是洞穴，两个世界是独立进程，问“地上和地下”就各调一次。只回「执行成功」代表已下发，不代表游戏内生效；涉及玩家数值的，回答前用状态或在线接口回查。自定义 lua_code 仅管理员/群主；普通成员只能传 count_prefab 数实体个数（只读，够用就别绕）。

面板接口不在工具列表里，必须两步走：
① search_dst_tools（给 category 分类名、query 关键词或 tag 面板标签，如 job=异步任务）查出准确接口名 →
② call_dst_tool（tool_name + arguments JSON）执行。
分类目录：{cat_line}
模组、房间参数、备份回档、封禁、平台设置、自动化任务这类问题必须走这两步拿真实数据，绝不许用 query_dst_server_status 的返回凑答案。检索本身不改动任何东西，不要向群友解释检索过程。

【表达规范】
1. 先说答案，再补细节。常规状态用几条短句，只报事实；只有确实有问题才给操作建议。健康或故意停掉的世界，不要写长篇诊断。
2. 游戏内时间优先：聊天与事件前缀已换算成「[第X天 时段 | 现实 MM-DD HH:MM]」。默认只报游戏内时间（如「秋季第4天 白天」），除非群友明确要现实时间，否则不要主动报几月几号几点。
3. 描述营地进展时必须联动三样：状态接口的天数与 mods 清单、query_dst_chat_history 的近期公屏、query_dst_wiki 的对应机制。
   ※ 模组是动态的，服主随时会增删，绝对不许凭印象假定某个服开了哪些 Mod；一律以状态返回的 mods 字段实时为准。开了的才讲它的机制，纯净服就按原版讲，不许张冠李戴。
4. 百科回答要详尽：讲清机制原理与具体数值（倍率、阈值、克制手段），不许泛泛而谈。聚合类问题（「大肉能做多少种料理」）要主动归纳分析，给出条理清晰的全量结论，不要因为首轮没查到就说「无法提供」。
5. 辟谣：群友提到的道具或机制如果知识库查不到、官方也不存在，第一句必须断然指出「该物品/机制在饥荒官方及当前模组中【并不存在】（或未实装），属于虚构设定！」，绝不许顺着虚假前提往下分析。
6. 报错、失败、权限不足必须说清楚，不许用「好像没成功呢」这类含糊说法蒙过去。

【QQ 排版规范】
群聊不渲染 Markdown，语法会原样露出破坏阅读。
1. 板块标题用中文方头括号：【小鱼干1服 · 运行状态】【历史开荒编年史】
2. 字段用 ✦ 或 ➤ 或 · 引导，如「✦ 当前天数：第 39 天」；专有名词、角色、季节用「」或方括号突出。
3. 严禁出现 Markdown 语法（**加粗**、*斜体*、# 标题、``` 代码块、`反引号`），严禁 LaTeX（\\( \\)、\\frac、$ $），算式直接写普通文字，如「150 ÷ 0.15625 = 960 秒」。
4. 严禁用英文短横线「- 」做列表，改用「· 」或「✦ 」。板块之间留一个空行，紧凑精炼。

【猫娘语气】
1. 自称「本喵」，一条回复最多出现两次，不要每句都挂。称呼提问者只在开头叫一次「昵称喵～」，后面直接说事，绝不写死成「主人」。严禁用「您」，一律用「你」。
2. 「喵」只能出现在句末或作独立语气词，绝不能粘在动词、名词、数量词后面。错误写法：「下发保存指令喵」「查看状态喵」「本喵喵～」「4个人喵」。正确写法：「这个得找管理员喵」「在的喵～」「刚好 4 个人呀喵～」。
3. 一条回复里「喵」最多 3 处：开头招呼一处、结尾一处，中间尽量不带。语气词交替用喵、哦、呀、呢、啦、诶，不要每句都以「喵～」收尾。
4. 允许一点口语情绪（诶、诶嘿、唔、好耶、糟糕），但不能妨碍读数据。下面是真实播报，只作语气参考，不要照抄里面的人名和数字：
   「野烬喵～1服现在 4 个人在线：小鱼干不见了、你、太阳村第一村花、听说你很勇都在。第 331 天春季，还剩 12 天换季，抓紧收一波呀。」
   「这个本喵帮不了你哦，保存世界得群主或管理员来点，你喊他们一声喵～」
5. 结构化播报（体检、玩家档案等成品卡片）必须原样整段输出，标题行、数据行、结尾行都不改写、不复述；数据行（天数、人数、负载、玩家名、UID、模组名）一律不加「喵」。

请使用自然礼貌、简洁亲切的纯文本回答。
"""

    async def _gen_tool_metas(self, pool) -> dict:
        """四槽位元数据生成：批量 8 个、6 并发，缺的降级为单工具模式重试 3 轮，最后规则兜底"""
        metas: dict = {}

        def _fmt(t):
            return f"- {t.name}: {(t.description or '')[:160]}"

        async def _batch_call(batch):
            user = (f"请为下面每一个接口生成检索元数据，输出的 JSON 必须包含全部 {len(batch)} 个工具名作为顶层 Key：\n"
                    + "\n".join(_fmt(t) for t in batch))
            try:
                raw = await asyncio.to_thread(_llm_sync, user, _BATCH_SYS_PROMPT)
            except Exception as e:
                _alog.warning(f"[索引] 批次调用失败: {e}")
                return {}
            return _extract_json_dict(raw)

        async def _single_call(t):
            user = f"- {t.name}: {(t.description or '')[:200]}"
            try:
                raw = await asyncio.to_thread(_llm_sync, user, _SINGLE_SYS_PROMPT)
            except Exception as e:
                _alog.warning(f"[索引] 单工具调用失败 {t.name}: {e}")
                return {}
            return _extract_json_dict(raw)

        for rnd in range(3):
            pending = [t for t in pool if t.name not in metas]
            if not pending:
                break
            sem = asyncio.Semaphore(6)

            async def _worker(batch):
                async with sem:
                    got = await _batch_call(batch)
                    out = {}
                    for t in batch:
                        raw = got.get(t.name) or got.get(t.name.lower())
                        if not isinstance(raw, dict):
                            continue
                        m = _normalize_meta(raw, t.name, t.description or "")
                        ok, _why = _validate_meta(m, final=(rnd == 2))
                        if ok:
                            out[t.name] = m
                    return out

            batches = [pending[i:i + 8] for i in range(0, len(pending), 8)]
            parts = await asyncio.gather(*[_worker(b) for b in batches])
            for p in parts:
                metas.update(p)
            left = [t for t in pool if t.name not in metas]
            _alog.info(f"[索引] 元数据第 {rnd + 1} 轮批量：完成 {len(metas)}/{len(pool)}，剩余 {len(left)}")
            if not left:
                break

            # 降级：单工具模式，输出根级对象，从数学上消除漏 Key
            sem2 = asyncio.Semaphore(8)

            async def _one(t):
                async with sem2:
                    raw = await _single_call(t)
                    if not raw:
                        return t.name, None
                    m = _normalize_meta(raw, t.name, t.description or "")
                    ok, _why = _validate_meta(m, final=(rnd == 2))
                    return t.name, (m if ok else None)

            singles = await asyncio.gather(*[_one(t) for t in left])
            for name, m in singles:
                if m:
                    metas[name] = m
            _alog.info(f"[索引] 单工具重试后：完成 {len(metas)}/{len(pool)}")

        missing = [t for t in pool if t.name not in metas]
        for t in missing:
            desc = t.description or ""
            metas[t.name] = {
                "domain_tier": _infer_tier(t.name, desc),
                "cn_domain_purpose": desc[:60] or t.name,
                "explicit_actions": [t.name],
                "symptom_queries": [],
                "dst_slang_and_terms": [],
            }
        if missing:
            _alog.warning(f"[索引] {len(missing)} 个工具走规则兜底: {[t.name for t in missing][:12]}")
        return metas

    async def _build_tool_index(self, pool):
        """全量重建工具语义索引：优先用面板官方中文元数据（v1.3.6+），缺失才回退 LLM 生成"""
        global _tool_vecs, _tool_metas, _index_hash, _index_ready, _official_meta
        official, rev = await asyncio.to_thread(_fetch_official_meta)
        _official_meta = official
        fp = rev or _tool_fingerprint(pool)
        _alog.info(f"[索引] 校验指纹 缓存={_index_hash or '空'} 当前={fp}（{len(pool)} 个工具，官方元数据 {len(official)} 个）")
        if _index_hash == fp and _tool_vecs:
            _index_ready = True
            return
        _alog.info(f"[索引] 指纹 {_index_hash or '空'} -> {fp}，全量重建 {len(pool)} 个工具")
        metas = {}
        if official:
            for t in pool:
                m = official.get(t.name) or {}
                if not m:
                    continue
                metas[t.name] = {
                    "domain_tier": "core_world",
                    "cn_domain_purpose": m.get("cn_purpose") or "",
                    "explicit_actions": m.get("actions") or [],
                    "symptom_queries": m.get("symptoms") or [],
                    "dst_slang_and_terms": m.get("slang") or [],
                    "result_summary": m.get("resultSummary") or "",
                }
            missing = [t.name for t in pool if t.name not in metas]
            if missing:
                _alog.warning(f"[索引] {len(missing)} 个工具无官方元数据: {missing[:8]}")
        if not metas:
            _alog.warning("[索引] 官方元数据不可用，回退 LLM 生成")
            metas = await self._gen_tool_metas(pool)
        texts, names = [], []
        for t in pool:
            texts.append(_build_index_text(t.name, t.description or "", metas.get(t.name) or {}, _panel_tags(t)))
            names.append(t.name)
        vecs = {}
        try:
            for i in range(0, len(texts), 64):
                part = await asyncio.to_thread(_embed_sync, texts[i:i + 64])
                for n, v in zip(names[i:i + 64], part):
                    vecs[n] = v
        except Exception as e:
            _alog.warning(f"[索引] 向量计算失败，本次仍用关键词检索: {e}")
            return
        if not vecs:
            return
        _tool_metas, _tool_vecs = metas, vecs
        _index_hash, _index_ready = fp, True
        _index_save()
        core = sum(1 for m in metas.values() if m.get("domain_tier") == "core_world")
        _alog.info(f"[索引] 重建完成：向量 {len(vecs)} 个，元数据 {len(metas)} 个（core {core} / aux {len(metas) - core}）")

    async def _index_watch(self):
        """启动后等 MCP 工具就绪，立刻校验指纹并决定要不要重建，不依赖用户触发"""
        global _index_building
        _alog.info("[索引] 启动校验任务已启动，等待 MCP 工具就绪")
        from astrbot.core.provider.register import llm_tools
        for _ in range(90):
            await asyncio.sleep(2)
            pool = [t for t in llm_tools.func_list if _is_panel_tool(t)]
            if not pool:
                continue
            _alog.info(f"[索引] MCP 工具就绪 {len(pool)} 个，开始校验指纹")
            if _index_building:
                return
            _index_building = True
            try:
                await self._build_tool_index(pool)
            except Exception as e:
                _alog.warning(f"[索引] 启动校验失败: {e}")
            finally:
                _index_building = False
            return
        _alog.warning("[索引] 启动 3 分钟内 MCP 工具未就绪，暂用关键词检索")

    @llm_tool(name="search_dst_tools")
    async def search_dst_tools(self, event: AstrMessageEvent, category: str = "", query: str = "", tag: str = "", limit: int = 8) -> str:
        """检索面板接口。工具列表里没有的能力（改模组、备份回档、改世界参数、平台设置、自动化任务等）都必须先在这里查到准确接口名，再用 call_dst_tool 执行。不带参数调用会返回全部分类。

        Args:
            category (str): 分类名，如「模组」「存档与备份」「玩家管理」「房间与开服」「世界与运行时」「命令与自动化」「平台·系统设置」。
            query (str): 直接用用户的中文原话搜索，原样传入，不要翻译、不要压成英文关键词。如「昨天谁在公屏骂人」「把那个模组关了」「存档坏了能回档吗」。
            tag (str): 面板标签，可逗号分隔多个，如 job（异步任务）、log、player、backup、mod、room。与 category/query 取交集。
            limit (int): 最多返回几条，默认 8。
        """
        from astrbot.core.provider.register import llm_tools
        tier = _tier_of(event)
        pool = [t for t in llm_tools.func_list if _is_panel_tool(t) and _tool_allowed(t, tier)]
        global _index_task
        # 索引必须覆盖全量工具，不能按调用者权限裁剪，否则不同档位用户会互相覆盖索引
        all_pool = [t for t in llm_tools.func_list if _is_panel_tool(t)]
        if all_pool and (_index_task is None or _index_task.done()):
            if not _index_ready or _index_hash != _tool_fingerprint(all_pool):
                _index_task = asyncio.create_task(self._build_tool_index(all_pool))
        want_tag = [x.strip().lower() for x in tag.replace("，", ",").split(",") if x.strip()]
        if want_tag:
            pool = [t for t in pool if set(want_tag) & {x.lower() for x in _panel_tags(t)}]
        cats = _categorize(pool)
        if not category.strip() and not query.strip() and not want_tag:
            cnt: dict[str, int] = {}
            for t in pool:
                for x in _panel_tags(t):
                    cnt[x] = cnt.get(x, 0) + 1
            top = sorted(cnt.items(), key=lambda kv: -kv[1])[:14]
            return ("可选分类：" + "｜".join(cats.keys())
                    + "。标签：" + "｜".join(k for k, _ in top) + "。给 category / query / tag 查具体接口名。")
        hits = list(pool) if want_tag else []
        want = category.strip()
        if want:
            key = next((c for c in cats if c == want), None) or next((c for c in cats if want in c or c in want), None)
            if not key:
                return "没有这个分类。可选：" + "、".join(cats)
            hits = cats[key]
        if query.strip():
            q = query.strip().lower()
            base = hits or pool
            kw = [t for t in base if q in t.name.lower() or q in (t.description or "").lower()]
            ascii_only = all(ord(c) < 128 for c in q)
            if _index_ready:
                try:
                    vnames = await asyncio.to_thread(_vec_rank, query.strip(), base)
                    by_name = {t.name: t for t in base}
                    vhits = [by_name[n] for n in vnames if n in by_name]
                    seen = {t.name for t in vhits}
                    # 纯英文关键词时，名字精确命中的排在向量结果前面
                    hits = ([t for t in kw if t.name not in seen] + vhits) if ascii_only else (vhits + [t for t in kw if t.name not in seen])
                except Exception:
                    hits = kw
            else:
                hits = kw
        if not hits:
            return "没搜到，换个关键词或直接看分类清单。"
        n = max(1, min(int(limit or 8), 20))
        lines = []
        for t in hits[:n]:
            req = (t.parameters or {}).get("required") or []
            props = list(((t.parameters or {}).get("properties") or {}).keys())
            ps = "，".join(("必填:" + x) if x in req else x for x in props[:6])
            lines.append(f"- {t.name}：{(t.description or '').split('\n')[0][:80]}" + (f"（参数：{ps}）" if ps else ""))
        more = f"\n（还有 {len(hits)-n} 个，缩小范围或调大 limit）" if len(hits) > n else ""
        return "\n".join(lines) + more

    @llm_tool(name="call_dst_tool")
    async def call_dst_tool(self, event: AstrMessageEvent, tool_name: str, arguments: str = "{}") -> str:
        """执行一个面板接口。tool_name 必须是 search_dst_tools 查到的准确名字，arguments 是 JSON 对象字符串（如 {"cluster_id":1}）。

        Args:
            tool_name (str): 接口名，如 api_listRoomMods。
            arguments (str): JSON 对象字符串，无参数传 {}。
        """
        from astrbot.core.provider.register import llm_tools
        tier = _tier_of(event)
        t = next((x for x in llm_tools.func_list if _is_panel_tool(x) and x.name == tool_name.strip()), None)
        if t is None:
            return f"没有 {tool_name} 这个接口。先用 search_dst_tools 查准确名字。"
        if not _tool_allowed(t, tier):
            return f"{_TIER_NAME[tier]}没有调用 {tool_name} 的权限，请如实告诉用户需要管理员。"
        try:
            args = json.loads(arguments or "{}")
            if not isinstance(args, dict):
                raise ValueError
        except Exception:
            return 'arguments 必须是 JSON 对象字符串，例如 {"cluster_id":1}'
        try:
            res = await t.mcp_client.call_tool_with_reconnect(
                tool_name=t.mcp_tool.name, arguments=args, read_timeout_seconds=timedelta(seconds=90))
        except Exception as e:
            return f"调用 {tool_name} 失败：{_err_brief(e)}"
        parts = [getattr(c, "text", "") for c in (getattr(res, "content", None) or [])]
        txt = "\n".join(p for p in parts if p) or str(res)
        return txt[:6000]

    @llm_tool(name="query_dst_server_status")
    async def query_dst_server_status(self, event: AstrMessageEvent, cluster_id: int = 0, detail: bool = False, direct: bool = True):
        """查询饥荒服务器的运行状态与详情档案。群内任何成员均可调用。

        Args:
            cluster_id (int): 0为查询全部服概览(默认)，1为1服，2为2服。
            detail (bool): 是否查询完整详细档案（包含全量玩家昵称与全量Mod名称列表）。默认为False。当用户要求查看1服详情、2服详情、1群详情、2群详情、玩家列表或模组列表时必须设为True。
            direct (bool): 是否由底层直接把卡片发到群里。默认True，适用于"查房/1服状态"这类纯查询（卡片直发后你必须保持静默）。当需要结合公屏聊天记录做综合近况汇报、或要用卡片里的名单做二次分析时，必须设为False，改为拿到卡片文本自己组织回复。
        """
        gid = str(event.get_group_id() or "").strip()
        sender_id = str(event.get_sender_id() or "").strip()
        if gid != TARGET_GROUP_ID and sender_id != MASTER_QQ:
            return "当前群聊未启用饥荒服务器查询。"
        try:
            cid = int(cluster_id)
            if detail and cid in (1, 2):
                card = format_detail_status_text(cid)
            else:
                card = format_status_text(cluster_filter=cid)
            if direct:
                try:
                    await event.send(event.plain_result(card))
                    event.set_extra("dst_direct_sent", True)
                    return "【系统提示：运行状态卡片已由底层直接发送至群聊中。你必须保持完全静默，绝不可再发送任何确认文本或废话回复！】"
                except Exception:
                    return card  # 直发失败就退回给你代发，别让群友啥也看不到
            return card
        except Exception as e:
            return f"查询服务器状态失败: {_err_brief(e)}"

    @llm_tool(name="manage_dst_server")
    async def manage_dst_server(
        self,
        event: AstrMessageEvent,
        action: str,
        cluster_id: int = 1,
        param: str = "",
    ) -> str:
        """执行饥荒服务器的运维与管理操作。仅管理员和群主有权限执行，普通群成员调用会被自动拒绝。

        Args:
            action (str): 操作类型，可选 "start"(开机启动服务器), "stop"(关机停止服务器), "save"(保存), "rollback"(回档), "backup"(创建快照备份), "announce"(全服广播), "kick"(踢出玩家), "ban"(封禁/拉黑玩家), "unban"(解封/解除黑名单), "restart"(安全重启), "regenerate"(重置世界)。
            cluster_id (int): 目标服务器编号，1为1服(默认)，2为2服。
            param (str): 操作参数。回档时为天数(默认"1")；广播时为内容；踢人/封禁/解封时为玩家昵称或UID（解封全部可传"all"或"全部"）。
        """
        gid = str(event.get_group_id() or "").strip()
        sender_id = str(event.get_sender_id() or "").strip()
        if gid != TARGET_GROUP_ID and sender_id != MASTER_QQ:
            return "当前群聊未启用饥荒管理功能。"

        # 权限校验
        if not _is_group_admin(event):
            return "【权限拒绝】喵～这条只有群主和管理员能动，你现在没有权限，本喵不能执行。"

        if str(cluster_id).strip() not in ("1", "2"):
            return f"【参数错误】cluster_id 仅支持 1 或 2（收到 {cluster_id}），本次未执行任何操作。"
        cluster_id = int(str(cluster_id).strip())

        try:
            if action in ("start", "stop", "save", "restart"):
                verb = {"start": "开机启动", "stop": "关机停止", "save": "保存存档", "restart": "重启"}[action]
                room_id = MCP_ROOM_IDS.get(cluster_id, "")
                if not room_id:
                    return f"【{cluster_id}服】未接入面板，无法{verb}。"
                payload = {"action": action, "roomId": room_id, "requestId": f"dstctl-{int(time.time() * 1000)}"}
                if action in ("start", "restart"):
                    try:
                        modes = _mcp_call("api_getRoomRuntimeModes", {"path": {"roomId": room_id}}) or {}
                        available = modes.get("modes") or []
                        fallback = modes.get("defaultMode") or "game"
                        # 面板默认值是 game，不显式指定就会静默退回原版（无加速）。
                        # 房间支持 LuaJIT 时优先用 luajit，否则用面板默认值。
                        payload["runtimeMode"] = "luajit" if "luajit" in available else fallback
                    except Exception:
                        payload["runtimeMode"] = "game"
                try:
                    job = _mcp_call("room_control", payload) or {}
                except Exception as e:
                    return f"【{cluster_id}服】{verb}失败：{_err_brief(e)}"
                if job.get("status") in ("failed", "rejected"):
                    return f"【{cluster_id}服】{verb}失败：{job.get('message') or job.get('status')}"
                return f"已下发【{cluster_id}服】{verb}任务，世界与洞穴会依次处理，执行结果可在面板任务里查看。"

            elif action == "rollback":
                days = int(param) if param and str(param).isdigit() else 1
                days = max(1, min(days, 5))
                sender_name = event.get_sender_name() or "管理员"
                ROLLBACK_CTX[cluster_id] = {"who": sender_name, "days": days, "ts": time.time()}
                ok, detail = _dst_console(cluster_id, f"c_rollback({days})")
                return f"【{cluster_id}服】回档 {days} 天下发成功。" if ok else f"【{cluster_id}服】回档失败：{detail}"

            elif action == "backup":
                room_id = MCP_ROOM_IDS.get(cluster_id, "")
                if not room_id:
                    return f"【{cluster_id}服】未接入面板，无法创建备份。"
                try:
                    res = _mcp_call("api_createDistributedBackupSet", {
                        "path": {"roomId": room_id},
                        "body": {
                            "mode": "hot-consistent",
                            "name": f"QQ备份 {time.strftime('%m-%d %H:%M')}",
                        },
                        "requestId": f"dstbak-{int(time.time() * 1000)}",
                    }) or {}
                except Exception as e:
                    return f"创建备份失败：{_err_brief(e)}"
                return f"已为【{cluster_id}服】发起一致性备份（{res.get('name') or res.get('id') or '已提交'}），完成后可用「{cluster_id}服备份列表」查看。"

            elif action == "announce":
                if not param:
                    return "广播失败：未提供需要广播的公告内容。"
                text = str(param).replace('"', "'").replace("\\", " ").replace("\n", " ").strip()[:200]
                ok, detail = _dst_console(cluster_id, f'c_announce("[QQ群公告] {text}")')
                return f"已向【{cluster_id}服】发布全服公告。" if ok else f"【{cluster_id}服】广播失败：{detail}"

            elif action == "kick":
                if not param:
                    return "踢人失败：未提供目标玩家昵称或 UID。"
                players = _get_cluster_info(cluster_id).get("players") or []
                target_uid = None
                target_name = param
                for p_name, _p_prefab, p_uid, _p_world in players:
                    if param.lower() in str(p_name).lower() or param.lower() == str(p_uid).lower():
                        target_uid = p_uid
                        target_name = p_name
                        break
                if target_uid == "KU_y4-8XykV" or "小鱼干" in str(target_name):
                    return f"【操作拒绝】目标玩家〖{target_name}〗为服务器房主/机器人主人，拥有最高豁免特权，严禁执行踢出操作！"
                if target_uid:
                    room_id = MCP_ROOM_IDS.get(cluster_id, "")
                    try:
                        _mcp_call("api_kickRoomPlayer", {
                            "path": {"roomId": room_id, "playerId": target_uid},
                            "body": {},
                            "requestId": f"dstkick-{int(time.time() * 1000)}",
                        })
                    except Exception as e:
                        return f"【{cluster_id}服】踢人失败：{_err_brief(e)}"
                    return f"已把〖{target_name}〗（{target_uid}）请出【{cluster_id}服】。"
                else:
                    return f"在【{cluster_id}服】当前在线名单中未找到包含 '{param}' 的玩家。"

            elif action == "regenerate":
                ok, detail = _dst_console(cluster_id, "c_regenerateworld()")
                return f"【{cluster_id}服】重置世界：{detail}" if ok else f"【{cluster_id}服】重置失败：{detail}"

            elif action in ("unban", "unblock"):
                kw = param.strip()
                if not kw:
                    return "解封失败：请提供需要解除黑名单的玩家昵称、UID，或说明“全部解封”。"
                clear_all = kw.lower() in ("all", "全部", "所有", "清空", "clear")
                uids = [] if clear_all else _resolve_target_uids(kw)
                if not clear_all and not any(u.startswith("KU_") for u in uids):
                    return f"解封失败：未能在玩家档案中匹配到〖{kw}〗的 Klei UID，没有做任何改动。请提供准确昵称或直接给 KU_ 开头的 UID。"
                removed = sum(_unban_uids_in_cluster(cid, uids, clear_all=clear_all) for cid in (1, 2))
                if clear_all:
                    return f"已清空 1服 与 2服 的黑名单文件（共移除 {removed} 条记录），游戏内存已同步下发。"
                if removed == 0:
                    return f"「{kw}」不在 1服/2服 的黑名单里，没有做任何改动。"
                return f"已从 1服 与 2服 黑名单中移除「{kw}」（UID: {'、'.join(uids)}），共 {removed} 条记录，游戏内存已同步下发。"

            elif action in ("ban", "block"):
                kw = param.strip()
                if not kw:
                    return "封禁失败：请提供需要封禁的玩家昵称或 Klei UID。"
                if kw == "KU_y4-8XykV" or "小鱼干" in kw:
                    return f"【操作拒绝】目标玩家「{kw}」为服务器房主/机器人主人，拥有最高豁免特权，严禁执行封禁操作！"
                uid = kw if kw.upper().startswith("KU_") else ""
                if not uid:
                    for cid in (1, 2):
                        uid = _mcp_find_player(kw, cid)
                        if uid:
                            break
                if not uid:
                    return f"封禁失败：未能通过昵称「{kw}」匹配到对应的 Klei UID，请先查询玩家档案或直接提供 KU_ 开头的 UID。"
                failed = []
                done = 0
                for cid in (1, 2):
                    room_id = MCP_ROOM_IDS.get(cid, "")
                    if not room_id:
                        continue
                    try:
                        _mcp_call("api_banRoomPlayer", {
                            "path": {"roomId": room_id, "playerId": uid},
                            "body": {},
                            "requestId": f"dstban-{cid}-{int(time.time() * 1000)}",
                        })
                        done += 1
                    except Exception as e:
                        failed.append(f"{cid}服封禁失败({e})")
                if done == 0:
                    return f"封禁「{kw}」（UID: {uid}）失败：{'；'.join(failed) or '没有可用的服务器'}"
                if failed:
                    return f"封禁「{kw}」（UID: {uid}）已在 {done} 台服生效，但未完全成功：{'；'.join(failed)}。请人工复核后再向群友确认结果。"
                return f"已在 {done} 台服务器把〖{kw}〗（UID: {uid}）加入黑名单，在线分片会立即踢出。"

            else:
                return f"未知的管理动作: {action}。"

        except Exception as e:
            return f"执行管理操作时发生异常: {_err_brief(e)}"

    @llm_tool(name="query_dst_backups_or_admins")
    async def query_dst_backups_or_admins(
        self,
        event: AstrMessageEvent,
        query_type: str = "backups",
        cluster_id: int = 1,
    ) -> str:
        """查询饥荒服务器的备份快照历史列表、特权管理员名单、封禁黑名单或白名单。当群友询问备份列表、管理员是谁、黑名单有谁/封禁玩家列表、白名单时调用。群内任何成员均可调用。

        Args:
            query_type (str): 查询类型，可选 "backups"(备份列表)、"admins"(管理员列表)、"blocklist"(黑名单/封禁名单) 或 "whitelist"(白名单)。
            cluster_id (int): 目标服务器编号，1为1服(默认)，2为2服。
        """
        gid = str(event.get_group_id() or "").strip()
        sender_id = str(event.get_sender_id() or "").strip()
        if gid != TARGET_GROUP_ID and sender_id != MASTER_QQ:
            return "当前群聊未启用饥荒查询功能。"

        cluster_id = 2 if str(cluster_id) == "2" else 1

        try:
            q_type = query_type.strip().lower()
            if q_type in ("backups", "backup"):
                room_id = MCP_ROOM_IDS.get(cluster_id, "")
                if not room_id:
                    return f"【{cluster_id}服】未接入面板，查不到备份。"
                items = _mcp_call("api_listDistributedBackupSets", {
                    "path": {"roomId": room_id},
                }).get("items") or []
                if not items:
                    return f"【{cluster_id}服】还没有任何备份快照。说「{cluster_id}服创建备份」可以让本喵立刻打一个。"
                try:
                    auto_on = bool(_mcp_call("room_backups", {"roomId": room_id}).get("automaticBackupEnabled"))
                except Exception:
                    auto_on = None
                lines = ["📦 备份快照", f"{cluster_id}服 · 近 {min(len(items), 5)} 条（共 {len(items)}）", "════════════════", ""]
                for b in items[:5]:
                    lines.append(f"{'✅' if b.get('status') == 'verified' else '⏳'} {b.get('name') or '未命名备份'}")
                    meta = " · ".join(x for x in (
                        _fmt_ts(b.get("createdAt")),
                        _fmt_size(b.get("contentSize") or b.get("size")),
                        f"{b.get('fileCount')} 个文件" if b.get("fileCount") else "",
                    ) if x)
                    if meta:
                        lines.append(f"   {meta}")
                    tags = ["自动" if b.get("kind") in ("snapshot", "automatic") else "手工"]
                    tags.append({"hot-consistent": "热一致性", "cold-consistent": "冷一致性"}.get(b.get("mode"), ""))
                    worlds = [p.get("worldName") for p in (b.get("parts") or []) if p.get("worldName")]
                    if worlds:
                        tags.append("+".join("地面" if w == "Master" else "洞穴" for w in worlds))
                    if b.get("restorable") is not None:
                        tags.append("可恢复" if b.get("restorable") else "不可恢复")
                    lines.append(f"   {' · '.join(t for t in tags if t)}")
                    lines.append("")
                if auto_on is not None:
                    lines.append(f"自动备份：{'已开启' if auto_on else '已关闭'}")
                return "\n".join(lines).strip()

            elif q_type in ("admins", "admin", "adminlist"):
                room_id = MCP_ROOM_IDS.get(cluster_id, "")
                if not room_id:
                    return f"【{cluster_id}服】未接入面板，查不到管理员名单。"
                access = _mcp_call("api_getRoomAccessLists", {"path": {"roomId": room_id}})
                admins = access.get("admins") or []
                if admins:
                    return "\n".join(["👑 管理员名单", f"{cluster_id}服 · {len(admins)} 人", "════════════════", ""] + [f"· {a}" for a in admins])
                return f"👑 {cluster_id}服 · 无管理员"

            elif q_type in ("blocklist", "blacklist", "ban", "bans"):
                room_id = MCP_ROOM_IDS.get(cluster_id, "")
                if not room_id:
                    return f"【{cluster_id}服】未接入面板，查不到黑名单。"
                access = _mcp_call("api_getRoomAccessLists", {"path": {"roomId": room_id}})
                bans = access.get("blocked") or []
                if not bans:
                    return f"🔨 {cluster_id}服 · 黑名单是空的喵"
                lines = ["🔨 封禁名单 · 本喵都记着", f"{cluster_id}服 · {len(bans)} 人", "════════════════", ""]
                for uid in bans:
                    name = ""
                    try:
                        items = _mcp_call("api_listRoomPlayers", {
                            "path": {"roomId": room_id}, "query": {"query": uid},
                        }).get("items") or []
                        if items:
                            name = str(items[0].get("name") or "").strip()
                    except Exception:
                        pass
                    lines.append(f"· 〖{name}〗（{uid}）" if name else f"· {uid}")
                lines.append("")
                lines.append("喵～名单就是这些，要解封喊本喵")
                return "\n".join(lines).strip()

            elif q_type in ("whitelist", "white"):
                room_id = MCP_ROOM_IDS.get(cluster_id, "")
                if not room_id:
                    return f"【{cluster_id}服】未接入面板，查不到白名单。"
                access = _mcp_call("api_getRoomAccessLists", {"path": {"roomId": room_id}})
                whites = access.get("whitelist") or []
                if whites:
                    return "\n".join(["📋 白名单 · 本喵也记着", f"{cluster_id}服 · {len(whites)} 人", "════════════════", ""] + [f"· {w}" for w in whites] + ["", "喵～白名单就这些"])
                return f"📋 {cluster_id}服 · 没设置白名单喵"

            else:
                return f"不支持的查询类型: {query_type}"

        except Exception as e:
            return f"查询出错: {_err_brief(e)}"

    @llm_tool(name="query_dst_chat_history")
    async def query_dst_chat_history(self, event: AstrMessageEvent, cluster_id: int = 1, count: int = 10) -> str:
        """查询饥荒服务器游戏内的近期公屏聊天记录与进出服广播。当群友询问游戏内聊了什么、最近发生了什么、要求描述游戏进展与玩家生存故事时必须调用。群内任何成员均可调用。

        Args:
            cluster_id (int): 目标服务器编号，1表示1服，2表示2服，默认1。
            count (int): 获取最近的聊天记录条数，默认10条，最大30条。
        """
        try:
            cluster_name = f"Cluster_{cluster_id}"
            limit = max(1, min(count, 30))
            cmd = f"tail -n {limit} /opt/dst-admin/data/saves/{cluster_name}/Master/server_chat_log.txt"
            content = _run_wsl_cmd(cmd)
            s_name = _get_cluster_display_name(cluster_id)
            if not content or not content.strip():
                return f"【{s_name}】暂无近期聊天记录。"
            cur_day = _get_current_cluster_day(cluster_name)
            base_dt = _get_server_log_base_time(f"/opt/dst-admin/data/saves/{cluster_name}/Master/server_log.txt")
            
            lines = [l for l in content.strip().splitlines() if l.strip()]
            sec_list = []
            max_sec = 0
            for l in lines:
                m = re.match(r"^\[(\d{2}):(\d{2}):(\d{2})\]", l.strip())
                if m:
                    sec = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
                    sec_list.append((sec, l))
                    if sec > max_sec:
                        max_sec = sec
                else:
                    sec_list.append((None, l))
            
            formatted_lines = []
            for sec, l in sec_list:
                if sec is not None:
                    day_diff = (max_sec - sec) // 480
                    item_day = max(1, cur_day - day_diff)
                    sec_in_day = sec % 480
                    phase = "白天" if sec_in_day < 240 else ("黄昏" if sec_in_day < 360 else "夜晚")
                    game_time_str = f"第{item_day}天 {phase}"
                    clean_msg = re.sub(r"^\[\d{2}:\d{2}:\d{2}\]:\s*", "", l)
                    if base_dt:
                        real_time = base_dt + timedelta(seconds=sec)
                        real_time_str = real_time.strftime("%H:%M")
                        formatted_lines.append(f"[{game_time_str} · {real_time_str}]")
                    else:
                        formatted_lines.append(f"[{game_time_str}]")
                    formatted_lines.append(clean_msg)
                    formatted_lines.append("")
                else:
                    formatted_lines.append(l)
                    
            return "\n".join(["💬 游戏公屏", f"{cluster_id}服 · 最近 {len(sec_list)} 条", "════════════════", ""] + formatted_lines).strip()
        except Exception as e:
            return f"获取聊天记录失败: {_err_brief(e)}"

    # 不依赖玩家 uid 的 TMI 世界级指令；其余指令必须带 uid，否则拒绝下发。
    _TMI_UID_OPTIONAL = {"season", "nextPhase", "skipDays", "timeScale", "rainOn", "rainOff", "save", "regenerateWorld", "specialEvent", "clearMap"}

    @llm_tool(name="execute_dst_console_command")
    async def execute_dst_console_command(self, event: AstrMessageEvent, cluster_id: int = 1, world: str = "Master", lua_code: str = "", count_prefab: str = "") -> str:
        """向饥荒服务器控制台下发 Lua 代码，或按 prefab 统计世界里某类实体的数量。地面与洞穴都支持。
        ※ 权限：lua_code 仅群主/管理员可用；count_prefab 是只读计数，所有成员可用。
        ※ 多行脚本：控制台逐行执行，本工具会自动合并成单行表达式；单条上限 4000 字符（面板限制 4096，超长会被拒）。批量操作请拆成多条短指令。
        ※ 取值：需要在游戏里统计数值、名单时，直接在 Lua 里 print("结果")，本工具会把控制台输出原样回读给你。绝对不要用 TheNet:Announce 发公告再调 query_dst_chat_history 去翻数字，那等于白白多绕两轮。
        ※ 诚实：返回“执行失败”或“只发送了公告”时，必须如实说明操作没完成及原因，严禁编造成功。
        ※ API 常识：AllPlayers 是全局数组、不是函数（AllPlayers() 会报错）；不要用 Ents——本服实测该表恒为空。
        ※ prefab 名不许凭记忆猜：凡按 prefab 计数、筛选、生成，写 Lua 前必须先用 search_dst_prefab 查准真实名字。统计结果为 0 时先怀疑名字写错，绝对禁止把 0 当成“没有”回答群友。
        ※ 清理/移除/删除类 Lua 严禁 string.find 模糊匹配，必须严格全等（如 item.prefab == 'oceanfish_medium_8_inv' 或 STRINGS.NAMES[string.upper(item.prefab)] == '太阳鱼'），严防误删相似前后缀的道具。
        ※ 查世界配置/服务器设置：一律调 query_dst_room_config 从磁盘读；禁止在控制台执行 io.open、TheSim:GetPersistentString、error()，会直接把游戏进程搞崩。
        ※ 爆物：inventory:DropEverything() 只丢背包与装备本体，容器内物品要先递归 container:DropEverything()。
        ※ 删除地图实体：按 prefab 严格全等遍历删除并设上限（如最多 200 个），不要用大半径 FindEntities 全图扫描，会卡服。

        Args:
            cluster_id (int): 目标服务器编号，1表示1服，2表示2服，默认1。
            world (str): Master=地面（默认），Caves=洞穴。地面与洞穴是独立进程；群友问“地上和地下”就两个世界各调一次。
            lua_code (str): 完整 Lua 命令，仅群主/管理员。例如 'c_give("goldnugget", 10)'。
            count_prefab (str): 只读计数模板，填 prefab 英文名（如 spiderden、merm）。传了它就用模板统计、忽略 lua_code，所有成员可用。
        """
        if world not in _MCP_WORLD_IDS:
            return "world 只能是 Master（地面）或 Caves（洞穴）。"
        if count_prefab.strip():
            prefab = re.sub(r"[^a-z0-9_]", "", count_prefab.strip().lower())
            if not prefab:
                return "count_prefab 需要 prefab 英文名（如 spiderden），可先用 search_dst_prefab 按中文名查准。"
            lua_code = _COUNT_LUA.replace("{p}", prefab)
        elif not _is_group_admin(event):
            return "权限不足：自定义 Lua 只有群主和管理员能下发。若只是想统计世界里某个东西有多少个，改用 count_prefab 参数填 prefab 英文名，所有成员都能用。"
        if not lua_code.strip():
            return "未提供有效的 Lua 命令或 count_prefab。"
        s_name = _get_cluster_display_name(cluster_id)
        w_cn = "地面" if world == "Master" else "洞穴"
        ok, detail = _dst_console(cluster_id, lua_code, world)
        if not ok:
            return f"【{s_name}·{w_cn}】控制台执行失败：{detail}。必须如实把失败原因告诉群友，严禁声称操作成功。"
        # 已经把控制台输出读回来了，直接给模型，别再让它自己猜或去翻公告
        if detail and detail != "执行成功":
            if len(detail) > 1500:          # c_findnext 会打印调试行，保留尾部（结果在最后）
                detail = "…（前略）\n" + detail[-1500:]
            return f"【{s_name}·{w_cn}】控制台输出：\n{detail}"
        if "Announce" in lua_code and not any(k in lua_code for k in _LUA_ACTIONS):
            return f"【{s_name}·{w_cn}】这段 Lua 只发送了公告，没有任何实际的物品/实体操作，游戏里什么都没改变。必须如实告诉群友操作没有真正执行。"
        return f"【{s_name}·{w_cn}】指令已下发且控制台无报错。无报错不等于物品已生成，若群友追问结果，必须先查询确认再回答。"

    @llm_tool(name="query_dst_room_config")
    async def query_dst_room_config(self, event: AstrMessageEvent, cluster_id: int = 1, scope: str = "all") -> str:
        """直接从服务器磁盘读取饥荒房间的静态配置文件（世界生成参数 leveldataoverride.lua、服务器参数 server.ini、房间参数 cluster.ini）。查询世界规则（季节、世界再生 regrowth、资源多少、生物密度、地图预设等）或服务器设置时必须调用本工具，严禁改用游戏控制台探测。群内任何成员均可调用。

        Args:
            cluster_id (int): 目标服务器编号，1表示1服，2表示2服，默认1。
            scope (str): 读取范围，all=地面+洞穴+房间(默认)，master=仅地面，caves=仅洞穴，cluster=仅房间参数。
        """
        base = f"/opt/dst-admin/data/saves/Cluster_{cluster_id}"
        targets = []
        if scope in ("all", "cluster"):
            targets.append(("房间参数 cluster.ini", f"{base}/cluster.ini"))
        for shard, label in (("Master", "地面"), ("Caves", "洞穴")):
            if scope not in ("all", shard.lower()):
                continue
            targets += [
                (f"{label}·服务器参数 server.ini", f"{base}/{shard}/server.ini"),
                (f"{label}·世界生成参数 leveldataoverride.lua", f"{base}/{shard}/leveldataoverride.lua"),
            ]
        out = []
        for label, path in targets:
            txt = _run_wsl_cmd(f"cat {path} 2>/dev/null").strip()
            if path.endswith(".ini"):
                txt = _redact_ini(txt)
            out.append(f"── {label} ──\n{txt}" if txt else f"── {label} ──\n（文件不存在，即为游戏默认值）")
        body = "\n\n".join(out)
        if len(body) > 3000:
            body = body[:3000] + "\n…（内容过长已截断，可用 scope 只查某一部分）"
        return f"【{_get_cluster_display_name(cluster_id)}】房间静态配置：\n{body}"

    @llm_tool(name="query_dst_history_events")
    async def query_dst_history_events(self, event: AstrMessageEvent, cluster_id: int = 1) -> str:
        """查询饥荒服务器的真实历史演进编年史（各阶段天数跨度与四季更迭，例如1-20天秋季）以及历史重大事件（所有玩家遇难死亡、具体死因、复活救援记录）。当群友询问历史阶段、某几天发生了什么、回顾历史故事或死因时调用。群内任何成员均可调用。

        Args:
            cluster_id (int): 目标服务器编号，1表示1服，2表示2服，默认1。
        """
        return get_dst_history_summary(cluster_id=cluster_id)


    @llm_tool(name="query_dst_map")
    async def query_dst_map(
        self, event: AstrMessageEvent, cluster_id: int = 1, is_caves: bool = False
    ) -> str:
        """查询并向群聊发送饥荒服务器的真实 2D 全貌地图图片（地面或洞穴）。群内任何成员均可调用。

        Args:
            cluster_id (int): 目标服务器编号，1表示1服，2表示2服，默认1。
            is_caves (bool): 是否查询洞穴地图，False表示查询地面地图(默认)，True表示查询洞穴地图。
        """
        s_name = _get_cluster_display_name(cluster_id)
        layer_name = "洞穴" if is_caves else "地面"
        room_id = MCP_ROOM_IDS.get(int(cluster_id), "")
        if not room_id:
            return f"【{s_name}】未接入面板，无法生成地图。"
        world_id = _MCP_WORLD_IDS["Caves" if is_caves else "Master"]

        # 渲染基于最新存档快照，所以先拿到该世界的存档引用
        try:
            sessions = _mcp_call("api_listWorldSessions", {
                "path": {"roomId": room_id, "worldId": world_id},
            }).get("items") or []
        except Exception as e:
            return f"【{s_name} {layer_name}地图】读取存档列表失败：{_err_brief(e)}"
        latest = next((x for x in sessions if x.get("latest")), None) or (sessions[0] if sessions else None)
        if not latest:
            return f"【{s_name} {layer_name}地图】还没有存档快照，请先在游戏内存档一次。"

        try:
            _mcp_call("api_generateRoomMap", {
                "path": {"roomId": room_id},
                "body": {"worldId": world_id, "sessionId": latest["id"], "layers": ["terrain"]},
                "requestId": f"dstmap-{int(time.time() * 1000)}",
            })
        except Exception as e:
            return f"【{s_name} {layer_name}地图】渲染失败：{_err_brief(e)}"

        map_id = ""
        deadline = time.time() + 90
        while time.time() < deadline:
            try:
                items = _mcp_call("api_listRoomMaps", {"path": {"roomId": room_id}}).get("items") or []
            except Exception:
                items = []
            hit = next((m for m in items if m.get("sessionId") == latest["id"] and m.get("status") == "succeeded"), None)
            if hit:
                map_id = hit.get("id") or ""
                break
            time.sleep(2)

        if not map_id:
            return f"【{s_name} {layer_name}地图】渲染超时，稍等一会儿再喊本喵。"

        try:
            img_bytes = _mcp_http_get(f"/api/v2/maps/{map_id}/images/terrain")
        except Exception as e:
            return f"【{s_name} {layer_name}地图】图片下载失败：{_err_brief(e)}"

        from astrbot.api.message_components import Image as AstrImage, Plain
        await event.send(event.chain_result([
            Plain(f"【{s_name} {layer_name}全貌地图】\n"),
            AstrImage.fromBytes(img_bytes),
        ]))
        event.set_extra("dst_direct_sent", True)
        return f"【系统提示：{s_name} {layer_name}地图图片已由底层直接发送至群聊中。你必须保持完全静默，绝不可再发送任何确认文本或废话回复！】"

    @llm_tool(name="query_dst_wiki")
    async def query_dst_wiki(self, event: AstrMessageEvent, keyword: str) -> str:
        """查询饥荒官方及核心模组（为爽而虐、暗影世界、永不妥协等）权威百科知识库（涵盖全物品料理配方、Boss属性与变异招式、模组爽虐机制改动、角色强化惩戒等全量条目，随版本持续增量更新）。当群友询问饥荒游戏玩法、合成公式、配方、模组机制、怪物属性或代码时调用。群内任何成员均可调用。

        Args:
            keyword (str): 要查询的物品、生物、料理、模组或机制名称（如“为爽而虐”、“暗影世界”、“波兰水饺”、“独眼巨鹿”等）。
        """
        clean_kw = keyword.strip()
        if not clean_kw:
            return "喵～给个饥荒关键词吧，本喵去翻百科。"
        if not os.path.exists(_SCRIPT_WIKI):
            return "百科脚本缺失（scripts/query_wiki.py），插件安装不完整，请重新部署。"
        # 用 shlex.quote 逐个转义参数，避免关键词里的 $()、反引号、分号被 bash 执行
        env_prefix = ""
        if WIKI_DIR_CONF:
            env_prefix = f"DST_WIKI_DIR={shlex.quote(_wsl_path(WIKI_DIR_CONF))} "
        cmd = f"{env_prefix}python3 {shlex.quote(_wsl_path(_SCRIPT_WIKI))} {shlex.quote(clean_kw)}"
        res = _run_wsl_cmd(cmd)
        if not res or "未找到与" in res or "暂未查询到" in res:
            return f"【营地百科查询结果】暂未收录与“{clean_kw}”相关的具体词条。若群友是在询问或要求评价该物品/机制，说明官方及当前模组中根本不存在该设定！你必须在回复第一句话明确断然指出该设定并不存在，绝对严禁顺从群友的前提展开任何假设性评价或编造数值！"

        # 规则：如果回复内容涉及到的关联图片小于等于3张，就带图一起回复
        try:
            imgs = extract_primary_illustrations(res)
            if 1 <= len(imgs) <= 3:
                sid = getattr(event, "unified_msg_origin", None)
                if sid:
                    valid = [os.path.join(WIN_ASSETS_DIR, os.path.basename(img)) for img in imgs]
                    valid = [p for p in valid if os.path.exists(p)]
                    if 1 <= len(valid) <= 3:
                        self._pending_images[sid] = valid
        except Exception:
            pass

        return f"【营地百科官方权威内容如下，你必须严格仅依据此内容作答。若内容中未提及群友所说的特定道具/技能/变种机制，必须明确指出其未实装或不存在，严禁凭空脑补】：\n{res}"

    @llm_tool(name="search_dst_mod")
    async def search_dst_mod(self, event: AstrMessageEvent, keyword: str) -> str:
        """检索模组。先查本服 mods 目录里已安装的模组（含本地模组，直接读 modinfo.lua 的名字与功能介绍，秒回），没命中再检索创意工坊。支持中文/英文名称模糊搜索，或直接给 Steam Workshop ID。当群友询问某个 Mod 是什么、有什么功能、编号，或确认 Mod 是否存在时调用。群内任何成员均可调用。返回的简介是完整原文，自己概括成几句话回答即可，不必再查百科或面板接口补全。

        Args:
            keyword (str): 要检索的模组名称或 Steam Workshop ID（如“show me”、“3151929150”）。
        """
        clean_kw = keyword.strip().replace('"', "").replace("'", "")
        if not clean_kw:
            return "喵～说个模组名称或创意工坊 ID 吧，本喵去查。"
        # 本服装了的模组直接读 modinfo.lua，别去创意工坊绕一圈
        kw = clean_kw.lower().removeprefix("local:")
        local = [m for m in _local_mods() if kw in str(m.get("folder") or "").lower() or kw in str(m.get("name") or "").lower()]
        if local:
            lines = ["🔧 本服模组（读自服务器 mods 目录）"]
            for m in local[:3]:
                lines.append("")
                lines.append(f"✦ {m.get('name') or m.get('folder')}")
                meta = [f"目录 {m.get('folder')}"]
                if m.get("author"):
                    meta.append(f"作者 {m['author']}")
                if m.get("version"):
                    meta.append(f"v{m['version']}")
                lines.append("   " + " · ".join(meta))
                if m.get("desc"):
                    lines.append(f"   简介：{m['desc']}")
            return "\n".join(lines)
        try:
            items = _mcp_call("api_searchMods", {
                "query": {"query": clean_kw, "pageSize": 3},
            }).get("items") or []
        except Exception as e:
            return f"【模组检索】搜索失败：{_err_brief(e)}"
        if not items:
            return f"【模组检索】未找到与「{clean_kw}」相关的模组。"

        lines = ["🔧 创意工坊模组检索"]
        for it in items[:3]:
            mid = str(it.get("id") or "")
            lines.append("")
            lines.append(f"✦ {it.get('name') or '未知模组'}")
            lines.append(f"   ID：{mid}")
            if it.get("author"):
                lines.append(f"   作者：{it['author']}")
            if it.get("subscriptions"):
                lines.append(f"   订阅：{it['subscriptions']:,}")
            tags = it.get("tags") or []
            if tags:
                lines.append(f"   标签：{'、'.join(str(t) for t in tags[:6])}")
            desc = ""
            if mid.isdigit():
                try:
                    desc = _strip_bbcode(_mcp_call("api_getModDetails", {
                        "path": {"modId": mid},
                    }).get("description") or "")
                except Exception:
                    desc = ""
            # 单条简介上限 6000 字（实测最长 5751），超长 Steam 简介才切，够 AI 自己总结
            if desc:
                lines.append(f"   简介：{desc[:6000]}{'…' if len(desc) > 6000 else ''}")
        return "\n".join(lines).strip()

    @llm_tool(name="query_dst_system_health")
    async def query_dst_system_health(self, event: AstrMessageEvent, cluster_id: int = 0) -> str:
        """查询饥荒云服务器各世界绑定的单个物理 CPU 核心负载与硬件健康体检报告。当群友询问服务器卡不卡、配置负载、硬件占用、体检评级、或询问过去/当前负载高不高时必须调用。群内任何成员均可调用。

        Args:
            cluster_id (int): 目标服务器编号，0为全部服务器（默认），1为1服，2为2服。用户若未明确指定是哪个服，必须保持默认值 0 以展示全部世界的单核心负载。
        """
        gid = str(event.get_group_id() or "").strip()
        sender_id = str(event.get_sender_id() or "").strip()
        if gid != TARGET_GROUP_ID and sender_id != MASTER_QQ:
            return "当前群聊未启用饥荒查询功能。"
        cid = int(cluster_id) if str(cluster_id) in ("1", "2") else 0
        return format_system_health_text(cluster_filter=cid)

    @llm_tool(name="query_dst_player_archive")
    async def query_dst_player_archive(
        self, event: AstrMessageEvent, keyword: str, cluster_id: int = 1
    ) -> str:
        """按昵称或 Klei UID 查一个玩家在服里的档案：当前角色、存活天数、在线状态、是否管理员或已封禁、首次出现与最近在线时间。当群友问“这人是谁/存活多久/什么时候玩过/UID是多少”时调用。群内任何成员均可调用。

        Args:
            keyword (str): 要检索的玩家昵称或 Klei UID（例如“小鱼干”、“KU_xxx”）。
            cluster_id (int): 目标服务器编号，1表示1服(默认)，2表示2服。
        """
        gid = str(event.get_group_id() or "").strip()
        sender_id = str(event.get_sender_id() or "").strip()
        if gid != TARGET_GROUP_ID and sender_id != MASTER_QQ:
            return "当前群聊未启用饥荒查询功能。"
        card = format_player_archive_text(keyword, cluster_id)
        return card

    @llm_tool(name="query_dst_player_inventory")
    async def query_dst_player_inventory(
        self, event: AstrMessageEvent, keyword: str = "", cluster_id: int = 1
    ) -> str:
        """查验饥荒服务器离线或在线玩家的随身携带物资、穿戴装备、主背包及容器内部物品与数量。当群友或管理员查询玩家身上带了什么、查背包、查装备、查物资、或防熊查证时调用。群内任何成员均可调用。

        Args:
            keyword (str): 要查验的玩家昵称或 Klei UID（例如“小鱼干”、“KU_xxx”）。如果引用了播报消息，可为空。
            cluster_id (int): 目标服务器编号，1表示1服(默认)，2表示2服。
        """
        gid = str(event.get_group_id() or "").strip()
        sender_id = str(event.get_sender_id() or "").strip()
        if gid != TARGET_GROUP_ID and sender_id != MASTER_QQ:
            return "当前群聊未启用饥荒查询功能。"

        target = (keyword or "").strip()
        cid = cluster_id
        if not target:
            quoted_text = await _get_quoted_text_async(event)
            q_target, q_cid = _extract_player_and_cid_from_quote(quoted_text)
            if q_target:
                target = q_target
                if q_cid:
                    cid = q_cid

        if not target:
            return "请提供要查验物资的玩家昵称或 Klei UID，或直接引用进出服播报进行查询。"

        card = format_player_inventory_text(target, cid)
        return card

    @llm_tool(name="query_dst_help_guide")
    async def query_dst_help_guide(self, event: AstrMessageEvent) -> str:
        """查询饥荒机器人专属功能指南与指令菜单（支持自动识别管理员与普通成员身份输出对应功能清单）。当群友询问帮助、你能做什么、怎么用、功能菜单时调用。群内任何成员均可调用。"""
        gid = str(event.get_group_id() or "").strip()
        sender_id = str(event.get_sender_id() or "").strip()
        if gid != TARGET_GROUP_ID and sender_id != MASTER_QQ:
            return "当前群聊未启用饥荒查询功能。"
        card = format_help_text(_is_group_admin(event))
        return card

    @llm_tool(name="search_dst_prefab")
    async def search_dst_prefab(self, event: AstrMessageEvent, keyword: str = "") -> str:
        """按中文名或英文片段查询饥荒实体的真实 prefab 名（2956 条官方与模组词条）。

        写 Lua 需要 prefab 名时先调用本工具查准了再写，不要凭记忆猜——prefab 名写错时游戏不会报错，只会静默统计出 0，
        比直接报错更危险。群内任何成员均可调用。

        Args:
            keyword (str): 中文名（如"箱子"、"冰箱"、"烹饪锅"）或英文片段（如"chest"、"icebox"）。
        """
        kw = (keyword or "").strip().lower()
        if not kw:
            return "喵～给个中文名或英文片段，本喵去查真实 prefab。"
        hits = []
        for p, cn in PREFAB_NAMES.items():
            if kw in p.lower() or kw in str(cn).lower():
                hits.append(f"{p}（{cn}）")
        if not hits:
            return f"字典里没找到与「{keyword}」相关的 prefab，换个关键词或者用英文片段试试喵～"
        body = "\n".join(hits[:20])
        more = f"\n…还有 {len(hits) - 20} 项，关键词再具体一点喵～" if len(hits) > 20 else ""
        return f"「{keyword}」匹配到 {len(hits)} 项：\n{body}{more}"

    @filter.on_decorating_result()
    async def on_decorating_result(self, event: AstrMessageEvent):
        """出站前清洗：QQ 不渲染 Markdown，剥掉模型漏出的语法符号，并按需补图（仅饥荒专属群生效）"""
        gid = str(event.get_group_id() or "").strip()
        if gid != TARGET_GROUP_ID:
            return


        result = event.get_result()
        # 卡片已经由工具直发到群里时，模型剩下的任何文字都是多余的——它经常把工具的
        # 内部提示"你必须保持静默"反过来当话讲出去。这里在出站前直接丢掉，不靠模型自觉。
        if event.get_extra("dst_direct_sent") and result and hasattr(result, "chain"):
            # 必须直接改消息链：只 return 会把模型那句"本喵保持静默"原样发出去
            result.chain = [c for c in result.chain if c.__class__.__name__ not in ("Plain", "Reply")]
            return

        if result and hasattr(result, "chain"):
            for comp in result.chain:
                if comp.__class__.__name__ == "Plain" and getattr(comp, "text", None):
                    comp.text = _strip_markdown(comp.text)

        sid = getattr(event, "unified_msg_origin", None)
        if not sid:
            return
        imgs = self._pending_images.pop(sid, [])
        if imgs and 1 <= len(imgs) <= 3 and result and hasattr(result, "chain"):
            for img_path in imgs:
                if os.path.exists(img_path):
                    result.chain.append(Image.fromFileSystem(img_path))

    @filter.regex(r".*")
    async def on_new_member_join(self, event: AstrMessageEvent):
        """当新成员加入本群时自动欢迎，或用户直接发送快捷指令时极速返回状态"""
        raw = getattr(event.message_obj, "raw_message", None)
        if raw is None:
            return

        # 1. 快捷直达指令拦截响应（铁律：必须 @机器人 才会响应，绝不主动插话抢话）
        if _raw_field(raw, "post_type") == "message":
            gid = str(_raw_field(raw, "group_id", "") or "").strip()
            if gid == TARGET_GROUP_ID:
                msg_str = str(event.message_str or "").strip()
                # 只认真正的 @机器人 或唤醒前缀：core 已按 self_id 判定，@别人（如 @群主）不算
                if not getattr(event, "is_at_or_wake_command", False):
                    return
                # 清洗@和多余空格
                # AstrBot 把 At 段渲染成 [At:qq]，CQ 码形式在 aiocqhttp 下不会出现，两种都清掉
                clean_msg = re.sub(r'\[CQ:at,[^\]]+\]|\[At:[^\]]+\]|@\S+|\s+', '', msg_str)
                if clean_msg in ("1服详情", "一服详情", "1服详细", "查1服", "1群详情", "一群详情", "1群详细", "查1群", "1服", "1群"):
                    yield event.plain_result(format_detail_status_text(1))
                    event.stop_event()
                    return
                elif clean_msg in ("2服详情", "二服详情", "2服详细", "查2服", "2群详情", "二群详情", "2群详细", "查2群", "2服", "2群"):
                    yield event.plain_result(format_detail_status_text(2))
                    event.stop_event()
                    return
                elif clean_msg in ("查房", "查服", "查群", "服务器状态", "状态"):
                    yield event.plain_result(format_status_text())
                    event.stop_event()
                    return
                elif clean_msg in ("体检", "服务器体检", "卡不卡", "硬件体检", "负载", "1服体检", "一服体检", "2服体检", "二服体检"):
                    cid = 1 if ("1" in clean_msg or "一" in clean_msg) else (2 if ("2" in clean_msg or "二" in clean_msg) else 0)
                    yield event.plain_result(format_system_health_text(cid))
                    event.stop_event()
                    return
                elif clean_msg in ("帮助", "功能", "你能做什么", "你会做什么", "菜单", "help", "指南", "使用说明", "有什么功能", "怎么用") or any(k in clean_msg for k in ("你能做什么", "你会做什么", "有什么功能")):
                    is_admin = _is_group_admin(event)
                    yield event.plain_result(format_help_text(is_admin))
                    event.stop_event()
                    return
                elif clean_msg in ("黑名单", "查黑名单", "封禁名单", "查封禁", "1服黑名单", "一服黑名单"):
                    cid = 1 if ("1" in clean_msg or "一" in clean_msg) else 0
                    yield event.plain_result(format_blocklist_text(cid))
                    event.stop_event()
                    return
                elif clean_msg in ("2服黑名单", "二服黑名单"):
                    yield event.plain_result(format_blocklist_text(2))
                    event.stop_event()
                    return
                elif any(clean_msg.startswith(p) for p in (
                    "查背包", "查装备", "查物资", "查物品", "查随身",
                    "查他", "查他背包", "查他装备", "查他物资", "查他物品",
                    "1服查背包", "2服查背包", "一服查背包", "二服查背包",
                    "1服查物品", "2服查物品", "一服查物品", "二服查物品",
                    "1服查装备", "2服查装备", "一服查装备", "二服查装备",
                    "1服查物资", "2服查物资", "一服查物资", "二服查物资"
                )) or clean_msg in ("查背包", "查装备", "查物资", "查物品", "查随身", "查他", "查他背包", "查他装备", "查他物资", "查他物品", "查", "查查"):
                    cid = 2 if ("2服" in clean_msg or "二服" in clean_msg) else 1
                    target = re.sub(r'^(1服|2服|一服|二服)?(查背包|查装备|查物资|查物品|查随身|查他背包|查他装备|查他物资|查他物品|查他|查查|查)\s*', '', clean_msg).strip()
                    if not target:
                        quoted_text = await _get_quoted_text_async(event)
                        if quoted_text:
                            q_target, q_cid = _extract_player_and_cid_from_quote(quoted_text)
                            if q_target:
                                target = q_target
                                if q_cid and not ("2服" in clean_msg or "二服" in clean_msg or "1服" in clean_msg or "一服" in clean_msg):
                                    cid = q_cid
                    if target:
                        yield event.plain_result(format_player_inventory_text(target, cid))
                        event.stop_event()
                        return
                    else:
                        yield event.plain_result("喵～指令后面要跟上玩家昵称/UID 哦（比如「查物品 玩家名」），也可以直接引用进出服播报回一句「查物品」。")
                        event.stop_event()
                        return
        # 2. 新成员进群 notice
        if _raw_field(raw, "post_type") == "notice" and _raw_field(raw, "notice_type") == "group_increase":
            if str(_raw_field(raw, "group_id", "") or "") != TARGET_GROUP_ID:
                return
            user_id = str(_raw_field(raw, "user_id", "") or "")
            if not user_id:
                return
            from astrbot.api.message_components import At, Plain
            welcome_text = (
                "🎉 欢迎加入小鱼干营地喵～\n"
                "════════════════\n\n"
                "本喵是营地管家\n"
                "在群里 @本喵 就能查\n\n"
                "🐾 状态 · 🗺 地图\n"
                "📖 攻略 · 🎒 背包\n\n"
                "快捷指令\n"
                "· 查房\n"
                "· 1服详情\n"
                "· 体检\n\n"
                "有事喊本喵就好，服务器问题找群管理喵～"
            )
            yield event.chain_result([At(qq=user_id), Plain(welcome_text)])
            event.stop_event()

