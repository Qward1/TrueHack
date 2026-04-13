# Архитектурные решения

# Decisions log

## 2026-04-12
### Decision
Per-agent Ollama model selection is resolved centrally inside `src/core/llm.py`, not by branching the pipeline into separate client implementations.

### Why
Different LLM steps have different quality/latency requirements, but adding dedicated clients per node would duplicate configuration, logging, and request handling.

### Consequences
- The canonical runtime still constructs one `LLMProvider`.
- Each LLM call now passes its logical `agent_name`, and `LLMProvider` resolves the effective model from env key `OLLAMA_MODEL_<AGENT_NAME>` when present.
- Shared fallback order is:
  - per-agent env override;
  - CLI `--model`;
  - shared `OLLAMA_MODEL`;
  - built-in default.
- Request/response audit logs now include both `agent_name` and the effective `model`.

---

## 2026-04-10
### Decision
Используем `app.py + src/graph` как единственный поддерживаемый runtime.

### Why
В проекте было дублирование orchestration между graph path и legacy runtime.

### Consequences
Вся новая логика встраивается только в canonical graph pipeline.

---

## 2026-04-10
### Decision
Сохраняем Lua target path logic как обязательную бизнес-функцию.

### Why
Система должна уметь писать Lua-файлы в нужный путь, создавать директории и переиспользовать active target.

### Consequences
`resolve_target` и `save_code` остаются core-частью runtime.

---

## 2026-04-10
### Decision
Исключаем generic README/text artifact orchestration из продуктового runtime.

### Why
Product scope — Lua-centric pipeline, а не универсальный редактор артефактов.

### Consequences
Runtime остается Lua-only, README — документация репозитория.

---

## 2026-04-10
### Decision
Generation/refine/fix/verify/explain используют единый LLM provider (`src/core/llm.py`).

### Why
Разрозненные direct client paths приводят к дублированию и нестабильному runtime.

### Consequences
Один abstraction layer для всех LLM-шагов pipeline.

---

## 2026-04-10
### Decision
Временно отключаем e2e gate в каноническом runtime.

### Why
Нужно упростить текущий рабочий цикл до `generate/refine -> validate -> verify -> save -> explain`,
сохранив пост-объяснение и предложения улучшений после записи файла.

### Consequences
`save_code` вызывается после успешной локальной валидации и проверки требований.
`src/tools/lua_tools.py` сохраняет e2e helpers, но graph path их сейчас не вызывает.

---

## 2026-04-10
### Decision
Временно отключаем `luacheck` в канонической локальной валидации.

### Why
Нужно, чтобы runtime проверял и чинил код через фактический запуск `lua`, без отдельного lint-шага.

### Consequences
`validate_code` теперь опирается на запуск через `lua`, а `luacheck` wrappers остаются в кодовой базе неиспользуемыми.
Ответ пользователю после сохранения по-прежнему формируется через `explain_solution` и содержит предложения улучшений.

---

## 2026-04-10
### Decision
Унифицируем naming и path sanitization для создаваемых папок, Lua-файлов и chat titles.

### Why
Сырые prompt/path fragments давали слабые имена (`uluchshi_kod`) и могли ломать сохранение на Windows из-за невалидных сегментов пути.

### Consequences
`src/tools/target_tools.py` теперь отвечает за единые naming helpers:
- более информативный slug для auto-created project artifacts;
- sanitization невалидных Windows path components;
- генерацию chat title из очищенного prompt.

---

## 2026-04-10
### Decision
Переводим generation contract на LowCode-формат `Lua 5.5 + lua{...}lua`.

### Why
Новые продуктовые условия требуют описывать скрипт как JsonString, хранить данные схемы через `wf.vars` / `wf.initVariables` и не использовать JsonPath.

### Consequences
Prompt contract и user-facing code representation обновлены под `lua{...}lua`, при этом runtime продолжает валидировать и сохранять чистое Lua-body после нормализации wrapper.

---

