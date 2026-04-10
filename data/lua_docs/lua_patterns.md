# Common Lua Patterns and Idioms

## Default / nil-safe access

```lua
-- Default value
local x = value or default_value

-- Safe table access
local name = (person and person.name) or "unknown"

-- Nil-check before call
if obj and obj.method then
    obj:method()
end
```

## String operations

```lua
-- Reverse a string (works for ASCII)
local function reverse_string(s)
    if type(s) ~= "string" then return "" end
    return string.reverse(s)
end

-- Split string by delimiter
local function split(s, sep)
    local parts = {}
    local pattern = "([^" .. sep .. "]+)"
    for part in s:gmatch(pattern) do
        parts[#parts + 1] = part
    end
    return parts
end

-- Trim whitespace
local function trim(s)
    return (s:gsub("^%s*(.-)%s*$", "%1"))
end

-- Check if string starts with prefix
local function starts_with(s, prefix)
    return s:sub(1, #prefix) == prefix
end

-- Count occurrences
local function count_occurrences(s, pattern)
    local count = 0
    for _ in s:gmatch(pattern) do count = count + 1 end
    return count
end
```

## Table / array operations

```lua
-- Deep copy of a table
local function deep_copy(orig)
    local copy = {}
    for k, v in pairs(orig) do
        if type(v) == "table" then
            copy[k] = deep_copy(v)
        else
            copy[k] = v
        end
    end
    return copy
end

-- Check if value exists in array
local function contains(t, value)
    for _, v in ipairs(t) do
        if v == value then return true end
    end
    return false
end

-- Map: apply function to each element
local function map(t, fn)
    local result = {}
    for i, v in ipairs(t) do
        result[i] = fn(v)
    end
    return result
end

-- Filter: keep elements matching predicate
local function filter(t, pred)
    local result = {}
    for _, v in ipairs(t) do
        if pred(v) then result[#result + 1] = v end
    end
    return result
end

-- Reduce / fold
local function reduce(t, fn, init)
    local acc = init
    for _, v in ipairs(t) do
        acc = fn(acc, v)
    end
    return acc
end

-- Flatten one level
local function flatten(t)
    local result = {}
    for _, sub in ipairs(t) do
        if type(sub) == "table" then
            for _, v in ipairs(sub) do result[#result + 1] = v end
        else
            result[#result + 1] = sub
        end
    end
    return result
end
```

## Sorting patterns

```lua
-- Sort numbers ascending
table.sort(numbers)

-- Sort descending
table.sort(t, function(a, b) return a > b end)

-- Sort strings case-insensitive
table.sort(words, function(a, b)
    return a:lower() < b:lower()
end)

-- Sort table of objects by field
table.sort(people, function(a, b)
    return a.age < b.age
end)

-- Bubble sort (educational)
local function bubble_sort(t)
    if type(t) ~= "table" then return t end
    local n = #t
    for i = 1, n - 1 do
        for j = 1, n - i do
            if t[j] > t[j + 1] then
                t[j], t[j + 1] = t[j + 1], t[j]
            end
        end
    end
    return t
end
```

## Number / math patterns

```lua
-- Clamp value to range
local function clamp(x, min_val, max_val)
    return math.max(min_val, math.min(max_val, x))
end

-- Round to N decimal places
local function round(x, decimals)
    local factor = 10 ^ (decimals or 0)
    return math.floor(x * factor + 0.5) / factor
end

-- Check integer
local function is_integer(x)
    return type(x) == "number" and math.type(x) == "integer"
end

-- Factorial
local function factorial(n)
    if n < 0 then return nil end
    if n == 0 then return 1 end
    local result = 1
    for i = 2, n do result = result * i end
    return result
end
```

## OOP pattern (Lua classes)

```lua
-- Simple class
local Animal = {}
Animal.__index = Animal

function Animal.new(name, sound)
    local self = setmetatable({}, Animal)
    self.name = name
    self.sound = sound
    return self
end

function Animal:speak()
    return self.name .. " says " .. self.sound
end

-- Usage
local cat = Animal.new("Cat", "meow")
print(cat:speak())   -- Cat says meow
```

## Error handling patterns

```lua
-- Function that returns value or nil + error
local function safe_divide(a, b)
    if b == 0 then
        return nil, "division by zero"
    end
    return a / b
end

local result, err = safe_divide(10, 0)
if err then
    print("Error:", err)
else
    print("Result:", result)
end

-- Wrap pcall for cleaner API
local function try(fn, ...)
    local ok, result = pcall(fn, ...)
    if ok then return result end
    return nil, result
end
```
