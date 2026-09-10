"""Trade finance document mismatch detector.

Two layers, and they only meet in one place. `Detector.services` is the model layer:
hand `DetectorPipeline` a `CaseInput`, get a `CaseResult` back, with no HTTP anywhere
in sight. `Detector.api` is the FastAPI application that exposes it.

```python
from Detector.services.pipeline import DetectorPipeline

async with DetectorPipeline.open() as pipeline:
    result = await pipeline.run(case_input)
```
"""
