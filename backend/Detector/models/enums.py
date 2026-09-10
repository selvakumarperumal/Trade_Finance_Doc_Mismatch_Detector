"""Domain enumerations shared by the extraction and reconciliation models."""

from enum import StrEnum


class DocumentType(StrEnum):
    """The trade finance document families the detector understands."""

    LETTER_OF_CREDIT = 'letter_of_credit'
    COMMERCIAL_INVOICE = 'commercial_invoice'
    BILL_OF_LADING = 'bill_of_lading'
    PACKING_LIST = 'packing_list'
    CERTIFICATE_OF_ORIGIN = 'certificate_of_origin'
    INSURANCE_CERTIFICATE = 'insurance_certificate'
    BILL_OF_EXCHANGE = 'bill_of_exchange'
    INSPECTION_CERTIFICATE = 'inspection_certificate'
    UNKNOWN = 'unknown'


class Severity(StrEnum):
    """How badly a discrepancy hurts the presentation."""

    CRITICAL = 'critical'
    """A documentary discrepancy under LC rules: the bank would refuse."""

    WARNING = 'warning'
    """An ambiguity a human examiner should look at."""

    INFO = 'info'
    """A formatting difference or informational observation."""


class CaseStatus(StrEnum):
    """The overall verdict for a presentation."""

    CLEAN = 'clean'
    NEEDS_REVIEW = 'needs_review'
    BLOCKED = 'blocked'


class JobStatus(StrEnum):
    """Where a submitted case has got to.

    Distinct from `CaseStatus`, which is the compliance verdict. A case can be
    `succeeded` here and `blocked` there: the analysis ran to completion and its
    answer was that the bank would refuse.
    """

    QUEUED = 'queued'
    """Accepted, waiting for a slot."""

    RUNNING = 'running'
    """Documents are being read, classified and extracted."""

    SUCCEEDED = 'succeeded'
    """Finished; `result` holds the report."""

    FAILED = 'failed'
    """Stopped on an error the pipeline could not degrade around; `error` says why."""

    CANCELLED = 'cancelled'
    """Abandoned by the caller, or by the process shutting down."""

    @property
    def is_terminal(self) -> bool:
        """Whether this case will not change state again."""
        return self in {JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED}
