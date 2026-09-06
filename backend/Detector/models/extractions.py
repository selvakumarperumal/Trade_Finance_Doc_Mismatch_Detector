"""Typed extraction payloads, one per trade finance document family.

These are the `output_type` of the extraction agents, so every field name and
docstring here is part of the prompt the model sees. Keep them aligned with the
`*_extractor` system prompts in `Config/prompts.yaml`.

Every field is optional on purpose: the extractors are instructed to leave a
field `null` rather than guess, and a missing field is itself a finding the
reconciliation stage can reason about.
"""

from datetime import date
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from Detector.models.enums import DocumentType


class ExtractionBase(BaseModel):
    """Shared configuration for every extraction payload."""

    model_config = ConfigDict(extra='forbid', use_attribute_docstrings=True)


class LineItem(ExtractionBase):
    """A single priced line on a commercial invoice."""

    description: str | None = None
    quantity: Decimal | None = None
    unit: str | None = None
    unit_price: Decimal | None = None
    total_price: Decimal | None = None


class ShippingDetails(ExtractionBase):
    """Carriage details as stated on a commercial invoice."""

    vessel: str | None = None
    voyage: str | None = None
    port_of_loading: str | None = None
    port_of_discharge: str | None = None


class PackageBreakdownEntry(ExtractionBase):
    """One row of a packing list's package breakdown."""

    marks: str | None = None
    description: str | None = None
    package_count: int | None = None
    gross_weight: str | None = None
    net_weight: str | None = None


class LetterOfCredit(ExtractionBase):
    """Structured fields of a documentary credit (MT700 style)."""

    kind: Literal[DocumentType.LETTER_OF_CREDIT] = DocumentType.LETTER_OF_CREDIT

    lc_number: str | None = None
    issuing_bank: str | None = None
    advising_bank: str | None = None
    applicant: str | None = None
    beneficiary: str | None = None
    credit_amount: Decimal | None = None
    currency: str | None = None
    issue_date: date | None = None
    expiry_date: date | None = None
    expiry_place: str | None = None
    latest_shipment_date: date | None = None
    port_of_loading: str | None = None
    port_of_discharge: str | None = None
    place_of_delivery: str | None = None
    description_of_goods: str | None = None
    partial_shipments_allowed: bool | None = None
    transhipment_allowed: bool | None = None
    presentation_period: str | None = None
    """Presentation period as written, e.g. 'within 21 days after shipment date'."""

    presentation_period_days: int | None = None
    """The presentation period reduced to a number of days, when it is expressed that way."""

    tolerance_percent: Decimal | None = None
    """Amount tolerance stated on the credit, e.g. 5 for 'about'/'+/- 5 pct'."""

    required_insurance_percent: Decimal | None = None
    """Minimum insured percentage the credit demands, e.g. 110."""


class CommercialInvoice(ExtractionBase):
    """Structured fields of a commercial invoice."""

    kind: Literal[DocumentType.COMMERCIAL_INVOICE] = DocumentType.COMMERCIAL_INVOICE

    invoice_number: str | None = None
    invoice_date: date | None = None
    lc_reference_number: str | None = None
    purchase_order_number: str | None = None
    seller: str | None = None
    buyer: str | None = None
    currency: str | None = None
    total_amount: Decimal | None = None
    incoterms: str | None = None
    payment_terms: str | None = None
    description_of_goods: str | None = None
    line_items: list[LineItem] = Field(default_factory=list)
    shipping_details: ShippingDetails | None = None


class BillOfLading(ExtractionBase):
    """Structured fields of a bill of lading."""

    kind: Literal[DocumentType.BILL_OF_LADING] = DocumentType.BILL_OF_LADING

    bl_number: str | None = None
    carrier_name: str | None = None
    shipper: str | None = None
    consignee: str | None = None
    notify_party: str | None = None
    vessel_name: str | None = None
    voyage_number: str | None = None
    port_of_loading: str | None = None
    port_of_discharge: str | None = None
    place_of_delivery: str | None = None
    shipment_date: date | None = None
    """On-board date; this is what UCP 600 treats as the date of shipment."""

    description_of_goods: str | None = None
    gross_weight: str | None = None
    net_weight: str | None = None
    package_count: int | None = None
    freight_terms: str | None = None


