# GENERATION

## 1. Общая схема генерации

### Entry point

Полный цикл запускается через `PipelineEngine.process_message(...)` в [src/graph/engine.py](/mnt/c/Users/Admin/Desktop/3ABCD/TrueHack/src/graph/engine.py).  
Он:

1. Собирает `initial_state` типа `PipelineState`.
2. Подставляет в state текущее сообщение, текущий код, target path, историю правок, planner-state.
3. Передает state в собранный `LangGraph` из [src/graph/builder.py](/mnt/c/Users/Admin/Desktop/3ABCD/TrueHack/src/graph/builder.py).

### Реальный порядок узлов

Основная схема такая:

`START -> resolve_target -> route_intent -> plan_request -> prepare_generation_context -> generate_code/refine_code -> validate_code -> verify_requirements -> save_code -> explain_solution -> prepare_response -> END`

Но она ветвится по условиям:

1. `START -> route_from_start`
   Если `awaiting_planner_clarification=True`, пайплайн пропускает `resolve_target` и `route_intent` и сразу идет в `plan_request` как follow-up на уточнение.

2. `resolve_target`
   Определяет, куда сохранять Lua-файл, и подгружает существующий код из target, если файл уже есть.

3. `route_intent`
   Определяет интент:
   - `create`
   - `change`
   - `retry`
   - `inspect`
   - `question`
   - `general`

4. После `route_intent`
   - `create/change/retry` идут в `plan_request`
   - `inspect/question/general` идут в `answer_question`

5. `plan_request`
   Работает только если planner включен через `PLANNER_ENABLED`.
   - если planner считает запрос неоднозначным, выставляет `awaiting_planner_clarification=True` и пайплайн идет в `prepare_response`
   - если planner не требует уточнения, пайплайн идет дальше в `prepare_generation_context`
   - после 2 неудачных раундов уточнений planner принудительно пропускает задачу дальше

6. `prepare_generation_context`
   Строит `compiled_request`.
   - если `compiled_request["needs_clarification"]=True`, пайплайн останавливает генерацию и идет в `prepare_response`
   - если интент `change/retry` и есть `current_code`, идет в `refine_code`
   - иначе идет в `generate_code`

7. `generate_code` или `refine_code`
   Генерируют новый код или полную обновленную версию старого кода.

8. `validate_code`
   Запускает локальную Lua-проверку через временный harness.
   - если validation passed, идет в `verify_requirements`
   - если validation failed и `fix_iterations < max_fix_iterations`, идет в `fix_validation_code`
   - если лимит фиксов исчерпан, все равно идет в `verify_requirements`, но `validation_passed` остается `False`

9. `fix_validation_code`
   Пытается исправить runtime/syntax ошибки и возвращает код обратно в `validate_code`.

10. `verify_requirements`
   Делает LLM-проверку соответствия задачи.
   - если verification passed, идет в `save_code`
   - если verification failed и `fix_verification_iterations < max_fix_iterations`, идет в `fix_verification_code`
   - если verification error или лимит фиксов исчерпан, все равно идет в `save_code`, но `verification_passed` остается `False`

11. `fix_verification_code`
   Исправляет логические/требовательные расхождения и возвращает код обратно в `verify_requirements`.

12. `save_code`
   Сохраняет файл только если одновременно:
   - есть код
   - `validation_passed=True`
   - `verification_passed=True`
   - есть `target_path`

13. `explain_solution`
   Генерирует пользовательское объяснение, список улучшений и вопросы.

14. `prepare_response`
   Собирает финальный текст ответа: статус, JSON payload с `lua{...}lua`, runtime/verification summary, путь сохранения, explanation.

### Когда реально кто работает

#### Ветка генерации/доработки

- `create` без существующего кода -> `generate_code`
- `change` со существующим кодом -> `refine_code`
- `retry` со существующим кодом -> `refine_code`
- `change/retry` без кода -> деградирует в `generate_code`

#### Ветка без генерации

- `inspect`
- `question`
- `general`

Для них вызывается только `answer_question`, без validation/save.

#### Ветка уточнения

Уточнение может потребовать:

