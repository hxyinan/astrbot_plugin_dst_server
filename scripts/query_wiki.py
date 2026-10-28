import sys, json, os, re

q = sys.argv[1].strip() if len(sys.argv) > 1 else ""
if not q:
    print("未提供查询关键词")
    sys.exit(0)

# 词条库目录：优先环境变量 DST_WIKI_DIR，其次探测两个历史常见位置。
# 词条为 Markdown 文件，一篇一个词条（见 README「百科词条库」一节）。
_WIKI_CANDIDATES = [
    os.environ.get("DST_WIKI_DIR", ""),
    "/root/vault/饥荒联机版",
    "/root/dmp_files/plugins/ai_chat/wiki",
]
wiki_dir = next((p for p in _WIKI_CANDIDATES if p and os.path.exists(p)), "")
q_clean = q.lower().strip()
best_fn = None

# 0. 动态概览与统计拦截（不记任何硬编码数字，随知识库实时统计）
if any(k in q_clean for k in ["概览", "统计", "总览", "多少条", "条目数", "条目总量", "知识库介绍", "百科介绍"]):
    if os.path.exists(wiki_dir):
        all_mds = [f for f in os.listdir(wiki_dir) if f.endswith(".md")]
        assets_dir = os.path.join(wiki_dir, "assets")
        asset_count = 0
        if os.path.exists(assets_dir):
            for root, _, files in os.walk(assets_dir):
                asset_count += len(files)
        print(f"【饥荒官方营地百科 · 实时概览】\n"
              f"本百科基于官方灰机 Wiki 持续增量镜像与本地化构建，随时与游戏版本保持同步。\n"
              f"- 权威词条总数：实时收录 {len(all_mds)} 篇（涵盖料理、生物Boss、装备、科技、角色、任务机制等）\n"
              f"- 本地高清资源：实时收录 {asset_count} 张原版图鉴与插画\n"
              f"- 查询支持：支持全量物品直查、生物属性、料理公式配方推演及多步机制关联")
        sys.exit(0)

# 0. 常用别名映射
ALIASES = {
    "大肉": "肉.md",
    "生肉": "肉.md",
    "熟大肉": "肉.md",
    "大肉干": "肉.md",
    "小肉干": "肉.md",
    "小肉": "小肉.md",
    "寄居蟹奶奶": "寄居蟹隐士.md",
    "隐士奶奶": "寄居蟹隐士.md",
    "珍珠奶奶": "寄居蟹隐士.md",
    "奶奶任务": "寄居蟹隐士.md",
    "隐士任务": "寄居蟹隐士.md",
    "寄居蟹任务": "寄居蟹隐士.md",
    "寄居蟹好感度": "寄居蟹隐士.md",
    "寄居蟹": "寄居蟹隐士.md",
    "老奶奶": "薇克巴顿.md",
    "女武神": "薇格弗德.md",
    "机器人": "WX-78.md",
    "火女": "薇洛.md",
    "大力士": "沃尔夫冈.md",
    "刺客": "沃拓克斯.md",
    "恶魔人": "沃拓克斯.md",
    "鱼人": "沃特.md",
    "植物人": "沃姆伍德.md",
    "老麦": "麦斯威尔.md",
    "四本": "暗影操控器.md",
    "魔法二本": "暗影操控器.md",
    "三本": "灵子分解器.md",
    "魔法一本": "灵子分解器.md",
    "二本": "炼金引擎.md",
    "一本": "科学机器.md",
    "为爽而虐": "模组/为爽而虐/00_总览_全机制架构与配置索引.md",
    "为爽而虐模组": "模组/为爽而虐/00_总览_全机制架构与配置索引.md",
    "爽虐": "模组/为爽而虐/00_总览_全机制架构与配置索引.md",
    "暗影世界": "模组/为爽而虐/03_暗影世界与分身生态/暗影世界系统深度解析.md",
    "海棠诅咒": "模组/为爽而虐/01_角色图鉴/99_通用战斗惩戒与生存诅咒.md",
    "永不妥协": "模组/永不妥协/00_总览_全机制架构与配置索引.md",
    "永不妥协简介": "模组/永不妥协/00_总览_全机制架构与配置索引.md",
    "um": "模组/永不妥协/00_总览_全机制架构与配置索引.md",
    "um简介": "模组/永不妥协/00_总览_全机制架构与配置索引.md",
    "薇茜": "模组/永不妥协/01_专属角色与原版重构/专属角色_薇茜(Wixie).md",
    "wixie": "模组/永不妥协/01_专属角色与原版重构/专属角色_薇茜(Wixie).md",
    "瓦索姆": "模组/永不妥协/01_专属角色与原版重构/专属角色_瓦索姆(Wathom).md",
    "wathom": "模组/永不妥协/01_专属角色与原版重构/专属角色_瓦索姆(Wathom).md",
    "温基": "模组/永不妥协/01_专属角色与原版重构/专属角色_温基(Winky).md",
    "winky": "模组/永不妥协/01_专属角色与原版重构/专属角色_温基(Winky).md",
    "鼠潮": "模组/永不妥协/02_核心硬核法则与灾变/鼠潮危机与粮仓防御.md",
    "老兵诅咒": "模组/永不妥协/02_核心硬核法则与灾变/老兵诅咒与世界阶段.md",
    "永不妥协数值": "模组/永不妥协/00_永不妥协_全机制核心常量与精确数值字典.md",
    "永不妥协常量": "模组/永不妥协/00_永不妥协_全机制核心常量与精确数值字典.md",
    "um数值": "模组/永不妥协/00_永不妥协_全机制核心常量与精确数值字典.md",
    "为爽而虐数值": "模组/为爽而虐/00_为爽而虐_全机制底层硬核数值与倍率字典.md",
    "为爽而虐倍率": "模组/为爽而虐/00_为爽而虐_全机制底层硬核数值与倍率字典.md",
    "爽虐数值": "模组/为爽而虐/00_为爽而虐_全机制底层硬核数值与倍率字典.md",
}

