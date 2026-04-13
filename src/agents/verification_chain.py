"""Registry for the new standalone verification chain.

This file does not switch the active pipeline yet.
It only declares the order and node factories for the new verifier chain
plus the shared post-verification fixer node.
"""

from __future__ import annotations

from typing import Any, TypedDict

from src.agents.contract_verifier import create_contract_verifier_node
from src.agents.robustness_verifier import create_robustness_verifier_node
from src.agents.runtime_state_verifier import create_runtime_state_verifier_node
from src.agents.semantic_logic_verifier import create_semantic_logic_verifier_node
from src.agents.shape_type_verifier import create_shape_type_verifier_node
from src.agents.universal_verification_fixer import create_universal_verification_fixer_node
from src.core.llm import LLMProvider


class VerificationStageSpec(TypedDict):
    node_name: str
    verifier_name: str
    default_max_fix_iterations: int


def get_verification_chain_specs() -> list[VerificationStageSpec]:
    return [
        {
            "node_name": "verify_contract",
            "verifier_name": "ContractVerifier",
            "default_max_fix_iterations": 1,
        },
        {
            "node_name": "verify_shape_type",
            "verifier_name": "ShapeTypeVerifier",
            "default_max_fix_iterations": 1,
        },
        {
            "node_name": "verify_semantic_logic",
            "verifier_name": "SemanticLogicVerifier",
            "default_max_fix_iterations": 1,
        },
        {
            "node_name": "verify_runtime_state",
            "verifier_name": "RuntimeStateVerifier",
            "default_max_fix_iterations": 1,
        },
        {
            "node_name": "verify_robustness",
            "verifier_name": "RobustnessVerifier",
            "default_max_fix_iterations": 1,
        },
    ]


def create_verification_chain_nodes(llm: LLMProvider) -> dict[str, Any]:
    return {
        "verify_contract": create_contract_verifier_node(llm),
        "verify_shape_type": create_shape_type_verifier_node(llm),
        "verify_semantic_logic": create_semantic_logic_verifier_node(llm),
        "verify_runtime_state": create_runtime_state_verifier_node(llm),
        "verify_robustness": create_robustness_verifier_node(llm),
        "fix_verification_issue": create_universal_verification_fixer_node(llm),
    }