- `TaskPlanner` в `plan_request`
- `compile_lowcode_request` в `prepare_generation_context`

В обоих случаях генерация не стартует, пока не будет достаточно контекста.

## 2. Входы и выходы по этапам

### Общий state

Через весь граф ходит `PipelineState` из [src/core/state.py](/mnt/c/Users/Admin/Desktop/3ABCD/TrueHack/src/core/state.py).  
Ключевые поля:

- вход пользователя: `user_input`
- файл/путь: `workspace_root`, `target_path`, `target_directory`, `target_explicit`
- контекст задачи: `intent`, `base_prompt`, `change_requests`, `compiled_request`
- код: `current_code`, `generated_code`
- циклы фикса: `fix_iterations`, `fix_verification_iterations`, `max_fix_iterations`
- локальная диагностика: `diagnostics`, `validation_passed`
- проверка требований: `verification`, `verification_passed`
- сохранение: `save_success`, `save_skipped`, `save_error`, `saved_to`, `saved_jsonstring_to`
- объяснение: `explanation`, `suggested_changes`, `clarifying_questions`
- planner: `planner_result`, `awaiting_planner_clarification`, `planner_pending_questions`

### `resolve_target`

Вход:

- `user_input`
- `workspace_root`
- предыдущий `target_path`
- текущий `current_code`

Выход:

- `workspace_root`
- `target_path`
- `target_directory`
- `target_explicit`
- `current_code` из файла, если target существует

### `route_intent`

Вход:

- `user_input`
- `current_code`
- `base_prompt`

Выход:

- `intent`
- иногда `current_code`, если пользователь вставил Lua-код прямо в сообщение
- иногда `base_prompt`, если код пришел из сообщения

### `plan_request`

Вход:

- `user_input`
- `intent`
- `current_code`
- planner flags: `awaiting_planner_clarification`, `planner_pending_questions`, `planner_original_input`, `planner_clarification_attempts`

Выход:

- `planner_result`
- `planner_skipped`
- либо:
  - `awaiting_planner_clarification=True`
  - `planner_pending_questions`
  - `response` с вопросами
- либо:
  - очищенный planner-state
  - возможно переписанный `user_input` в merged-форму для follow-up

### `prepare_generation_context`

Вход:

- `user_input`
- `base_prompt`
- `current_code`
- `intent`
- `planner_result`

Выход:

- `base_prompt`
- `compiled_request`
- либо response с уточняющим вопросом

### `generate_code`

Вход:

- `compiled_request`
- `target_path`
- `target_directory`

Выход:

- `generated_code`
- сброшенные counters/verification/save/explanation поля

### `refine_code`

Вход:

- `current_code`
- `compiled_request`
- `user_input`

Выход:

- `generated_code`
- обновленный `change_requests`

### `validate_code`

Вход:

- `generated_code`
- `compiled_request`

Выход:

- нормализованный `generated_code`
- `validation_passed`
- `diagnostics`
- `failure_stage="validation"` при ошибке

### `fix_validation_code`

Вход:

- `generated_code`
- `diagnostics.run_error`
- `diagnostics.llm_fix_hint`
- `compiled_request`
- `fix_iterations`

Выход:

- новый `generated_code`
- `fix_iterations + 1`

### `verify_requirements`

Вход:

- `generated_code`
- `compiled_request["verification_prompt"]`
- `diagnostics.run_output`
- `diagnostics.result_value/result_preview`
- `diagnostics.workflow_state`

Выход:

- `verification`
- `verification_passed`
- обновленный `diagnostics`
- `failure_stage="requirements"` при провале

### `fix_verification_code`

Вход:

- `generated_code`
- `verification.missing_requirements`
- `verification.checks`
- runtime result / workflow snapshot
- `compiled_request`

Выход:

- новый `generated_code`
- `fix_verification_iterations + 1`

### `save_code`

Вход:

- `generated_code`
- `target_path`
- `compiled_request`
- `validation_passed`
- `verification_passed`

Выход:

- `current_code`
- `save_success/save_skipped/save_error`
- `saved_to`
- `saved_jsonstring_to`

