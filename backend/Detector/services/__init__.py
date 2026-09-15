"""The model layer: OCR, agents, the graph that orchestrates them, and the facade.

Import from the module that defines what you need:

    pipeline.py  DetectorPipeline — hand it a CaseInput, get a CaseResult back
    runner.py    CaseRunner — runs cases in the background, and says no when full
    store.py     CaseStore — where a case lives between the 202 and its collection
    graph.py     the Pydantic Graph itself: ingest, fan out, classify, extract, reconcile
    agents.py    the Pydantic AI agents, built once and shared
    ocr.py       Textract, one page at a time
    deps.py      what every graph node is handed: agents, settings, per-case state

Nothing here imports FastAPI, which is why the same engine serves the API, the notebook
and the tests. This package re-exports nothing on purpose — the module name is the
better address.
"""
