"""SQL builders for every section of the BoD tab.

Follow-up definition (matches the Tableau workbook — verified 1:1 for Q3 2025
through Q2 2026 with defaults):

    row-level FU  ⇔  NO_VISIT_30 <> 'NO VISIT'  AND  ENCOUNTER_IN_30_D_WINDOW = 1
                     (use NO_VISIT_90 / ENCOUNTER_IN_90_D_WINDOW for 90-day)
    episode-level FU  =  any of the episode's rows is row-level FU.

`NO_VISIT_30` alone is not enough — it labels the raw VISIT for encounters
that fall in the window but is also non-'NO VISIT' for rows outside the
window. The `ENCOUNTER_IN_30_D_WINDOW = 1` guard is required.

All patient/episode counts use COUNT(DISTINCT ...) to defuse the HR-unpivot
fanout. Time buckets cast EPISODE_START_DATE to DATE so TIMESTAMP_TZ DST
does not split quarters.
"""

from __future__ import annotations

from typing import Any

from .params import (
    BoDFilters,
    encounter_window_col,
    no_visit_col,
    time_bucket,
    where_clause,
)
from .snowflake_client import FACT_TABLE


# ==========================================================================
# Data refresh + headline #Episodes
# ==========================================================================

def q_data_refresh_date() -> tuple[str, dict[str, Any]]:
    return f"SELECT MAX(LAST_REFRESH_DATE) AS D FROM {FACT_TABLE}", {}


def q_headline_counts(f: BoDFilters) -> tuple[str, dict[str, Any]]:
    where, binds = where_clause(f)
    sql = f"SELECT COUNT(DISTINCT EPISODE_ID) AS EPISODES FROM {FACT_TABLE} {where}"
    return sql, binds


# ==========================================================================
# Sections 1 / 2 / 3 — episode-level follow-up rate
# ==========================================================================

def _fu_predicate(f: BoDFilters) -> str:
    """SQL fragment that is TRUE for rows classified as a Follow-up Visit."""
    return f"{no_visit_col(f)} <> 'NO VISIT' AND {encounter_window_col(f)} = 1"


def _episode_cte(f: BoDFilters, group_by_cohort: bool, group_by_bucket: bool
                  ) -> tuple[str, dict[str, Any]]:
    where, binds = where_clause(f)
    fu = _fu_predicate(f)
    bucket_expr = time_bucket("EPISODE_START_DATE", f.time_level)
    cohort_expr = "CASE WHEN COVID_TX_FLAG=1 THEN 'Treated' ELSE 'Untreated' END"

    grp_cols: list[str] = []
    if group_by_bucket:
        grp_cols.append(f"{bucket_expr} AS TIME_BUCKET")
    if group_by_cohort:
        grp_cols.append(f"{cohort_expr} AS COHORT")

    grp_sql = (", " + ", ".join(grp_cols)) if grp_cols else ""
    group_by_positions = ", ".join(str(3 + i) for i in range(len(grp_cols)))
    group_by_extra = ", " + group_by_positions if grp_cols else ""

    cte = f"""
        WITH episodes AS (
            SELECT EPISODE_ID, PATIENT_ID{grp_sql},
                   MAX(CASE WHEN {fu} THEN 1 ELSE 0 END)               AS HAS_FOLLOWUP,
                   COUNT(DISTINCT CASE WHEN {fu}
                                        THEN ENCOUNTER_KEY END)         AS N_FOLLOWUP_VISITS
            FROM {FACT_TABLE}
            {where}
            GROUP BY EPISODE_ID, PATIENT_ID{group_by_extra}
        )
    """
    return cte, binds


