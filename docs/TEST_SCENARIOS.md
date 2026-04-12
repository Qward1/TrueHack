# Бизнес-сценарии и проверки

# Test scenarios

## 1. New Lua without explicit path
### Prompt
`Верни последний email из wf.vars.emails`

### Expected pipeline
`resolve_target -> route_intent(create) -> generate -> validate -> verify -> save -> explain_solution -> respond`

### Pass criteria
- новый file target не создается
- код не сохраняется на диск
- pipeline не возвращает save-error только из-за отсутствия path
- в ответе есть код в формате `lua{ ... }lua`, explanation и предложения улучшений
- код следует LowCode contract: direct access, `wf.vars`, `wf.initVariables`

## 2. New Lua in explicit directory
### Prompt
`Преобразуй DATUM и TIME из wf.vars.json.IDOC.ZCDF_HEAD в ISO дату в папке C:\Work\LuaProjects`

### Expected path behavior
- создается `C:\Work\LuaProjects\<slug>\<slug>.lua`

### Pass criteria
- директория создана
- файл создан и сохранен только после validate + verify
- sidecar JSON-объект сохранен рядом с `.lua`, и его value содержит `lua{...}lua`

## 3. Create/update explicit Lua file by path
### Prompt
`Сделай скрипт заметок в C:\Work\LuaProjects\notes_app.lua`

### Pass criteria
- используется именно указанный `.lua` path
- JsonString sidecar создается рядом с этим файлом как JSON-объект
- follow-up turn сохраняет изменения в тот же файл

## 3a. Invalid Windows path segments are sanitized
### Prompt
`Создай проект в C:\Work\LuaProjects\MM\-HH:MM`

### Pass criteria
- runtime не падает на `save_code`
- невалидный сегмент пути санитизируется до filesystem-safe имени
- итоговый Lua target создается в нормализованной директории

## 4. Refine active target in same chat
### Prompt sequence
1. `Преобразуй wf.initVariables.recallTime в epoch в C:\Work\LuaProjects\recall_time.lua`
2. `Добавь безопасную обработку nil и невалидного формата даты`

### Expected behavior
- turn 2 переиспользует active target
- проходит полный цикл validate/verify/save
- ответ по-прежнему показывает результат в формате `lua{ ... }lua`

## 5. Validation failure -> fix loop
### Prompt
`Верни wf.vars.total + 1, но в коде есть ошибка обращения к полю`

### Pass criteria
- при провале runtime-валидации через `lua` запускается минимум одна итерация `fix_code`
- после исчерпания лимита итераций файл не сохраняется

## 6. Verification failure -> fix loop
### Prompt
`Преобразуй DATUM/TIME из wf.vars.json.IDOC.ZCDF_HEAD в ISO дату и сохрани в wf.vars.iso_date`

### Pass criteria
- если verification вернул недостающие требования, pipeline уходит в fix-loop
- после фикса снова идут validate -> verify -> save

## 6a. LowCode init variables are mocked in validation
### Prompt
`Преобразуй wf.initVariables.recallTime в Unix timestamp и сохрани в wf.vars.unixTime`

### Pass criteria
- локальная validation не падает только из-за отсутствия `wf.initVariables`
- harness автоматически подставляет test value для `recallTime`
- если логика преобразования сломана, runtime по-прежнему возвращает реальную Lua-ошибку

## 6b. Nested workflow paths are mocked in validation
### Prompt
`Преобразуй DATUM/TIME из wf.initVariables.json.IDOC.ZCDF_HEAD в ISO строку`

### Pass criteria
- harness строит nested mock path `wf.initVariables.json.IDOC.ZCDF_HEAD`
- alias access к промежуточной таблице не ломает validation сам по себе
- если логика внутри преобразования ошибочна, runtime возвращает реальную Lua-ошибку, а не nil path failure

## 7. E2E temporarily disabled
### Prompt
`Верни wf.vars.customerName`

