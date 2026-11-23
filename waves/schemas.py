"""Pydantic gates for every record that crosses a wave."""
import datetime as dt
import re
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

TAIL_RE = re.compile(r"^N\d{3}[A-Z]{2}$")


# ------------------------------------------------------------ billing
class EpcrDeid(BaseModel):
    doc_id: str
    transport_id: str
    deid_text: str
    n_redactions: int = Field(ge=0)
    redaction_types: list[str]


class EpcrExtract(BaseModel):
    """Model-facing extraction schema (no numbers are computed, only read)."""
    diagnosis: Optional[str] = None
    instability_evidence: Optional[str] = None
    ground_contraindication: Optional[str] = Field(
        None, description="verbatim span from the chart saying why ground transport was unsafe, or null")
    physician_certification: bool = False
    loaded_miles: Optional[int] = Field(None, ge=0, le=2000)


class EpcrExtractRow(EpcrExtract):
    doc_id: str
    transport_id: str


class NameList(BaseModel):
    names: list[str] = Field(default_factory=list, description="person names exactly as written in the text")


class ClaimReconciliation(BaseModel):
    claim_id: str
    transport_id: str
    payer: str
    transport_date: dt.date
    at_risk: bool
    reason: str
    policy_doc_id: Optional[str] = None
    policy_section: Optional[str] = None
    policy_effective_date: Optional[dt.date] = None
    citation: Optional[list] = None  # [doc_id, section, effective_date]
    failed_requirements: list[str] = Field(default_factory=list)


# ------------------------------------------------------------ ops
class MxClass(BaseModel):
    issue_category: str = Field(min_length=2, max_length=60)
    system: str = Field(min_length=2, max_length=40)
    severity: Literal["AOG", "deferred", "routine"]

    @field_validator("issue_category", "system")
    @classmethod
    def _lower(cls, v):
        return v.strip().lower()


class MxClassRow(MxClass):
    mx_id: str
    tail_number: str
    base_id: str


class DocSection(BaseModel):
    doc_id: str
    dept: str
    title: str
    status: Literal["ACTIVE"]
    effective_date: dt.date
    section: str
    text: str


class AogImpact(BaseModel):
    mx_id: str
    tail_number: str
    base_id: str
    start_ts: dt.datetime
    end_ts: Optional[dt.datetime] = None
    aog_hours: float = Field(ge=0)
    missed_requests: int = Field(ge=0)
    declined_aog_logged: int = Field(ge=0)
    est_lost_transports: int = Field(ge=0)
    value_per_lost_transport: Optional[float] = None
    value_basis: str
    est_lost_revenue: Optional[float] = None


# ------------------------------------------------------------ finance
class LedgerLine(BaseModel):
    base_id: str
    month: dt.date
    line: str
    line_type: Literal["revenue", "cost"]
    actual: float
    planned: Optional[float] = None


class Variance(BaseModel):
    base_id: str
    month: dt.date
    line: str
    line_type: Literal["revenue", "cost"]
    actual: float
    planned: float
    variance: float
    variance_pct: Optional[float] = None
    favorable: bool
    driver: str
    driver_value: Optional[float] = None


# ------------------------------------------------------------ hr
class LicenseStatus(BaseModel):
    crew_id: str
    kind: str
    expires: dt.date
    status: Literal["expired", "expiring_30d", "valid"]
    expires_in_window: bool
    as_of: dt.date


class OvertimeCause(BaseModel):
    cause: Literal["aog", "weather", "staffing", "training", "mission", "scheduled", "other"]
    tail_number: Optional[str] = None
    mentions_aog: bool = False

    @field_validator("tail_number")
    @classmethod
    def _tail(cls, v):
        if v in (None, "", "null"):
            return None
        v = v.strip().upper()
        if not TAIL_RE.match(v):
            raise ValueError(f"not a tail number: {v}")
        return v


class OvertimeCauseRow(OvertimeCause):
    shift_id: str
    crew_id: str
    date: dt.date
    overtime_hours: float = Field(ge=0)
    mx_id: Optional[str] = None


class ComplianceFlag(BaseModel):
    shift_id: str
    crew_id: str
    base_id: Optional[str] = None
    date: dt.date
    flag: Literal["expired_license_on_flown_shift", "duty_limit_breach"]
    detail: str
    citation: Optional[list] = None


class AogOvertime(BaseModel):
    shift_id: str
    crew_id: str
    base_id: Optional[str] = None
    date: dt.date
    mx_id: str
    tail_number: Optional[str] = None
    overtime_hours: float = Field(ge=0)
    ot_rate: Optional[float] = None
    overtime_cost: Optional[float] = None
