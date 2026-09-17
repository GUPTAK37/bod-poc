"""BoD layout matching the Tableau screenshot: 10 sections, blue title bars,
top parameter row, sub-header with time period + window + episode count,
data table under every chart. All heavy callbacks are gated on the
`Apply Filters` button so opening the page does not stampede Snowflake.
"""

from __future__ import annotations

import logging
from datetime import date

import pandas as pd
from dash import ALL, MATCH, Input, Output, State, dcc, html
import dash_bootstrap_components as dbc

from ..charts import theme as th
from ..data import queries as Q
from ..data.params import (
    BoDFilters,
    CHILD_ACCOUNT_EXPR,
    ENCOUNTER_WINDOWS,
    GENDER_EXPR,
    GRANULARITIES,
    PARENT_ACCOUNT_EXPR,
    PROVIDER_PRIMARY_SPECIALTY_EXPR,
    PROVIDER_SPECIALTY_GROUP_EXPR,
    TIME_LEVELS,
    granularity_value_expr,
)
from ..data.snowflake_client import FACT_TABLE, run_query


# ---------------------------------------------------------------------------
# Multi-select dropdown IDs — collected as _dd(multi=True) creates them so a
# single loop at callback-registration time can wire an overlay-summary
# clientside callback for each.
# ---------------------------------------------------------------------------
_MULTI_DD_IDS: list[str] = []


def _register_multi_dd(id_: str) -> None:
    if id_ not in _MULTI_DD_IDS:
        _MULTI_DD_IDS.append(id_)


# ---------------------------------------------------------------------------
# Domain lookups (fetched lazily on first Apply, not at import time)
# ---------------------------------------------------------------------------

_DOMAIN_CACHE: dict[str, list[str]] = {}


def _domain(col: str, expr: str | None = None,
            top_n: int | None = None) -> list[str]:
    """Distinct values of a column (or SQL expression) scoped to the SAME
    cohort filters that every SQL builder in `params.where_clause` applies.

    Special-cased: when we're enumerating the `HIGH_RISK_CONDITION` column
    itself, we DON'T restrict to `HIGH_RISK_CONDITION='ALL HR CONDITIONS'`
    (that would collapse the result to a single row). For every other
    column the HR-rollup restriction is safe and keeps the scan cheap.

    Passing `expr` allows filters that need Tableau's calc-column semantics
    (e.g. Primary Specialty is the physical column `REPORTING_PRIMARY_SPECIALTY`
    with a bin+UPPER transform) to fetch the *cleaned* option list rather
    than raw values like specialty codes "104", "105".

    `top_n` caps the option list to the N most frequent values (by row
    count). Essential for filters like `PARENT_NAME` / `CHILD_NAME` whose
    distinct-value counts (14k / 187k) would otherwise ship a monster JSON
    payload and freeze the browser.
    """
    cache_key = (expr or col, top_n)
    if cache_key in _DOMAIN_CACHE:
        return _DOMAIN_CACHE[cache_key]
    select_expr = expr if expr else col
    where_extra = ""
    if col.upper() != "HIGH_RISK_CONDITION":
        where_extra = "  AND HIGH_RISK_CONDITION = 'ALL HR CONDITIONS' "
    if top_n:
        # Bounded scan: rank by frequency, keep top N.
        order_by = "COUNT(*) DESC"
        limit_clause = f" LIMIT {int(top_n)}"
    else:
        order_by = "1"
        limit_clause = ""
    try:
        df = run_query(
            f"SELECT {select_expr} AS V, COUNT(*) AS N FROM {FACT_TABLE} "
            f"WHERE MEDICAL_PATIENT_COHORT_FLAG = 1 "
            f"  AND INCOMPLETE_EPISODE_FLAG = 0 "
            f"  AND EPISODE_PER_QUARTER_FLAG = '1' "
            f"{where_extra}"
            f"  AND {select_expr} IS NOT NULL "
            f"GROUP BY 1 ORDER BY {order_by}{limit_clause}"
        )
        vals = [str(v) for v in df["V"].tolist()]
        # Sort alphabetically for consistent display, regardless of scan order.
        if top_n:
            vals = sorted(vals)
        _DOMAIN_CACHE[cache_key] = vals
    except Exception:
        _DOMAIN_CACHE[cache_key] = []
    return _DOMAIN_CACHE[cache_key]


# ---------------------------------------------------------------------------
# Relevant-value domain (Tableau-style "Only Relevant Values")
# ---------------------------------------------------------------------------

_RELEVANT_CACHE: dict[tuple, list[str]] = {}


def _relevant_domain(col: str, expr: str | None = None,
                     *, context: dict[str, list[str]] | None = None,
                     time_range: tuple[date, date] | None = None,
                     top_n: int | None = None) -> list[str]:
    """Distinct values of `col` (or `expr`) scoped to the cohort + a caller-
    supplied context of upstream filter selections. Cached per
    (col, frozen-context, time-range, top_n) so repeated popover opens are
    free. Falls back to the global `_domain()` on any Snowflake error.
    """
    key = (
        col, expr,
        tuple(sorted((k, tuple(sorted(v))) for k, v in (context or {}).items())),
        time_range, top_n,
    )
    if key in _RELEVANT_CACHE:
        return _RELEVANT_CACHE[key]

    select_expr = expr if expr else col
    parts = [
        "MEDICAL_PATIENT_COHORT_FLAG = 1",
        "INCOMPLETE_EPISODE_FLAG = 0",
        "EPISODE_PER_QUARTER_FLAG = '1'",
        f"{select_expr} IS NOT NULL",
    ]
    if col.upper() != "HIGH_RISK_CONDITION":
        parts.append("HIGH_RISK_CONDITION = 'ALL HR CONDITIONS'")
    binds: dict = {}

    if time_range is not None and time_range[0] is not None and time_range[1] is not None:
        binds["start_month"] = time_range[0]
        binds["end_month"] = time_range[1]
        parts.append(
            "EPISODE_START_DATE >= %(start_month)s "
            "AND EPISODE_START_DATE < DATEADD(month, 1, %(end_month)s)"
        )

    for i, (ctx_col, ctx_vals) in enumerate((context or {}).items()):
        clean = [v for v in (ctx_vals or []) if v not in ("__ALL__", "(All)")]
        if not clean:
            continue
        placeholders = []
        for j, v in enumerate(clean):
            k = f"ctx{i}_{j}"
            binds[k] = v
            placeholders.append(f"%({k})s")
        parts.append(f"{ctx_col} IN ({', '.join(placeholders)})")

    if top_n:
        order_by, limit_clause = "COUNT(*) DESC", f" LIMIT {int(top_n)}"
        sql = (
            f"SELECT {select_expr} AS V, COUNT(*) AS N FROM {FACT_TABLE} "
            f"WHERE " + " AND ".join(parts) +
            f" GROUP BY 1 ORDER BY {order_by}{limit_clause}"
        )
    else:
        sql = (
            f"SELECT DISTINCT {select_expr} AS V FROM {FACT_TABLE} "
            f"WHERE " + " AND ".join(parts) + " ORDER BY 1"
        )
    try:
        df = run_query(sql, binds)
        vals = [str(v) for v in df["V"].tolist() if v is not None]
        if top_n:
            vals = sorted(vals)
    except Exception:
        logging.exception("Relevant-domain query failed for %s", col)
        vals = list(_domain(col, expr=expr, top_n=top_n))
    _RELEVANT_CACHE[key] = vals
    return vals


def _run_group_cascade(
    group: "list[tuple[str, str, str | None, int | None]]",
    selections: dict[str, list[str]],
    currents: dict[str, list[str]],
) -> list:
    """Reusable body for the Patient / Account group cascades.

    `group` is a list of `(filter-id, physical-col, calc-expr, top_n_cap)`.
    `selections[fid]` is the SQL-facing value currently in the store (used
    to build the "OTHER filters" context). `currents[fid]` is the
    checklist's live `value` (used to reconcile checked boxes).

    Returns a flat list of three entries per filter, in `group` order:
      [options, allopts, value, options, allopts, value, ...]
    for consumption by a Dash callback with a matching Output list. The
    filter that triggered this callback (per `dash.callback_context`) is
    returned as no_update so the user's active selection is never
    overwritten mid-click.
    """
    from dash import ctx, no_update

    trigger = ctx.triggered_id if hasattr(ctx, "triggered_id") else None
    trig_id = trigger.get("id") if isinstance(trigger, dict) else None

    outputs: list = []
    for fid, phys, expr, cap in group:
        if fid == trig_id:
            outputs.extend([no_update, no_update, no_update])
            continue
        # Context = every OTHER filter's current selection.
        context: dict[str, list[str]] = {}
        for fid2, phys2, expr2, _ in group:
            if fid2 == fid:
                continue
            clean = [v for v in (selections.get(fid2) or [])
                     if v not in ("__ALL__", "(All)")]
            if clean:
                context[expr2 or phys2] = clean
        try:
            vals = _relevant_domain(phys, expr=expr, context=context,
                                    top_n=cap)
        except Exception:
            vals = _domain(phys, expr=expr, top_n=cap)
        if phys == "HIGH_RISK_CONDITION":
            vals = [v for v in vals if v != "ALL HR CONDITIONS"]

        opts = _multi_options(vals)

        # Reconcile the current checklist value.
        prev = currents.get(fid) or []
        prev_real = [v for v in prev if v not in ("__ALL__",)]
        still_valid = [v for v in prev_real if v in vals]
        all_was_checked = "__ALL__" in prev or (
            prev_real and vals and len(prev_real) >= len(vals))
        if all_was_checked or not still_valid:
            new_value = [ALL_SENTINEL] + vals
        elif len(still_valid) == len(vals):
            new_value = [ALL_SENTINEL] + vals
        else:
            new_value = still_valid

        outputs.extend([opts, opts, new_value])
    return outputs


