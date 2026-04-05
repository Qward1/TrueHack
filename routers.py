from state import STATUS_FAILED


def route_after_execute(state):
    if state.get("status") == STATUS_FAILED:
        return "to_failed"
    return "to_test" if state.get("execution_ok") else "to_repair"


def route_after_generate(state):
    return "to_failed" if state.get("status") == STATUS_FAILED else "to_execute"


def route_after_test(state):
    if state.get("status") == STATUS_FAILED:
        return "to_failed"
    return "to_finalize" if state.get("tests_passed") else "to_repair"


def route_after_repair(state):
    return "to_failed" if state.get("status") == STATUS_FAILED else "to_execute"
