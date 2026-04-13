# PROGON.md

## Что разбирается

Ниже разобран один полный кейс для запроса:

> Преобразуй время из формата `YYYYMMDD` и `HHMMSS` в строку в формате ISO 8601 с использованием Lua.

С примером workflow-контекста, где вход лежит в:

- `wf.vars.json.IDOC.ZCDF_HEAD.DATUM`
- `wf.vars.json.IDOC.ZCDF_HEAD.TIME`

И ожидаемый результат сохраняется в:

- `wf.vars.json.IDOC.ZCDF_HEAD.ISO_DATETIME`

Разбор сделан по текущей архитектуре пайплайна и дополнен реальным похожим прогоном из логов:

- первичный запуск: `chat_id=58`, `turn_id=98b83fc18896`
- follow-up после уточнения: `chat_id=58`, `turn_id=a117b26a6be9`

Важно: в текущем коде один пользовательский запрос проходит через `PipelineEngine`, `LangGraph` и набор узлов/агентов. В зависимости от результата часть шагов может ветвиться: уточнение, генерация, исправление runtime-ошибки, исправление по verifier, сохранение, объяснение.

---

## 1. Точка входа в систему

### 1.1. Пользователь пишет сообщение

Сообщение попадает в chat-слой `app.py`, затем вызывается:

- `PipelineEngine.process_message(...)` из [src/graph/engine.py](/mnt/c/Users/Admin/Desktop/3ABCD/TrueHack/src/graph/engine.py:30)

На вход `process_message(...)` получает:

- `chat_id`
- `turn_id`
- `user_input`
- `current_code`
- `base_prompt`
- `change_requests`
- `workspace_root`
- `target_path`
- planner-related поля
- `active_clarifying_questions`

Для первого запуска по такому промту типичный initial state такой:

```python
{
  "chat_id": 58,
  "user_input": "<весь текст промта + JSON контекст>",
  "workspace_root": "...",
  "target_path": "",
  "target_directory": "...",
  "target_explicit": False,
  "intent": "",
  "base_prompt": "",
  "change_requests": [],
  "compiled_request": {},
  "current_code": "",
  "generated_code": "",
  "failure_stage": "",
  "diagnostics": {},
  "validation_passed": False,
  "fix_iterations": 0,
  "fix_verification_iterations": 0,
  "max_fix_iterations": 3,
  "verification": {},
  "verification_passed": False,
  "save_success": False,
  "save_skipped": False,
  "save_skip_reason": "",
  "save_error": "",
  "saved_to": "",
  "saved_jsonstring_to": "",
  "explanation": {},
  "suggested_changes": [],
  "clarifying_questions": [],
  "active_clarifying_questions": [],
  "response": "",
  "response_type": "text",
  "planner_result": {},
  "planner_skipped": False,
  "awaiting_planner_clarification": False,
  "planner_pending_questions": [],
  "planner_original_input": "",
  "planner_clarification_attempts": 0,
}
```

Этот state создается в [src/graph/engine.py](/mnt/c/Users/Admin/Desktop/3ABCD/TrueHack/src/graph/engine.py:44).

---

## 2. Граф пайплайна

Граф собирается в [src/graph/builder.py](/mnt/c/Users/Admin/Desktop/3ABCD/TrueHack/src/graph/builder.py:15).

Порядок узлов такой:

1. `START`
2. `resolve_target`
3. `route_intent`
4. `plan_request`
5. `prepare_generation_context`
6. `generate_code` или `refine_code`
7. `validate_code`
8. `fix_validation_code` при runtime/syntax fail
9. `verify_requirements`
10. `fix_verification_code` при semantic fail
11. `save_code`
12. `explain_solution`
13. `prepare_response`
14. `END`

Если это не кодовый запрос, вместо generation-ветки идет:

- `answer_question -> END`

---

## 3. Полный порядок вызовов для этого кейса

Ниже разбор стандартного create-прогона для такого промта.

### 3.1. `resolve_target`

Функция:

- `create_nodes(...).resolve_target` в [src/graph/nodes.py](/mnt/c/Users/Admin/Desktop/3ABCD/TrueHack/src/graph/nodes.py:1200)

Что делает:

1. Смотрит, просил ли пользователь сохранить код в файл.
2. Вычисляет `target_path`, `target_directory`, `workspace_root`.
3. Если путь явно указан и файл уже существует, может загрузить `current_code`.

Для этого промта:

- явного пути нет
- `target_path=""`
- `current_code` остается пустым

Что возвращает в state:

- `workspace_root`
- `target_path`
- `target_directory`
- `target_explicit`
- `current_code`

### 3.2. `route_intent`

Функция:

- [src/graph/nodes.py](/mnt/c/Users/Admin/Desktop/3ABCD/TrueHack/src/graph/nodes.py:1223)

Внутренние функции:

1. `_collect_intent_features(...)`
2. `_deterministic_intent_from_features(...)`
3. при необходимости LLM route через `llm.generate_json(...)`

Что получает:

- `user_input`
- `current_code`
- `base_prompt`
- `active_clarifying_questions`

Что делает:

1. Определяет, это новый запрос, вопрос, изменение существующего кода или retry.
2. Для данного кейса, когда кода еще нет, результат обычно:
   - `intent = "create"`

Что записывает в state:

- `intent`
- иногда `current_code`, если код был вставлен прямо в сообщение
- иногда `base_prompt`

### 3.3. `plan_request` (`TaskPlanner`)

Функция-фабрика:

- [src/agents/planner.py](/mnt/c/Users/Admin/Desktop/3ABCD/TrueHack/src/agents/planner.py:316)

Внутри вызывает:

- `PlannerAgent.plan(...)` [src/agents/planner.py](/mnt/c/Users/Admin/Desktop/3ABCD/TrueHack/src/agents/planner.py:261)
- `_normalize_planner_result(...)`
- `_build_clarification_response(...)`, если нужны уточнения

Что получает:

- `user_input`
- `intent`
- `current_code`
- `compiled_request`
- `active_clarifying_questions`
- planner follow-up поля

Что делает:

1. Собирает `effective_input`.
2. Формирует planner prompt `_PLANNER_USER`.
3. Вызывает LLM как агент `TaskPlanner`.
4. Нормализует JSON-ответ planner-а.

Ключевые поля результата planner-а:

- `reformulated_task`
- `identified_workflow_paths`
- `target_operation`
- `key_entities`
- `data_types`
- `expected_result_action`
- `followup_action`
- `needs_clarification`
- `clarification_questions`
- `confidence`

Что кладет в state:

- `planner_result`
- `planner_skipped`
- при необходимости:
  - `awaiting_planner_clarification`
  - `planner_pending_questions`
  - `planner_original_input`
  - `planner_clarification_attempts`
  - `response`
  - `response_type="text"`
  - `failure_stage="clarification"`
  - `clarifying_questions`

### Что происходит на этом кейсе

Для такого промта planner должен понять:

- операция: `convert`
- источник: `DATUM` + `TIME`
- целевой результат: строка ISO 8601
- действие: `save_to_wf_vars`

Если planner уверен, что все уже понятно, он пропускает дальше.

Если planner считает, что путь назначения недостаточно явно сформулирован, он может выдать уточняющие вопросы. В логах этого кейса именно так и произошло: после первого прогона пользователь дал follow-up-ответ с явным target:

- `wf.vars.json.IDOC.ZCDF_HEAD.ISO_DATETIME`

### 3.4. `prepare_generation_context`

Функция:

- [src/graph/nodes.py](/mnt/c/Users/Admin/Desktop/3ABCD/TrueHack/src/graph/nodes.py:1310)

Ключевые внутренние вызовы:

1. `split_task_and_context(...)`
2. `compile_lowcode_request(...)`
3. возможный recompile с `planner_result.reformulated_task`

Что получает:

- `user_input`
- `base_prompt`
- `intent`
- `planner_result`
- `current_code`

Что делает:

1. Делит сообщение на task text и raw JSON context.
2. Компилирует запрос в нормализованный `compiled_request`.
3. Проставляет:
   - `task_text`
   - `original_task`
   - `verification_prompt`
4. Если нужно уточнение, готовит текстовый ответ и завершает кодовую ветку.

Что записывает в state:

- `base_prompt`
- `compiled_request`
- `response`
- `response_type`
- `clarifying_questions`
- `failure_stage`

### Что такое `compiled_request`

Это главный структурированный объект, которым дальше питаются почти все агенты.

Для этого кейса в нем обычно оказываются:

- нормализованное описание задачи
- parseable workflow context
- выбранная операция `convert`
- primary path / related paths
- ожидание `datetime_to_iso`
- prompt для verifier

---

## 4. Генерация кода

### 4.1. `generate_code`

Функция:

- [src/graph/nodes.py](/mnt/c/Users/Admin/Desktop/3ABCD/TrueHack/src/graph/nodes.py:1404)

Ключевые внутренние функции:

1. `_build_generation_prompt(compiled_request)`
2. `_generation_temperature(compiled_request)`
3. `llm.generate(...)`
4. `validate_lowcode_llm_output(...)`
5. `is_truncated_lowcode_response(...)`
6. `_attempt_continuation(...)`
7. `smart_normalize(...)`

Что получает:

- `compiled_request`
- `base_prompt`
- `target_path`
- `target_directory`

Что делает:

1. Строит generation prompt.
2. Вызывает агента `CodeGenerator`.
3. Проверяет, что ответ имеет корректный формат `lua{...}lua`.
4. Если формат сломан, делает внутренний retry.
5. Кладет нормализованный код в `generated_code`.

Что записывает в state:

- `generated_code`
- `base_prompt`
- `compiled_request`
- `fix_iterations=0`
- служебные save/verification поля сбрасываются

### Что получает `CodeGenerator` на вход

Не raw user prompt, а уже подготовленный generation prompt, куда входят:

- `compiled_request["task_text"]`
- workflow anchor
- path types
- semantic expectations
- workflow context
- decision rules

### Что отдает `CodeGenerator`

- raw text, который потом нормализуется в Lua JsonString payload

---

## 5. Валидация кода

### 5.1. `validate_code`

Функция:

- [src/graph/nodes.py](/mnt/c/Users/Admin/Desktop/3ABCD/TrueHack/src/graph/nodes.py:1630)

Ключевые внутренние функции:

1. `_normalize_runtime_candidate(...)`
2. `_run_diagnostics_with_optional_context(...)`
3. при fail:
   - `_format_numbered_code_block(...)`
   - `_clean_run_error(...)`
   - `llm.generate(...)` у агента `CodeValidator`

Что получает:

- `generated_code`
- `compiled_request`

Что делает:

1. Нормализует код для запуска.
2. Запускает local runtime harness.
3. Если код падает:
   - сохраняет `run_error`
   - зовет `CodeValidator`, который формирует короткий fix hint

Что записывает в state:

- `generated_code`
- `validation_passed`
- `failure_stage`
- `diagnostics`

### Что находится в `diagnostics`

Обычно:

- `success`
- `failure_kind`
- `run_error`
- `run_output`
- `timed_out`
- `result_preview`
- `workflow_state`
- `llm_fix_hint`

---

## 6. Цикл исправления runtime-ошибок

Если `validation_passed=False`, граф уходит в:

- `fix_validation_code`

Условие задается в [src/graph/conditions.py](/mnt/c/Users/Admin/Desktop/3ABCD/TrueHack/src/graph/conditions.py:53).

### 6.1. `fix_validation_code`

Функция:

- [src/graph/nodes.py](/mnt/c/Users/Admin/Desktop/3ABCD/TrueHack/src/graph/nodes.py:1714)

Ключевые внутренние функции:

1. `_build_fix_validation_prompt(...)`
2. `llm.chat(...)` агента `ValidationFixer`
3. `validate_lowcode_llm_output(...)`
4. при необходимости `_attempt_continuation(...)`
5. дополнительный runtime check фикс-кандидата
6. повторный `CodeValidator`
7. еще один retry у `ValidationFixer`

Что получает:

- `generated_code`
- `diagnostics.run_error`
- `diagnostics.llm_fix_hint`
- `compiled_request`

Что делает:

1. Строит prompt на исправление сломанного кода.
2. Получает новый вариант Lua.
3. При необходимости делает дополнительную внутреннюю попытку.

Что записывает в state:

- `generated_code`
- `fix_iterations = fix_iterations + 1`
- `validation_passed=False`
- `verification_passed=False`

После этого граф возвращается в:

- `validate_code`

То есть образуется loop:

- `validate_code -> fix_validation_code -> validate_code`

---

## 7. Проверка требований

Если `validation_passed=True`, граф идет в:

- `verify_requirements`

### 7.1. `verify_requirements`

Функция:

- [src/graph/nodes.py](/mnt/c/Users/Admin/Desktop/3ABCD/TrueHack/src/graph/nodes.py:1947)

Ключевые внутренние функции:

1. `_build_verification_extra_context(...)`
2. `async_verify_requirements(...)` из `src/tools/lua_tools.py`
3. `_normalize_verification_result(...)`

Что получает:

- `generated_code`
- `compiled_request["verification_prompt"]`
- `diagnostics.run_output`
- `diagnostics.result_preview`
- `diagnostics.workflow_state`

Что делает:

1. Собирает verifier prompt.
2. Передает verifier-агенту:
   - user request
   - planner analysis
   - runtime result
   - updated workflow snapshot
   - Lua code under review
3. Нормализует результат verifier-а.
4. Делает aggregate verdict:
   - `verification_passed`
   - `summary`
   - `missing_requirements`
   - `warnings`
   - `checks`

Что записывает в state:

- `verification`
- `verification_passed`
- `failure_stage`
- обновленный `diagnostics`

### Структура `verification`

Типично:

```json
{
  "passed": true|false,
  "score": 0..100,
  "summary": "...",
  "missing_requirements": [],
  "warnings": [],
  "checks": {
    "workflow_path_usage": {"status": "...", "reason": "..."},
    "source_shape_understood": {"status": "...", "reason": "..."},
    "target_shape_satisfied": {"status": "...", "reason": "..."},
    "logic_correctness": {"status": "...", "reason": "..."},
    "helper_api_usage": {"status": "...", "reason": "..."},
    "edge_case_handling": {"status": "...", "reason": "..."}
  }
}
```

---

## 8. Цикл исправления по verifier

Если `verification_passed=False`, граф уходит в:

- `fix_verification_code`

Условие задается в [src/graph/conditions.py](/mnt/c/Users/Admin/Desktop/3ABCD/TrueHack/src/graph/conditions.py:75).

### 8.1. `fix_verification_code`

Функция:

- [src/graph/nodes.py](/mnt/c/Users/Admin/Desktop/3ABCD/TrueHack/src/graph/nodes.py:1847)

Ключевые внутренние функции:

1. `_build_fix_verification_prompt(...)`
2. `llm.chat(...)` агента `VerificationFixer`
3. `validate_lowcode_llm_output(...)`
4. retry при empty/unchanged code

Что получает:

- `generated_code`
- `verification.missing_requirements`
- `verification.checks`
- `diagnostics.result_preview`
- `diagnostics.workflow_state`
- `compiled_request`

Что делает:

1. Формирует prompt уже не про runtime-ошибку, а про несоответствие задаче.
2. Просит модель доисправить логику.
3. Возвращает новый код.

Что записывает в state:

- `generated_code`
- `fix_verification_iterations = fix_verification_iterations + 1`
- `validation_passed=True`
- `verification_passed=False`

После этого граф снова идет в:

- `verify_requirements`

То есть второй loop:

- `verify_requirements -> fix_verification_code -> verify_requirements`

---

## 9. Сохранение

### 9.1. `save_code`

Функция:

- [src/graph/nodes.py](/mnt/c/Users/Admin/Desktop/3ABCD/TrueHack/src/graph/nodes.py:2094)

Что получает:

- `generated_code`
- `target_path`
- `compiled_request`
- `validation_passed`
- `verification_passed`

Что делает:

1. Если код пустой, завершает с ошибкой.
2. Если validation fail, не сохраняет.
3. Если verification fail, не сохраняет.
4. Если `target_path` не задан, не сохраняет, но код оставляет в ответе.
5. Если путь задан и проверки пройдены:
   - вызывает `save_final_output(...)`
   - сохраняет `.lua` и sidecar jsonstring payload

Для этого кейса обычно:

- `target_path=""`
- значит:
  - `save_success=False`
  - `save_skipped=True`
  - `save_skip_reason="Путь не указан..."`

Что записывает в state:

- `current_code`
- `save_success`
- `save_skipped`
- `save_skip_reason`
- `save_error`
- `saved_to`
- `saved_jsonstring_to`

---

## 10. Объяснение результата

### 10.1. `explain_solution`

Функция:

- [src/graph/nodes.py](/mnt/c/Users/Admin/Desktop/3ABCD/TrueHack/src/graph/nodes.py:2212)

Ключевые внутренние вызовы:

1. `format_lowcode_jsonstring(...)`
2. `llm.generate_json(...)` агента `SolutionExplainer`
3. `_normalize_string_list(...)`

Что получает:

- `generated_code`
- `compiled_request`
- `diagnostics`
- `verification`

Что делает:

1. Строит human-readable explanation prompt.
2. Просит `SolutionExplainer` описать:
   - summary
   - what_is_in_code
   - how_it_works
   - suggested_changes
   - clarifying_questions

Что записывает в state:

- `explanation`
- `suggested_changes`
- `clarifying_questions`

---

## 11. Финальная сборка ответа

### 11.1. `prepare_response`

Функция:

- [src/graph/nodes.py](/mnt/c/Users/Admin/Desktop/3ABCD/TrueHack/src/graph/nodes.py:2332)

Что получает:

- почти весь state:
  - `generated_code`
  - `diagnostics`
  - `verification`
  - `save_success`
  - `save_skipped`
  - `save_error`
  - `explanation`
  - `suggested_changes`
  - `clarifying_questions`

Что делает:

1. Формирует user-visible текст ответа.
2. Добавляет:
   - статус генерации
   - причину, почему код не сохранен
   - verification summary
   - json payload с `lua{...}lua`
   - runtime output при наличии
   - explanation
   - suggested changes
   - clarifying questions

Что записывает в state:

- `response`
- `response_type="code"`
- `current_code = generated_code`

После этого граф завершается.

---

## 12. Что именно получает и отдает каждый агент

### `TaskPlanner`

Вход:

- сырое пользовательское сообщение
- metadata о наличии кода и active clarification questions

Выход:

- `planner_result`
- иногда `clarifying_questions`
- иногда переводит flow в clarification branch

### `CodeGenerator`

Вход:

- `compiled_request`
- workflow context
- path expectations

Выход:

- raw Lua script в формате `lua{...}lua`
- затем в state попадает `generated_code`

### `CodeValidator`

Вход:

- код с номерами строк
- runtime error

Выход:

- короткий текст с:
  - error type
  - failing line
  - exact fix

Этот текст попадает в:

- `diagnostics["llm_fix_hint"]`

### `ValidationFixer`

Вход:

- сломанный код
- runtime error
- fix hint от `CodeValidator`
- `compiled_request`

Выход:

- новый кандидат кода

### `RequirementsVerifier`

Вход:

- verification prompt
- runtime result
- updated workflow state
- Lua solution under review

Выход:

- JSON verdict `verification`

### `VerificationFixer`

Вход:

- текущий код
- `missing_requirements`
- failed checks
- runtime result / workflow state

Выход:

- исправленный код

### `SolutionExplainer`

Вход:

- финальный код
- verification summary
- runtime summary

Выход:

- explanation JSON

---

## 13. Что меняется в state по ходу прогона

Ниже ключевые мутации state в порядке прохождения.

### После `resolve_target`

- `target_path`
- `target_directory`
- `target_explicit`
- `current_code`

### После `route_intent`

- `intent`

### После `plan_request`

- `planner_result`
- `planner_skipped`
- возможно:
  - `awaiting_planner_clarification`
  - `planner_pending_questions`
  - `planner_original_input`
  - `planner_clarification_attempts`
  - `response`
  - `clarifying_questions`

### После `prepare_generation_context`

- `base_prompt`
- `compiled_request`
- `verification_prompt`
- возможно clarification response

### После `generate_code`

- `generated_code`
- reset verification/save/explanation related fields

### После `validate_code`

- `validation_passed`
- `diagnostics`
- `failure_stage`

### После `fix_validation_code`

- `generated_code`
- `fix_iterations`

