"""Menu Agent node: builds the prompt context, runs the live agent or its mock policy, returns its report."""
from __future__ import annotations

from restaurant_ops.agents import base, mock_policies

NAME = "menu"
TASK = "Turn at-risk lots into specials and 86 dishes that stock cannot cover today."


def node(state: dict) -> dict:
    base.bind(state)
    rep = base.run_specialist(NAME, state, TASK, mock_policies.menu)
    return base.report_update(state, NAME, rep)
