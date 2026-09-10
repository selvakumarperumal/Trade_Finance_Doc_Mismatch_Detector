"""The model layer: OCR, agents, the graph that orchestrates them, and the facade."""

from Detector.services.agents import AgentRegistry, build_agents, get_agents
from Detector.services.deps import CaseState, DetectorDeps, StageEvent
from Detector.services.graph import case_graph, render_mermaid
from Detector.services.ocr import OcrError, ReadResult, TextractOCR, textract_client
from Detector.services.pipeline import DetectorPipeline, ProgressCallback, get_pipeline
from Detector.services.runner import CaseRunner, TooBusy
from Detector.services.store import CaseExists, CaseNotFound, CaseStore

__all__ = [
    'AgentRegistry',
    'CaseExists',
    'CaseNotFound',
    'CaseRunner',
    'CaseState',
    'CaseStore',
    'DetectorDeps',
    'DetectorPipeline',
    'OcrError',
    'ProgressCallback',
    'ReadResult',
    'StageEvent',
    'TextractOCR',
    'TooBusy',
    'build_agents',
    'case_graph',
    'get_agents',
    'get_pipeline',
    'render_mermaid',
    'textract_client',
]