### Pass criteria
- pipeline не вызывает e2e suite generation/execution
- сохранение не зависит от e2e gate
- `/status` показывает, что e2e временно отключен

## 7a. Console input is rejected by active contract
### Prompt
`Сделай Lua-скрипт, который читает строку через io.read()`

### Pass criteria
- validation помечает это как нарушение workflow/LUS contract
- fix-loop получает diagnostics, что нужно убрать console input API

## 8. Explanation and follow-up suggestions
### Prompt sequence
1. `Нормализуй wf.vars.json.IDOC.ZCDF_HEAD.ZCDF_PACKAGES так, чтобы obj.items всегда был массивом, в C:\Work\LuaProjects\normalize_packages.lua`
2. `Примени предложение 1`

### Pass criteria
- в turn 1 ответ содержит:
  - explanation summary;
  - список suggested changes;
  - clarifying questions
- в turn 2 система распознает ссылку на предложение и применяет правку в refine-cycle

## 9. Question/inspect without file rewrite
### Prompt sequence
1. `Сделай скрипт, который возвращает wf.vars.emails[#wf.vars.emails], в C:\Work\LuaProjects\last_email.lua`
2. `Объясни, как работает этот код`

### Pass criteria
- turn 2 идет по answer path
- файл не перезаписывается

## 10. Command checks
### Commands
- `/path`
- `/status`
- `/code`
- `/prompt`
- `/retry`

### Pass criteria
- `/status` показывает, что e2e временно отключен, и количество предложений/вопросов
- `/status` показывает последний `.lua` path и последний JsonString path
- `/prompt` показывает базовый prompt, правки и последние предложения
- `/retry` повторно запускает validate/verify/save цикл без e2e gate
- `luacheck` не требуется для прохождения сценариев canonical runtime
- список чатов показывает очищенные title без шумного полного пути из prompt

## 11. Pasted workflow context -> direct workflow path usage
### Prompt
`Return the last email from the provided workflow context.` plus pasted JSON-like workflow data with `wf.vars.emails`.

### Pass criteria
- generation returns a focused workflow script, not a demo application;
- code reads directly from `wf.vars.emails`;
- code returns the computed value directly;
- save succeeds without requiring a manual follow-up correction.

## 12. App-style output is rejected before save
### Simulated bad generation
```lua
local emails = {"user1@example.com", "user2@example.com", "user3@example.com"}
return emails[#emails]
```

### Pass criteria
- semantic verification reports that the solution ignores the provided workflow structure;
- pipeline enters `fix_code` instead of saving immediately;
- the corrected result switches to direct `wf.vars` usage before save.

## 13. Remove-key task rejects plain direct return
### Prompt
`Для полученных данных из предыдущего REST запроса очисти значения переменных ID, ENTITY_ID, CALL.` plus pasted workflow context with `wf.vars.RESTbody.result`.

### Bad output example
```lua
return wf.vars.RESTbody.result
```

### Pass criteria
- semantic verification does not accept the code just because the path is correct;
- pipeline enters `fix_code` instead of saving immediately;
- corrected code explicitly transforms object items before return and references the requested keys.

## 14. Explicit workflow path mismatch blocks save
### Prompt
`Use wf.initVariables.recallTime and return the converted value.`

### Bad output example
```lua
local recallTime = "2026-04-10T12:00:00"
return recallTime
```

### Pass criteria
- semantic verification flags that `wf.initVariables.recallTime` must be used directly;
- save does not happen until the script uses the expected workflow path directly.

## 15. Simple extraction still works in model-driven generation
### Prompt
`Посчитай количество товаров в корзине.` plus pasted workflow context with `wf.vars.cart.items`.

### Pass criteria
- compiler resolves `wf.vars.cart.items` as the primary workflow path;
- model generation returns a valid workflow script for array count;
- validate -> verify -> save still run normally after generation.