for alias, target in ALIASES.items():
    if alias == q_clean or alias in q_clean:
        if os.path.exists(os.path.join(wiki_dir, target)):
            best_fn = target
            break

# 1. 优先在本地知识库中精确与模糊匹配（支持子目录递归）
if not best_fn and os.path.exists(wiki_dir):
    all_files = []
    for root, _, files in os.walk(wiki_dir):
        for f in files:
            if f.endswith(".md"):
                rel = os.path.relpath(os.path.join(root, f), wiki_dir)
                all_files.append(rel)

    # 完全匹配（文件名优先）
    for rel in all_files:
        base = os.path.basename(rel)[:-3].lower()
        if q_clean == base:
            best_fn = rel
            break
    # 前缀匹配
    if not best_fn:
        for rel in all_files:
            base = os.path.basename(rel)[:-3].lower()
            if base.startswith(q_clean):
                best_fn = rel
                break
    # 包含匹配
    if not best_fn:
        for rel in all_files:
            base = os.path.basename(rel)[:-3].lower()
            if q_clean in base or base in q_clean:
                best_fn = rel
                break

if not best_fn:
    print(f"【营地百科查询结果】暂未查询到与“{q}”相关的官方条目。")
    sys.exit(0)

# 读取核心匹配条目
doc_path = os.path.join(wiki_dir, best_fn)
with open(doc_path, "r", encoding="utf-8") as f:
    main_content = f.read().strip()

title_name = os.path.basename(best_fn)[:-3]
output = [f"【饥荒百科 · {title_name}】\n{main_content}"]

# 2. 智能联想关联机制（当查询食材与料理相关时，自动附带烹饪锅对应食谱）
is_cooking_query = any(k in q_clean for k in ["料理", "做", "烹饪", "食谱", "锅", "菜", "多少种", "吃"])
is_meat_query = any(k in q_clean for k in ["肉", "大肉", "小肉", "怪物肉", "蛙腿", "鱼", "叶肉"]) or best_fn in ("肉.md", "小肉.md", "怪物肉.md")

if (is_cooking_query or is_meat_query) and os.path.exists(os.path.join(wiki_dir, "烹饪.md")):
    with open(os.path.join(wiki_dir, "烹饪.md"), "r", encoding="utf-8") as f:
        cook_text = f.read()
    
    # 提取所有包含肉度要求的食谱
    cook_lines = cook_text.splitlines()
    meat_recipes = []
    for line in cook_lines:
        if ("肉度" in line or "肉类" in line) and "|" in line and any(sym in line for sym in [">", "=", "≥"]):
            meat_recipes.append(line.strip())
    
    if meat_recipes:
        output.append("\n【关联机制扩展 · 《烹饪》食谱中涉及肉度的全部料理要求】")
        output.append("在烹饪锅中，大肉（肉、熟肉、肉干）提供 1.0 肉度，以下为涉及肉度条件的料理配方汇总（可据此分析统计）：")
        output.extend(meat_recipes[:40])

print("\n".join(output))
