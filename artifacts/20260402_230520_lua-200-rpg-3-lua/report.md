# Lua Solution Report

## User Prompt
Создай Lua-скрипт размером не менее 200 строк, который реализует текстовую RPG-игру с элементами выживания и случайных событий. Скрипт должен работать в консоли и быть полностью автономным, без сторонних библиотек. В игре у персонажа должны быть характеристики: здоровье, энергия, голод, золото, опыт и уровень. Добавь систему инвентаря с возможностью находить, использовать, покупать и продавать предметы. Реализуй несколько типов предметов: еда, зелья, оружие, броня и редкие артефакты. Сделай магазин, где цены могут немного меняться случайным образом. Добавь систему врагов с разными параметрами, шансом критического удара, защитой и уникальными типами поведения. В игре должны быть локации, например: лес, пещера, деревня, руины и лагерь. В каждой локации должны происходить разные события: бой, находка ресурсов, ловушки, торговцы, отдых, случайные сюжетные сцены. Реализуй пошаговую боевую систему, где игрок может атаковать, защищаться, использовать предмет или попытаться сбежать. Добавь простой ИИ врагов, чтобы разные противники выбирали действия по-разному. Сделай систему опыта и повышения уровня, где с новым уровнем немного растут характеристики игрока. Также добавь квестовую механику: хотя бы 3 простых задания, например победить определённое число врагов, найти редкий предмет или накопить сумму золота. Нужна система сохранения текущего прогресса в файл и загрузки из файла при запуске. В коде обязательно используй таблицы, функции, циклы, условия, работу со строками и файлами. Архитектуру сделай аккуратной: отдельные функции для меню, генерации событий, боя, торговли, инвентаря, квестов и сохранения. Добавь комментарии по основным блокам, чтобы код было легко читать. Итоговый скрипт должен быть достаточно подробным, логически структурированным и выглядеть как полноценный мини-проект на Lua, а не как набор разрозненных функций..

## Script
`solution.lua`

## Parsed Spec
```json
{
  "acceptance_criteria": [
    "Скрипт соответствует исходному запросу",
    "Скрипт проходит локальный запуск и тестирование",
    "Итоговый код содержит не менее 200 строк.",
    "Основные блоки снабжены понятными комментариями."
  ],
  "assumptions": [
    "Автоматический разбор выполнен в fallback-режиме",
    "Причина fallback: Prompt is too long for reliable structured parsing with the local model."
  ],
  "constraints": [
    "Код должен быть читаемым и пригодным для локального запуска",
    "Финальный Lua-файл должен содержать не менее 200 строк.",
    "Использовать только стандартный Lua без внешних библиотек.",
    "Скрипт должен быть автономным и запускаться без дополнительных модулей."
  ],
  "edge_cases": [
    "Неочевидные входные данные нужно обработать безопасно"
  ],
  "inputs": [
    "Текстовый пользовательский запрос"
  ],
  "outputs": [
    "Lua-скрипт, реализующий запрос"
  ],
  "requested_behavior": [
    "Реализовать полноценный игровой цикл текстовой RPG в консоли.",
    "Добавить инвентарь с поиском, использованием, покупкой и продажей предметов.",
    "Добавить магазин и механику торговли с изменяемыми ценами.",
    "Добавить врагов, пошаговый бой и простое поведение ИИ.",
    "Добавить несколько локаций и генерацию событий по локациям.",
    "Добавить как минимум несколько квестов и обновление прогресса квестов.",
    "Добавить сохранение прогресса в файл.",
    "Добавить загрузку прогресса из файла.",
    "Использовать случайные события и случайный выбор врагов/сцен.",
    "Добавить опыт, уровни и рост характеристик."
  ],
  "task_summary": "Создай Lua-скрипт размером не менее 200 строк, который реализует текстовую RPG-игру с элементами выживания и случайных событий. Скрипт должен работать в консоли и быть полностью автономным, без сторонних библиотек. В игре у персонажа должны быть характеристики: здоровье, энергия, голод, золото, опыт и уровень. Добавь систему инвентаря с возможностью находить, использовать, покупать и продавать предметы. Реализуй несколько типов предметов: еда, зелья, оружие, броня и редкие артефакты. Сделай магазин, где цены могут немного меняться случайным образом. Добавь систему врагов с разными параметрами, шансом критического удара, защитой и уникальными типами поведения. В игре должны быть локации, например: лес, пещера, деревня, руины и лагерь. В каждой локации должны происходить разные события: бой, находка ресурсов, ловушки, торговцы, отдых, случайные сюжетные сцены. Реализуй пошаговую боевую систему, где игрок может атаковать, защищаться, использовать предмет или попытаться сбежать. Добавь простой ИИ врагов, чтобы разные противники выбирали действия по-разному. Сделай систему опыта и повышения уровня, где с новым уровнем немного растут характеристики игрока. Также добавь квестовую механику: хотя бы 3 простых задания, например победить определённое число врагов, найти редкий предмет или накопить сумму золота. Нужна система сохранения текущего прогресса в файл и загрузки из файла при запуске. В коде обязательно используй таблицы, функции, циклы, условия, работу со строками и файлами. Архитектуру сделай аккуратной: отдельные функции для меню, генерации событий, боя, торговли, инвентаря, квестов и сохранения. Добавь комментарии по основным блокам, чтобы код было легко читать. Итоговый скрипт должен быть достаточно подробным, логически структурированным и выглядеть как полноценный мини-проект на Lua, а не как набор разрозненных функций.."
}
```