### `explain_solution`

Вход:

- `generated_code`
- `compiled_request`
- `diagnostics`
- `verification`

Выход:

- `explanation`
- `suggested_changes`
- `clarifying_questions`

### `prepare_response`

Вход:

- почти весь итоговый state

Выход:

- `response`
- `response_type`

## 3. Что делает каждый агент и какие функции внутри него идут по порядку

Ниже порядок дан по реальному выполнению кода в [src/graph/nodes.py](/mnt/c/Users/Admin/Desktop/3ABCD/TrueHack/src/graph/nodes.py) и [src/agents/planner.py](/mnt/c/Users/Admin/Desktop/3ABCD/TrueHack/src/agents/planner.py).

### 3.1 `TargetResolver` -> `resolve_target`

Что делает в целом: определяет active target Lua-файл и, если он уже существует, подгружает существующий код для refine/follow-up.

Порядок функций:

1. `resolve_lua_target(...)` из [src/tools/target_tools.py](/mnt/c/Users/Admin/Desktop/3ABCD/TrueHack/src/tools/target_tools.py)
   Что делает:
   - ищет явный путь к `.lua`
   - если найден путь к директории, строит slug и путь `<dir>/<slug>/<slug>.lua`
   - если путь не указан, переиспользует `current_target_path`
   - если fallback запрещен и target не найден, возвращает пустой `target_path`

2. `load_target_code(target_path)`
   Что делает:
   - читает код из target-файла, если он существует
   - возвращает строку Lua-кода или пустую строку

Результат агента:

- знает, куда сохранять
- знает, есть ли уже код для refine

### 3.2 `IntentRouter` -> `route_intent`

Что делает в целом: решает, это генерация, доработка, повторный прогон, просто вопрос или инспекция.

Порядок функций:

1. `_collect_intent_features(...)`
   Что делает:
   - достает код из сообщения через `_extract_message_code_block`
   - убирает код из текста через `_strip_message_code_blocks`
   - делит сообщение на task/context через `split_task_and_context`
   - ставит флаги: есть ли existing code, workflow context, change signal, retry signal, inspect signal, question signal

2. `_deterministic_intent_from_features(features)`
   Что делает:
   - пытается без LLM вычислить интент по эвристикам
   - например, `retry` с кодом -> `retry`
   - запрос на fix при наличии кода -> `change`
   - вопрос без code-task сигналов -> `question`

3. Если эвристик не хватило, `llm.generate_json(..., system=_ROUTE_SYSTEM)`
   Что делает:
   - классифицирует интент через LLM
   - набор разрешенных интентов зависит от того, есть код или нет

4. Пост-обработка результата
   Что делает:
   - валидирует интент
   - если кода нет, запрещает `change/inspect/retry`
   - если код был вставлен прямо в сообщение, может положить его в `current_code`

Результат агента:

- определяет высокоуровневую ветку графа

### 3.3 `TaskPlanner` -> `plan_request`

Что делает в целом: переписывает задачу в более точную формулировку для генератора и при необходимости задает уточняющие вопросы.

Когда работает:

- только для `create/change/retry`
- только если planner включен через `PLANNER_ENABLED`

Порядок функций:

1. Внутри `plan_request` собирается `effective_input`
   Что делает:
   - если это ответ на уточняющий вопрос, склеивает:
     - исходную задачу
     - список предыдущих вопросов
     - ответ пользователя
   - иначе берет обычный `user_input`

2. `agent.plan(...)`
   Это метод `PlannerAgent.plan`

3. Внутри `PlannerAgent.plan(...)`:
   - `_extract_workflow_paths_from_text(...)`
     Извлекает `wf.vars.*` и `wf.initVariables.*`
   - `llm.generate_json(..., system=_PLANNER_SYSTEM)`
     Просит LLM вернуть:
     - `reformulated_task`
     - `identified_workflow_paths`
     - `target_operation`
     - `data_types`
     - `expected_result_action`
     - `needs_clarification`
     - `clarification_questions`
   - `_normalize_planner_result(...)`
     Нормализует типы, режет мусор, ограничивает вопросы, чинит invalid values