## 2026-04-10
### Decision
Сохраняем оба представления результата: canonical `.lua` и JsonString sidecar.

### Why
Продуктовый контракт требует работать и с исполняемым Lua-файлом, и с форматом `lua{...}lua`, но дублировать pipeline ради этого не нужно.

### Consequences
`save_code` выполняет один save-step и пишет:
- основной `.lua` файл для runtime;
- соседний `*.jsonstring.txt` c wrapper `lua{...}lua`.
UI `/status` и финальный ответ показывают оба пути, если сохранение прошло успешно.

---

## 2026-04-10
### Decision
Локальная validation для LowCode-скриптов запускается через temporary mock harness.

### Why
Голый запуск `lua script.lua` давал ложные падения на `wf.initVariables` и `_utils`, хотя проблема была в отсутствии платформенного контекста, а не в самом Lua-коде.

### Consequences
`validate_code` теперь исполняет временный harness, который:
- создаёт mock `wf.vars` и `wf.initVariables`;
- добавляет `_utils.array.new()` / `_utils.array.markAsArray(arr)`;
- строит nested mock tables для найденных `wf.vars.*` / `wf.initVariables.*`, в том числе при alias access.
Это снижает ложные validation failures и даёт fix-loop реальные runtime diagnostics.

---

## 2026-04-11
### Decision
Канонический runtime генерирует workflow/LUS scripts, а не console/CLI Lua-программы.

### Why
Текущий продуктовый формат — это data/workflow chunk с работой через `wf.vars` / `wf.initVariables`, а не интерактивное консольное приложение.

### Consequences
Generation/refine/fix prompts теперь требуют:
- workflow/LUS script shape;
- прямую работу с `wf.vars` и `wf.initVariables`;
- возврат значения и/или обновление `wf.vars`;
- отсутствие console input/output по умолчанию.
Локальная validation дополнительно блокирует `io.read` / `io.stdin:read` как нарушение активного контракта.

---

## 2026-04-10
### Decision
После успешного сохранения система обязана возвращать объяснение решения и следующий шаг для пользователя.

### Why
Нужно не только выдать код, но и объяснить реализацию, предложить улучшения и задать уточняющие вопросы.

### Consequences
Добавлен `explain_solution` и хранение `suggested_changes`/`clarifying_questions` в chat state.
Follow-up вида `примени предложение N` поддерживается в следующем turn.

---

## 2026-04-11
### Decision
Миграция canonical runtime с LM Studio на Ollama завершена.

### Why
Прямое требование условий хакатона: модель должна запускаться через Ollama.

### Consequences
- Дефолт: `http://127.0.0.1:11434/v1` + `qwen2.5-coder:7b-instruct`
- Параметры хакатона зафиксированы в `Modelfile` (num_ctx=4096, num_predict=256, num_gpu=99)
- Смена модели: CLI `--model`, env `OLLAMA_MODEL`, или кастомный Modelfile
- Для 4GB VRAM: `--model qwen2.5-coder:3b-instruct`
- LM Studio env-переменные (`LMSTUDIO_MODEL`, `LMSTUDIO_URL`) удалены
- AsyncOpenAI клиент не изменён — Ollama совместим с OpenAI API

---

## 2026-04-11
### Decision
Workflow-path alignment is a hard save-gate requirement.

### Why
LLM-only verification was insufficient for public-sample tasks: the model could still return an app-style script with invented input tables and pass an overly optimistic semantic verdict. The product requirement is stricter: generated Lua must operate on the provided workflow structure directly.

### Consequences
- The product rule stays the same: code that ignores the provided workflow structure must not be saved.
- The initial implementation used a deterministic guard.
- The active implementation now enforces the same rule through semantic verification with parsed workflow context and concrete runtime evidence.

---

## 2026-04-12
### Decision
Сводим generation/fix format contract к одному жёсткому правилу и убираем prompt-noise для малой модели.

