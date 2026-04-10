# LocalScript / Lua Console Builder

## Architecture
- LangGraph pipeline in `src/graph/` orchestrates all agents
- Existing tool files (`check_lua.py`, `run_lua.py`, `generate.py`, `auto_fix_lua.py`, `prompt_verifier.py`) are used as-is via async wrappers in `src/tools/lua_tools.py`
- `app.py` serves web UI and calls `PipelineEngine.process_message()` for each user turn
- `src/core/llm.py` — async LLM provider using `openai` library (LM Studio compatible)

## LangGraph Pipeline (src/graph/)
Flow: route_intent → [generate|refine|answer] → validate → [verify|fix] → respond
- `builder.py` — builds the StateGraph
- `nodes.py` — all node functions (route, generate, refine, validate, fix, verify, answer, respond)
- `conditions.py` — edge conditions (route_by_intent, check_validation, check_verification)
- `engine.py` — PipelineEngine wraps the graph with a simple `process_message()` API

## Key Quality Features
- **Smart response parsing** from `generate.py` (strip fences, preamble, zero-width chars, retry on non-Lua)
- **Preservation guard** in refine: extract function names before/after, force-restore silently dropped functions
- **Auto-fix loop** with classification (syntax/runtime/lint/format/requirements) via `auto_fix_lua.py`
- **Requirements verification** via `prompt_verifier.py`
- **Mojibake repair** for Windows console encoding issues

## Platform
- Windows, Python 3.12, LM Studio on localhost:1234
- Lua via `lua` / `luacheck` (system PATH)
