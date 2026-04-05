from dataclasses import dataclass
from agents.base import BaseAgent

@dataclass
class AgentRegistry:
    parse_task: BaseAgent
    plan_task: BaseAgent
    generate_code: BaseAgent
    execute_code: BaseAgent
    test_code: BaseAgent
    repair_code: BaseAgent
    finalize_artifact: BaseAgent
