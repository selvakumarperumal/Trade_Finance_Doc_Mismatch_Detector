"""The model layer: agents, the graph that orchestrates them, and the facade."""

from Detector.services.agents import AgentRegistry, build_agents, build_limiter, get_agents
from Detector.services.deps import CaseState, DetectorDeps, StageEvent
from Detector.services.graph import case_graph, render_mermaid
from Detector.services.pipeline import DetectorPipeline, ProgressCallback, get_pipeline

__all__ = [
    'AgentRegistry',
    'CaseState',
    'DetectorDeps',
    'DetectorPipeline',
    'ProgressCallback',
    'StageEvent',
    'build_agents',
    'build_limiter',
    'case_graph',
    'get_agents',
    'get_pipeline',
    'render_mermaid',
]