## Implementation Plan
```json
{
  "done_definition": [
    "Скрипт соответствует исходному запросу",
    "Скрипт проходит локальный запуск и тестирование",
    "Итоговый код содержит не менее 200 строк.",
    "Основные блоки снабжены понятными комментариями."
  ],
  "implementation_steps": [
    "Определить основные функции и данные, которые нужны скрипту",
    "Написать Lua-код с понятными именами и небольшой модульной структурой",
    "Подготовить точку входа или функции, удобные для тестирования"
  ],
  "repair_strategy": [
    "При ошибке выполнения исправлять код по тексту ошибки",
    "При провале тестов исправлять код по тестовым ожиданиям",
    "Не переписывать решение полностью без необходимости"
  ],
  "risks": [
    "Модель могла не вернуть строгий JSON",
    "План построен в fallback-режиме: Planning uses the deterministic fallback after parsed_spec fallback."
  ],
  "testing_steps": [
    "Проверить основные сценарии из acceptance_criteria",
    "Проверить граничные случаи из edge_cases"
  ],
  "validation_steps": [
    "Сгенерировать файл со скриптом",
    "Запустить код через доступный Lua runtime",
    "Собрать stdout, stderr и результаты статической проверки"
  ]
}
```

## Execution Result
```json
{
  "command": "C:\\Users\\Admin\\AppData\\Local\\Programs\\Lua\\bin\\lua.EXE C:\\Users\\Admin\\AppData\\Local\\Temp\\lua-agent-tests-eyuacmvf\\test_runner.lua",
  "environment": {
    "backend_preference": "auto",
    "linter": "C:\\Users\\Admin\\AppData\\Roaming\\luarocks\\bin\\luacheck",
    "lua_path": "C:\\Users\\Admin\\AppData\\Local\\Programs\\Lua\\bin\\lua.EXE",
    "luajit_path": null,
    "lupa_available": false,
    "ready": true,
    "selected_backend": "lua"
  },
  "lint": {
    "available": true,
    "command": "C:\\Users\\Admin\\AppData\\Local\\Programs\\Lua\\bin\\lua.EXE -e package.path=[[C:\\Users\\Admin\\AppData\\Roaming\\luarocks\\share\\lua\\5.4\\?.lua;C:\\Users\\Admin\\AppData\\Roaming\\luarocks\\share\\lua\\5.4\\?\\init.lua;]] .. package.path; package.cpath=[[C:\\Users\\Admin\\AppData\\Roaming\\luarocks\\lib\\lua\\5.4\\?.dll;]] .. package.cpath C:\\Users\\Admin\\AppData\\Roaming\\luarocks\\bin\\luacheck C:\\Users\\Admin\\AppData\\Local\\Temp\\lua-agent-tests-eyuacmvf\\target.lua --formatter plain",
    "stderr": "",
    "stdout": "C:\\Users\\Admin\\AppData\\Local\\Temp\\lua-agent-tests-eyuacmvf\\target.lua:123:9: unused loop variable 'i'\nC:\\Users\\Admin\\AppData\\Local\\Temp\\lua-agent-tests-eyuacmvf\\target.lua:215:20: accessing undefined variable 'json'\nC:\\Users\\Admin\\AppData\\Local\\Temp\\lua-agent-tests-eyuacmvf\\target.lua:228:20: accessing undefined variable 'json'",
    "success": false
  },
  "runtime_seconds": 0.0287,
  "smoke_mode": true,
  "stderr": "",
  "stdout": "SMOKE_PASSED",
  "success": true
}
```