# ---------------------------------------------------------------------------
# Data-extents helpers — years and month range present in the fact table.
# Cached in `_DOMAIN_CACHE` under sentinel keys.
# ---------------------------------------------------------------------------

def _date_extents() -> tuple[date, date, list[int]]:
    """Return `(min_date, max_date, distinct_years)` from the cohort-scoped
    fact table. Cached across calls."""
    key = "__DATE_EXTENTS__"
    if key in _DOMAIN_CACHE:
        return _DOMAIN_CACHE[key]  # type: ignore[return-value]
    try:
        df = run_query(
            f"SELECT MIN(EPISODE_START_DATE::date) AS MIN_D, "
            f"       MAX(EPISODE_START_DATE::date) AS MAX_D "
            f"FROM {FACT_TABLE} "
            f"WHERE MEDICAL_PATIENT_COHORT_FLAG = 1 "
            f"  AND INCOMPLETE_EPISODE_FLAG = 0 "
            f"  AND EPISODE_PER_QUARTER_FLAG = '1' "
            f"  AND HIGH_RISK_CONDITION = 'ALL HR CONDITIONS'"
        )
        min_d = pd.to_datetime(df.iloc[0, 0]).date()
        max_d = pd.to_datetime(df.iloc[0, 1]).date()
        years = list(range(min_d.year, max_d.year + 1))
        _DOMAIN_CACHE[key] = (min_d, max_d, years)  # type: ignore[assignment]
    except Exception:
        # Sensible fallback so the layout still builds if the query fails.
        _DOMAIN_CACHE[key] = (date(2024, 1, 1), date(2026, 12, 1),  # type: ignore[assignment]
                              [2024, 2025, 2026])
    return _DOMAIN_CACHE[key]  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# UI helpers
# ---------------------------------------------------------------------------

ALL_SENTINEL = "__ALL__"


def _multi_options(options: list[str]) -> list[dict]:
    """Options for a multi-select. The '(All)' row is prepended so the
    user sees it as the first checkbox in the popover."""
    return ([{"label": "(All)", "value": ALL_SENTINEL}] +
            [{"label": o, "value": o} for o in options])


def _ms(id_: str, label: str, options: list[str],
        value: list[str] | None = None,
        css_extra: str = "",
        label_class: str = "param-label",
        searchable: bool | None = None) -> html.Div:
    """Tableau-style multi-select filter (button + popover + checklist).

    If `searchable` is None the search box is auto-enabled for filters with
    more than 10 options; pass True/False to force. Selected values remain
    checked even when hidden by search (clientside only filters display).
    """
    opts = list(options)
    # Default: all checkboxes ticked → button reads "(All)". This branch
    # fires when `value` is None, empty (== "no filter applied"), OR
    # already equal to the full option set (Tableau's "all manually
    # selected → collapse to All" rule).
    if not value or (len(value) == len(opts) and set(value) == set(opts)):
        initial_chk = [ALL_SENTINEL] + opts
        initial_sql: list[str] = []
    else:
        # Explicit partial selection.
        initial_chk = list(value)
        initial_sql = list(value)
    if searchable is None:
        searchable = len(opts) > 10
    _register_multi_dd(id_)

    body_children = []
    if searchable:
        body_children.append(
            dcc.Input(
                id={"type": "ms-search", "id": id_},
                type="search",
                placeholder="Search\u2026",
                debounce=False,
                className="ms-search",
                autoComplete="off",
            )
        )
    body_children.append(
        dcc.Checklist(
            options=_multi_options(opts),
            value=initial_chk,
            id={"type": "ms-chk", "id": id_},
            className="ms-chk",
            inputClassName="ms-checkbox",
            labelClassName="ms-checkbox-label",
        )
    )
    return html.Div([
        html.Div(label, className=label_class),
        html.Div([
            html.Button(
                [html.Span("", id={"type": "ms-btntext", "id": id_},
                           className="ms-btn-text"),
                 html.Span("\u25be", className="ms-btn-arrow")],
                id={"type": "ms-btn", "id": id_},
                type="button",
                className=f"ms-btn {css_extra}".strip(),
            ),
            dbc.Popover(
                dbc.PopoverBody(body_children, className="ms-body"),
                id={"type": "ms-pop", "id": id_},
                target={"type": "ms-btn", "id": id_},
                trigger="legacy",
                placement="bottom-start",
                hide_arrow=True,
                className="ms-pop",
            ),
            # Immutable full-option list; sync callback reads THIS (not
            # ms-chk.options which the search callback narrows down) to
            # know the true total count for the (All) collapse rule.
            dcc.Store(id={"type": "ms-allopts", "id": id_},
                      data=_multi_options(opts)),
            # Previous-state tracker for the sync callback.
            dcc.Store(id={"type": "ms-prev", "id": id_}, data=initial_chk),
            # SQL-facing value: [] means "no filter" (i.e. All).
            dcc.Store(id={"type": "ms-sql", "id": id_}, data=initial_sql),
        ], className="ms-cell"),
    ], className="param-cell")


def _ss(id_: str, label: str,
        options: "dict[str, str] | list[str]",
        value: str,
        css_extra: str = "",
        label_class: str = "param-label") -> html.Div:
    """Tableau-style single-select filter — same visual language as `_ms`
    but with a `dcc.RadioItems` inside the popover. Selecting a radio auto-
    closes the popover.

    `options` accepts either a `list[str]` (value == label) or an ordered
    dict `{value: label}`. `value` is the initial selection.
    """
    if isinstance(options, dict):
        opts = [{"label": v, "value": k} for k, v in options.items()]
        label_of = dict(options)
    else:
        opts = [{"label": o, "value": o} for o in options]
        label_of = {o: o for o in options}
    _register_single_ss(id_)
    return html.Div([
        html.Div(label, className=label_class),
        html.Div([
            html.Button(
                [html.Span(label_of.get(value, ""),
                           id={"type": "ss-btntext", "id": id_},
                           className="ms-btn-text"),
                 html.Span("\u25be", className="ms-btn-arrow")],
                id={"type": "ss-btn", "id": id_},
                type="button",
                className=f"ms-btn {css_extra}".strip(),
            ),
            dbc.Popover(
                dbc.PopoverBody(
                    dcc.RadioItems(
                        options=opts, value=value,
                        id=id_,
                        className="ss-radio",
                        inputClassName="ms-checkbox",
                        labelClassName="ms-checkbox-label",
                    ),
                    className="ms-body",
                ),
                id={"type": "ss-pop", "id": id_},
                target={"type": "ss-btn", "id": id_},
                trigger="legacy",
                placement="bottom-start",
                hide_arrow=True,
                className="ms-pop",
            ),
        ], className="ms-cell"),
    ], className="param-cell")


# Track single-selects that use the popover (for the button-text sync
# clientside callback).
_SS_IDS: list[str] = []


def _register_single_ss(id_: str) -> None:
    if id_ not in _SS_IDS:
        _SS_IDS.append(id_)


def _dd(id_: str, label: str, options: list[str], multi: bool = True,
        value=None, placeholder: str = "(All)"):
    """Compatibility wrapper: multi=True routes to `_ms`, multi=False routes
    to `_ss` (single-select popover)."""
    if multi:
        return _ms(id_, label, options)
    return _ss(id_, label, options, value=value)


def _dd_static(id_: str, label: str, options: dict, value):
    """Ordered-dict variant used for TIME_LEVELS / GRANULARITIES / windows."""
    return _ss(id_, label, options, value=value)


def _param_group(title: str, children: list, flex: int = 1) -> html.Div:
    """Fieldset-style block matching Tableau's dashed-green filter groups."""
    return html.Div([
        html.Div(title, className="param-group-title"),
        html.Div(children, className="param-group-body"),
    ], className="param-group", style={"flex": flex})


def _hr_local_filters(prefix: str) -> list:
    """Local filter row for S8 — mirrors the workbook's shelf:

      1. VISIT           (single-select, default 'ED VISIT')
      2. High Risk Conditions  (multi-select against the `High Risk Top 7`
         calc: 'Top 7 HR Conditions' or any specific HR name)
      3. Top 7 HR Conditions   (multi-select of the actual top-N members +
         'Other Conditions'; options refresh dynamically)
    """
    # Data-derived: physical visit types present in the fact table
    # (excludes the sentinel 'NO VISIT' value).
    visit_options = [v for v in _domain("NO_VISIT_30") if v != "NO VISIT"]
    hr_all = [v for v in _domain("HIGH_RISK_CONDITION")
              if v not in ("ALL HR CONDITIONS", "NO HIGH RISK CONDITION")]
    # Filter-2 options include the rollup pseudo-value at the bottom.
    f2_options = hr_all + ["Top 7 HR Conditions"]

    return [
        _ss(f"{prefix}-visit-filter", "VISIT", visit_options,
            value="ED VISIT",
            css_extra="local-filter-btn",
            label_class="local-filter-label"),
        _ms(f"{prefix}-hr-filter", "High Risk Conditions", f2_options,
            value=["Top 7 HR Conditions"],
            css_extra="local-filter-btn",
            label_class="local-filter-label"),
        _ms(f"{prefix}-top7-items", "Top 7 HR Conditions", [],
            css_extra="local-filter-btn",
            label_class="local-filter-label"),
    ]


