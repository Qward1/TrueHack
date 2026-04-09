```markdown
# Lua 5.4 Reference

## Basic Types
- nil, boolean, number, string, function, userdata, thread, table

## Variables
- Local: local x = 10
- Global: x = 10 (avoid in modules)

## Tables
- Constructor: t = {1, 2, 3} or t = {key = "value"}
- Access: t[1], t.key, t["key"]
- Length: #t

## Control Flow
- if condition then ... elseif ... else ... end
- while condition do ... end
- for i = 1, 10 do ... end
- for k, v in pairs(t) do ... end
- for i, v in ipairs(t) do ... end
- repeat ... until condition

## Functions
- function name(args) ... return value end
- local function name(args) ... end
- Anonymous: function(x) return x * 2 end

## String Library
- string.format, string.find, string.gsub, string.match
- string.sub, string.len, string.upper, string.lower
- string.rep, string.reverse, string.byte, string.char

## Table Library
- table.insert(t, value), table.insert(t, pos, value)
- table.remove(t, pos)
- table.sort(t, comp)
- table.concat(t, sep)
- table.move(t, from, to, dest)

## Math Library
- math.abs, math.ceil, math.floor, math.max, math.min
- math.sqrt, math.random, math.randomseed
- math.pi, math.huge

## IO Library
- io.open(filename, mode), file:read, file:write, file:close
- io.lines(filename)

## OS Library
- os.time, os.date, os.clock, os.difftime

## Modules
- require("module_name")
- Module pattern: local M = {} ... return M

## Error Handling
- pcall(func, args) -> ok, result
- xpcall(func, handler, args)
- error("message", level)
- assert(condition, message)

## Metatables
- setmetatable(t, mt)
- getmetatable(t)
- __index, __newindex, __add, __sub, __mul, __div
- __eq, __lt, __le, __tostring, __len, __call

## Coroutines
- coroutine.create(func)
- coroutine.resume(co, args)
- coroutine.yield(values)
- coroutine.status(co)
- coroutine.wrap(func)

## Common Patterns

### Class pattern
local MyClass = {}
MyClass.__index = MyClass

function MyClass.new(name)
    local self = setmetatable({}, MyClass)
    self.name = name
    return self
end

function MyClass:greet()
    return "Hello, " .. self.name
end

### Module pattern
local M = {}

function M.add(a, b)
    return a + b
end

function M.subtract(a, b)
    return a - b
end

return M

### Error handling pattern
local ok, result = pcall(function()
    -- dangerous code here
    return some_value
end)

if not ok then
    print("Error: " .. tostring(result))
else
    print("Success: " .. tostring(result))
end

### File reading pattern
local function read_file(path)
    local file = io.open(path, "r")
    if not file then
        return nil, "Cannot open file: " .. path
    end
    local content = file:read("*a")
    file:close()
    return content
end

### Table deep copy
local function deep_copy(t)
    if type(t) ~= "table" then return t end
    local copy = {}
    for k, v in pairs(t) do
        copy[deep_copy(k)] = deep_copy(v)
    end
    return setmetatable(copy, getmetatable(t))
end

### String split
local function split(str, sep)
    local result = {}
    for part in str:gmatch("([^" .. sep .. "]+)") do
        table.insert(result, part)
    end
    return result
end

### Map / Filter / Reduce
local function map(t, fn)
    local result = {}
    for i, v in ipairs(t) do
        result[i] = fn(v)
    end
    return result
end

local function filter(t, fn)
    local result = {}
    for _, v in ipairs(t) do
        if fn(v) then
            table.insert(result, v)
        end
    end
    return result
end

local function reduce(t, fn, init)
    local acc = init
    for _, v in ipairs(t) do
        acc = fn(acc, v)
    end
    return acc
end
```