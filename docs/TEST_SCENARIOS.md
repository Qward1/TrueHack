# Бизнес-сценарии и проверки

# Test scenarios

## 1. New Lua without explicit path
### Prompt
`Создай Lua-скрипт калькулятора с консольным вводом`

### Expected pipeline
`resolve_target -> route_intent(create) -> generate -> validate -> verify -> save -> explain_solution -> respond`

### Pass criteria
- fallback target создан в workspace
- код сохранен на диск
- в ответе есть код, explanation и предложения улучшений

## 2. New Lua in explicit directory
### Prompt
`Создай текстовую игру в папке C:\Work\LuaProjects`

### Expected path behavior
- создается `C:\Work\LuaProjects\<slug>\<slug>.lua`

### Pass criteria
- директория создана
- файл создан и сохранен только после validate + verify

## 3. Create/update explicit Lua file by path
### Prompt
`Сделай скрипт заметок в C:\Work\LuaProjects\notes_app.lua`

### Pass criteria
- используется именно указанный `.lua` path
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
1. `Создай калькулятор в C:\Work\LuaProjects\calc.lua`
2. `Добавь историю последних операций`

### Expected behavior
- turn 2 переиспользует active target
- проходит полный цикл validate/verify/save

## 5. Validation failure -> fix loop
### Prompt
`Создай интерактивный Lua-скрипт с обработкой ввода`

### Pass criteria
- при провале runtime-валидации через `lua` запускается минимум одна итерация `fix_code`
- после исчерпания лимита итераций файл не сохраняется

## 6. Verification failure -> fix loop
### Prompt
`Сделай калькулятор с историей и экспортом истории в файл`

### Pass criteria
- если verification вернул недостающие требования, pipeline уходит в fix-loop
- после фикса снова идут validate -> verify -> save

## 7. E2E temporarily disabled
### Prompt
`Сделай CLI-скрипт, который принимает имя и печатает приветствие`

### Pass criteria
- pipeline не вызывает e2e suite generation/execution
- сохранение не зависит от e2e gate
- `/status` показывает, что e2e временно отключен

## 8. Explanation and follow-up suggestions
### Prompt sequence
1. `Создай todo-менеджер в C:\Work\LuaProjects\todo.lua`
2. `Примени предложение 1`

### Pass criteria
- в turn 1 ответ содержит:
  - explanation summary;
  - список suggested changes;
  - clarifying questions
- в turn 2 система распознает ссылку на предложение и применяет правку в refine-cycle

## 9. Question/inspect without file rewrite
### Prompt sequence
1. `Создай игру в C:\Work\LuaProjects\game.lua`
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
- `/prompt` показывает базовый prompt, правки и последние предложения
- `/retry` повторно запускает validate/verify/save цикл без e2e gate
- `luacheck` не требуется для прохождения сценариев canonical runtime
- список чатов показывает очищенные title без шумного полного пути из prompt