### Why
Логи показали, что `qwen2.5-coder:7b-instruct` хуже работает, когда prompt одновременно говорит про `lua{...}lua`, JsonString, запрет на JSON, markdown и другие meta-format требования, а также когда fix-prompt перегружен ranked candidates, confidence, verifier summary, длинными diagnostics и прошлым сломанным assistant output.

### Consequences
- generation/refine/fix prompts теперь требуют только одно правило формата: ответ должен начинаться с `lua{` и заканчиваться `}lua`, без кавычек и без code fences;
- raw LLM output считается невалидным, если он приходит в fences или quoted wrapper;
- generation/fix prompts сокращены до задачи, основного workflow path, текущего context и короткого mandatory-fix списка;
- `fix_code` больше не подсовывает модели её прошлый сломанный ответ;
- для shape-sensitive задач prompt contract теперь явно фиксирует определение массива: numeric keys `1..n` без пропусков, пустая table считается массивом.

---

## 2026-04-11
### Decision
Не создаём fallback file target для нового чата без явного пути.

### Why
Пользовательское ожидание разделено на два режима:
- без явного path код нужен только как ответ в чате;
- с явным path система должна работать как file-based Lua builder.
Автоматическое сохранение в workspace без явного запроса смешивало эти режимы и создавало лишние файлы.

### Consequences
- `resolve_target` по-прежнему умеет:
  - explicit `.lua` path;
  - директорию;
  - active target текущего чата.
- Если новый turn не содержит path и active target ещё не задан, pipeline всё равно выполняет `validate -> verify -> explain/respond`, но `save_code` намеренно пропускает запись на диск.
- Пользователь получает код в чате без save-error.

---

## 2026-04-11
### Decision
Cleanup/remove-key задачи в workflow object data не считаются простыми `return`-операциями.

### Why
Фраза вида `очисти/удали ключи ...` может сослаться на правильный workflow path, но всё равно требовать реальную трансформацию массива/объекта. Старый deterministic guard пропускал `return wf.vars.some.path`, если путь был выбран верно.

### Consequences
- Operation detection выделяет key-cleanup запросы отдельно от простого `return`.
- Для таких задач простой `return` исходного workflow path блокирует save.
- Verification требует явного упоминания и обработки запрошенных ключей перед возвратом результата.

---

## 2026-04-11
### Decision
Intent `change` без existing code не должен заходить в refine-path.

### Why
LLM intent classifier может выбрать `change` для новых задач со словами `улучши`, `исправь`, `очисти`, даже если в чате или target file ещё нет текущего кода. В этом случае вход в `refine_code` создавал ложный warning и тут же падал обратно в `generate_code`.

### Consequences
- routing после preparation теперь учитывает не только intent, но и наличие `current_code`;
- `change`/`retry` без existing code идут сразу в `generate_code`;
- warning `no existing code — falling back to generate_code` остаётся только как defensive fallback, а не как штатный путь.

---

## 2026-04-12
### Decision
Semantic verification and final response assembly now depend on parsed workflow context and post-execution workflow state, not only on prompt text and static code review.

### Why
Recent failures showed two product-level gaps:
- wrapper and fence noise could corrupt otherwise usable Lua before validation;
- semantically wrong code could pass because the verifier only reviewed the prompt and source text;
- when validation or verification finally failed, the UI still returned the last broken payload as if it were a usable final answer.

### Consequences
- the validation harness serializes both the actual Lua return value and the updated workflow snapshot from the provided workflow context;
- the verifier prompt now includes parsed workflow context and updated workflow state for mutation-oriented mutation-safe review;
- common malformed `lua{...}lua` fence variants and loose `"wf": {...}` fragments are repaired before validation;
- failed validation or verification turns return diagnostics together with the current code payload; save remains blocked.

---

## 2026-04-12
### Decision
Static deterministic verification guard is removed from the active pipeline.

### Why
The guard was producing false positives, obscuring the real logic failures, and sending the fix loop after the wrong problem. The product now relies on stronger semantic verification with concrete runtime evidence instead.

