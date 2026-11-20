"""Automated evaluation dataset, two-layer grader, and benchmark runner."""

from enterprise_ops_agent.evals.dataset import (
    EvalCase,
    EvalExpectation,
    build_golden_eval_cases,
)
from enterprise_ops_agent.evals.grader import CaseEvalResult, JudgeVerdict, grade_case
from enterprise_ops_agent.evals.runner import EvalRunner, SuiteReport

__all__ = [
    "CaseEvalResult",
    "EvalCase",
    "EvalExpectation",
    "EvalRunner",
    "JudgeVerdict",
    "SuiteReport",
    "build_golden_eval_cases",
    "grade_case",
]