def _s10_local_filters() -> list:
    """Local VISIT filter for S10 — mirrors Tableau's ' VISIT' quick filter.
    Multi-select against the 8 VISIT_FILTER (Covid) categories. Default = (All).
    """
    # Data-derived: distinct VISIT_FILTER_COVID categories present in the
    # fact table, excluding the 'Redundant' sentinel which is filtered out
    # by the S10 SQL builder.
    visit_options = [v for v in _domain("VISIT_FILTER_COVID")
                     if v and v != "Redundant"]
    return [
        _ms("s10-visit-filter", "VISIT", visit_options,
            css_extra="local-filter-btn",
            label_class="local-filter-label"),
    ]


def _section(section_id: str, title: str, chart_height: int = 300,
              legend: list[tuple[str, str]] | None = None,
              local_filters: list | None = None) -> html.Div:
    """Render a section shell (blue header + sub-header + chart + table).

    `legend` is an optional ordered list of `(label, color)` tuples rendered
    as a manual HTML legend above the chart. `local_filters` is an optional
    list of Dash components rendered as a filter row immediately below the
    section title (used by S8/S9 which have their own filter shelves in
    Tableau).
    """
    legend_children = []
    if legend:
        for label, color in legend:
            legend_children.append(html.Div([
                html.Span(className="legend-swatch",
                          style={"background": color}),
                html.Span(label, className="legend-label"),
            ], className="legend-item"))
    return html.Div([
        # Blue title bar. If we have local filters, put them inline on the
        # right side of the same bar (matching Tableau's shelf placement).
        html.Div([
            html.Div(title, className="section-title-text"),
            (html.Div(local_filters, className="section-local-filters-inline")
             if local_filters else None),
        ], className="section-title"),
        html.Div([
            html.Div(id=f"{section_id}-timeperiod", className="section-sub"),
            html.Div(id=f"{section_id}-window",     className="section-sub"),
            html.Div(id=f"{section_id}-episodes",   className="section-sub section-sub-strong"),
        ], className="section-sub-row"),
        html.Div([
            html.Div(legend_children, className="bod-legend") if legend else None,
            html.Div(
                dcc.Loading(dcc.Graph(
                    id=f"{section_id}-fig",
                    config={"displayModeBar": False, "responsive": True},
                    style={"height": f"{chart_height}px", "width": "100%"},
                    responsive=True,
                ), type="dot"),
                className="bod-chart-wrap",
            ),
            dcc.Loading(html.Div(id=f"{section_id}-table", className="bod-table"),
                        type="dot"),
        ], className="section-body"),
    ], className="section-card")


# ---------------------------------------------------------------------------
# Full page layout
# ---------------------------------------------------------------------------

def build_layout() -> html.Div:
    # Data-driven date extents so filter options reflect what actually exists
    # in the fact table (not a hardcoded 2020-2027 range).
    min_d, max_d, years = _date_extents()
    months = pd.date_range(min_d.replace(day=1), max_d.replace(day=1),
                           freq="MS").date
    # Pretty labels for month pickers ("Aug '25" instead of "2025-08-01").
    month_options = {str(m): m.strftime("%b '%y") for m in months}
    year_options = [str(y) for y in years]

    return html.Div([

        # -------- Top pfizer-style header --------
        html.Div([
            html.Div("Covid-19 Quality Care Insights Tool", className="app-title"),
            html.Div(id="header-refresh", className="header-refresh"),
        ], className="app-header"),

        # -------- Filter panel (Tableau-style grouped fieldsets) --------
        html.Div([
            # Row 1: General Parameters (wide) + Patient Parameters
            html.Div([
                _param_group("General Parameters", [
                    _dd_static("p-time-level", "Time Frequency", TIME_LEVELS, "quarter"),
                    _dd("p-year", "Year", year_options,
                        multi=True, value=[], placeholder="(All)"),
                    _dd("p-start-month", "Start Month",
                        month_options, multi=False,
                        value="2025-08-01" if "2025-08-01" in month_options
                              else str(min_d.replace(day=1))),
                    _dd("p-end-month", "End Month",
                        month_options, multi=False,
                        value="2026-07-01" if "2026-07-01" in month_options
                              else str(max_d.replace(day=1))),
                    _dd_static("p-granularity", "Select Granularity",
                               GRANULARITIES, "3"),
                    _dd("p-granularity-value", "Select Granularity Value",
                        [], multi=True, placeholder="(All)"),
                    _dd_static("p-encounter-window", "Follow-up Visit Window",
                               ENCOUNTER_WINDOWS, "1"),
                ], flex=8),
                _param_group("Patient Parameters", [
                    _dd("f-age-group", "Age Group", _domain("AGE_GROUP")),
                    _dd("f-gender",    "Gender",
                        _domain("PATIENT_GENDER", expr=GENDER_EXPR)),
                    _dd("f-payer",     "Payer Channel", _domain("PAYER_TYPE")),
                    _dd("f-hr-condition", "High Risk Condition",
                        [v for v in _domain("HIGH_RISK_CONDITION") if v != "ALL HR CONDITIONS"],
                        multi=True, value=[], placeholder="(All)"),
                ], flex=4),
            ], className="param-row"),

            # Row 2: Account/HCP Parameters + Apply/Reset buttons
            html.Div([
                _param_group("Account/HCP Parameters", [
                    _dd("f-grouped-account", "Grouped Accounts",
                        _domain("GROUPED_ACCOUNT")),
                    _dd("f-parent-id", "Parent Account",
                        _domain("PARENT_NAME", expr=PARENT_ACCOUNT_EXPR)),
                    _dd("f-child-id",  "Child Account",
                        _domain("CHILD_NAME",  expr=CHILD_ACCOUNT_EXPR,
                                top_n=500)),
                    _dd("f-specialty-group", "Specialty Group",
                        _domain("SPECIALTY_GROUP",
                                expr=PROVIDER_SPECIALTY_GROUP_EXPR)),
                    _dd("f-specialty", "Primary Specialty",
                        _domain("HCP_PRIMARY_SPECIALTY",
                                expr=PROVIDER_PRIMARY_SPECIALTY_EXPR)),
                    _dd("f-area", "Area Type", _domain("AREA_TYPE")),
                ], flex=1),
                html.Div([
                    html.Button("Apply Filters", id="apply-btn", className="apply-btn"),
                    html.Button("↻", id="reset-btn", className="reset-btn",
                                title="Reset filters"),
                ], className="param-actions"),
            ], className="param-row"),
        ], className="param-panel"),

        # -------- All 10 sections --------
        html.Div([
            _section("s1", "% Episodes With Follow-up Visits",
                     legend=[("Follow-Up Visit", "#2AA198"),
                             ("No Follow-Up Visit of Interest", "#B0B0B0")]),
            _section("s2", "% Episodes With Follow-up Visits Across Treated and Untreated Cohort",
                     legend=[("Follow-Up Visit", "#2AA198"),
                             ("No Follow-Up Visit of Interest", "#B0B0B0")]),
            _section("s3", "Quarterly % Episodes With Follow-up Visits Across Treated and Untreated Cohort",
                     chart_height=340,
                     legend=[("Follow-Up Visit", "#2AA198"),
                             ("No Follow-Up Visit of Interest", "#B0B0B0")]),
            _section("s4", "Average # Follow-up Visits Per Total Episodes",
                     chart_height=320,
                     legend=[("Treated", "#531679"),
                             ("Untreated", "#ed7239")]),
            _section("s5", "Characterizing Follow-up Visits",
                     chart_height=320,
                     legend=[
                         ("HOSPITALIZATION", "#97cfd0"),
                         ("ED VISIT",        "#466db0"),
                         ("UC VISIT",        "#b07aa1"),
                         ("LTAC/SNF VISIT",  "#e15759"),
                         ("OFFICE VISIT",    "#59a14f"),
                         ("TELEHEALTH VISIT","#edc948"),
                     ]),
            _section("s6", "Follow-up Visits across Treated and Untreated Cohort",
                     chart_height=320,
                     legend=[
                         ("HOSPITALIZATION", "#97cfd0"),
                         ("ED VISIT",        "#466db0"),
                         ("UC VISIT",        "#b07aa1"),
                         ("LTAC/SNF VISIT",  "#e15759"),
                         ("OFFICE VISIT",    "#59a14f"),
                         ("TELEHEALTH VISIT","#edc948"),
                     ]),
            _section("s7", "Quarterly Follow-up Visits Across Treated and Untreated Cohort",
                     chart_height=340,
                     legend=[
                         ("HOSPITALIZATION", "#97cfd0"),
                         ("ED VISIT",        "#466db0"),
                         ("UC VISIT",        "#b07aa1"),
                         ("LTAC/SNF VISIT",  "#e15759"),
                         ("OFFICE VISIT",    "#59a14f"),
                         ("TELEHEALTH VISIT","#edc948"),
                     ]),
            _section("s8", "#Follow-Up Visits Across High Risk Conditions",
                     chart_height=340,
                     local_filters=_hr_local_filters("s8")),
            _section("s9", "#Follow-Up Visits Across High Risk Conditions and Treated and Untreated Cohort",
                     chart_height=340,
                     legend=[("Treated", "#531679"),
                             ("Untreated", "#ed7239")]),
            _section("s10", "COVID-19 Initial Diagnosis/Treatment Location",
                     local_filters=_s10_local_filters(),
                     legend=[
                         ("HOSPITALIZATION", "#97cfd0"),
                         ("ED VISIT",        "#466db0"),
                         ("UC VISIT",        "#b07aa1"),
                         ("LTAC/SNF VISIT",  "#e15759"),
                         ("OFFICE VISIT",    "#59a14f"),
                         ("TELEHEALTH VISIT","#edc948"),
                         ("OTHERS",          "#ba9789"),
                         ("PHARMACY",        "#b0a983"),
                     ]),
        ], className="sections-wrap"),

        dcc.Store(id="bod-filters"),
    ])