def q_s1_episodes_by_bucket(f: BoDFilters) -> tuple[str, dict[str, Any]]:
    cte, binds = _episode_cte(f, group_by_cohort=False, group_by_bucket=True)
    sql = cte + """
        SELECT TIME_BUCKET,
               COUNT(*)                                        AS TOTAL_EPISODES,
               SUM(HAS_FOLLOWUP)                               AS EPISODES_WITH_FOLLOWUP,
               COUNT(*) - SUM(HAS_FOLLOWUP)                    AS EPISODES_NO_FOLLOWUP,
               SUM(HAS_FOLLOWUP)::float / NULLIF(COUNT(*),0)   AS PCT_FOLLOWUP,
               SUM(N_FOLLOWUP_VISITS)                          AS TOTAL_FOLLOWUP_VISITS
        FROM episodes GROUP BY 1 ORDER BY 1
    """
    return sql, binds


def q_s2_episodes_by_cohort(f: BoDFilters) -> tuple[str, dict[str, Any]]:
    cte, binds = _episode_cte(f, group_by_cohort=True, group_by_bucket=False)
    sql = cte + """
        SELECT COHORT,
               COUNT(*) AS TOTAL_EPISODES,
               SUM(HAS_FOLLOWUP) AS EPISODES_WITH_FOLLOWUP,
               COUNT(*) - SUM(HAS_FOLLOWUP) AS EPISODES_NO_FOLLOWUP,
               SUM(HAS_FOLLOWUP)::float / NULLIF(COUNT(*),0) AS PCT_FOLLOWUP,
               SUM(N_FOLLOWUP_VISITS) AS TOTAL_FOLLOWUP_VISITS
        FROM episodes GROUP BY 1 ORDER BY 1
    """
    return sql, binds


def q_s3_episodes_by_bucket_cohort(f: BoDFilters) -> tuple[str, dict[str, Any]]:
    cte, binds = _episode_cte(f, group_by_cohort=True, group_by_bucket=True)
    sql = cte + """
        SELECT TIME_BUCKET, COHORT,
               COUNT(*) AS TOTAL_EPISODES,
               SUM(HAS_FOLLOWUP) AS EPISODES_WITH_FOLLOWUP,
               COUNT(*) - SUM(HAS_FOLLOWUP) AS EPISODES_NO_FOLLOWUP,
               SUM(HAS_FOLLOWUP)::float / NULLIF(COUNT(*),0) AS PCT_FOLLOWUP,
               SUM(N_FOLLOWUP_VISITS) AS TOTAL_FOLLOWUP_VISITS
        FROM episodes GROUP BY 1,2 ORDER BY 1,2
    """
    return sql, binds


# ==========================================================================
# Section 4 — average # follow-up visits per episode
# ==========================================================================

def q_s4_avg_visits_per_episode(f: BoDFilters) -> tuple[str, dict[str, Any]]:
    cte, binds = _episode_cte(f, group_by_cohort=True, group_by_bucket=True)
    sql = cte + """
        SELECT TIME_BUCKET, COHORT,
               COUNT(*) AS TOTAL_EPISODES,
               SUM(HAS_FOLLOWUP) AS EPISODES_WITH_FOLLOWUP,
               SUM(N_FOLLOWUP_VISITS) AS TOTAL_FOLLOWUP_VISITS,
               SUM(N_FOLLOWUP_VISITS)::float / NULLIF(COUNT(*),0) AS AVG_VISITS_PER_EPISODE
        FROM episodes GROUP BY 1,2 ORDER BY 1,2
    """
    return sql, binds


# ==========================================================================
# Sections 5 / 6 / 7 — visit-type mix (row-level FU predicate applied)
# ==========================================================================

