"""Prep Planner node: builds the prompt context, runs the live agent or its mock policy, returns its report."""
from __future__ import annotations

from restaurant_ops.agents import base, mock_policies

NAME = "prep"
TASK = "Produce today's prep list per meal period from the final forecast, respecting station capacity."


def node(state: dict) -> dict:
    base.bind(state)
    rep = base.run_specialist(NAME, state, TASK, mock_policies.prep)
    return base.report_update(state, NAME, rep)