# ---------------------------------------------------------------------------
# Callbacks — every heavy fetch is gated behind Apply button
# ---------------------------------------------------------------------------

def register_callbacks(app):

    # ---- Collect filters when Apply is clicked OR when a local S8 filter changes ----
    # Multi-selects now live in `dcc.Store(id="<filter>", data=[])` — the
    # store is written by the pattern-matching sync callback. Downstream
    # reads `.data`, single-selects still read `.value`.
    @app.callback(
        Output("bod-filters", "data"),
        Input("apply-btn", "n_clicks"),
        Input("s8-visit-filter", "value"),
        Input({"type": "ms-sql", "id": "s8-hr-filter"},    "data"),
        Input({"type": "ms-sql", "id": "s8-top7-items"},   "data"),
        Input({"type": "ms-sql", "id": "s10-visit-filter"}, "data"),
        State("p-encounter-window", "value"),
        State("p-time-level", "value"),
        State("p-granularity", "value"),
        State("p-start-month", "value"),
        State("p-end-month", "value"),
        State({"type": "ms-sql", "id": "f-hr-condition"},    "data"),
        State({"type": "ms-sql", "id": "f-age-group"},       "data"),
        State({"type": "ms-sql", "id": "f-gender"},          "data"),
        State({"type": "ms-sql", "id": "f-payer"},           "data"),
        State({"type": "ms-sql", "id": "f-area"},            "data"),
        State({"type": "ms-sql", "id": "f-specialty"},       "data"),
        State({"type": "ms-sql", "id": "f-specialty-group"}, "data"),
        State({"type": "ms-sql", "id": "f-parent-id"},       "data"),
        State({"type": "ms-sql", "id": "f-child-id"},        "data"),
        State({"type": "ms-sql", "id": "f-grouped-account"}, "data"),
        State({"type": "ms-sql", "id": "p-granularity-value"}, "data"),
    )
    def _collect(_n, s8_visit, s8_hr, s8_top7, s10_visit,
                 enc, lvl, gran, start, end, hr,
                 age, gen, pay, area, spec, sg, parent, child, gacc, gv):
        # VISIT is single-select — normalize to a list for the SQL builder.
        visit_list = [s8_visit] if s8_visit else []
        return BoDFilters(
            encounter_window=enc or "1",
            metric_mode=2.0,
            granularity=gran or "3",
            granularity_value=gv or [],
            start_month=date.fromisoformat(start) if start else date(2025, 8, 1),
            end_month=date.fromisoformat(end) if end else date(2026, 7, 1),
            time_level=lvl or "quarter",
            reference_line="No",
            hr_condition=hr or [],
            age_group=age or [], patient_gender=gen or [], payer_type=pay or [],
            area_type=area or [],
            hcp_primary_specialty=spec or [], specialty_group=sg or [],
            parent_id=parent or [], child_id=child or [], grouped_account=gacc or [],
            s8_visit_types=visit_list,
            s8_hr_conditions=s8_hr or [],
            s8_top7_items=s8_top7 or [],
            s8_top_n=7,
            s10_visit_types=s10_visit or [],
        ).to_store()

    # ---- Header refresh pill ----
    @app.callback(Output("header-refresh", "children"),
                  Input("bod-filters", "data"), prevent_initial_call=True)
    def _refresh(_data):
        try:
            df = run_query(*Q.q_data_refresh_date())
            d = str(df.iloc[0, 0])[:10]
        except Exception:
            d = "—"
        return f"Data Refresh Date: {d}"

    # ---- Reset ----
    # ---- Reset ----
    # Single-selects reset via .value; multi-selects reset by writing the
    # checklist (dict-ID) — the pattern-matching sync callback then rewires
    # button text, prev store, and SQL store. `allow_duplicate=True` because
    # the sync callback also writes to the same checklist Outputs.
    _MS_ID_TO_RESET_VALUE = {
        "f-hr-condition": None,   # None → checklist defaults to all-checked
        "f-age-group": None, "f-gender": None, "f-payer": None,
        "f-area": None, "f-specialty": None, "f-specialty-group": None,
        "f-parent-id": None, "f-child-id": None, "f-grouped-account": None,
        "s8-hr-filter": ["Top 7 HR Conditions"],
        "s8-top7-items": None,
        "s10-visit-filter": None,
    }

    def _reset_checklist_value(id_: str, options: list[str]) -> list[str]:
        v = _MS_ID_TO_RESET_VALUE.get(id_)
        if v is None:
            return [ALL_SENTINEL] + options
        return v

    _RESET_STATIC_OPTIONS = {
        "f-hr-condition": [v for v in _domain("HIGH_RISK_CONDITION")
                           if v != "ALL HR CONDITIONS"],
        "f-age-group": _domain("AGE_GROUP"),
        "f-gender": _domain("PATIENT_GENDER", expr=GENDER_EXPR),
        "f-payer": _domain("PAYER_TYPE"),
        "f-area": _domain("AREA_TYPE"),
        "f-specialty": _domain("HCP_PRIMARY_SPECIALTY",
                                expr=PROVIDER_PRIMARY_SPECIALTY_EXPR),
        "f-specialty-group": _domain("SPECIALTY_GROUP",
                                     expr=PROVIDER_SPECIALTY_GROUP_EXPR),
        "f-parent-id": _domain("PARENT_NAME", expr=PARENT_ACCOUNT_EXPR),
        "f-child-id":  _domain("CHILD_NAME",  expr=CHILD_ACCOUNT_EXPR,
                                top_n=500),
        "f-grouped-account": _domain("GROUPED_ACCOUNT"),
        "s10-visit-filter": ["HOSPITALIZATION", "ED VISIT", "UC VISIT",
                             "LTAC/SNF VISIT", "OFFICE VISIT",
                             "TELEHEALTH VISIT", "OTHERS", "PHARMACY"],
    }

    @app.callback(
        Output("p-encounter-window", "value"), Output("p-time-level", "value"),
        Output("p-granularity", "value"), Output("p-start-month", "value"),
        Output("p-end-month", "value"),
        Output("s8-visit-filter", "value"),
        Output({"type": "ms-chk",     "id": ALL}, "value",   allow_duplicate=True),
        Output({"type": "ms-chk",     "id": ALL}, "options", allow_duplicate=True),
        Output({"type": "ms-allopts", "id": ALL}, "data",    allow_duplicate=True),
        Input("reset-btn", "n_clicks"), prevent_initial_call="initial_duplicate",
    )
    def _reset(_n):
        # Hard reset: for every registered multi-select restore both the
        # full option list AND the checked value. Necessary because the
        # group cascades narrow options after Apply and the narrowed list
        # would otherwise persist even after the value is (All).
        chk_values: list = []
        chk_options: list = []
        allopts_data: list = []
        for id_ in _MULTI_DD_IDS:
            opts = _RESET_STATIC_OPTIONS.get(id_, [])
            chk_values.append(_reset_checklist_value(id_, opts))
            full_opts = _multi_options(opts)
            chk_options.append(full_opts)
            allopts_data.append(full_opts)
        return ("1", "quarter", "3", "2025-08-01", "2026-07-01",
                "ED VISIT", chk_values, chk_options, allopts_data)

    # ---- Cascading: Granularity → Granularity Value -----------------------
    # `p-granularity-value` is a DERIVED FILTER whose available options
    # depend on the current `p-granularity` PARAMETER selection. National
    # yields an empty list (no drill-down), Region/State/MSA populate from
    # the cohort-scoped fact table.
    @app.callback(
        Output({"type": "ms-chk",     "id": "p-granularity-value"}, "options",
               allow_duplicate=True),
        Output({"type": "ms-allopts", "id": "p-granularity-value"}, "data"),
        Output({"type": "ms-chk",     "id": "p-granularity-value"}, "value",
               allow_duplicate=True),
        Input("p-granularity", "value"),
        prevent_initial_call="initial_duplicate",
    )
    def _cascade_granularity_value(gran):
        # National yields no drilldown; Region/State/MSA fetch cohort-scoped
        # distinct values THROUGH the same calc used by `where_clause`.
        if gran == "1" or not gran:
            opts = _multi_options([])
            return opts, opts, [ALL_SENTINEL]
        expr = granularity_value_expr(gran)
        vals = [v for v in _domain(f"gv_{gran}", expr=expr) if v]
        opts = _multi_options(vals)
        return opts, opts, [ALL_SENTINEL] + vals

    # ---- Tier-2 relevant-values recompute --------------------------------
    # DISABLED — this callback fired 10 SELECT DISTINCT queries on every
    # time-context change and blocked page load. Parent Account and Child
    # Account each return hundreds of thousands of distinct calc-strings
    # (Snowflake IDs + names + city + state), which also drowns the browser
    # JSON payload. The helper `_relevant_domain(...)` is kept in place for
    # future opt-in use (e.g. a per-popover "narrow options" toggle) but no
    # callback is registered right now, so every filter keeps its full
    # cohort-scoped option list.

    # ---- Unified cross-group cascade -------------------------------------
    # Year + Granularity-Value + Patient + Account/HCP filters are ALL
    # mutually dependent. On Apply, every filter's option list is recomputed
    # against the OTHER filters' current selections (Tableau "Only Relevant
    # Values"). Only Parent Account and Child Account use a top_n cap; every
    # other filter loads its full cohort domain. `p-granularity-value` uses
    # a runtime expression because its underlying column varies with the
    # granularity parameter (Region / State / MSA).
    _DATA_GROUP: list[tuple[str, str, str | None, int | None]] = [
        # (filter-id, physical column, calc expression, top_n cap)
        ("p-year",            "YEAR",
         "DATE_PART('year', EPISODE_START_DATE::date)::VARCHAR",         None),
        ("f-age-group",       "AGE_GROUP",             None,                          None),
        ("f-gender",          "PATIENT_GENDER",        GENDER_EXPR,                   None),
        ("f-payer",           "PAYER_TYPE",            None,                          None),
        ("f-hr-condition",    "HIGH_RISK_CONDITION",   None,                          None),
        ("f-grouped-account", "GROUPED_ACCOUNT",       None,                          None),
        ("f-parent-id",       "PARENT_NAME",           PARENT_ACCOUNT_EXPR,           None),
        ("f-child-id",        "CHILD_NAME",            CHILD_ACCOUNT_EXPR,            500),
        ("f-specialty-group", "SPECIALTY_GROUP",       PROVIDER_SPECIALTY_GROUP_EXPR, None),
        ("f-specialty",       "HCP_PRIMARY_SPECIALTY", PROVIDER_PRIMARY_SPECIALTY_EXPR, None),
        ("f-area",            "AREA_TYPE",             None,                          None),
    ]

    _DATA_GROUP_IDS = [spec[0] for spec in _DATA_GROUP]

    @app.callback(
        # Options / allopts / value outputs for the 11 static filters …
        *[Output({"type": "ms-chk",     "id": fid}, "options", allow_duplicate=True)
          for fid in _DATA_GROUP_IDS],
        *[Output({"type": "ms-allopts", "id": fid}, "data",    allow_duplicate=True)
          for fid in _DATA_GROUP_IDS],
        *[Output({"type": "ms-chk",     "id": fid}, "value",   allow_duplicate=True)
          for fid in _DATA_GROUP_IDS],
        # … and the granularity-value filter (dynamic column expression).
        Output({"type": "ms-chk",     "id": "p-granularity-value"}, "options", allow_duplicate=True),
        Output({"type": "ms-allopts", "id": "p-granularity-value"}, "data",    allow_duplicate=True),
        Output({"type": "ms-chk",     "id": "p-granularity-value"}, "value",   allow_duplicate=True),
        Input("apply-btn", "n_clicks"),
        State("p-granularity", "value"),
        *[State({"type": "ms-sql", "id": fid}, "data")  for fid in _DATA_GROUP_IDS],
        *[State({"type": "ms-chk", "id": fid}, "value") for fid in _DATA_GROUP_IDS],
        State({"type": "ms-sql", "id": "p-granularity-value"}, "data"),
        State({"type": "ms-chk", "id": "p-granularity-value"}, "value"),
        prevent_initial_call="initial_duplicate",
    )
    def _cascade_all(_n, gran, *args):
        n = len(_DATA_GROUP_IDS)
        sels = list(args[:n])
        curs = list(args[n:2 * n])
        gv_sel, gv_cur = args[2 * n], args[2 * n + 1]

        # Build the dynamic granularity-value spec (Region / State / MSA).
        # For National (gran == "1") there is no drill-down; we handle it
        # by returning empty options / (All) for its three outputs.
        gv_expr = granularity_value_expr(gran) if gran and gran != "1" else None
        gv_col  = f"gv_{gran}" if gran else "gv_None"
        gv_entry = ("p-granularity-value", gv_col, gv_expr, None)

        full_group = _DATA_GROUP + [gv_entry]
        selections = {**dict(zip(_DATA_GROUP_IDS, sels)),
                      "p-granularity-value": gv_sel}
        currents   = {**dict(zip(_DATA_GROUP_IDS, curs)),
                      "p-granularity-value": gv_cur}

        flat = _run_group_cascade(full_group, selections, currents)
        opts   = [flat[i * 3]     for i in range(len(full_group))]
        allops = [flat[i * 3 + 1] for i in range(len(full_group))]
        vals   = [flat[i * 3 + 2] for i in range(len(full_group))]

        # National → empty gran-value.
        if gv_expr is None:
            empty = _multi_options([])
            opts[-1], allops[-1], vals[-1] = empty, empty, [ALL_SENTINEL]

        # Callback declares outputs in blocks: 11 options, 11 allopts,
        # 11 values, then p-granularity-value opts/allopts/value.
        # Our `flat` is in group order, so the last element is gran-value.
        return (
            *opts[:n],  *allops[:n],  *vals[:n],
            opts[-1],    allops[-1],   vals[-1],
        )

    # ---- Dynamic Top-7 options for Filter-3 ----
    # The checklists live at dict-ID {"type": "ms-chk", "id": <filter-id>},
    # so we target them by that dict address.
    @app.callback(
        Output({"type": "ms-chk",     "id": "s8-top7-items"}, "options",
               allow_duplicate=True),
        Output({"type": "ms-allopts", "id": "s8-top7-items"}, "data"),
        Output({"type": "ms-chk",     "id": "s8-hr-filter"},  "options",
               allow_duplicate=True),
        Output({"type": "ms-allopts", "id": "s8-hr-filter"},  "data"),
        Input("bod-filters", "data"),
        prevent_initial_call="initial_duplicate",
    )
    def _refresh_hr_filter_options(store):
        try:
            f = BoDFilters.from_store(store)
            df = run_query(*Q.q_s8_top7_list(f))
            top7 = df["HIGH_RISK_CONDITION"].tolist() if not df.empty else []
        except Exception:
            top7 = []

        # Filter 3 (Top-N picker): the actual top-N HR names + 'Other Conditions'.
        top7_options = _multi_options(top7 + ["Other Conditions"])

        # Filter 2 (HR grouping): everything EXCEPT the top-N + the 'Top 7
        # HR Conditions' rollup, mirroring the workbook's `High Risk Top 7`
        # calc domain.
        hr_all = [v for v in _domain("HIGH_RISK_CONDITION")
                  if v not in ("ALL HR CONDITIONS", "NO HIGH RISK CONDITION")
                  and v not in top7]
        f2_options = _multi_options(hr_all + ["Top 7 HR Conditions"])
        return top7_options, top7_options, f2_options, f2_options

    # ---- Section-1 to Section-10 ----
    _bind_section(app, "s1", Q.q_s1_episodes_by_bucket, _render_s1)
    _bind_section(app, "s2", Q.q_s2_episodes_by_cohort, _render_s2)
    _bind_section(app, "s3", Q.q_s3_episodes_by_bucket_cohort, _render_s3)
    _bind_section(app, "s4", Q.q_s4_avg_visits_per_episode, _render_s4)
    _bind_section(app, "s5", Q.q_s5_visit_mix_by_bucket, _render_s5)
    _bind_section(app, "s6", Q.q_s6_visit_mix_by_cohort, _render_s6)
    _bind_section(app, "s7", Q.q_s7_visit_mix_by_bucket_cohort, _render_s7)
    _bind_section(app, "s8", Q.q_s8_hr_conditions, _render_s8)
    _bind_section(app, "s9", Q.q_s9_hr_conditions_by_cohort, _render_s9)
    _bind_section(app, "s10", Q.q_s10_covid_dx_location, _render_s10)

    # ---- Single-select popover: update button text + auto-close ----------
    # One clientside callback per registered single-select maps
    # RadioItems.value → button-text span; a second closes the popover.
    for ss_id in _SS_IDS:
        app.clientside_callback(
            """function(v, options){
                if (v == null) return '';
                var opts = options || [];
                for (var i=0;i<opts.length;i++){
                    if (opts[i].value === v) return String(opts[i].label);
                }
                return String(v);
            }""",
            Output({"type": "ss-btntext", "id": ss_id}, "children"),
            [Input(ss_id, "value"), Input(ss_id, "options")],
        )
        app.clientside_callback(
            """function(v){ return false; }""",
            Output({"type": "ss-pop", "id": ss_id}, "is_open", allow_duplicate=True),
            Input(ss_id, "value"),
            prevent_initial_call=True,
        )

    # ---- Multi-select checkbox popover sync (Tableau-style) -----------------
    # The sync callback reads the IMMUTABLE full option list from
    # `ms-allopts` (not the possibly search-filtered `ms-chk.options`) so
    # that the (All) collapse rule uses the true total count. `ms-chk.value`
    # is preserved even if a row is temporarily hidden by search.
    app.clientside_callback(
        """function(cur, allOpts, prev){
            var ALL = '__ALL__';
            var opts = (allOpts || []).filter(function(o){ return o.value !== ALL; });
            var total = opts.length;
            var realOpts = opts.map(function(o){ return o.value; });
            cur  = Array.isArray(cur)  ? cur.slice()  : [];
            prev = Array.isArray(prev) ? prev.slice() : [];
            var allHad  = prev.indexOf(ALL) >= 0;
            var allNow  = cur.indexOf(ALL)  >= 0;
            var realNow = cur.filter(function(x){ return x !== ALL; });

            var newChk;
            if (allHad && !allNow) {
                newChk = [];                              // (All) unchecked → clear
            } else if (!allHad && allNow) {
                newChk = [ALL].concat(realOpts);          // (All) checked → select every real
            } else if (total > 0 && realNow.length === total && !allNow) {
                newChk = [ALL].concat(realNow);           // every real manually checked → auto-(All)
            } else if (allNow && realNow.length < total) {
                newChk = realNow;                         // one real unchecked while (All) on → drop (All)
            } else {
                newChk = cur;
            }

            var finalReal = newChk.filter(function(x){ return x !== ALL; });
            var finalHasAll = newChk.indexOf(ALL) >= 0;

            var sqlVal, text;
            if (finalHasAll || finalReal.length === total) {
                sqlVal = [];
                text = '(All)';
            } else if (finalReal.length === 0) {
                sqlVal = [];       // treat (None) as All for SQL
                text = '(None)';
            } else if (finalReal.length === 1) {
                sqlVal = finalReal;
                text = String(finalReal[0]);
            } else {
                sqlVal = finalReal;
                text = '(Multiple Values)';
            }
            return [newChk, newChk, sqlVal, text];
        }""",
        [Output({"type": "ms-chk",     "id": MATCH}, "value",    allow_duplicate=True),
         Output({"type": "ms-prev",    "id": MATCH}, "data"),
         Output({"type": "ms-sql",     "id": MATCH}, "data"),
         Output({"type": "ms-btntext", "id": MATCH}, "children")],
        [Input({"type": "ms-chk",     "id": MATCH}, "value"),
         Input({"type": "ms-allopts", "id": MATCH}, "data")],
        [State({"type": "ms-prev",    "id": MATCH}, "data")],
        prevent_initial_call="initial_duplicate",
    )

    # ---- Multi-select search: filters visible checklist rows ---------------
    # Case-insensitive contains match on option labels. Selected values stay
    # checked even when their row is hidden. Clearing the search box restores
    # every relevant option.
    app.clientside_callback(
        """function(q, allOpts){
            var opts = allOpts || [];
            if (!q || !String(q).trim()) return opts;
            var qq = String(q).trim().toLowerCase();
            return opts.filter(function(o){
                if (o.value === '__ALL__') return true;
                return String(o.label).toLowerCase().indexOf(qq) >= 0;
            });
        }""",
        Output({"type": "ms-chk",    "id": MATCH}, "options", allow_duplicate=True),
        Input({"type": "ms-search",  "id": MATCH}, "value"),
        State({"type": "ms-allopts", "id": MATCH}, "data"),
        prevent_initial_call="initial_duplicate",
    )


