-- 读取离线玩家存档快照，按 INV| 协议输出，由 main.py 统一渲染。
-- 用法: lua5.1 offline_inv.lua <玩家存档文件路径>
local path = arg[1]
local f = assert(io.open(path, 'rb'))
local s = f:read('*a')
f:close()

-- 存档前缀是 3 字节标记，正文从 return { 开始，尾部可能带控制字符
local i = assert(string.find(s, 'return {', 1, true))
local code = string.sub(s, i)
local j = string.find(code, '[%z\1-\8\11\12\14-\31]')
if j then code = string.sub(code, 1, j - 1) end
local t = assert(loadstring(code))()

-- 离线存档只存当前值，没有上限，所以输出绝对值而不是百分比
local function meta(d)
    local o = {}
    if d.finiteuses and d.finiteuses.uses then o[#o + 1] = 'N' .. math.floor(d.finiteuses.uses + 0.5) end
    if d.armor and d.armor.condition then o[#o + 1] = 'C' .. math.floor(d.armor.condition + 0.5) end
    if d.perishable and d.perishable.time then o[#o + 1] = 'T' .. math.floor(d.perishable.time + 0.5) end
    return table.concat(o, ',')
end

local function emit(p, it)
    local d = it.data or {}
    local n = d.stackable and d.stackable.stack or 1
    print('INV|' .. p .. '|' .. tostring(it.prefab) .. '|' .. n .. '||' .. meta(d))
end

local function walk(items, prefix, depth)
    if not items or depth > 3 then return end
    for k, it in ipairs(items) do
        if it and it.prefab then
            local p = prefix .. k
            emit(p, it)
            local c = it.data and it.data.container
            if c then walk(c.items, p .. '/', depth + 1) end
        end
    end
end

print('INV|NAME|')
print('INV|CHAR|' .. tostring(t.prefab) .. '|' .. tostring(t.age or ''))
local inv = (t.data or {}).inventory or {}
walk(inv.items, 'B', 0)
for k, it in pairs(inv.equip or {}) do
    if type(it) == 'table' and it.prefab then
        local p = 'E' .. tostring(k)
        emit(p, it)
        local c = it.data and it.data.container
        if c then walk(c.items, p .. '/', 1) end
    end
end
print('INV|END')
