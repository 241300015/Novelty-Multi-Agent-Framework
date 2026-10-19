"""Shared constructors for standalone experiment scopes."""

from ..schemas import SearchConcept, SearchPlan, SearchStrategy


def minimal_search_plan(task_id: str, novelty_point_id: str) -> SearchPlan:
    """Return a bounded plan for experiments that exercise non-Planner components."""

    return SearchPlan(
        task_id=task_id,
        novelty_point_id=novelty_point_id,
        concepts=[SearchConcept(concept_id="C1", name="experiment", terms=["experiment"])],
        strategies=[
            SearchStrategy(strategy_id="S1", level="strict", expression="C1")
        ],
    )
