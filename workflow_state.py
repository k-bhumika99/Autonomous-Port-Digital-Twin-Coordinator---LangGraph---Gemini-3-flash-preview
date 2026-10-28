"""Typed shared state for the LangGraph workflow and the structured agent outputs."""
from __future__ import annotations

import operator
from typing import Annotated, Any, Literal, TypedDict

from pydantic import BaseModel, Field

AgentStatus = Literal["pending", "running", "completed", "failed", "blocked"]

# Order and metadata of the six agents (used by the UI, the workflow and the database).
AGENTS = {
    "ship": {"name": "Ship Operations Agent", "emoji": "ship", "route": "/agents/ship",
             "role": "Analyses arrivals, berth queues and berth conflicts."},
    "crane": {"name": "Crane & Equipment Agent", "emoji": "crane", "route": "/agents/crane",
              "role": "Checks crane availability, workload and breakdowns."},
    "yard": {"name": "Container Yard Agent", "emoji": "container", "route": "/agents/yard",
             "role": "Tracks yard occupancy, overflow risk and container flows."},
    "truck": {"name": "Truck & Logistics Agent", "emoji": "truck", "route": "/agents/truck",
              "role": "Analyses truck queues, gate bottlenecks and collection timing."},
    "planning": {"name": "Planning & Optimization Agent", "emoji": "brain", "route": "/agents/planning",
                 "role": "Builds candidate plans with OR-Tools and tests them in the Digital Twin."},
    "validation": {"name": "Validation & Decision Agent", "emoji": "check", "route": "/agents/validation",
                   "role": "Checks hard constraints, verifies metrics and selects a plan."},
}
AGENT_ORDER = list(AGENTS)


class Finding(BaseModel):
    severity: Literal["info", "warning", "critical"] = "info"
    title: str
    detail: str = ""


class Explanation(BaseModel):
    """Plain-language explanation. `text` is ONLY ever filled by Gemini; if Gemini is not
    available `text` stays None and `status` says why (we never fabricate an AI answer)."""
    text: str | None = None
    status: Literal["ok", "unavailable", "ungrounded", "error", "skipped"] = "skipped"
    reason: str = ""


class AgentReport(BaseModel):
    agent_key: str
    agent_name: str
    role: str
    status: AgentStatus = "completed"
    summary: str = ""                                   # deterministic summary (always present)
    findings: list[Finding] = Field(default_factory=list)
    metrics: dict[str, Any] = Field(default_factory=dict)
    tables: dict[str, list[dict]] = Field(default_factory=dict)
    charts: dict[str, Any] = Field(default_factory=dict)
    proposal: dict[str, Any] = Field(default_factory=dict)         # plan levers suggested by the agent
    proposal_impact: dict[str, Any] = Field(default_factory=dict)  # simulated effect of that proposal
    inputs: dict[str, Any] = Field(default_factory=dict)          # what the agent received
    explanation: Explanation = Field(default_factory=Explanation)
    warnings: list[str] = Field(default_factory=list)
    log: list[str] = Field(default_factory=list)
    error: str | None = None


def merge_dicts(left: dict | None, right: dict | None) -> dict:
    """Reducer: parallel LangGraph nodes may each add keys to the same dict safely."""
    out = dict(left or {})
    out.update(right or {})
    return out


class WorkflowState(TypedDict, total=False):
    # identity & inputs
    sim_id: int
    config: dict
    scenario: dict
    settings: dict                     # weights, retry limits, horizon
    # derived data
    effects: dict
    disruption_brief: dict
    baseline: dict                     # full simulation result of Plan A
    # agent outputs (merged across parallel nodes)
    reports: Annotated[dict, merge_dicts]
    candidates: Annotated[dict, merge_dicts]    # label -> {"plan": {...}, "result": {...}}
    # control flow
    attempt: int
    feedback: list
    validation: dict
    final: dict
    status: str
    failed_agents: Annotated[list, operator.add]
    log: Annotated[list, operator.add]