4. Назад в `plan_request`
   Что делает:
   - если нужны уточнения и лимит попыток не исчерпан, вызывает `_build_clarification_response(...)` и завершает ход вопросом пользователю
   - если превышен `MAX_CLARIFICATION_ATTEMPTS=2`, forcibly continue
   - если это follow-up, перезаписывает `user_input` merged-версией, чтобы дальше пайплайн видел полный контекст

Результат агента:

- либо дает структурированный план задачи
- либо ставит пайплайн в режим ожидания уточнения

### 3.4 `GenerationContextCompiler` -> `prepare_generation_context`

Что делает в целом: строит единый `compiled_request`, который потом используют generator, refiner, validators и verifier.

Порядок функций:

1. `split_task_and_context(task_source_prompt)`
   Что делает:
   - отделяет текст задачи от вставленного JSON/контекста workflow

2. `compile_lowcode_request(task_text, raw_context, clarification_text)` из [src/tools/lua_tools.py](/mnt/c/Users/Admin/Desktop/3ABCD/TrueHack/src/tools/lua_tools.py)
   Это ключевая функция этапа.

3. Внутри `compile_lowcode_request(...)`:
   - `parse_lowcode_workflow_context(raw_context)`
     Парсит JSON/фрагмент workflow и строит inventory путей
   - `detect_lowcode_operation(merged_text)`
     Определяет тип операции: `count`, `first`, `last`, `increment`, `remove_keys` и т.д.
   - `extract_workflow_paths_from_text(...)`
     Ищет явные workflow-пути в тексте
   - `infer_explicit_paths_from_bare_field_names(...)`
     Пытается догадаться о путях по именам полей
   - `rank_workflow_paths(...)`
     Ранжирует кандидатов из inventory
   - `extract_requested_item_keys(...)`
     Пытается понять, какие поля элементов массива реально нужны
   - `request_explicitly_saves_to_workflow(...)`
     Определяет, просит ли пользователь сохранить результат в `wf.vars.*`
   - `infer_semantic_expectations(...)`
     Выводит semantic hints вроде `array_normalization`

4. После `compile_lowcode_request(...)`
   Что делает node:
   - при наличии planner может перекомпилировать запрос по `reformulated_task`
   - записывает `planner_result` внутрь `compiled_request`
   - переписывает `compiled_request["task_text"]` на planner-версию
   - сохраняет `original_task`
   - готовит `verification_prompt`

5. Если `compiled_request["needs_clarification"]=True`
   - `_build_clarification_response(compiled_request)`
   - завершает ход без генерации

Главный выход этапа: `compiled_request`

Самые важные поля `compiled_request`:

- `task_text`
- `original_task`
- `raw_context`
- `parsed_context`
- `workflow_path_inventory`
- `selected_operation`
- `selected_primary_path`
- `selected_primary_type`
- `selected_save_path`
- `requested_item_keys`
- `semantic_expectations`
- `needs_clarification`
- `verification_prompt`

### 3.5 `CodeGenerator` -> `generate_code`

Что делает в целом: генерирует Lua с нуля по compiled request.

Порядок функций:

1. `_build_generation_prompt(compiled_request)`
   Что делает:
   - собирает task
   - добавляет user clarification
   - добавляет workflow anchor из `_format_prompt_workflow_context(...)`
   - добавляет planner analysis из `_format_planner_section(...)`
   - добавляет raw workflow context
   - добавляет `_PROMPT_STYLE_RULES`, `_PROMPT_SYNTHESIS_GUIDANCE`, `LOWCODE_RESPONSE_FORMAT_REQUIREMENT`

2. `_generation_temperature(compiled_request)`
   Что делает:
   - если контекст хорошо понятен, снижает temperature до `0.0`
   - иначе оставляет более мягкий режим

3. `llm.generate(..., system=_GENERATE_SYSTEM)`
   Что делает:
   - генерирует ответ строго в формате `lua{...}lua`

4. `validate_lowcode_llm_output(raw)`
   Что делает:
   - проверяет wrapper `lua{...}lua`
   - нормализует код через `normalize_lua_code`
   - возвращает `normalized`