### Consequences
- `verify_requirements` now depends on semantic LLM review plus parsed workflow context and updated workflow state.
- Fix prompts receive semantic `missing_requirements` and failed-check reasons directly.
- False-positive first-pass verifier approvals can be challenged by a second contradiction-focused verifier pass that uses workflow-state evidence.

---

## 2026-04-12
### Decision
Intent routing больше не должен целиком зависеть от raw LLM-классификации.

### Why
LLM intent classifier склонен переоценивать `change` для новых задач с формулировками вроде `исправь`, `улучши`, `оберни`, даже когда в чате ещё нет кода. Это не должно быть источником истины для первого шага orchestration.

### Consequences
- `route_intent` стал hybrid:
  - deterministic signals из state + user message используются первыми;
  - LLM остаётся только fallback tie-breaker для неоднозначных случаев.
- Без current code в state и без pasted Lua в сообщении change-like prompt трактуется как `create`.
- Если пользователь вставил Lua прямо в сообщение, этот код может стать источником existing code для `change` / `inspect`.
- Guard в `route_after_preparation` сохраняется как дополнительная защита, но больше не является главным способом исправлять неверный `change`.

---

## 2026-04-11
### Decision
Bare field names from the task can be resolved to workflow paths using the pasted context.

### Why
Пользователь часто пишет `recallTime`, `emails`, `DATUM`, `TIME` без полного `wf.vars.*` / `wf.initVariables.*`. Если parseable workflow context уже есть в сообщении, отсутствие такого разрешения ослабляет verification/save gate и позволяет сохранить код с неправильным workflow path.

### Consequences
- compiler добавляет inferred explicit paths, если bare field name однозначно соответствует одному workflow path в pasted context;
- verification использует эти inferred paths как expected workflow paths;
- это остаётся общим правилом по parseable context, а не special-case под конкретный prompt.

---

## 2026-04-11
### Decision
Отказываемся от deterministic fast-path code emission и heavy few-shot code templates в generation/refine/fix prompts.

### Why
Short-return bias и встроенные canned snippets слишком сильно тянули модель к однострочным extraction-ответам даже там, где задача требовала loops, guards, normalization или helper functions. Это ухудшало универсальность генерации именно для реальных workflow scripts.

### Consequences
- compiler остаётся в pipeline как слой структурного контекста:
  - path inventory;
  - operation hints;
  - clarification gate;
  - verification/save guard;
- финальный Lua-код всегда синтезирует модель по текущей задаче и workflow context;
- prompts описывают ограничения и synthesis strategy абстрактно, без встраивания sample-code templates;
- generation может штатно возвращать и короткие, и более длинные multi-step workflow scripts.

---

## 2026-04-11
### Decision
Normalizing generation output must handle structured JSON code envelopes, not only raw `lua{...}lua`.

### Why
Некоторые модельные ответы приходят как JSON-объект с полем `lua` / `code` / `script` внутри LowCode wrapper. Без отдельного extraction шага validation пытается исполнить мета-структуру как Lua и падает синтаксически, хотя полезный код уже был в ответе.

### Consequences
- `normalize_lua_code()` теперь извлекает Lua из JSON-like envelopes before validation;
- extraction covers fenced/meta-wrapped JSON payloads as well, so runtime does not try to execute escaped JSON string contents as Lua;
- это общее поведение для class of structured model responses, а не patch под один prompt;
- if structured response does not actually contain code-bearing fields, pipeline still fails normally instead of guessing.

---

## 2026-04-11
### Decision
User-facing JsonString export should be emitted as a JSON object with a named field, not as a bare wrapper string.

### Why
Внешний артефакт удобнее интегрировать, когда он уже имеет JSON shape, а значение нужного поля содержит готовый `lua{...}lua`. При этом runtime validation должна по-прежнему работать на plain Lua body, без зависимости от export format.

### Consequences
- `.lua` файл остаётся canonical executable artifact;
- sidecar и code block в ответе формируются как JSON object with one field;
- field name выводится из `selected_save_path`, `selected_primary_path` или stem target file;
- normalizer already умеет вытаскивать Lua обратно из таких structured payloads, поэтому validation/fix loop не ломаются.

