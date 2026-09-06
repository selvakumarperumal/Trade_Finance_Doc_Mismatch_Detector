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