5. Если ответ битый:
   - `is_truncated_lowcode_response(raw)`
   - `_attempt_continuation(raw, agent_name)`
     Пытается достроить оборванный ответ
   - если не помогло, повторный `llm.generate(...)` с более жестким prompt

6. Если wrapper все равно плохой:
   - `smart_normalize(raw)`
     Пытается хотя бы извлечь нормальный Lua-код из мусорного ответа

Результат агента:

- `generated_code`

### 3.6 `CodeRefiner` -> `refine_code`

Что делает в целом: берет существующий Lua-код и возвращает полную обновленную версию.

Порядок функций:

1. Проверка на наличие `current_code`
   Если кода нет, агент просто падает назад в `generate_code`

2. `extract_function_names(existing)` из [src/tools/lua_tools.py](/mnt/c/Users/Admin/Desktop/3ABCD/TrueHack/src/tools/lua_tools.py)
   Что делает:
   - достает имена существующих функций, чтобы refiner их не потерял

3. `_build_refine_prompt(...)`
   Что делает:
   - добавляет original task
   - original workflow context
   - clarification
   - workflow anchor
   - planner analysis
   - список функций, которые надо сохранить
   - текущий код
   - change request

4. `llm.generate(..., system=_REFINE_SYSTEM)`
   Генерирует полный обновленный скрипт

5. `validate_lowcode_llm_output(raw)`
   Нормализует результат

6. При truncation:
   - `_attempt_continuation(...)`

7. `restore_lost_functions(existing, code, user_input)`
   Что делает:
   - сравнивает старый и новый код
   - возвращает потерянные функции обратно, если пользователь явно не просил их удалить

8. Обновляет `change_requests`

Результат агента:

- новая полная версия `generated_code`

### 3.7 `CodeValidator` -> `validate_code`

Что делает в целом: проверяет, запускается ли код локально как workflow Lua script.

Порядок функций:

1. `_normalize_runtime_candidate(original_code)`
   Что делает:
   - чистит и нормализует код перед запуском

2. `_run_diagnostics_with_optional_context(code, compiled_request)`
   Что делает:
   - если в `compiled_request` есть `parsed_context`, передает его в runtime
   - иначе запускает диагностику без контекста

3. Внутри `_run_diagnostics_with_optional_context(...)`
   - `_workflow_context_for_validation(compiled_request)`
   - `async_run_diagnostics(...)`

4. Внутри `async_run_diagnostics(...)` из [src/tools/lua_tools.py](/mnt/c/Users/Admin/Desktop/3ABCD/TrueHack/src/tools/lua_tools.py):
   - пишет код во временный файл
   - вызывает `_sync_run_diagnostics(...)` в executor

5. Внутри `_sync_run_diagnostics(...)`
   - `inspect_lowcode_script_contract(lua_code)`
     Отсекает консольные скрипты с `io.read`, `print` и т.п.
   - `build_lowcode_validation_harness(lua_file, lua_code, workflow_context)`
     Собирает временный Lua harness
   - внутри harness:
     - поднимает `wf.vars` и `wf.initVariables`
     - подставляет parsed workflow context
     - создает `_utils.array.new()` и `_utils.array.markAsArray()`
     - автоматически домокивает недостающие пути, найденные в коде
     - запускает пользовательский файл через `dofile(...)`
     - сериализует return value
     - сериализует финальный `wf`
     - снимает runtime context через `debug`
   - `run_lua_file(...)` из [src/tools/local_runtime.py](/mnt/c/Users/Admin/Desktop/3ABCD/TrueHack/src/tools/local_runtime.py)
     Реально запускает Lua-процесс
   - `merge_process_output(...)`, `repair_mojibake(...)`
   - `_extract_runtime_result(...)`
   - `_extract_runtime_workflow_state(...)`
   - `_extract_runtime_context(...)`
   - `infer_runtime_fix_hints(...)`
   - `classify_failure_kind(...)`

6. Если validation failed и есть `run_error`
   - `_format_numbered_code_block(code)`
   - `_clean_run_error(...)`
   - `llm.generate(..., system=_VALIDATOR_HINT_SYSTEM)`
   Получает текстовую подсказку, где ошибка и как чинить

