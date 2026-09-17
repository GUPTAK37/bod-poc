"""BoD parameter + filter model.

Mirrors the 11 user-facing Tableau parameters plus every dimensional filter
listed in the Filter and Parameter Guide (section 3.1). One `BoDFilters`
instance is serialized into a `dcc.Store` and travels through every callback.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import date
from typing import Any, Literal

# ---- Domain constants (mirror Tableau parameter allowed values) ------------

ENCOUNTER_WINDOWS = {"1": "30-day window", "2": "90-day window"}
METRIC_MODES = {1.0: "# Treatment Utilization", 2.0: "% Treatment Utilization"}
GRANULARITIES = {"1": "National", "2": "Region", "3": "State", "4": "MSA"}
TIME_LEVELS = {"month": "Monthly", "quarter": "Quarterly", "year": "Yearly"}
YES_NO = ["Yes", "No"]

VISIT_TYPES = [
    "OFFICE VISIT", "TELEHEALTH VISIT", "UC VISIT",
    "ED VISIT", "HOSPITALIZATION", "LTAC/SNF VISIT", "NO VISIT",
]

# ---- Filter model ----------------------------------------------------------

@dataclass
class BoDFilters:
    # Global parameters
    encounter_window: Literal["1", "2"] = "1"
    metric_mode: float = 2.0
    granularity: Literal["1", "2", "3", "4"] = "3"
    # Granularity Value — the actual REGION/STATE/MSA values the user picked.
    granularity_value: list[str] = field(default_factory=list)
    start_month: date = date(2025, 8, 1)
    end_month: date = date(2026, 7, 1)
    time_level: Literal["month", "quarter", "year"] = "quarter"
    reference_line: Literal["Yes", "No"] = "No"
    # Multi-select. Empty list == "All HR Conditions" (no HR filter applied).
    hr_condition: list[str] = field(default_factory=list)

    # Demographic filters (multi-select). Empty list == no restriction.
    age_group: list[str] = field(default_factory=list)
    patient_gender: list[str] = field(default_factory=list)
    payer_type: list[str] = field(default_factory=list)
    area_type: list[str] = field(default_factory=list)

    # Geographic filters
    region: list[str] = field(default_factory=list)
    state: list[str] = field(default_factory=list)
    msa: list[str] = field(default_factory=list)

    # Provider filters
    hcp_primary_specialty: list[str] = field(default_factory=list)
    specialty_group: list[str] = field(default_factory=list)

    # Account filters
    parent_id: list[str] = field(default_factory=list)
    child_id: list[str] = field(default_factory=list)
    grouped_account: list[str] = field(default_factory=list)

    # -----------------------------------------------------------------
    # Section-8 / Section-9 local filters (the HR-condition bars have
    # their own filter shelf in the workbook: VISIT type + specific HR
    # conditions + "Top-N" toggle).
    # -----------------------------------------------------------------
    s8_visit_types: list[str] = field(default_factory=lambda: ["ED VISIT"])
    # Filter 2 in the shelf. Multi-select against the workbook's
    # `High Risk Top 7` calc, whose values are 'Top 7 HR Conditions' or an
    # individual (non-top-N) HR name. Default: the rollup category.
    s8_hr_conditions: list[str] = field(default_factory=lambda: ["Top 7 HR Conditions"])
    # Filter 3 — multi-select over the top-N HR names + 'Other Conditions'.
    # Empty list is treated as "all".
    s8_top7_items: list[str] = field(default_factory=list)
    s8_top_n: int = 7                                          # 7 = "Top 7 HR"

    # -----------------------------------------------------------------
    # Section-10 local VISIT filter (multi-select against the emitted
    # VISIT_FILTER (Covid) categories: HOSPITALIZATION, ED VISIT, UC VISIT,
    # LTAC/SNF VISIT, OFFICE VISIT, TELEHEALTH VISIT, OTHERS, PHARMACY).
    # Empty list = "(All)" (no filter).
    # -----------------------------------------------------------------
    s10_visit_types: list[str] = field(default_factory=list)

    # ------------------------------------------------------------------
    def to_store(self) -> dict[str, Any]:
        d = asdict(self)
        d["start_month"] = self.start_month.isoformat()
        d["end_month"] = self.end_month.isoformat()
        return d

    @classmethod
    def from_store(cls, d: dict[str, Any] | None) -> "BoDFilters":
        if not d:
            return cls()
        d = dict(d)
        d["start_month"] = date.fromisoformat(d["start_month"])
        d["end_month"] = date.fromisoformat(d["end_month"])
        return cls(**d)


# ---- SQL fragment builders -------------------------------------------------

_COHORT_INTEGRITY = (
    "MEDICAL_PATIENT_COHORT_FLAG = 1",
    "INCOMPLETE_EPISODE_FLAG = 0",
    "EPISODE_PER_QUARTER_FLAG = '1'",
)


def encounter_window_col(f: BoDFilters) -> str:
    return "ENCOUNTER_IN_30_D_WINDOW" if f.encounter_window == "1" else "ENCOUNTER_IN_90_D_WINDOW"


def no_visit_col(f: BoDFilters) -> str:
    """Return the workbook's `NO_VISIT_*` column matching the selected window."""
    return "NO_VISIT_30" if f.encounter_window == "1" else "NO_VISIT_90"