# ---------------------------------------------------------------------------
# Section binder
# ---------------------------------------------------------------------------

def _bind_section(app, section_id: str, query_fn, render_fn):
    @app.callback(
        Output(f"{section_id}-fig", "figure"),
        Output(f"{section_id}-table", "children"),
        Output(f"{section_id}-timeperiod", "children"),
        Output(f"{section_id}-window", "children"),
        Output(f"{section_id}-episodes", "children"),
        Input("bod-filters", "data"),
        prevent_initial_call=True,
    )
    def _cb(store):
        f = BoDFilters.from_store(store)
        try:
            sql, binds = query_fn(f)
        except Exception as exc:  # noqa: BLE001
            logging.exception("Failed to build SQL for section %s", section_id)
            return (th._empty_figure(f"SQL build error: {exc}"),
                    html.Div(f"Error building SQL: {exc}", className="err"),
                    "", "", "")
        try:
            df = run_query(sql, binds)
        except Exception as exc:  # noqa: BLE001
            logging.exception("Query failed for section %s\nSQL:\n%s", section_id, sql)
            preview = (sql or "")[:400].replace("\n", " ")
            return (th._empty_figure(f"Query error: {exc}"),
                    html.Div([
                        html.Div(f"Error: {exc}", className="err"),
                        html.Details([
                            html.Summary("Show SQL"),
                            html.Pre(sql, style={"whiteSpace": "pre-wrap",
                                                  "fontSize": "10px",
                                                  "background": "#F5F5F5",
                                                  "padding": "8px",
                                                  "overflow": "auto"}),
                        ]),
                    ], className="err"),
                    "", "", "")
        fig, table = render_fn(df, f)
        # sub-header pills
        tp = f"Time Period: {f.start_month:%B %Y} - {f.end_month:%B %Y}"
        win = f"Follow-up Visit Window: {ENCOUNTER_WINDOWS[f.encounter_window]}"
        try:
            hc = run_query(*Q.q_headline_counts(f))
            eps_val = int(hc.iloc[0, 0])
        except Exception:
            eps_val = None
        # Section-1 style shows "#Episodes" + "#Follow-up Visits" on SEPARATE
        # lines, matching Tableau's sub-header stack.
        eps_lines = []
        if eps_val is not None:
            eps_lines.append(html.Div(f"#Episodes: {eps_val:,}"))
        if "TOTAL_FOLLOWUP_VISITS" in getattr(df, "columns", []):
            # In visit-mix sections (S5/S6/S7), TOTAL_FOLLOWUP_VISITS is a
            # bar-group total joined onto every visit-type row — so summing
            # naively over-counts by the number of visit types. Dedupe by
            # the bar-group dimension columns before summing.
            group_cols = [c for c in ("TIME_BUCKET", "COHORT")
                          if c in df.columns]
            if group_cols and ("VISIT" in df.columns or "VISIT_TYPE" in df.columns):
                fu_total = int(df.drop_duplicates(group_cols)
                                 ["TOTAL_FOLLOWUP_VISITS"].sum())
            else:
                fu_total = int(df["TOTAL_FOLLOWUP_VISITS"].sum())
            eps_lines.append(html.Div(f"#Follow-up Visits: {fu_total:,}"))
        eps = eps_lines
        return fig, table, tp, win, eps