def _visit_mix_query(f: BoDFilters, group_cohort: bool, group_bucket: bool
                     ) -> tuple[str, dict[str, Any]]:
    """Visit-type mix for S5 / S6 / S7.

    Returns one row per (bucket?, cohort?, visit_type) with:
      * N_VISITS      = # follow-up visits of that visit type
      * TOTAL_EPISODES = # total episodes for the same bar-group (denominator)
      * TOTAL_FOLLOWUP_VISITS = # follow-up visits (all types) for the same bar

    IMPORTANT: the fact table has a physical column literally named `VISIT`,
    so we must NOT alias the visit-type expression back to `VISIT` — GROUP BY
    would resolve to the physical column and Snowflake would raise a
    compilation error. We alias as `VISIT_TYPE` internally and rename in
    Python for the downstream renderers.
    """
    where, binds = where_clause(f)
    nv = no_visit_col(f)
    win = encounter_window_col(f)
    bucket = time_bucket("EPISODE_START_DATE", f.time_level)
    cohort_expr = "CASE WHEN COVID_TX_FLAG=1 THEN 'Treated' ELSE 'Untreated' END"

    # Build the bar-group dimension list (excluding VISIT_TYPE).
    bar_dims: list[tuple[str, str]] = []
    if group_bucket:
        bar_dims.append((f"{bucket}", "TIME_BUCKET"))
    if group_cohort:
        bar_dims.append((cohort_expr, "COHORT"))

    if not bar_dims:
        # Neither grouping means one big bar — this shouldn't happen for S5-7
        # but handle it defensively.
        bar_dims.append(("1", "ONE"))

    bar_select = ",\n            ".join(f"{expr} AS {name}" for expr, name in bar_dims)
    bar_join_cols = [name for _, name in bar_dims]
    bar_group_positions = ", ".join(str(i + 1) for i in range(len(bar_dims)))

    # Full SELECT list positions:
    #   1..N  = bar-group dims
    #   N+1   = VISIT_TYPE
    visit_pos = len(bar_dims) + 1
    all_group_positions = ", ".join(
        str(i + 1) for i in range(len(bar_dims) + 1)
    )
    join_on = " AND ".join(f"v.{c} = t.{c}" for c in bar_join_cols)

    sql = f"""
        WITH totals AS (
            SELECT
                {bar_select},
                COUNT(DISTINCT EPISODE_ID) AS TOTAL_EPISODES,
                COUNT(DISTINCT CASE WHEN {nv} <> 'NO VISIT' AND {win} = 1
                                     THEN ENCOUNTER_KEY END) AS TOTAL_FOLLOWUP_VISITS
            FROM {FACT_TABLE}
            {where}
            GROUP BY {bar_group_positions}
        ),
        visits AS (
            SELECT
                {bar_select},
                {nv} AS VISIT_TYPE,
                COUNT(DISTINCT ENCOUNTER_KEY) AS N_VISITS
            FROM {FACT_TABLE}
            {where}
              AND {nv} <> 'NO VISIT'
              AND {win} = 1
            GROUP BY {all_group_positions}
        )
        SELECT v.*, t.TOTAL_EPISODES, t.TOTAL_FOLLOWUP_VISITS
        FROM visits v
        JOIN totals t ON {join_on}
        ORDER BY {bar_group_positions}, {visit_pos}
    """
    return sql, binds


def q_s5_visit_mix_by_bucket(f):  return _visit_mix_query(f, False, True)
def q_s6_visit_mix_by_cohort(f):  return _visit_mix_query(f, True,  False)
def q_s7_visit_mix_by_bucket_cohort(f):  return _visit_mix_query(f, True, True)


# ==========================================================================
# Sections 8 / 9 — HR-condition breakdowns (bypass HR filter)
# ==========================================================================

def _hr_where(f: BoDFilters) -> tuple[str, dict[str, Any]]:
    return where_clause(f, bypass_hr=True)


def _hr_extra_predicates(f: BoDFilters, binds: dict[str, Any]) -> list[str]:
    """Local S8/S9 filters — visit-type multi-select applied via NO_VISIT_*.
    HR-condition multi-select is applied at OUTER SELECT (not here) so the
    top-N ranking still runs against the full HR domain.
    """
    parts: list[str] = []
    if f.s8_visit_types:
        keys = []
        for i, v in enumerate(f.s8_visit_types):
            k = f"s8v_{i}"
            binds[k] = v
            keys.append(f"%({k})s")
        nv = no_visit_col(f)
        parts.append(f"{nv} IN ({', '.join(keys)})")
    return parts


