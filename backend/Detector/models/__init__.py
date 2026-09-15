"""Domain models: what a document is, what was extracted from it, what disagreed.

Import from the module that defines what you need:

    enums.py           the four vocabularies: document type, severity, verdict, job status
    extractions.py     one typed payload per document family — the extractors' output
    documents.py       a document in flight: raw -> classified -> extracted
    reconciliation.py  findings, and the verdict over a whole presentation
    jobs.py            a submitted case as a record the API hands back

This package re-exports nothing on purpose. A second list of names is one more thing to
keep in step, and `from Detector.models.documents import CaseInput` already says where to
look when you meet `CaseInput` somewhere else.
"""