---

## 2026-04-11
### Decision
Fix-loop should receive normalized repair hints for common Lua runtime errors.

### Why
Raw stderr alone is often too noisy. For recurring Lua failures such as `bad argument`, `attempt to index/call nil`, arithmetic/type mismatch, or concatenation errors, the system needs a more stable signal about the root cause.

### Consequences
- validation diagnostics now include generic repair hints extracted from runtime errors;
- `fix_code` prompt includes these hints together with raw stderr;
- this remains API-agnostic and is not hardcoded to one function like `os.time`.

---

## 2026-04-12
### Decision
Fix-loop must not trust the first model repair attempt blindly.

### Why
Логи показали, что модель может игнорировать diagnostics и вернуть почти тот же код или повторить те же semantic requirement failures. Без дополнительной проверки pipeline тратит итерации fix-loop на косметические или пустые правки.

### Consequences
- after the first `fix_code` model call, the pipeline assesses the candidate before returning to validation;
- if the candidate is empty, not a plausible standalone Lua file, materially unchanged, or still repeats the same semantic requirement failures, `fix_code` performs one stricter internal retry immediately;
- the stricter retry explicitly lists why the previous fix attempt is rejected and demands a materially different full Lua script.

---

## 2026-04-12
### Decision
Concrete workflow-state evidence overrides an optimistic verifier pass.

### Why
LLM verifier can hallucinate and approve code that still violates the request. Concrete workflow-state evidence is stronger than a purely textual approval.

### Consequences
- verifier prompts include the original and updated workflow snapshots after execution;
- a contradiction-focused verifier retry can overturn an optimistic first pass;
- fix prompts receive the concrete request-vs-result mismatch instead of a static guard verdict.

---

## 2026-04-12
### Decision
Workflow code generation must use a stricter output contract and a lower-temperature policy when parseable workflow context is available.

### Why
Реальные логи показали два recurring failures:
- модель возвращает fenced / JSON / quoted-string wrappers around Lua instead of plain `lua{...}lua`;
- при temperature `0.2` для parseable workflow tasks возрастает доля shallow shortcut generations and meta-format noise.