### После `verify_requirements`

- `verification`
- `verification_passed`
- `diagnostics["verification_summary"]`
- `failure_stage`

### После `fix_verification_code`

- `generated_code`
- `fix_verification_iterations`

### После `save_code`

- `current_code`
- `save_success`
- `save_skipped`
- `save_skip_reason`
- `saved_to`
- `saved_jsonstring_to`

### После `explain_solution`

- `explanation`
- `suggested_changes`
- `clarifying_questions`

### После `prepare_response`

- `response`
- `response_type`
- `current_code`

---

## 14. Как это выглядело на реальном кейсе из логов

Для похожего кейса по этому промту в логах был такой сценарий.

### Первый запуск: `turn_id=98b83fc18896`

Порядок:

1. `pipeline_start`
2. generation-ветка
3. код сгенерирован
4. `validation_passed=true`
5. `verification_passed=false`
6. ответ показан пользователю, но без финального успешного согласования требований

Итог:

- пользователь увидел код
- система сохранила `base_prompt`
- follow-up пользователя был воспринят как продолжение того же кейса

### Второй запуск после уточнения: `turn_id=a117b26a6be9`

Порядок:

1. follow-up попадает в систему
2. planner/prepare собирают уже более точную задачу
3. generation/refine выдает более точный код
4. `validate_code` проходит
5. `RequirementsVerifier` подтверждает корректность
6. `save_code` пропускает сохранение, потому что `target_path` не задан
7. `SolutionExplainer` формирует объяснение
8. `prepare_response` отдает финальный ответ

В логах verifier-а итог был:

- `passed=true`
- `score=100`
- `ISO_DATETIME = "2023-10-15T15:30:00"`

---

## 15. Итоговая короткая схема

Для такого промта в create-ветке реальный порядок вызовов выглядит так:

1. `PipelineEngine.process_message`
2. `resolve_target`
3. `route_intent`
4. `plan_request`
5. `prepare_generation_context`
6. `generate_code`
7. `validate_code`
8. если runtime fail:
   `CodeValidator -> fix_validation_code -> validate_code`
9. `verify_requirements`
10. если semantic fail:
   `fix_verification_code -> verify_requirements`
11. `save_code`
12. `explain_solution`
13. `prepare_response`

Если planner не уверен в постановке задачи, между шагами `4` и `5` появляется ветка:

1. `plan_request`
2. `prepare_response`
3. пользователь отвечает
4. новый `turn`
5. `plan_request` уже видит исходную задачу + ответы пользователя
6. затем flow продолжается в generation branch

---

## 16. Практический вывод для этого конкретного запроса

Для запроса про `DATUM` + `TIME` пайплайн в норме должен прийти к такому коду по смыслу:

1. прочитать `wf.vars.json.IDOC.ZCDF_HEAD`
2. взять `DATUM` и `TIME`
3. собрать строку `YYYY-MM-DDTHH:MM:SS`
4. записать ее в `wf.vars.json.IDOC.ZCDF_HEAD.ISO_DATETIME`
5. вернуть обновленный объект или нужное значение, в зависимости от сформированного `compiled_request`

Если путь сохранения явно не определен в первичной формулировке, planner или compiler могут сначала перевести кейс в clarification flow. После уточнения система повторно запускает тот же pipeline, но уже с более точным `planner_result` и `compiled_request`.

---

## 17. Фактический live-run на текущем проекте

Ниже зафиксирован реальный запуск программы, который был выполнен уже после составления этого документа.

### 17.1. Как запускалось

Запуск был сделан не через теоретический разбор, а через реальный `AppRuntime.handle_message(...)` с новым чатом.

Использовалась текущая `.env` конфигурация:

- `OLLAMA_MODEL=qwen3.5:9b`
- `PLANNER_ENABLED=true`

### 17.2. Фактический вход

В `chat_id=67` в систему был подан этот prompt:

```text
Преобразуй время из формата 'YYYYMMDD' и 'HHMMSS' в строку в формате ISO 8601 с
использованием Lua. Пример в
{
 "wf": {
 "vars": {
 "json": {
 "IDOC": {
 "ZCDF_HEAD": {
 "DELIVERY": "123456789",
 "SUBJECT": "Order Confirmation",
 "DATUM": "20231015",
 "TIME": "153000",
 "STATUS": "Confirmed",
 "ROUTE": "Route 66",
 "ROUTE_TXT": "Main Distribution Route",
 "ZCDF_PACKAGES": [
 {
 "id": "PKG001",
 "number": "EXIDV001",
 "weight": "10",
 "volume": "50",
 "length": "100",
 "width": "50",
 "height": "30",
 "items": [
 {
 "sku": "SKU001",
 "externalId": "MAT001",
 "quantity": "5",
 "weight": "2"
 },
{
 "sku": "SKU002",
 "externalId": "MAT002",
 "quantity": "3",
 "weight": "1"
 }
 ]
 },
 {
 "id": "PKG002",
 "number": "EXIDV002",
 "weight": "20",
 "volume": "60",
 "length": "120",
 "width": "60",
 "height": "40",
 "items": [
 {
 "sku": "SKU003",
 "externalId": "MAT003",
 "quantity": "10",
 "weight": "2"
 }
 ]
 }
 ]
 }
 }
 }
 }
 }
}
```

### 17.3. Фактический порядок шагов

По `runtime.jsonl` и live stdout прогон выглядел так:

1. `chat_user_message_received`
2. `chat_pipeline_dispatch`
3. `pipeline_start`
4. `resolve_target`
5. `route_intent`
6. `plan_request` / агент `TaskPlanner`
7. `pipeline_failed`
8. `chat_pipeline_failed`
9. `chat_assistant_message_saved`

### 17.4. Что реально успело выполниться

#### `resolve_target`

Вход:

- user prompt
- пустой `target_path`

Выход:

- `target_path=""`
- `target_explicit=false`
- `current_code=""`

#### `route_intent`

Вход:

- user prompt
- `current_code=""`

Выход:

- `intent="create"`

Фактический лог:

```text
[IntentRouter] completed confidence=0.0 deterministic_reason=no_code_available intent=create
```

#### `plan_request` / `TaskPlanner`

Вход:

- весь prompt целиком
- metadata:
  - `has_context=false`
  - `has_code=false`
  - `workflow_paths=none`

Фактический LLM-вызов был записан в `logs/llm_prompts.jsonl`:

- `chat_id=67`
- `turn_id=7382ee28bc49`
- `agent_name="TaskPlanner"`
- `call_kind="generate_json"`

### 17.5. На чем прогон остановился

Реальный прогон упал прямо на `TaskPlanner`, до генерации кода он не дошел.

Фактическая ошибка:

```text
openai.APIConnectionError: Connection error.
```

Итоговые runtime-события:

```text
{"event": "pipeline_failed", "chat_id": 67, "turn_id": "7382ee28bc49"}
{"event": "chat_pipeline_failed", "chat_id": 67, "turn_id": "7382ee28bc49", "error": "Connection error.", "error_type": "APIConnectionError"}
```

### 17.6. Фактический выход пользователю

Пользовательский ответ системы в этом live-run был таким:

```text
Ошибка: Connection error.
```

### 17.7. Что осталось в state после этого запуска

Итоговый state chat payload:

```json
{
  "chat_id": 67,
  "has_project": false,
  "base_prompt": "",
  "change_requests": [],
  "current_code": "",
  "target_path": "",
  "workspace_root": "/mnt/c/Users/Admin/Desktop/3ABCD/TrueHack",
  "last_intent": "",
  "last_saved_path": "",
  "last_saved_jsonstring_path": "",
  "change_requests_count": 0,
  "suggested_changes": [],
  "clarifying_questions": []
}
```

### 17.8. Вывод по live-run

На текущем окружении программа действительно была запущена, но полный прогон именно этого промта не завершился не из-за логики графа, а из-за внешней недоступности LLM backend.

То есть в этом фактическом запуске:

- вход в систему произошел корректно
- `resolve_target` и `route_intent` отработали корректно
- `TaskPlanner` был реально вызван
- дальнейшие агенты (`CodeGenerator`, `CodeValidator`, `RequirementsVerifier`, `SolutionExplainer`) вообще не стартовали
- финальный выход был аварийным: `Ошибка: Connection error.`