Результат агента:

- `diagnostics`
- `validation_passed`
- runtime evidence для следующего verifier/fixer этапа

### 3.8 `ValidationFixer` -> `fix_validation_code`

Что делает в целом: чинит runtime/syntax ошибки после локального запуска.

Порядок функций:

1. `_normalize_runtime_candidate(...)`

2. `_build_fix_validation_prompt(...)`
   Что делает:
   - добавляет task
   - исходный workflow context
   - workflow anchor
   - planner analysis
   - runtime error
   - line hint через `_extract_runtime_line_number(...)` и `_extract_runtime_line_hint(...)`
   - кусок кода вокруг проблемной строки через `_format_code_context_window(...)`
   - llm error analysis
   - полный код с номерами строк

3. `llm.chat(..., system=_FIX_VALIDATION_SYSTEM)`
   Возвращает новую версию Lua-кода

4. `validate_lowcode_llm_output(raw)`

5. Если ответ оборван:
   - `_attempt_continuation(...)`

6. Если код изменился:
   - снова `_run_diagnostics_with_optional_context(...)`
   - если код все еще падает, еще раз просит hint через `_VALIDATOR_HINT_SYSTEM`
   - собирает retry prompt
   - делает второй `llm.chat(...)`

7. Если код не изменился:
   - делает еще одну попытку с явным note, что прошлый fix вернул тот же код

Результат агента:

- исправленный `generated_code`
- увеличенный `fix_iterations`

### 3.9 `RequirementsVerifier` -> `verify_requirements`

Что делает в целом: проверяет не синтаксис, а смысловое соответствие задаче.

Порядок функций:

1. Достает `verification_prompt` только из `compiled_request`
   Важный момент:
   - verifier не должен видеть raw `user_input`, если planner уже переформулировал задачу

2. `_build_verification_extra_context(compiled_request, diagnostics)`
   Что делает:
   - добавляет planner analysis
   - добавляет специальные инструкции для filter/select задач
   - добавляет parsed workflow context
   - добавляет actual runtime result
   - добавляет updated workflow state
   - если return `nil`, заставляет проверять еще и обновленный `wf`

3. `async_verify_requirements(llm, prompt, code, run_output, extra_context)`
   Что делает:
   - отправляет verifier system prompt
   - отправляет user request
   - отправляет extra context
   - отправляет runtime output
   - отправляет сам Lua-код
   - ждет strict JSON с checks

4. Внутри `async_verify_requirements(...)`
   - `_extract_json_block(...)`
   - `_normalize_verification_result(...)`
   - `_normalize_verification_checks(...)`

5. После ответа verifier:
   Что делает node:
   - собирает `failed_checks`
   - собирает `unclear_checks`
   - дополняет `missing_requirements` причинами из checks
   - формирует итоговый `passed`
   - обновляет `diagnostics`

Результат агента:

- `verification`
- `verification_passed`

### 3.10 `VerificationFixer` -> `fix_verification_code`

Что делает в целом: чинит логику, когда код запускается, но делает не то.

Порядок функций:

1. `_build_fix_verification_prompt(...)`
   Что делает:
   - добавляет task
   - original workflow context
   - workflow anchor
   - planner analysis
   - unmet requirements
   - failed verification checks
   - runtime result
   - updated workflow state
   - текущий код с номерами строк

2. `llm.chat(..., system=_FIX_VERIFICATION_SYSTEM)`

3. `validate_lowcode_llm_output(raw)`

4. Если ответ оборван:
   - `_attempt_continuation(...)`

5. Если код пустой или не изменился:
   - второй retry prompt с note про unchanged/empty result
   - еще один `llm.chat(...)`

Результат агента:

- новый `generated_code`
- увеличенный `fix_verification_iterations`

### 3.11 `CodeSaver` -> `save_code`

Что делает в целом: решает, можно ли реально писать файл на диск, и если можно, сохраняет `.lua` и sidecar payload.

Порядок функций:

1. `_normalize_runtime_candidate(...)`