def q_s8_hr_conditions(f: BoDFilters) -> tuple[str, dict[str, Any]]:
    """S8 chart data.

    SQL shape (matches Tableau's calc chain):
      * `top7`   – dynamic top-N HR conditions ranked by follow-up count for
                   the selected visit type (uses the same base filters as
                   the chart, so top-N is context-relevant).
      * `tagged` – every fact row tagged with:
                     hr_top7_calc         → 'Top 7 HR Conditions' if HR is
                                            in top-N else the HR name
                     hr_top7_select_calc  → the HR name if in top-N else
                                            'Other Conditions'
                   which mirror the workbook's `High Risk Top 7` and
                   `High Risk top 7 select` calcs.
      * `reference` – total follow-up count for the selected visit type
                     across ALL patients (the 100% bar).
      * `per_hr`  – top-N HR conditions after applying Filter-2 (on
                   hr_top7_calc) and Filter-3 (on hr_top7_select_calc).

    Every row returned has: HIGH_RISK_CONDITION, N_EPISODES_WITH_FU,
    N_FOLLOWUP_VISITS.  Reference row uses HIGH_RISK_CONDITION='REFERENCE'.
    """
    where, binds = _hr_where(f)
    nv = no_visit_col(f)
    win = encounter_window_col(f)
    top_n = max(1, min(int(f.s8_top_n or 7), 20))

    # ---- Visit-type predicate (single-select) ---------------------------------
    vt_pred = ""
    if f.s8_visit_types:
        keys = []
        for i, v in enumerate(f.s8_visit_types):
            k = f"s8v_{i}"
            binds[k] = v
            keys.append(f"%({k})s")
        vt_pred = f"AND {nv} IN ({', '.join(keys)})"

    # ---- Filter-2 (HR top-7 categorical) --------------------------------------
    # Applied against a CASE that mirrors the workbook's `High Risk Top 7`
    # calc: returns 'Top 7 HR Conditions' if HR is in the top-N set, else
    # the HR name.
    hr_top7_calc = (
        "CASE WHEN HIGH_RISK_CONDITION IN (SELECT HIGH_RISK_CONDITION FROM top7) "
        "THEN 'Top 7 HR Conditions' ELSE HIGH_RISK_CONDITION END"
    )
    hr_top7_select_calc = (
        "CASE WHEN HIGH_RISK_CONDITION IN (SELECT HIGH_RISK_CONDITION FROM top7) "
        "THEN HIGH_RISK_CONDITION ELSE 'Other Conditions' END"
    )

    f2_pred = ""
    if f.s8_hr_conditions:
        keys = []
        for i, v in enumerate(f.s8_hr_conditions):
            k = f"s8f2_{i}"
            binds[k] = v
            keys.append(f"%({k})s")
        f2_pred = f"AND ({hr_top7_calc}) IN ({', '.join(keys)})"

    # ---- Filter-3 (which top-7 items to show + 'Other Conditions') -----------
    f3_pred = ""
    if f.s8_top7_items:
        keys = []
        for i, v in enumerate(f.s8_top7_items):
            k = f"s8f3_{i}"
            binds[k] = v
            keys.append(f"%({k})s")
        f3_pred = f"AND ({hr_top7_select_calc}) IN ({', '.join(keys)})"

    sql = f"""
        WITH top7 AS (
            SELECT HIGH_RISK_CONDITION
            FROM {FACT_TABLE}
            {where}
              AND HIGH_RISK_CONDITION IS NOT NULL
              AND HIGH_RISK_CONDITION NOT IN ('NO HIGH RISK CONDITION','ALL HR CONDITIONS')
              {vt_pred}
              AND {nv} <> 'NO VISIT' AND {win} = 1
            GROUP BY HIGH_RISK_CONDITION
            ORDER BY COUNT(DISTINCT ENCOUNTER_KEY) DESC
            LIMIT {top_n}
        ),
        reference AS (
            SELECT 'REFERENCE'::VARCHAR AS HIGH_RISK_CONDITION,
                   0 AS ORD,
                   COUNT(DISTINCT CASE WHEN {nv} <> 'NO VISIT' AND {win} = 1 {vt_pred}
                                        THEN EPISODE_ID END)    AS N_EPISODES_WITH_FU,
                   COUNT(DISTINCT CASE WHEN {nv} <> 'NO VISIT' AND {win} = 1 {vt_pred}
                                        THEN ENCOUNTER_KEY END) AS N_FOLLOWUP_VISITS
            FROM {FACT_TABLE}
            {where}
        ),
        per_hr AS (
            SELECT HIGH_RISK_CONDITION,
                   1 AS ORD,
                   COUNT(DISTINCT CASE WHEN {nv} <> 'NO VISIT' AND {win} = 1 {vt_pred}
                                        THEN EPISODE_ID END)    AS N_EPISODES_WITH_FU,
                   COUNT(DISTINCT CASE WHEN {nv} <> 'NO VISIT' AND {win} = 1 {vt_pred}
                                        THEN ENCOUNTER_KEY END) AS N_FOLLOWUP_VISITS
            FROM {FACT_TABLE}
            {where}
              AND HIGH_RISK_CONDITION IS NOT NULL
              AND HIGH_RISK_CONDITION NOT IN ('NO HIGH RISK CONDITION','ALL HR CONDITIONS')
              {f2_pred}
              {f3_pred}
            GROUP BY HIGH_RISK_CONDITION
            ORDER BY N_FOLLOWUP_VISITS DESC
            LIMIT {top_n}
        )
        SELECT * FROM reference
        UNION ALL
        SELECT * FROM per_hr
        ORDER BY ORD, N_FOLLOWUP_VISITS DESC
    """
    return sql, binds


