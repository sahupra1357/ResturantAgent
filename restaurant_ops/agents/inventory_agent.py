"""Inventory Agent node: builds the prompt context, runs the live agent or its mock policy, returns its report."""
from __future__ import annotations

from restaurant_ops.agents import base, mock_policies

NAME = "inventory"
TASK = "Assess waste risk and stock variances for the next 3 days and propose discards and variance flags."


def node(state: dict) -> dict:
    base.bind(state)
    rep = base.run_specialist(NAME, state, TASK, mock_policies.inventory)
    return base.report_update(state, NAME, rep)