### Consequences
- generate/refine/fix prompts now explicitly require that the first non-whitespace characters are `lua{` and the last are `}lua`;
- prompts explicitly forbid ``` fences, JSON objects, quoted strings, and `json` labels around the final code;
- generation/refine/fix use a lower temperature when workflow context is parseable, to improve determinism for contract-heavy tasks.

---

## 2026-04-11
### Decision
Simple workflow data tasks are compiled deterministically from parsed context before main LLM generation.

### Why
Prompt-only steering was insufficient. The model could still fall back to tutorial/application-style code whenever the request drifted away from the few-shot examples. The missing piece was structural understanding of pasted workflow JSON.

### Consequences
- The pipeline now compiles parseable workflow context into an internal request object before generation/refinement.
- For simple single-target tasks, code is generated deterministically instead of asking the main LLM.
- When multiple workflow paths match the request with similar confidence, the system asks for clarification and stops before generation/save.
- Main LLM generation remains for complex transformations, but now receives normalized workflow-path/type context rather than only raw pasted JSON.

Status
Superseded by the later model-driven generation decision. The compiled request object remains, but it now steers generation/verification instead of emitting Lua templates directly.

---

## 2026-04-12
### Decision
TaskPlanner LLM-агент интегрирован в canonical pipeline как отдельная нода `plan_request` между intent routing и `prepare_generation_context`.

### Why
До интеграции pipeline шёл напрямую от `route_intent` к deterministic compiler: не было LLM-уровня переформулировки задачи, понимания target operation/expected result action и нормального clarification flow для ambiguous task-level запросов. Detailed compiler-level clarifications покрывали path-level ambiguity, но не "что именно вообще нужно сделать".

### Consequences
- В graph добавлен узел `plan_request` поверх существующего `src/agents/planner.py` (модуль уже существовал и был покрыт 37 unit-тестами).
- Новые conditional edges:
  - `route_from_start`: если `awaiting_planner_clarification=True`, START идёт сразу в `plan_request`, обходя `resolve_target` и `route_intent`. Это требование: ответ пользователя на уточнение должен попадать **напрямую** в планировщик.
  - `route_after_planning`: после планировщика — либо `prepare_generation_context` (continue), либо `prepare_response` (clarify).
- 6 новых полей в `PipelineState`: `planner_result`, `planner_skipped`, `awaiting_planner_clarification`, `planner_pending_questions`, `planner_original_input`, `planner_clarification_attempts`.
- `app.py` персистит planner-state между turn'ами и сбрасывает его в `/new`.
- При follow-up планировщик собирает merged input: `Исходная задача + Уточняющие вопросы + Ответ пользователя` и переписывает `state.user_input` на этот merged context, чтобы дальнейший pipeline видел полную задачу.
- `MAX_CLARIFICATION_ATTEMPTS=2` — защита от зацикливания: после 2 попыток уточнения принудительно `needs_clarification=False`.
- Deterministic compiler остаётся единственным источником истины по path-level решениям. Planner output только обогащает prompt'ы generation/refine/fix через `_format_planner_section` (`Reformulated task`, `Planner-identified workflow paths`, `Expected result action`).
- Soft enrichment: если у запроса не было parseable context, но `reformulated_task` помогает compiler'у найти больше expected paths — берём улучшенный compiled_request.
- Toggle: `PLANNER_ENABLED=true` в `.env.example`. При `false` нода short-circuits с `planner_skipped=True` и pipeline работает как раньше — это сохраняет совместимость с существующими тестами и даёт fallback при медленной модели.

---

## 2026-04-12
### Decision
Generation and verification must reason explicitly about shape-sensitive workflow tasks instead of trusting path alignment alone.

### Why
Логи показали, что модель может выбрать правильный `wf.vars.*` path и всё равно сгенерировать логически слабый код, особенно для normalization задач. Одного path guard недостаточно: нужны stronger prompt guidance и semantic review по source/target shape и helper API usage.

### Consequences
- generation/refine/fix prompts now instruct the model to reason about:
  - source shape;
  - target/output shape;
  - mutation-vs-return;
  - edge cases;
  - helper API usage;
- verification prompt now returns a checklist, not only a free-form summary;
- semantic verification and fix prompts now explicitly reject:
  - array-normalization code that relies only on `type(x) == "table"`;
  - array-normalization code that treats `next(x)` / empty-vs-non-empty tests as a substitute for object-vs-array shape detection;
  - relabeling the original workflow object/scalar as an array in place via `_utils.array.markAsArray(source)`;
  - `_utils.array.new(...)` with inline arguments;
  - new arrays returned without `_utils.array.markAsArray(arr)`.

---

## 2026-04-12
### Decision
Semantic verification must evaluate both direct return values and post-execution workflow mutations.

### Why
Many workflow scripts satisfy the task by updating `wf.vars` and returning `nil`. Checking only the direct return value causes false positives and false negatives: verifier misses wrong mutations, while correct mutation scripts look like `null`.

### Consequences
- validation harness serializes both the direct Lua result and the updated workflow snapshot;
- verifier and verification-fix prompts can judge mutation tasks against the updated workflow state when direct result is `null`;
- runtime marker extraction is CRLF-safe, so service markers are stripped reliably on Windows.

---

## 2026-04-12
### Decision
Solution explanation sections should accept both array and string JSON fields from the explainer model.

### Why
`SolutionExplainer` sometimes returns `what_is_in_code` / `how_it_works` as single strings instead of JSON arrays. Treating those fields as invalid caused needless fallback to generic text even when the model produced useful content.

### Consequences
- explainer response normalization now converts single strings and multiline text into short string lists;
- generic fallback text is used only when the explainer returned no usable content for that section.
