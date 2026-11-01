"""Purchasing Agent node: builds the prompt context, runs the live agent or its mock policy, returns its report."""
from __future__ import annotations

from restaurant_ops.agents import base, mock_policies

NAME = "purchasing"
TASK = "Plan supplier orders for the next 3 days with the optimizer and handle any supplier problem named in the trigger."


def node(state: dict) -> dict:
    base.bind(state)
    rep = base.run_specialist(NAME, state, TASK, mock_policies.purchasing)
    return base.report_update(state, NAME, rep)
