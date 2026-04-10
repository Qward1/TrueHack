# Бизнес-сценарии и проверки

# Test scenarios

## 1. New Lua without explicit path
### Prompt
`Верни последний email из wf.vars.emails`

### Expected pipeline
`resolve_target -> route_intent(create) -> generate -> validate -> verify -> save -> explain_solution -> respond`

### Pass criteria
- fallback target создан в workspace
- код сохранен на диск как `.lua`
- рядом создан JsonString sidecar `*.jsonstring.txt`
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
- sidecar JsonString сохранен рядом с `.lua`

## 3. Create/update explicit Lua file by path
### Prompt
`Сделай скрипт заметок в C:\Work\LuaProjects\notes_app.lua`

### Pass criteria
- используется именно указанный `.lua` path
- JsonString sidecar создается рядом с этим файлом
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
