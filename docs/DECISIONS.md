# "Архитектурные решения"

# Decisions log

## 2026-04-10
### Decision
Используем `app.py + src/graph` как единственный поддерживаемый runtime.

### Why
В проекте было дублирование orchestration между новым graph path и legacy `main.py`.
Для управляемого продукта нужен один pipeline и один entry point.

### Consequences
`main.py` удалён из поддерживаемого runtime.
Вся новая логика должна встраиваться только в canonical graph pipeline.

## 2026-04-10
### Decision
Сохраняем Lua target path logic как обязательную бизнес-функцию.

### Why
Система должна уметь писать Lua-скрипты в нужное место, создавать папки и переиспользовать активный target в рамках чата.
Это часть продуктовой логики, а не legacy baggage.

### Consequences
Target resolution и save flow встроены в canonical runtime.
Cleanup не должен удалять explicit path / directory behavior.

## 2026-04-10
### Decision
Удаляем generic README/text artifact orchestration из продуктового runtime.

### Why
Продуктовый scope проекта — LocalScript / Lua generation pipeline.
Generic text/document logic усложняла архитектуру и создавала второй продукт внутри одного репозитория.

### Consequences
Canonical runtime теперь Lua-only.
README остаётся документацией репозитория, а не целевым artifact type приложения.

## 2026-04-10
### Decision
Generation, refine, fix и verification должны идти через один LLM abstraction layer.

### Why
Ранее в проекте были разрозненные direct client paths и legacy verification path, что создавало дублирование и делало runtime неочевидным.

### Consequences
Canonical runtime использует единый provider из `src/core/llm.py`.
Legacy direct LLM client modules удалены.

## 2026-04-10
### Decision
Текущий dev-runtime может использовать local OpenAI-compatible endpoint, но финальная хакатонная конфигурация должна быть воспроизводима через Ollama.

### Why
Это прямое требование хакатона.

### Consequences
Docs и README должны честно различать:
- что уже работает в canonical runtime сейчас;
- что ещё нужно довести до финального Ollama-target.
