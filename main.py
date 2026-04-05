
import argparse
import json

from langgraph.errors import GraphRecursionError

from config import AGENT_ROLES, ModelConfig, RuntimeConfig
from factory import create_registry, list_available_versions
from graph import build_graph
from state import STATUS_FAILED, STATUS_FINALIZED, STATUS_NEW_TASK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Локальная агентская система для генерации и проверки Lua-скриптов."
    )
    parser.add_argument("--prompt", help="Пользовательский запрос для генерации Lua-скрипта.")
    parser.add_argument("--model", help="Имя модели LM Studio.")
    parser.add_argument("--base-url", help="OpenAI-совместимый URL локальной модели.")
    parser.add_argument(
        "--lua-backend",
        choices=["auto", "lua", "luajit", "lupa"],
        help="Какой Lua backend использовать для исполнения и тестов.",
    )
    parser.add_argument("--lua-path", help="Путь до бинарника lua.")
    parser.add_argument("--luajit-path", help="Путь до бинарника luajit.")
    parser.add_argument("--luacheck-path", help="Путь до бинарника luacheck.")
    parser.add_argument(
        "--max-attempts",
        type=int,
        help="Максимальное число попыток ремонта кода.",
    )
    parser.add_argument(
        "--agent-version",
        action="append",
        default=[],
        metavar="ROLE=VERSION",
        help="Переопределение версии агента, например parse_task=v2",
    )
    parser.add_argument(
        "--list-versions",
        action="store_true",
        help="Показать доступные версии агентов и выйти.",
    )
    parser.add_argument(
        "--show-state",
        action="store_true",
        help="Напечатать полное финальное состояние графа.",
    )
    parser.add_argument(
        "--check-runtime",
        action="store_true",
        help="Показать обнаруженные Lua runtime-инструменты и выйти.",
    )
    return parser


def parse_agent_version_overrides(entries: list[str]) -> dict[str, str]:
    overrides: dict[str, str] = {}
    for entry in entries:
        if "=" not in entry:
            raise SystemExit(
                f"Некорректный формат --agent-version: {entry}. Ожидается ROLE=VERSION."
            )
        role, version = entry.split("=", 1)
        role = role.strip()
        version = version.strip()
        if role not in AGENT_ROLES:
            raise SystemExit(f"Неизвестная роль агента: {role}")
        if not version:
            raise SystemExit(f"Не указана версия для роли: {role}")
        overrides[role] = version
    return overrides


def print_available_versions() -> None:
    versions = list_available_versions()
    print("Доступные версии агентов:")
    for role, available_versions in versions.items():
        print(f"- {role}: {', '.join(available_versions) if available_versions else 'нет файлов'}")


def compute_recursion_limit(max_attempts: int) -> int:
    safe_attempts = max(1, int(max_attempts))
    return max(50, safe_attempts * 8 + 20)


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.list_versions:
        print_available_versions()
        return

    prompt = args.prompt.strip() if args.prompt else input("Введите prompt для Lua-скрипта: ").strip()
    if not prompt:
        raise SystemExit("Пустой prompt. Остановлено.")

    base_model_config = ModelConfig()
    base_runtime_config = RuntimeConfig()
    model_config = ModelConfig(
        base_url=args.base_url or base_model_config.base_url,
        model=args.model or base_model_config.model,
        api_key=base_model_config.api_key,
        timeout_seconds=base_model_config.timeout_seconds,
        temperature=base_model_config.temperature,
    )
    runtime_config = RuntimeConfig(
        artifacts_dir=base_runtime_config.artifacts_dir,
        max_attempts=args.max_attempts or base_runtime_config.max_attempts,
        execution_timeout_seconds=base_runtime_config.execution_timeout_seconds,
        lua_backend=args.lua_backend or base_runtime_config.lua_backend,
        lua_path=args.lua_path or base_runtime_config.lua_path,
        luajit_path=args.luajit_path or base_runtime_config.luajit_path,
        luacheck_path=args.luacheck_path or base_runtime_config.luacheck_path,
    )

    registry = create_registry(
        agent_versions=parse_agent_version_overrides(args.agent_version),
        model_config=model_config,
        runtime_config=runtime_config,
    )

    if args.check_runtime:
        print(json.dumps(registry.execute_code.lua_toolchain.describe_environment(), ensure_ascii=False, indent=2))
        return

    graph = build_graph(registry)

    initial_state = {
        "user_prompt": prompt,
        "status": STATUS_NEW_TASK,
        "max_attempts": runtime_config.max_attempts,
    }
    invoke_config = {
        "recursion_limit": compute_recursion_limit(runtime_config.max_attempts),
    }

    try:
        result = graph.invoke(initial_state, config=invoke_config)
    except GraphRecursionError as exc:
        result = {
            **initial_state,
            "status": STATUS_FAILED,
            "final_artifact": {
                "failure_reason": (
                    "The LangGraph recursion limit was reached before the pipeline converged. "
                    f"Configured recursion_limit={invoke_config['recursion_limit']}. "
                    f"Original error: {exc}"
                ),
            },
        }

    print(f"Status: {result.get('status')}")
    if result.get("status") == STATUS_FINALIZED:
        artifact = result.get("final_artifact", {})
        print(f"Task goal: {artifact.get('task_goal')}")
        validation = artifact.get("validation_summary", {})
        print(f"Execution: {validation.get('execution')}")
        print(f"Tests: {validation.get('tests')}")
        if artifact.get("artifact_dir"):
            print(f"Saved to: {artifact.get('artifact_dir')}")
    elif result.get("status") == STATUS_FAILED:
        artifact = result.get("final_artifact", {})
        print(f"Failure reason: {artifact.get('failure_reason', 'Unknown error')}")

    if args.show_state:
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