# ---------------------------------------------------------------------------
# Per-section renderers -> (figure, table html)
# ---------------------------------------------------------------------------

def _bucket_labels(df: pd.DataFrame, level: str) -> list[str]:
    def fmt(d):
        d = pd.to_datetime(d)
        if level == "month":
            return d.strftime("%b %Y")
        if level == "year":
            return d.strftime("%Y")
        return f"Q{(d.month - 1) // 3 + 1} {d.year}"
    return [fmt(x) for x in df["TIME_BUCKET"]]


def _render_s1(df, f):
    if df is None or df.empty:
        return th._empty_figure(), _empty_table()
    labels = _bucket_labels(df, f.time_level)
    fig = th.followup_stacked(df, x_col="TIME_BUCKET", x_labels=labels)
    tbl = _matrix_table(labels, [
        ("# Total Episodes",
            [_i(v) for v in df["TOTAL_EPISODES"]]),
        ("# Episodes with No Follow-Up Visit of Interest",
            [_i(v) for v in df["EPISODES_NO_FOLLOWUP"]]),
        ("% Episodes with No Follow-Up Visit of Interest",
            [_pct(v) for v in (df["EPISODES_NO_FOLLOWUP"] / df["TOTAL_EPISODES"])]),
        ("# Episodes with Follow-Up Visits",
            [_i(v) for v in df["EPISODES_WITH_FOLLOWUP"]]),
        ("% Episodes with Follow-Up Visits",
            [_pct(v) for v in df["PCT_FOLLOWUP"]]),
        ("# Follow-Up Visits",
            [_i(v) for v in df["TOTAL_FOLLOWUP_VISITS"]]),
    ])
    return fig, tbl


def _render_s2(df, f):
    if df is None or df.empty:
        return th._empty_figure(), _empty_table()
    fig = th.followup_stacked(df, x_col="COHORT")
    labels = df["COHORT"].tolist()
    # Cohort's share of overall episodes (matches Tableau's "(47%)" / "(53%)")
    grand_total = int(df["TOTAL_EPISODES"].sum()) or 1
    tbl = _matrix_table(labels, [
        ("# Total Episodes (% treated or untreated)",
            [f"{_i(v)} ({int(v)/grand_total:.0%})" for v in df["TOTAL_EPISODES"]]),
        ("# Episodes with No Follow-Up Visit of Interest",
            [_i(v) for v in df["EPISODES_NO_FOLLOWUP"]]),
        ("% Episodes with No Follow-Up Visit of Interest",
            [_pct(v) for v in (df["EPISODES_NO_FOLLOWUP"] / df["TOTAL_EPISODES"])]),
        ("# Episodes with Follow-Up Visits",
            [_i(v) for v in df["EPISODES_WITH_FOLLOWUP"]]),
        ("% Episodes with Follow-Up Visits",
            [_pct(v) for v in df["PCT_FOLLOWUP"]]),
        ("# Follow-Up Visits",
            [_i(v) for v in df["TOTAL_FOLLOWUP_VISITS"]]),
    ])
    return fig, tbl