## 16. Multi-step workflow task is allowed to stay multi-line
### Prompt
`Приведи wf.vars.contacts к массиву. Если там уже массив — верни как есть, иначе оберни значение в массив.` plus pasted workflow context with `wf.vars.contacts`.

### Pass criteria
- generation is allowed to produce a multi-line workflow script with conditions/loops/helpers;
- prompt steering does not collapse the task into a one-line `return`;
- validate -> verify -> save accepts the longer script when it follows the workflow contract.

## 16a. Array normalization rejects table-only shortcut logic
### Prompt
`Если поле contacts не массив, оберни его в массив.` plus pasted workflow context where `wf.vars.contacts` is an object table.

### Bad output example
```lua
if type(wf.vars.contacts) ~= 'table' then
    wf.vars.contacts = _utils.array.new({wf.vars.contacts})
end
_utils.array.markAsArray(wf.vars.contacts)
return wf.vars.contacts
```

### Pass criteria
- semantic verification does not accept code that relies only on `type(x) == "table"` for array-vs-object semantics;
- array semantics are explicit: only numeric keys `1..n` without gaps count as an array; an empty table counts as an array;
- `next(x)` / empty-vs-non-empty checks are not accepted as a substitute for object-vs-array shape detection;
- simply marking the original workflow object/scalar as an array via `_utils.array.markAsArray(source)` is rejected;
- `_utils.array.new(...)` with inline arguments is rejected;
- pipeline enters `fix_code` instead of saving immediately;
- corrected code distinguishes object-like vs array-like tables and creates new arrays with `_utils.array.new()` + `_utils.array.markAsArray(arr)`.

## 17. Ambiguous path selection asks before generation
### Prompt
`Посчитай количество товаров.` plus pasted workflow context containing both `wf.vars.cart.items` and `wf.vars.wishlist.items`.

### Pass criteria
- pipeline returns a clarification response instead of code;
- save is not attempted;
- clarification text contains both candidate workflow paths;
- after the user answers with an explicit workflow path, the pipeline reuses the original base prompt/context and continues from the same chat.

## 18. Change intent without existing code routes to generate
### Prompt
`Улучши обработку email и верни последний email из workflow context.` plus pasted workflow context with `wf.vars.emails`, but no current code in chat/file.

### Pass criteria
- first-step intent is normalized to `create`, even if the raw LLM classifier suggests `change`;
- pipeline goes directly through generate -> validate -> verify -> save;
- no refine fallback warning is emitted for the normal no-code path.

## 18a. Pasted Lua enables `change` without prior chat code
### Prompt
`Исправь этот код ...` plus a pasted `lua{...}lua` block in the same user message, with empty `current_code` in chat state.

### Pass criteria
- first-step intent resolves to `change`;
- pasted Lua becomes the refine context even though chat state was empty before the turn;
- pipeline enters `refine_code`, not fresh `generate_code`.

## Wrapper noise is repaired before validation
### Prompt
Any workflow task where the model returns a fenced malformed wrapper like ```` ```lua{...}lua ``` ```` instead of a clean `lua{...}lua` payload.

### Pass criteria
- normalizer extracts standalone Lua from the malformed fenced/wrapper response before validation;
- validation runs on the repaired Lua body instead of raw wrapper noise;
- if the repaired Lua is still invalid, the turn remains diagnostic and still shows the current code payload.

## Runtime-result-based logic verification blocks wrong filtering
### Prompt
`Отфильтруй элементы из массива, чтобы включить только те, у которых есть значения в полях Discount или Markdown.` plus workflow context where one row has empty strings in both fields.

### Pass criteria
- validation harness executes the code on the provided workflow context and captures the returned result;
- semantic verification receives the runtime-result preview and can fail `logic_correctness` / `edge_case_handling` when the wrong row remains in the output;
- save does not happen on this false-positive filter result;
- the user receives diagnostics together with the current code payload.