def time_bucket(col: str, level: str) -> str:
    # Cast to DATE to defuse TIMESTAMP_TZ DST-split buckets.
    return f"DATE_TRUNC('{level}', {col}::date)"


def geo_col(f: BoDFilters) -> str:
    return {"1": "'National'", "2": "REGION", "3": "STATE", "4": "MSA"}[f.granularity]


def benchmark_col(level: str, mode: float) -> str:
    suffix = "" if mode == 2.0 else "_NUMBERS"
    return {"month": "MONTHLY", "quarter": "QUARTERLY", "year": "YEARLY"}[level] + f"_BENCHMARK{suffix}"


def _in_clause(col: str, vals: list[str], binds: dict, prefix: str) -> str | None:
    """Build a `col IN (...)` predicate. `col` may be either a physical
    column name or a SQL expression (both are safe here)."""
    # Strip Tableau-style "(All)" sentinel — a clientside callback normally
    # collapses it to [] before it reaches here, but keep as a safety net.
    vals = [v for v in (vals or []) if v not in ("__ALL__", "(All)")]
    if not vals:
        return None
    keys = []
    for i, v in enumerate(vals):
        k = f"{prefix}_{i}"
        binds[k] = v
        keys.append(f"%({k})s")
    return f"{col} IN ({', '.join(keys)})"


# ---------------------------------------------------------------------------
# Tableau-parity column expressions
# ---------------------------------------------------------------------------

# `Provider Primary Specialty` calc from the workbook. The physical column
# `HCP_PRIMARY_SPECIALTY` contains raw numeric specialty codes (104, 105, ...)
# alongside clean names — Tableau uses `REPORTING_PRIMARY_SPECIALTY` bound
# through a grouping bin that maps NULL + "Acute Care Medicine" + "OTHER"
# → "OTHER", then `IFNULL(UPPER(...), 'UNSPECIFIED')`.
PROVIDER_PRIMARY_SPECIALTY_EXPR = (
    "IFNULL(UPPER("
    "  CASE WHEN REPORTING_PRIMARY_SPECIALTY IN ('Acute Care Medicine','OTHER') "
    "         OR REPORTING_PRIMARY_SPECIALTY IS NULL "
    "       THEN 'OTHER' "
    "       ELSE REPORTING_PRIMARY_SPECIALTY "
    "  END"
    "), 'UNSPECIFIED')"
)

# `Provider Specialty Group` calc from the workbook. Similar pattern: bin
# NULL + "OTHER" → "OTHER", then `IFNULL(..., 'UNSPECIFIED')` (no UPPER).
PROVIDER_SPECIALTY_GROUP_EXPR = (
    "IFNULL("
    "  CASE WHEN SPECIALTY_GROUP IN ('OTHER') OR SPECIALTY_GROUP IS NULL "
    "       THEN 'OTHER' "
    "       ELSE SPECIALTY_GROUP "
    "  END"
    ", 'UNSPECIFIED')"
)

# `Sex` calc from the workbook — maps single-letter PATIENT_GENDER to
# FEMALE / MALE / UNSPECIFIED so filter options match Tableau exactly.
GENDER_EXPR = (
    "CASE WHEN PATIENT_GENDER = 'F' THEN 'FEMALE' "
    "     WHEN PATIENT_GENDER = 'M' THEN 'MALE' "
    "     WHEN PATIENT_GENDER = 'U' THEN 'UNSPECIFIED' "
    "     ELSE 'UNSPECIFIED' END"
)