class PackingList(ExtractionBase):
    """Structured fields of a packing list."""

    kind: Literal[DocumentType.PACKING_LIST] = DocumentType.PACKING_LIST

    packing_list_number: str | None = None
    packing_list_date: date | None = None
    invoice_reference: str | None = None
    lc_reference: str | None = None
    shipper: str | None = None
    consignee: str | None = None
    total_packages: int | None = None
    package_types: str | None = None
    dimensions: str | None = None
    gross_weight: str | None = None
    net_weight: str | None = None
    measurement_cbm: Decimal | None = None
    shipping_marks: str | None = None
    package_breakdown: list[PackageBreakdownEntry] = Field(default_factory=list)


class CertificateOfOrigin(ExtractionBase):
    """Structured fields of a certificate of origin."""

    kind: Literal[DocumentType.CERTIFICATE_OF_ORIGIN] = DocumentType.CERTIFICATE_OF_ORIGIN

    certificate_number: str | None = None
    country_of_origin: str | None = None
    issuing_authority: str | None = None
    issue_date: date | None = None
    exporter: str | None = None
    consignee: str | None = None
    invoice_reference: str | None = None
    description_of_goods: str | None = None
    certified_quantities: str | None = None


class InsuranceCertificate(ExtractionBase):
    """Structured fields of an insurance certificate or policy."""

    kind: Literal[DocumentType.INSURANCE_CERTIFICATE] = DocumentType.INSURANCE_CERTIFICATE

    policy_number: str | None = None
    insurer_name: str | None = None
    insured_party: str | None = None
    insured_amount: Decimal | None = None
    currency: str | None = None
    coverage_terms: str | None = None
    """Coverage clauses as written, e.g. 'Institute Cargo Clauses (A)'."""

    effective_date: date | None = None
    voyage_from: str | None = None
    voyage_to: str | None = None
    claims_payable_at: str | None = None
    claims_agent: str | None = None


class BillOfExchange(ExtractionBase):
    """Structured fields of a bill of exchange (draft)."""

    kind: Literal[DocumentType.BILL_OF_EXCHANGE] = DocumentType.BILL_OF_EXCHANGE

    draft_number: str | None = None
    issue_date: date | None = None
    drawer: str | None = None
    drawee: str | None = None
    payee: str | None = None
    amount: Decimal | None = None
    currency: str | None = None
    tenor: str | None = None
    """Tenor as written, e.g. 'at sight' or '90 days after sight'."""

    lc_reference: str | None = None


class InspectionCertificate(ExtractionBase):
    """Structured fields of an inspection certificate."""

    kind: Literal[DocumentType.INSPECTION_CERTIFICATE] = DocumentType.INSPECTION_CERTIFICATE

    certificate_number: str | None = None
    inspection_agency: str | None = None
    inspection_date: date | None = None
    issue_date: date | None = None
    applicant: str | None = None
    inspected_party: str | None = None
    description_of_goods: str | None = None
    inspection_scope: str | None = None
    inspection_result: str | None = None
    """The verdict as written, e.g. 'Pass', 'Fail', 'Satisfactory'."""

    summary_findings: str | None = None


ExtractionPayload = Annotated[
    LetterOfCredit
    | CommercialInvoice
    | BillOfLading
    | PackingList
    | CertificateOfOrigin
    | InsuranceCertificate
    | BillOfExchange
    | InspectionCertificate,
    Field(discriminator='kind'),
]
"""Any extraction payload, tagged by document type so it round-trips through JSON."""


EXTRACTION_PAYLOAD_TYPES: dict[DocumentType, type[ExtractionBase]] = {
    DocumentType.LETTER_OF_CREDIT: LetterOfCredit,
    DocumentType.COMMERCIAL_INVOICE: CommercialInvoice,
    DocumentType.BILL_OF_LADING: BillOfLading,
    DocumentType.PACKING_LIST: PackingList,
    DocumentType.CERTIFICATE_OF_ORIGIN: CertificateOfOrigin,
    DocumentType.INSURANCE_CERTIFICATE: InsuranceCertificate,
    DocumentType.BILL_OF_EXCHANGE: BillOfExchange,
    DocumentType.INSPECTION_CERTIFICATE: InspectionCertificate,
}
"""Maps a classified document type to the payload the extractor must return."""