2. Проверки условий сохранения
   - если код пустой -> ошибка
   - если validation failed -> `save_skipped=True`
   - если verification failed -> `save_skipped=True`
   - если нет `target_path` -> `save_skipped=True`

3. `format_lowcode_json_payload(code, compiled_request, target_path)` из [src/tools/lua_tools.py](/mnt/c/Users/Admin/Desktop/3ABCD/TrueHack/src/tools/lua_tools.py)
   Что делает:
   - выбирает имя поля через `suggest_json_payload_field_name(...)`
   - оборачивает Lua в `format_lowcode_jsonstring(...)`
   - создает JSON-объект с embedded `lua{...}lua`

4. `save_final_output(target_path, code, jsonstring_code)` из [src/tools/target_tools.py](/mnt/c/Users/Admin/Desktop/3ABCD/TrueHack/src/tools/target_tools.py)
   Что делает:
   - создает директорию
   - пишет `.lua`
   - пишет sidecar `*.jsonstring.txt`

Результат агента:

- `current_code`
- путь к `.lua`
- путь к sidecar JSON payload

### 3.12 `SolutionExplainer` -> `explain_solution`

Что делает в целом: превращает итоговый код в человеческое описание и генерирует идеи следующей итерации.

Порядок функций:

1. Выбирает `user_request_text`
   Что делает:
   - берет сначала `compiled_request["original_task"]`
   - потом `compiled_request["task_text"]`
   - и только в крайнем случае `user_input`

2. `format_lowcode_jsonstring(code)`
   Нужен для передачи кода в explainer prompt

3. `llm.generate_json(..., system=_EXPLAIN_SYSTEM)`
   Ожидает JSON:
   - `summary`
   - `what_is_in_code`
   - `how_it_works`
   - `suggested_changes`
   - `clarifying_questions`

4. `_normalize_string_list(...)`
   Нормализует списки и строки

5. Если LLM вернул пустоту:
   Подставляет fallback summary/sections

Результат агента:

- explanation для пользователя
- список улучшений
- список уточняющих вопросов

### 3.13 `QuestionAnswerer` -> `answer_question`

Что делает в целом: отвечает на вопросы без запуска generation pipeline.

Порядок функций:

1. `_target_context(state)`
   Если есть target file, добавляет его в контекст ответа

2. Формирует prompt
   - только `user_input`
   - или `current_code + user_input`, если в чате уже есть код

3. `llm.generate(..., system=_ANSWER_SYSTEM)`

Результат агента:

- обычный текстовый ответ

### 3.14 `ResponseAssembler` -> `prepare_response`

Что делает в целом: собирает финальный user-facing ответ на основе всего state.

Порядок функций:

1. `_normalize_runtime_candidate(code)`

2. Определяет статус:
   - сохранено
   - не сохранено, но проверки прошли
   - не сохранено из-за validation/verification problem

3. Добавляет:
   - `diagnostics.run_error`
   - `verification.summary`
   - пути `saved_to`, `saved_jsonstring_to`

4. `format_lowcode_json_payload(...)`
   Вставляет JSON payload с `lua{...}lua`

5. Добавляет:
   - `run_output`
   - explanation summary
   - `what_is_in_code`
   - `how_it_works`
   - `suggested_changes`
   - `clarifying_questions`

Итог:

- готовая строка `response`

## 4. Что является фактическим ядром этапа генерации

Если сжать весь runtime до минимального ядра, генерация держится на 5 сущностях:

1. `resolve_lua_target(...)`
   Решает, куда работать и есть ли уже файл.

2. `compile_lowcode_request(...)`
   Превращает пользовательский текст и workflow context в структурированную задачу.

3. `generate_code` / `refine_code`
   Строят итоговый Lua-код.

4. `async_run_diagnostics(...)` + временный validation harness
   Проверяют, что код реально исполняется как workflow script.

5. `async_verify_requirements(...)`
   Проверяют, что код не просто запускается, а решает нужную задачу.

Без этих пяти блоков пайплайн не может ни сгенерировать код корректно, ни безопасно сохранить его в файл.