# `Parent Account` calc from the workbook:
#   IFNULL(PARENT_ID + ' - ' + PARENT_NAME + ',' + ' ' + PARENT_CITY + ','
#          + ' ' + PARENT_STATE, 'UNSPECIFIED')
# Snowflake string concat (||) propagates NULL, so any missing component
# yields NULL → 'UNSPECIFIED' (matches Tableau's `+` semantics).
PARENT_ACCOUNT_EXPR = (
    "IFNULL("
    "  PARENT_ID || ' - ' || PARENT_NAME || ', ' || PARENT_CITY || ', ' || PARENT_STATE"
    ", 'UNSPECIFIED')"
)

# `Child Account` calc from the workbook:
#   IFNULL(CHILD_ID + ' - ' + CHILD_NAME + ', ' + CHILD_ZIP + ', '
#          + CHILD_CITY + ', ' + CHILD_COUNTY + ', ' + CHILD_STATE, 'UNSPECIFIED')
CHILD_ACCOUNT_EXPR = (
    "IFNULL("
    "  CHILD_ID || ' - ' || CHILD_NAME || ', ' || CHILD_ZIP || ', '"
    "  || CHILD_CITY || ', ' || CHILD_COUNTY || ', ' || CHILD_STATE"
    ", 'UNSPECIFIED')"
)


def granularity_value_expr(granularity: str) -> str:
    """`Select Granularity Main` calc — value column depends on the current
    granularity parameter selection (matches the workbook's CASE calc)."""
    return {
        "1": "'National'",
        "2": "REGION",
        "3": "IFF(STATE = 'NA', NULL, STATE)",
        "4": "IFNULL(MSA, 'UNSPECIFIED')",
    }.get(granularity, "REGION")


def where_clause(f: BoDFilters, extra: list[str] | None = None,
                 bypass_hr: bool = False) -> tuple[str, dict[str, Any]]:
    """Return `(WHERE ..., binds)` including always-on cohort integrity flags.

    HR handling:
      - `bypass_hr=True`  → no HIGH_RISK_CONDITION predicate (sections 8/9).
      - `hr_condition=[]` → filter to the `ALL HR CONDITIONS` rollup so each
        encounter appears exactly once (huge speed-up over scanning the
        HR-unpivoted fanout, while giving identical distinct-episode counts).
      - non-empty list     → `HIGH_RISK_CONDITION IN (...)`.
    """
    binds: dict[str, Any] = {}
    parts: list[str] = list(_COHORT_INTEGRITY)

    parts.append(
        f"EPISODE_START_DATE >= %(start_month)s AND EPISODE_START_DATE < DATEADD(month, 1, %(end_month)s)"
    )
    binds["start_month"] = f.start_month
    binds["end_month"] = f.end_month

    for col, vals, key in [
        ("AGE_GROUP", f.age_group, "age"),
        (GENDER_EXPR, f.patient_gender, "gen"),
        ("PAYER_TYPE", f.payer_type, "pay"),
        ("AREA_TYPE", f.area_type, "area"),
        ("REGION", f.region, "reg"),
        ("STATE", f.state, "st"),
        ("MSA", f.msa, "msa"),
        (PROVIDER_PRIMARY_SPECIALTY_EXPR, f.hcp_primary_specialty, "spec"),
        (PROVIDER_SPECIALTY_GROUP_EXPR, f.specialty_group, "sg"),
        (PARENT_ACCOUNT_EXPR, f.parent_id, "pid"),
        (CHILD_ACCOUNT_EXPR, f.child_id, "cid"),
        ("GROUPED_ACCOUNT", f.grouped_account, "gacc"),
    ]:
        frag = _in_clause(col, vals, binds, key)
        if frag:
            parts.append(frag)

    # Granularity Value filter — the column varies with the granularity
    # parameter (matches Tableau's `Select Granurality Main` calc).
    if getattr(f, "granularity_value", None):
        gv_expr = granularity_value_expr(f.granularity)
        frag = _in_clause(gv_expr, f.granularity_value, binds, "gv")
        if frag:
            parts.append(frag)

    # High-risk condition
    if not bypass_hr:
        if f.hr_condition:
            hr_frag = _in_clause("HIGH_RISK_CONDITION", f.hr_condition, binds, "hr")
            parts.append(hr_frag)
        else:
            # Rollup row exists once per encounter — huge speedup over the fanout.
            parts.append("HIGH_RISK_CONDITION = 'ALL HR CONDITIONS'")

    if extra:
        parts.extend(extra)
    return "WHERE " + " AND ".join(parts), binds