def q_s8_top7_list(f: BoDFilters) -> tuple[str, dict[str, Any]]:
    """Return the current top-N HR conditions (used to populate Filter-3)."""
    where, binds = _hr_where(f)
    nv = no_visit_col(f)
    win = encounter_window_col(f)
    top_n = max(1, min(int(f.s8_top_n or 7), 20))

    vt_pred = ""
    if f.s8_visit_types:
        keys = []
        for i, v in enumerate(f.s8_visit_types):
            k = f"s8v_{i}"
            binds[k] = v
            keys.append(f"%({k})s")
        vt_pred = f"AND {nv} IN ({', '.join(keys)})"

    sql = f"""
        SELECT HIGH_RISK_CONDITION,
               COUNT(DISTINCT ENCOUNTER_KEY) AS N
        FROM {FACT_TABLE}
        {where}
          AND HIGH_RISK_CONDITION IS NOT NULL
          AND HIGH_RISK_CONDITION NOT IN ('NO HIGH RISK CONDITION','ALL HR CONDITIONS')
          {vt_pred}
          AND {nv} <> 'NO VISIT' AND {win} = 1
        GROUP BY HIGH_RISK_CONDITION
        ORDER BY N DESC
        LIMIT {top_n}
    """
    return sql, binds


def q_s9_hr_conditions_by_cohort(f: BoDFilters) -> tuple[str, dict[str, Any]]:
    """Section-9 data: same shape as S8 (reference + top-N HR) but split by
    Treated / Untreated cohort. Uses the same local filters as S8.

    Every row: HIGH_RISK_CONDITION, ORD (0=reference, 1=HR), COHORT,
               N_EPISODES_WITH_FU, N_FOLLOWUP_VISITS.
    """
    where, binds = _hr_where(f)
    nv = no_visit_col(f)
    win = encounter_window_col(f)
    top_n = max(1, min(int(f.s8_top_n or 7), 20))
    cohort_expr = "CASE WHEN COVID_TX_FLAG=1 THEN 'Treated' ELSE 'Untreated' END"

    # ---- Visit-type filter (single-select) ----
    vt_pred = ""
    if f.s8_visit_types:
        keys = []
        for i, v in enumerate(f.s8_visit_types):
            k = f"s8v_{i}"; binds[k] = v; keys.append(f"%({k})s")
        vt_pred = f"AND {nv} IN ({', '.join(keys)})"

    # ---- Filter-2 / Filter-3 mirror the same calc columns as S8 ----
    hr_top7_calc = (
        "CASE WHEN HIGH_RISK_CONDITION IN (SELECT HIGH_RISK_CONDITION FROM top7) "
        "THEN 'Top 7 HR Conditions' ELSE HIGH_RISK_CONDITION END"
    )
    hr_top7_select_calc = (
        "CASE WHEN HIGH_RISK_CONDITION IN (SELECT HIGH_RISK_CONDITION FROM top7) "
        "THEN HIGH_RISK_CONDITION ELSE 'Other Conditions' END"
    )
    f2_pred = ""
    if f.s8_hr_conditions:
        keys = []
        for i, v in enumerate(f.s8_hr_conditions):
            k = f"s9f2_{i}"; binds[k] = v; keys.append(f"%({k})s")
        f2_pred = f"AND ({hr_top7_calc}) IN ({', '.join(keys)})"
    f3_pred = ""
    if f.s8_top7_items:
        keys = []
        for i, v in enumerate(f.s8_top7_items):
            k = f"s9f3_{i}"; binds[k] = v; keys.append(f"%({k})s")
        f3_pred = f"AND ({hr_top7_select_calc}) IN ({', '.join(keys)})"

    sql = f"""
        WITH top7 AS (
            SELECT HIGH_RISK_CONDITION
            FROM {FACT_TABLE}
            {where}
              AND HIGH_RISK_CONDITION IS NOT NULL
              AND HIGH_RISK_CONDITION NOT IN ('NO HIGH RISK CONDITION','ALL HR CONDITIONS')
              {vt_pred}
              AND {nv} <> 'NO VISIT' AND {win} = 1
            GROUP BY HIGH_RISK_CONDITION
            ORDER BY COUNT(DISTINCT ENCOUNTER_KEY) DESC
            LIMIT {top_n}
        ),
        reference AS (
            SELECT 'REFERENCE'::VARCHAR AS HIGH_RISK_CONDITION,
                   0 AS ORD,
                   {cohort_expr} AS COHORT,
                   COUNT(DISTINCT CASE WHEN {nv} <> 'NO VISIT' AND {win} = 1 {vt_pred}
                                        THEN EPISODE_ID END)    AS N_EPISODES_WITH_FU,
                   COUNT(DISTINCT CASE WHEN {nv} <> 'NO VISIT' AND {win} = 1 {vt_pred}
                                        THEN ENCOUNTER_KEY END) AS N_FOLLOWUP_VISITS
            FROM {FACT_TABLE}
            {where}
            GROUP BY 3
        ),
        per_hr AS (
            SELECT HIGH_RISK_CONDITION,
                   1 AS ORD,
                   {cohort_expr} AS COHORT,
                   COUNT(DISTINCT CASE WHEN {nv} <> 'NO VISIT' AND {win} = 1 {vt_pred}
                                        THEN EPISODE_ID END)    AS N_EPISODES_WITH_FU,
                   COUNT(DISTINCT CASE WHEN {nv} <> 'NO VISIT' AND {win} = 1 {vt_pred}
                                        THEN ENCOUNTER_KEY END) AS N_FOLLOWUP_VISITS
            FROM {FACT_TABLE}
            {where}
              AND HIGH_RISK_CONDITION IS NOT NULL
              AND HIGH_RISK_CONDITION NOT IN ('NO HIGH RISK CONDITION','ALL HR CONDITIONS')
              AND HIGH_RISK_CONDITION IN (SELECT HIGH_RISK_CONDITION FROM top7)
              {f2_pred}
              {f3_pred}
            GROUP BY 1, 3
        )
        SELECT * FROM reference
        UNION ALL
        SELECT * FROM per_hr
        ORDER BY ORD, HIGH_RISK_CONDITION, COHORT
    """
    return sql, binds