def _render_s3(df, f):
    if df is None or df.empty:
        return th._empty_figure(), _empty_table()
    df = df.copy()
    df["LABEL"] = _bucket_labels(df, f.time_level)
    fig = th.followup_stacked_grouped_by_cohort(
        df, bucket_col="LABEL", bucket_labels=list(df["LABEL"].unique()))

    # Two-level table headers: quarter → Treated/Untreated
    # Order columns as (Q3 Treated, Q3 Untreated, Q4 Treated, Q4 Untreated, ...)
    quarters = list(df["LABEL"].unique())
    cohorts = ["Treated", "Untreated"]
    df_i = df.set_index(["LABEL", "COHORT"])

    def col_vals(col: str):
        return [df_i.loc[(q, c), col] if (q, c) in df_i.index else 0
                for q in quarters for c in cohorts]

    # Cohort share within quarter (e.g. "699,423 (55%)")
    per_quarter_totals = df.groupby("LABEL")["TOTAL_EPISODES"].sum().to_dict()
    total_cells = []
    for q in quarters:
        qt = per_quarter_totals[q] or 1
        for c in cohorts:
            v = df_i.loc[(q, c), "TOTAL_EPISODES"] if (q, c) in df_i.index else 0
            total_cells.append(f"{_i(v)} ({int(v)/qt:.0%})")

    tbl = _grouped_matrix_table(
        quarters, cohorts,
        [
            ("# Total Episodes (% treated or untreated)", total_cells),
            ("# Episodes with No Follow-Up Visit of Interest",
                [_i(v) for v in col_vals("EPISODES_NO_FOLLOWUP")]),
            ("% Episodes with No Follow-Up Visit of Interest",
                [_pct(a / b) if b else "—"
                 for a, b in zip(col_vals("EPISODES_NO_FOLLOWUP"),
                                 col_vals("TOTAL_EPISODES"))]),
            ("# Episodes with Follow-Up Visits",
                [_i(v) for v in col_vals("EPISODES_WITH_FOLLOWUP")]),
            ("% Episodes with Follow-Up Visits",
                [_pct(v) for v in col_vals("PCT_FOLLOWUP")]),
            ("# Follow-Up Visits",
                [_i(v) for v in col_vals("TOTAL_FOLLOWUP_VISITS")]),
        ],
    )
    return fig, tbl


def _render_s4(df, f):
    if df is None or df.empty:
        return th._empty_figure(), _empty_table()
    df = df.copy()
    df["LABEL"] = _bucket_labels(df, f.time_level)
    quarters = list(df["LABEL"].unique())
    cohorts = ["Treated", "Untreated"]
    fig = th.avg_visits_grouped(df, bucket_labels=quarters)

    df_i = df.set_index(["LABEL", "COHORT"])
    def col_vals(col: str):
        return [df_i.loc[(q, c), col] if (q, c) in df_i.index else 0
                for q in quarters for c in cohorts]

    # cohort share within each quarter (e.g. "699,423 (55%)")
    per_quarter_totals = df.groupby("LABEL")["TOTAL_EPISODES"].sum().to_dict()
    total_cells = []
    for q in quarters:
        qt = per_quarter_totals[q] or 1
        for c in cohorts:
            v = df_i.loc[(q, c), "TOTAL_EPISODES"] if (q, c) in df_i.index else 0
            total_cells.append(f"{_i(v)} ({int(v)/qt:.0%})")

    tbl = _grouped_matrix_table(
        quarters, cohorts,
        [
            ("# Total Episodes (% treated or untreated)", total_cells),
            ("# Episodes with Follow-Up Visits",
                [_i(v) for v in col_vals("EPISODES_WITH_FOLLOWUP")]),
            ("# Follow-Up Visits",
                [_i(v) for v in col_vals("TOTAL_FOLLOWUP_VISITS")]),
            ("Average # Follow-Up Visits per Total Episodes",
                [f"{v:.1f}" if v else "—" for v in col_vals("AVG_VISITS_PER_EPISODE")]),
        ],
    )
    return fig, tbl


def _render_visit_mix(df, f, *, group_by_cohort: bool, group_by_bucket: bool):
    """Shared renderer for S5 / S6 / S7 visit-type mix sections.

    Table rows match Tableau's layout:
        1. # Total Episodes  (per bar-group)
        2. # Follow-up Visits (per bar-group total)
        3..N. # <visit type> Follow-up Visits (per bar-group per visit type)
    """
    if df is None or df.empty:
        return th._empty_figure(), _empty_table()
    df = df.copy()
    # SQL returns VISIT_TYPE (aliased to avoid colliding with the physical
    # column named `VISIT`). Chart helpers expect a column called VISIT.
    if "VISIT_TYPE" in df.columns and "VISIT" not in df.columns:
        df = df.rename(columns={"VISIT_TYPE": "VISIT"})

    # ---- Build bar-label order matching the table columns ----
    if group_by_bucket:
        df["LABEL"] = _bucket_labels(df, f.time_level)
        quarters = list(dict.fromkeys(df["LABEL"].tolist()))
    else:
        quarters = None

    if group_by_bucket and group_by_cohort:
        cohorts = ["Treated", "Untreated"]
        x_labels = [f"{q}|{c}" for q in quarters for c in cohorts]
        key_cols = ["LABEL", "COHORT"]
        df["_XKEY"] = df["LABEL"].astype(str) + "|" + df["COHORT"].astype(str)
    elif group_by_bucket:
        cohorts = None
        x_labels = quarters
        key_cols = ["LABEL"]
        df["_XKEY"] = df["LABEL"].astype(str)
    else:  # group_by_cohort only
        cohorts = ["Treated", "Untreated"]
        x_labels = cohorts
        key_cols = ["COHORT"]
        df["_XKEY"] = df["COHORT"].astype(str)

    fig = th.visit_mix_stacked(df, x_labels=x_labels, key_cols=key_cols)

    # ---- Build table (# Total Episodes → # FU Visits → per-type rows) ----
    # Table row order matches Tableau's legend / table order (independent
    # of the stack order used in the chart).
    TABLE_ORDER = [
        "HOSPITALIZATION", "ED VISIT", "UC VISIT",
        "LTAC/SNF VISIT", "OFFICE VISIT", "TELEHEALTH VISIT",
        "PHARMACY", "OTHERS",
    ]
    # Display labels as they appear in the Tableau workbook.
    DISPLAY_NAMES = {
        "HOSPITALIZATION":  "# Hospitalizations",
        "ED VISIT":         "# ED Visits",
        "UC VISIT":         "# UC Visits",
        "LTAC/SNF VISIT":   "# LTAC/SNF Visits",
        "OFFICE VISIT":     "# Office Visits",
        "TELEHEALTH VISIT": "# Telehealth Visits",
        "PHARMACY":         "# Pharmacy",
        "OTHERS":           "# Others",
    }
    visit_types = [v for v in TABLE_ORDER if v in df["VISIT"].unique()]
    visit_types += [v for v in df["VISIT"].unique() if v not in visit_types]

    pivot = df.pivot_table(index="VISIT", columns="_XKEY", values="N_VISITS",
                            aggfunc="sum", fill_value=0)
    pivot = pivot.reindex(index=visit_types, columns=x_labels, fill_value=0)

    # Per-bar totals — pulled from the joined-in `totals` CTE.
    totals_df = (df.groupby("_XKEY")[["TOTAL_EPISODES", "TOTAL_FOLLOWUP_VISITS"]]
                   .first()
                   .reindex(x_labels, fill_value=0))
    total_ep    = totals_df["TOTAL_EPISODES"].tolist()
    total_fu    = totals_df["TOTAL_FOLLOWUP_VISITS"].tolist()

    # When there's a cohort split, add the within-group share to the total.
    # Denominator is the sum of Treated + Untreated for the same quarter
    # (S7), or overall Treated + Untreated (S6).
    if group_by_cohort:
        if group_by_bucket:
            # x_labels look like "Q3 2025|Treated" — group by quarter prefix
            def denom_for(x: str) -> int:
                q = x.split("|", 1)[0]
                return sum(v for lbl, v in zip(x_labels, total_ep)
                           if lbl.split("|", 1)[0] == q) or 1
            total_ep_cells = [
                f"{_i(v)} ({int(v)/denom_for(lbl):.0%})"
                for lbl, v in zip(x_labels, total_ep)
            ]
        else:
            denom = sum(total_ep) or 1
            total_ep_cells = [
                f"{_i(v)} ({int(v)/denom:.0%})" for v in total_ep
            ]
        total_label = "# Total Episodes (% treated or untreated)"
    else:
        total_ep_cells = [_i(v) for v in total_ep]
        total_label = "# Total Episodes"

    rows: list[tuple[str, list]] = [
        (total_label, total_ep_cells),
        ("# Follow-up Visits", [_i(v) for v in total_fu]),
    ]
    for v in visit_types:
        label = DISPLAY_NAMES.get(v, f"# {v.title()}")
        rows.append((label, [_i(x) for x in pivot.loc[v].tolist()]))

    if group_by_bucket and group_by_cohort:
        tbl = _grouped_matrix_table(quarters, cohorts, rows)
    elif group_by_bucket:
        tbl = _matrix_table(quarters, rows)
    else:
        tbl = _matrix_table(cohorts, rows)
    return fig, tbl


def _render_s5(df, f): return _render_visit_mix(df, f, group_by_cohort=False, group_by_bucket=True)
def _render_s6(df, f): return _render_visit_mix(df, f, group_by_cohort=True,  group_by_bucket=False)
def _render_s7(df, f): return _render_visit_mix(df, f, group_by_cohort=True,  group_by_bucket=True)