## Test Result
```json
{
  "command": "C:\\Users\\Admin\\AppData\\Local\\Programs\\Lua\\bin\\lua.EXE C:\\Users\\Admin\\AppData\\Local\\Temp\\lua-agent-tests-jq5f647d\\test_runner.lua",
  "environment": {
    "backend_preference": "auto",
    "linter": "C:\\Users\\Admin\\AppData\\Roaming\\luarocks\\bin\\luacheck",
    "lua_path": "C:\\Users\\Admin\\AppData\\Local\\Programs\\Lua\\bin\\lua.EXE",
    "luajit_path": null,
    "lupa_available": false,
    "ready": true,
    "selected_backend": "lua"
  },
  "generator_mode": "deterministic",
  "lint": {
    "available": true,
    "command": "C:\\Users\\Admin\\AppData\\Local\\Programs\\Lua\\bin\\lua.EXE -e package.path=[[C:\\Users\\Admin\\AppData\\Roaming\\luarocks\\share\\lua\\5.4\\?.lua;C:\\Users\\Admin\\AppData\\Roaming\\luarocks\\share\\lua\\5.4\\?\\init.lua;]] .. package.path; package.cpath=[[C:\\Users\\Admin\\AppData\\Roaming\\luarocks\\lib\\lua\\5.4\\?.dll;]] .. package.cpath C:\\Users\\Admin\\AppData\\Roaming\\luarocks\\bin\\luacheck C:\\Users\\Admin\\AppData\\Local\\Temp\\lua-agent-tests-jq5f647d\\target.lua --formatter plain",
    "stderr": "",
    "stdout": "C:\\Users\\Admin\\AppData\\Local\\Temp\\lua-agent-tests-jq5f647d\\target.lua:123:9: unused loop variable 'i'\nC:\\Users\\Admin\\AppData\\Local\\Temp\\lua-agent-tests-jq5f647d\\target.lua:215:20: accessing undefined variable 'json'\nC:\\Users\\Admin\\AppData\\Local\\Temp\\lua-agent-tests-jq5f647d\\target.lua:228:20: accessing undefined variable 'json'",
    "success": false
  },
  "runtime_seconds": 0.0261,
  "static_issues": [],
  "stderr": "",
  "stdout": "TESTS_PASSED",
  "success": true,
  "test_cases": [
    "Скрипт должен загрузиться в режиме __test__ без запуска полного CLI-цикла.",
    "После загрузки target должен быть доступен.",
    "У модуля должна быть тестируемая точка входа main."
  ],
  "test_script": "assert(target ~= nil, 'target must be available after loading target.lua')\nassert(type(target.main) == 'function', 'target.main must be a function')\nprint('TESTS_PASSED')"
}
```
