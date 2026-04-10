# Lua 5.4 Standard Library Reference

## string library

```lua
-- Length
local n = #s                        -- byte length of string s

-- Concatenation
local s3 = s1 .. s2

-- Case conversion
string.upper(s)   -- "hello" -> "HELLO"
string.lower(s)   -- "HELLO" -> "hello"

-- Substrings  (1-based, negative counts from end)
string.sub(s, i, j)   -- s:sub(2, 4)
string.sub(s, -3)     -- last 3 chars

-- Finding / matching
string.find(s, pattern, init, plain)   -- returns start, end (or nil)
string.match(s, pattern)               -- returns capture(s)
string.gmatch(s, pattern)              -- iterator over matches
string.gsub(s, pattern, repl, n)       -- replace, returns new string + count

-- Formatting
string.format("%d %s %.2f", 42, "hi", 3.14)

-- Byte / char conversion
string.byte(s, i, j)   -- char codes
string.char(65, 66)    -- "AB"

-- Repeat / reverse
string.rep(s, n, sep)  -- repeat n times with separator
string.reverse(s)      -- reverse bytes (ASCII only)

-- Length as method
s:len()   -- same as #s
```

## table library

```lua
-- Insert / remove
table.insert(t, v)        -- append to end
table.insert(t, pos, v)   -- insert at pos
table.remove(t)           -- remove last, return it
table.remove(t, pos)      -- remove at pos, return it

-- Sort (in-place)
table.sort(t)                       -- ascending, values must be comparable
table.sort(t, function(a,b) return a > b end)  -- custom comparator

-- Concatenate array of strings
table.concat(t, sep, i, j)   -- table.concat({"a","b","c"}, ", ")  -> "a, b, c"

-- Move elements
table.move(a1, f, e, t, a2)  -- copy a1[f..e] to a2 starting at t

-- Pack / unpack
local packed = table.pack(10, 20, 30)   -- {10,20,30, n=3}
local a, b, c = table.unpack(t, 1, 3)
```

## math library

```lua
math.abs(x)          math.ceil(x)       math.floor(x)
math.sqrt(x)         math.max(a,b,...)  math.min(a,b,...)
math.huge            math.pi            math.maxinteger   math.mininteger
math.type(x)         -- "integer" | "float" | fail
math.tointeger(x)    -- converts if possible, else nil
math.random()        -- [0,1)
math.random(n)       -- [1,n]
math.random(m,n)     -- [m,n]
math.randomseed(x)
math.fmod(x,y)       math.modf(x)   -- integer and fractional parts
```

## io library (file I/O)

```lua
-- Simple I/O
io.write("text")           -- no newline
io.read("*l")              -- read line  (alias: "l")
io.read("*n")              -- read number
io.read("*a")              -- read all

-- File handles
local f = io.open("file.txt", "r")   -- modes: r, w, a, rb, wb
if f then
    local content = f:read("*a")
    f:close()
end

-- Safe open with error handling
local f, err = io.open("x.txt", "w")
if not f then error(err) end
f:write("hello\n")
f:close()
```

## os library

```lua
os.time()                 -- Unix timestamp
os.date("%Y-%m-%d %H:%M:%S")
os.clock()                -- CPU time (seconds)
os.difftime(t2, t1)       -- difference in seconds
os.exit(code)             -- terminate (nilified in sandbox)
```

## pcall / error handling

```lua
-- Basic protected call
local ok, result = pcall(function()
    return risky_operation()
end)
if not ok then
    print("Error:", result)
end

-- With xpcall for traceback
local ok, err = xpcall(function()
    error("oops")
end, debug.traceback)

-- Raising errors
error("message")           -- level 1 (caller)
error("message", 2)        -- level 2 (caller's caller)
error({code=404, msg="not found"})  -- error objects
```

## Iterators

```lua
-- ipairs: array order, stops at first nil
for i, v in ipairs(t) do ... end

-- pairs: all keys (hash + array), unordered
for k, v in pairs(t) do ... end

-- Numeric for
for i = 1, 10, 2 do ... end   -- step 2

-- Generic iterator pattern
local function values(t)
    local i = 0
    return function()
        i = i + 1
        return t[i]
    end
end
```