def _render_s8(df, f):
    if df is None or df.empty:
        return th._empty_figure(), _empty_table()
    df = df.copy().reset_index(drop=True)
    fig = th.hr_condition_bar(df)

    # Dynamic row-1 label — matches Tableau's "# Episodes with <VISIT>
    # Follow-Up Visits" that names the filter value.
    if f.s8_visit_types:
        visit_lbl = _pretty_visit(f.s8_visit_types[0]) if len(f.s8_visit_types) == 1 \
                    else "Selected"
    else:
        visit_lbl = ""
    row1 = ("# Episodes with " + (visit_lbl + " " if visit_lbl else "") + "Follow-Up Visits").replace("  ", " ")

    # Column headers: use the actual VISIT value for the reference column,
    # HR condition name for the rest.
    hr_names = df["HIGH_RISK_CONDITION"].tolist()
    ref_header = (f.s8_visit_types[0] if len(f.s8_visit_types) == 1
                  else ("All Visits" if not f.s8_visit_types else "Selected"))
    columns = [ref_header] + hr_names[1:]

    ref_ep = float(df["N_EPISODES_WITH_FU"].iloc[0] or 0)
    ref_fu = float(df["N_FOLLOWUP_VISITS"].iloc[0] or 0)

    def _cell(v: float, ref: float, is_ref: bool) -> str:
        if is_ref:
            return _i(v)
        return f"{_i(v)} ({(v/ref if ref else 0):.0%})"

    row_ep = [_cell(v, ref_ep, i == 0) for i, v in enumerate(df["N_EPISODES_WITH_FU"])]
    row_fu = [_cell(v, ref_fu, i == 0) for i, v in enumerate(df["N_FOLLOWUP_VISITS"])]

    tbl = _matrix_table(columns, [
        (row1,                     row_ep),
        ("# Follow-Up Visits",     row_fu),
    ], first_col_highlight=True)
    return fig, tbl


def _pretty_visit(v: str) -> str:
    """Convert 'ED VISIT' → 'ED', 'OFFICE VISIT' → 'Office', etc. for the
    dynamic row-1 label."""
    mapping = {
        "ED VISIT": "ED",
        "OFFICE VISIT": "Office",
        "TELEHEALTH VISIT": "Telehealth",
        "UC VISIT": "UC",
        "LTAC/SNF VISIT": "LTAC/SNF",
        "HOSPITALIZATION": "Hospitalization",
    }
    return mapping.get(v, v.title())


def _render_s9(df, f):
    """Section-9: HR conditions × Treated/Untreated cohort.

    - Reference column first (Total for the selected VISIT), rest are the
      current top-N HR conditions.
    - Each column has two sub-columns (Treated / Untreated) in the table.
    - Chart shows Treated + Untreated bars grouped per HR category, cohort
      share % inside each bar.
    """
    if df is None or df.empty:
        return th._empty_figure(), _empty_table()
    df = df.copy().reset_index(drop=True)

    # Preserve the order emitted by SQL (REFERENCE first, then top-N by desc).
    hr_order: list[str] = []
    for h in df["HIGH_RISK_CONDITION"]:
        if h not in hr_order:
            hr_order.append(h)

    fig = th.hr_condition_by_cohort_bar(df, hr_order=hr_order)

    # Header labels: use raw VISIT value for the reference column.
    ref_header = (f.s8_visit_types[0] if len(f.s8_visit_types) == 1
                  else ("All Visits" if not f.s8_visit_types else "Selected"))
    top_headers = [ref_header] + [h for h in hr_order[1:]]

    cohorts = ["Treated", "Untreated"]

    # Dynamic row-1 label.
    if f.s8_visit_types:
        visit_lbl = _pretty_visit(f.s8_visit_types[0]) if len(f.s8_visit_types) == 1 \
                    else "Selected"
    else:
        visit_lbl = ""
    row1 = ("# Episodes with " + (visit_lbl + " " if visit_lbl else "") + "Follow-Up Visits").replace("  ", " ")

    # ---- Build the 2 × (N_top+1) matrix of values ----
    idx = df.set_index(["HIGH_RISK_CONDITION", "COHORT"])

    def col_vals(col: str):
        out = []
        for h in hr_order:
            for c in cohorts:
                v = float(idx.loc[(h, c), col]) if (h, c) in idx.index else 0.0
                out.append(v)
        return out

    ep_vals = col_vals("N_EPISODES_WITH_FU")
    fu_vals = col_vals("N_FOLLOWUP_VISITS")

    # Percentage denominator: sum of Treated + Untreated within each HR category.
    def with_pct(vals: list[float]) -> list[str]:
        cells = []
        for i in range(0, len(vals), 2):
            t, u = vals[i], vals[i + 1]
            total = t + u
            cells.append(f"{_i(t)} ({(t/total if total else 0):.0%})")
            cells.append(f"{_i(u)} ({(u/total if total else 0):.0%})")
        return cells

    tbl = _grouped_matrix_table(
        top_headers, cohorts,
        [
            (row1,                  with_pct(ep_vals)),
            ("# Follow-Up Visits",  with_pct(fu_vals)),
        ],
        first_group_highlight=True,
    )
    return fig, tbl


def _render_s10(df, f):
    if df is None or df.empty:
        return th._empty_figure(), _empty_table()
    df = df.copy()
    df["LABEL"] = _bucket_labels(df, f.time_level)
    quarters = list(dict.fromkeys(df["LABEL"].tolist()))

    fig = th.visit_mix_stacked(
        df, x_labels=quarters, key_cols=["LABEL"], value_col="N_EPISODES",
        yaxis_title="% COVID-19 Initial Diagnosis/Treatment Location",
    )

    pivot = df.pivot_table(index="VISIT", columns="LABEL", values="N_EPISODES",
                           aggfunc="sum", fill_value=0)
    pivot = pivot.reindex(columns=quarters, fill_value=0)
    totals = pivot.sum(axis=0)

    # Order rows top-to-bottom by Tableau's stack (top-of-stack first).
    ordered = [v for v in reversed(th.VISIT_STACK_ORDER) if v in pivot.index]
    ordered += [v for v in pivot.index if v not in ordered]

    rows = [("Total # Episodes with Covid-19 Initial Diagnosis/Treatment Location",
             [_i(v) for v in totals.tolist()])]
    for visit in ordered:
        rows.append((f"# {visit}", [_i(x) for x in pivot.loc[visit].tolist()]))
    tbl = _matrix_table(quarters, rows)
    return fig, tbl


# ---------------------------------------------------------------------------
# Matrix-table renderer (row = metric, col = X-axis label)
# ---------------------------------------------------------------------------

def _matrix_table(columns: list[str], rows: list[tuple[str, list]],
                    first_col_highlight: bool = False):
    hdr_cells = [html.Th("", className="row-label")]
    for i, c in enumerate(columns):
        cls = "num"
        if first_col_highlight and i == 0:
            cls += " reference-col-header"
        hdr_cells.append(html.Th(c, className=cls))
    thead = html.Thead(html.Tr(hdr_cells))

    body_rows = []
    for label, values in rows:
        cells = [html.Td(label, className="row-label")]
        for i, v in enumerate(values):
            cls = "num"
            if first_col_highlight and i == 0:
                cls += " reference-col-cell"
            cells.append(html.Td(v, className=cls))
        body_rows.append(html.Tr(cells))
    tbody = html.Tbody(body_rows)
    return html.Table([thead, tbody])


def _grouped_matrix_table(groups: list[str], subcols: list[str],
                           rows: list[tuple[str, list]],
                           first_group_highlight: bool = False):
    """Table with two-level column headers.

    `groups` is the top-level list, `subcols` is the inner list. Every row's
    values list has length `len(groups) * len(subcols)`, ordered group-major.
    If `first_group_highlight` is True, the whole first group (both sub-cols)
    is styled as the reference column, matching S8's blue/grey highlight.
    """
    n_sub = len(subcols)

    def group_cls(gi: int, base: str) -> str:
        return base + (" reference-col-header" if first_group_highlight and gi == 0 else "")

    def sub_cls(gi: int) -> str:
        return "num sub-header" + (" reference-col-header" if first_group_highlight and gi == 0 else "")

    def cell_cls(gi: int) -> str:
        return "num" + (" reference-col-cell" if first_group_highlight and gi == 0 else "")

    top = html.Tr(
        [html.Th("", className="row-label", rowSpan=2)] +
        [html.Th(g, className=group_cls(gi, "num group-header"), colSpan=n_sub)
         for gi, g in enumerate(groups)]
    )
    sub = html.Tr(
        [html.Th(c, className=sub_cls(gi))
         for gi in range(len(groups)) for c in subcols]
    )
    body_rows = []
    for label, values in rows:
        tds = [html.Td(label, className="row-label")]
        for i, v in enumerate(values):
            gi = i // n_sub
            tds.append(html.Td(v, className=cell_cls(gi)))
        body_rows.append(html.Tr(tds))
    return html.Table([html.Thead([top, sub]), html.Tbody(body_rows)])


def _empty_table():
    return html.Div("No data.", className="empty-note")


def _i(v):
    if v is None or pd.isna(v):
        return "—"
    return f"{int(v):,}"


def _pct(v):
    if v is None or pd.isna(v):
        return "—"
    return f"{v:.0%}"