# ==========================================================================
# Section 10 — COVID-19 initial diagnosis / treatment location
# ==========================================================================

def q_s10_covid_dx_location(f: BoDFilters) -> tuple[str, dict[str, Any]]:
    """Section-10 — reproduces Tableau's `VISIT FILTER (Covid)` categorization.

    Critical: in this fact table `COVID_DX_ENCOUNTER`, `COVID_DX_FLAG`, and
    `COVID_TX_FLAG` are NULL (not 0) for most rows. Tableau's `ZN()` converts
    NULL → 0, so we must `IFNULL(..., 0)` before comparing to 0/1 -- otherwise
    the `EP_HAS_DX_ENC = 0` branches never fire and OTHERS/PHARMACY collapse to
    zero rows.

      * Episode has ANY DX-encounter → for the DX row(s), category = VISIT.
      * Episode has no DX-encounter but COVID_DX_FLAG=1  → 'OTHERS'.
      * Episode has no DX-encounter, no DX_FLAG, TX_FLAG=1 → 'PHARMACY'.
      * Everything else ("Redundant") is dropped.

    Local VISIT filter (`s10_visit_types`) narrows the emitted categories.
    Measure per (time bucket × category) is COUNT(DISTINCT EPISODE_ID).
    """
    where, binds = where_clause(f)
    bucket = time_bucket("EPISODE_START_DATE", f.time_level)

    vt_pred = ""
    if getattr(f, "s10_visit_types", None):
        keys = []
        for i, v in enumerate(f.s10_visit_types):
            k = f"s10v_{i}"; binds[k] = v; keys.append(f"%({k})s")
        vt_pred = f"AND VISIT_CAT IN ({', '.join(keys)})"

    sql = f"""
        WITH src AS (
            SELECT EPISODE_ID,
                   EPISODE_START_DATE,
                   VISIT,
                   IFNULL(COVID_DX_ENCOUNTER, 0) AS DX_ENC,
                   IFNULL(COVID_DX_FLAG, 0)      AS DX_FLAG,
                   IFNULL(COVID_TX_FLAG, 0)      AS TX_FLAG,
                   MAX(IFNULL(COVID_DX_ENCOUNTER, 0))
                       OVER (PARTITION BY EPISODE_ID) AS EP_HAS_DX_ENC
            FROM {FACT_TABLE}
            {where}
        ),
        categ AS (
            SELECT EPISODE_ID,
                   EPISODE_START_DATE,
                   CASE
                     WHEN EP_HAS_DX_ENC = 1 AND DX_ENC = 1
                          THEN VISIT
                     WHEN EP_HAS_DX_ENC = 0 AND DX_FLAG = 1
                          THEN 'OTHERS'
                     WHEN EP_HAS_DX_ENC = 0 AND TX_FLAG = 1 AND DX_FLAG = 0
                          THEN 'PHARMACY'
                   END AS VISIT_CAT
            FROM src
        )
        SELECT {bucket}                    AS TIME_BUCKET,
               VISIT_CAT                   AS VISIT,
               COUNT(DISTINCT EPISODE_ID)  AS N_EPISODES
        FROM   categ
        WHERE  VISIT_CAT IS NOT NULL
        {vt_pred}
        GROUP BY 1, 2
        ORDER BY 1, 2
    """
    return sql, binds