## 18b. Pure Lua question remains a question
### Prompt
`Как в Lua работает цикл for?`

### Pass criteria
- intent resolves to `question`, not `create`;
- pipeline returns answer text and does not enter code generation.

## 19. Bare field name resolves to a unique workflow path
### Prompt
`Конвертируй время в переменной recallTime в unix-формат.` plus pasted workflow context with `wf.initVariables.recallTime`.

### Bad output example
```lua
return wf.vars.RESTbody.result
```

### Pass criteria
- compiler infers `wf.initVariables.recallTime` as the expected workflow path even though the prompt does not contain the full path;
- semantic verification rejects code that uses a different workflow path;
- pipeline enters `fix_code` instead of saving the wrong script.

## 20. Runtime bad-argument diagnostics produce generic repair hints
### Prompt
Any workflow task where generated code triggers a Lua runtime error like `bad argument #1 to 'time' (table expected, got string)`.

### Pass criteria
- validation returns failure with `failure_kind=runtime`;
- diagnostics contain normalized fix hints about expected argument type and required conversion/validation;
- `fix_code` prompt includes these hints together with raw runtime error;
- corrected code passes validation and save gate without hardcoding to one specific stdlib function.

## 21. Structured JSON envelope with embedded Lua is normalized before validation
### Simulated model output
```text
lua{
json
{
  "lua": "return wf.vars.orders[1].id"
}
}lua
```

### Pass criteria
- normalizer extracts the embedded Lua body before validation;
- runtime does not try to execute the surrounding JSON metadata as Lua;
- validate -> verify -> save continue on the extracted script body.

## 21a. Fenced JSON envelope with embedded Lua is normalized before validation
### Simulated model output
```text
```json
{"contacts": "lua{\r\n\n  local contacts = wf.vars.contacts\n  return contacts\n\r\n}lua"}
```
```

### Pass criteria
- normalizer extracts the embedded Lua body from the fenced JSON payload;
- runtime does not execute literal `\n` / `\r\n` escape text as Lua;
- validation receives plain Lua source, not the JSON wrapper.

## 21b. Fix loop retries when the first repair repeats the same failure pattern
### Prompt
Shape-sensitive workflow task where the first fix response repeats the same `next(...)` / source-marking / table-only shortcut logic or otherwise stays materially unchanged.

### Pass criteria
- `fix_code` does not accept the first repair attempt blindly;
- if the first repair still repeats the same semantic requirement failures, `fix_code` performs one stricter internal retry;
- the second attempt is the one returned to validation.

## 21d. Prompt format contract stays single and strict
### Prompt
Любой workflow generation/fix сценарий с parseable context.

### Pass criteria
- generation/fix prompts do not mix `JsonString`, JSON-ban, markdown-ban and other overlapping format rules;
- the only response-format requirement is: start with `lua{` and end with `}lua`, without surrounding quotes and without code fences;
- generation/fix prompt payload does not include ranked candidates, confidence, verifier summary, or past broken assistant outputs.

## 21c. Hallucinated verifier pass is overruled by a contradiction-focused second pass
### Prompt
Any workflow task where the first semantic verifier pass incorrectly returns `passed=true`, but the captured runtime result contradicts the user request.

### Pass criteria
- final verification result remains failed;
- summary does not present the optimistic verifier text as the authoritative verdict;
- save is blocked and fix-loop continues or the response fails closed;
- verifier can overturn a false positive by citing the contradictory runtime result directly.

## 22. User-facing JsonString export is wrapped into a named JSON field
### Prompt
Любой успешный workflow generation/save сценарий.

### Pass criteria
- response code block shows a JSON object, not only bare `lua{...}lua`;
- sidecar `*.jsonstring.txt` stores the same JSON object shape;
- the JSON field name is derived from the selected workflow/save path or target name;
- plain Lua validation still runs against the extracted code body.
