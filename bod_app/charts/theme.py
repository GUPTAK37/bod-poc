"""Plotly figure factories matching the Tableau BoD tab visuals."""

from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go

# --- Tableau palette --------------------------------------------------------
# Cohort colors extracted from the workbook's color encoding:
#   Treated  → #531679   (deep purple)
#   Untreated → #ed7239  (orange)
COLOR_TREATED = "#531679"
COLOR_UNTREATED = "#ed7239"

# Follow-up vs No-Follow-Up (sections 1-3)
COLOR_FOLLOWUP = "#2AA198"       # teal (top of stack)
COLOR_NO_FOLLOWUP = "#B0B0B0"    # grey

# Visit-type palette — extracted verbatim from the workbook's color encoding.
VISIT_COLORS = {
    "ED VISIT":         "#466db0",
    "OFFICE VISIT":     "#59a14f",
    "HOSPITALIZATION":  "#97cfd0",
    "UC VISIT":         "#b07aa1",
    "PHARMACY":         "#b0a983",
    "OTHERS":           "#ba9789",
    "LTAC/SNF VISIT":   "#e15759",
    "TELEHEALTH VISIT": "#edc948",
}
# Order in which segments stack (bottom → top) and legend items appear.
# Matches Tableau: TELEHEALTH at the bottom, HOSPITALIZATION on top,
# reflecting the "most-common → most-severe" reading order.
VISIT_STACK_ORDER = [
    "TELEHEALTH VISIT", "OFFICE VISIT", "UC VISIT",
    "LTAC/SNF VISIT", "ED VISIT", "HOSPITALIZATION",
    "PHARMACY", "OTHERS",
]

FONT_FAMILY = "Trebuchet MS, Trebuchet, Arial, sans-serif"
COLOR_GRID = "#E5E5E5"

# Layout constants — chart's plot area is aligned with the value columns of the
# table beneath it. The row-label column width below MUST match the CSS
# `.bod-table td.row-label { width: ... }` value.
ROW_LABEL_PX = 300
CHART_RIGHT_MARGIN_PX = 10
CHART_TOP_MARGIN_PX = 24
CHART_BOTTOM_MARGIN_PX = 44


def _layout(**overrides) -> dict:
    base = dict(
        font=dict(family=FONT_FAMILY, size=11, color="#333"),
        plot_bgcolor="white",
        paper_bgcolor="white",
        margin=dict(l=ROW_LABEL_PX, r=CHART_RIGHT_MARGIN_PX,
                    t=8, b=8),
        showlegend=False,   # rendered as HTML above the chart instead
        xaxis=dict(showgrid=False, showline=False, ticks="",
                   showticklabels=False, type="category"),
        yaxis=dict(showgrid=False, zeroline=False,
                   tickfont=dict(size=10),
                   showline=False, ticks=""),
        bargap=0.55,
        bargroupgap=0.05,
    )
    base.update(overrides)
    return base


def _empty_figure(msg: str = "No data") -> go.Figure:
    fig = go.Figure()
    fig.update_layout(**_layout())
    fig.add_annotation(text=msg, showarrow=False, xref="paper", yref="paper",
                       x=0.5, y=0.5, font=dict(family=FONT_FAMILY, size=12, color="#888"))
    return fig


# ---------------------------------------------------------------------------
# S1 / S2 / S3 — 100%-stacked follow-up vs no-follow-up
# ---------------------------------------------------------------------------

def followup_stacked(df: pd.DataFrame, *, x_col: str, x_labels=None) -> go.Figure:
    """Stacked 100% bar. Order matches Tableau: FU (teal) below, NoFU (grey) on top."""
    if df is None or df.empty:
        return _empty_figure("No episodes match the current filters")
    df = df.copy()
    df["PCT_FU"]   = df["EPISODES_WITH_FOLLOWUP"] / df["TOTAL_EPISODES"]
    df["PCT_NOFU"] = df["EPISODES_NO_FOLLOWUP"]   / df["TOTAL_EPISODES"]
    x = df[x_col].astype(str) if x_labels is None else x_labels

    fig = go.Figure()
    # NoFU drawn first → sits at BOTTOM of stack (grey).
    fig.add_trace(go.Bar(
        name="No Follow-Up Visit of Interest",
        x=x, y=df["PCT_NOFU"], marker_color=COLOR_NO_FOLLOWUP,
        text=[f"{v:.0%}" for v in df["PCT_NOFU"]], textposition="inside",
        insidetextanchor="middle",
        textfont=dict(color="#333", size=13, family=FONT_FAMILY),
        hovertemplate="%{x}<br>No Follow-Up: %{y:.1%}<extra></extra>",
        width=0.55,
        legendrank=2,
    ))
    # FU drawn second → sits on TOP of stack (teal).
    fig.add_trace(go.Bar(
        name="Follow-Up Visit",
        x=x, y=df["PCT_FU"], marker_color=COLOR_FOLLOWUP,
        text=[f"{v:.0%}" for v in df["PCT_FU"]], textposition="inside",
        insidetextanchor="middle",
        textfont=dict(color="white", size=13, family=FONT_FAMILY),
        hovertemplate="%{x}<br>Follow-Up: %{y:.1%}<extra></extra>",
        width=0.55,
        legendrank=1,
    ))
    layout = _layout(
        barmode="stack",
        yaxis=dict(range=[0, 1.001], tickformat=".0%",
                   showgrid=False, zeroline=False,
                   tickfont=dict(size=10),
                   showline=False, ticks="",
                   title=dict(text="% Episodes",
                              font=dict(family=FONT_FAMILY, size=11, color="#333"),
                              standoff=8)),
    )
    fig.update_layout(**layout)
    return fig


# ---------------------------------------------------------------------------
# S3 — grouped 100%-stacked (Treated / Untreated per quarter)
# ---------------------------------------------------------------------------

def followup_stacked_grouped_by_cohort(df: pd.DataFrame, *, bucket_col: str,
                                        bucket_labels: list[str]) -> go.Figure:
    """Section-3 chart. Renders 8 bars = 4 quarters × 2 cohorts along a single
    categorical axis so bar positions map 1:1 to the 8 table value columns
    below. Quarter labels are drawn as annotations centered over each pair.
    """
    if df is None or df.empty:
        return _empty_figure()
    df = df.copy()
    df["PCT_FU"]   = df["EPISODES_WITH_FOLLOWUP"] / df["TOTAL_EPISODES"]
    df["PCT_NOFU"] = df["EPISODES_NO_FOLLOWUP"]   / df["TOTAL_EPISODES"]

    # Build flat, ordered list of (bucket, cohort) columns matching the table.
    cohorts = ["Treated", "Untreated"]
    xkeys = [f"{q}|{c}" for q in bucket_labels for c in cohorts]
    tick_text = [c for _ in bucket_labels for c in cohorts]

    def series(col: str) -> list[float]:
        idx = df.set_index([bucket_col, "COHORT"])
        return [float(idx.loc[(q, c), col]) if (q, c) in idx.index else 0.0
                for q in bucket_labels for c in cohorts]

    fig = go.Figure()
    # NoFU at bottom (grey).
    fig.add_trace(go.Bar(
        name="No Follow-Up Visit of Interest",
        x=xkeys, y=series("PCT_NOFU"), marker_color=COLOR_NO_FOLLOWUP,
        text=[f"{v:.0%}" for v in series("PCT_NOFU")],
        textposition="inside", insidetextanchor="middle",
        textfont=dict(color="#333", size=12, family=FONT_FAMILY),
        width=0.7,
        hovertemplate="%{x}<br>No Follow-Up: %{y:.1%}<extra></extra>",
        legendrank=2,
    ))
    # FU on top (teal).
    fig.add_trace(go.Bar(
        name="Follow-Up Visit",
        x=xkeys, y=series("PCT_FU"), marker_color=COLOR_FOLLOWUP,
        text=[f"{v:.0%}" for v in series("PCT_FU")],
        textposition="inside", insidetextanchor="middle",
        textfont=dict(color="white", size=12, family=FONT_FAMILY),
        width=0.7,
        hovertemplate="%{x}<br>Follow-Up: %{y:.1%}<extra></extra>",
        legendrank=1,
    ))

    layout = _layout(
        barmode="stack",
        yaxis=dict(range=[0, 1.001], tickformat=".0%",
                   showgrid=False, zeroline=False,
                   tickfont=dict(size=10),
                   showline=False, ticks="",
                   title=dict(text="% Episodes",
                              font=dict(family=FONT_FAMILY, size=11, color="#333"),
                              standoff=8)),
        xaxis=dict(showgrid=False, showline=False, ticks="",
                   showticklabels=False, type="category"),
    )
    fig.update_layout(**layout)
    return fig


# ---------------------------------------------------------------------------
# S4 — grouped bars: avg # follow-up visits per episode, Treated vs Untreated
# ---------------------------------------------------------------------------

def avg_visits_grouped(df: pd.DataFrame, *, bucket_labels: list[str]) -> go.Figure:
    """Section-4 chart. 4 quarter categories × 2 grouped traces so Treated and
    Untreated bars touch within each quarter (matches Tableau) and there is
    visible whitespace only between quarters.
    """
    if df is None or df.empty:
        return _empty_figure()
    df = df.copy()
    idx = df.set_index(["LABEL", "COHORT"])
    def get(q: str, c: str) -> float:
        return float(idx.loc[(q, c), "AVG_VISITS_PER_EPISODE"]) \
            if (q, c) in idx.index else 0.0

    treated_y = [get(q, "Treated")   for q in bucket_labels]
    untreated_y = [get(q, "Untreated") for q in bucket_labels]
    y_max = max(treated_y + untreated_y + [0.0])

    fig = go.Figure()
    fig.add_trace(go.Bar(
        name="Treated",
        x=bucket_labels, y=treated_y,
        marker_color=COLOR_TREATED,
        text=[f"{v:.1f}" for v in treated_y],
        textposition="outside",
        textfont=dict(family=FONT_FAMILY, size=11, color="#333"),
        hovertemplate="Treated %{x}<br>Avg: %{y:.2f}<extra></extra>",
    ))
    fig.add_trace(go.Bar(
        name="Untreated",
        x=bucket_labels, y=untreated_y,
        marker_color=COLOR_UNTREATED,
        text=[f"{v:.1f}" for v in untreated_y],
        textposition="outside",
        textfont=dict(family=FONT_FAMILY, size=11, color="#333"),
        hovertemplate="Untreated %{x}<br>Avg: %{y:.2f}<extra></extra>",
    ))
    layout = _layout(
        barmode="group",
        bargap=0.4,          # gap between quarters
        bargroupgap=0,       # Treated & Untreated bars touch inside a quarter
        yaxis=dict(showgrid=False, zeroline=False, showline=False,
                   ticks="", showticklabels=False,
                   range=[0, y_max * 1.25 if y_max else 1]),
        xaxis=dict(showgrid=False, showline=False, ticks="",
                   showticklabels=False, type="category"),
    )
    fig.update_layout(**layout)
    return fig


# ---------------------------------------------------------------------------
# Visit-type stacked charts (S5 / S6 / S7 / S10)
# ---------------------------------------------------------------------------

def visit_mix_stacked(df: pd.DataFrame, *, x_labels: list[str],
                       key_cols: list[str], value_col: str = "N_VISITS",
                       yaxis_title: str = "% Follow-up Visits",
                       ) -> go.Figure:
    """100%-stacked bar per x-column, split by VISIT type.

    `key_cols` are the columns identifying each bar (e.g., ['LABEL'] for S5,
    ['LABEL','COHORT'] for S7). `x_labels` is the ordered list of bar labels
    matching the table columns beneath the chart.
    """
    if df is None or df.empty:
        return _empty_figure()
    df = df.copy()
    # Build a bar-key column from key_cols in the same order as x_labels.
    if len(key_cols) == 1:
        df["_XKEY"] = df[key_cols[0]].astype(str)
    else:
        df["_XKEY"] = df[key_cols].astype(str).agg("|".join, axis=1)

    totals = df.groupby("_XKEY")[value_col].transform("sum")
    df["_PCT"] = df[value_col] / totals.replace(0, pd.NA)

    fig = go.Figure()
    # Add traces in stack order → bottom first, top last.
    present = [v for v in VISIT_STACK_ORDER if v in df["VISIT"].unique()]
    # Include any extra visits not in the canonical order (defensive).
    present += [v for v in df["VISIT"].unique() if v not in present]

    for visit in present:
        sub = df[df["VISIT"] == visit]
        # Reindex to x_labels so missing quarter buckets show as zero.
        by_x = {row["_XKEY"]: row for _, row in sub.iterrows()}
        y = [float(by_x[x]["_PCT"]) if x in by_x else 0.0 for x in x_labels]
        counts = [int(by_x[x][value_col]) if x in by_x else 0 for x in x_labels]
        text = [f"{v:.0%}" if v >= 0.03 else "" for v in y]
        fig.add_trace(go.Bar(
            name=visit,
            x=x_labels, y=y,
            marker_color=VISIT_COLORS.get(visit, "#999"),
            text=text, textposition="inside", insidetextanchor="middle",
            textfont=dict(color="white", size=11, family=FONT_FAMILY),
            width=0.55,
            customdata=counts,
            hovertemplate=(f"<b>{visit}</b><br>%{{x}}<br>"
                           "Share: %{y:.1%}<br>Encounters: %{customdata:,}"
                           "<extra></extra>"),
        ))
    layout = _layout(
        barmode="stack",
        yaxis=dict(range=[0, 1.001], tickformat=".0%",
                   showgrid=False, zeroline=False, showline=False,
                   ticks="", showticklabels=False,
                   title=dict(text=yaxis_title,
                              font=dict(family=FONT_FAMILY, size=11, color="#333"),
                              standoff=8)),
    )
    fig.update_layout(**layout)
    return fig


# ---------------------------------------------------------------------------
# S8 — HR-condition bar (one green bar per HR condition, reference at left)
# ---------------------------------------------------------------------------

def hr_condition_bar(df: pd.DataFrame) -> go.Figure:
    """`df` columns: HIGH_RISK_CONDITION (str, 'REFERENCE' at index 0),
    N_FOLLOWUP_VISITS.  Bars are green with percentage labels inside
    (share of the reference bar).  Y-axis visible with numeric ticks.
    """
    if df is None or df.empty:
        return _empty_figure()
    df = df.copy().reset_index(drop=True)

    ref = float(df["N_FOLLOWUP_VISITS"].iloc[0]) if len(df) else 0.0
    pcts = [(float(v) / ref if ref else 0.0) for v in df["N_FOLLOWUP_VISITS"]]
    y_vals = df["N_FOLLOWUP_VISITS"].tolist()
    x_vals = df["HIGH_RISK_CONDITION"].tolist()

    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=x_vals, y=y_vals,
        marker_color="#59a14f",             # workbook green
        text=[f"{p:.0%}" for p in pcts],
        textposition="inside", insidetextanchor="middle",
        textfont=dict(color="white", size=12, family=FONT_FAMILY),
        width=0.55,
        customdata=[[p] for p in pcts],
        hovertemplate=(
            "<b>%{x}</b><br>"
            "# Follow-Up Visits: %{y:,}<br>"
            "Share of reference: %{customdata[0]:.1%}<extra></extra>"
        ),
    ))
    layout = _layout(
        margin=dict(l=ROW_LABEL_PX, r=CHART_RIGHT_MARGIN_PX, t=24, b=8),
        yaxis=dict(showgrid=False, zeroline=False,
                   showline=False, ticks="",
                   showticklabels=True,
                   tickformat="~s",           # 50K / 100K
                   tickfont=dict(family=FONT_FAMILY, size=10, color="#333"),
                   title=dict(text="# Follow-Up Visits",
                              font=dict(family=FONT_FAMILY, size=11, color="#333"),
                              standoff=6)),
        xaxis=dict(showgrid=False, showline=False, ticks="",
                   type="category", showticklabels=False),
    )
    fig.update_layout(**layout)
    return fig


# ---------------------------------------------------------------------------
# S9 — HR-condition grouped by cohort (Treated | Untreated per HR)
# ---------------------------------------------------------------------------

def hr_condition_by_cohort_bar(df: pd.DataFrame, *,
                                hr_order: list[str]) -> go.Figure:
    """8 categories × 2 cohorts. Bars touch inside each category (like S4);
    gap between categories. Y = # Follow-Up Visits (raw); text label =
    cohort share within the category (matches Tableau's chart labels).
    """
    if df is None or df.empty:
        return _empty_figure()
    df = df.copy()

    idx = df.set_index(["HIGH_RISK_CONDITION", "COHORT"])
    def get(hr: str, c: str, col: str) -> float:
        return float(idx.loc[(hr, c), col]) if (hr, c) in idx.index else 0.0

    y_treated   = [get(h, "Treated",   "N_FOLLOWUP_VISITS") for h in hr_order]
    y_untreated = [get(h, "Untreated", "N_FOLLOWUP_VISITS") for h in hr_order]

    def pcts(same_side, other_side):
        out = []
        for s, o in zip(same_side, other_side):
            total = s + o
            out.append(f"{(s/total):.0%}" if total > 0 else "")
        return out

    y_max = max(y_treated + y_untreated + [0.0])

    fig = go.Figure()
    fig.add_trace(go.Bar(
        name="Treated",
        x=hr_order, y=y_treated,
        marker_color=COLOR_TREATED,
        text=pcts(y_treated, y_untreated),
        textposition="inside", insidetextanchor="middle",
        textfont=dict(color="white", size=11, family=FONT_FAMILY),
        hovertemplate="<b>Treated · %{x}</b><br># Follow-Up Visits: %{y:,}<extra></extra>",
    ))
    fig.add_trace(go.Bar(
        name="Untreated",
        x=hr_order, y=y_untreated,
        marker_color=COLOR_UNTREATED,
        text=pcts(y_untreated, y_treated),
        textposition="inside", insidetextanchor="middle",
        textfont=dict(color="white", size=11, family=FONT_FAMILY),
        hovertemplate="<b>Untreated · %{x}</b><br># Follow-Up Visits: %{y:,}<extra></extra>",
    ))
    layout = _layout(
        barmode="group",
        bargap=0.4,          # gap between HR categories
        bargroupgap=0,       # Treated + Untreated touch inside a category
        margin=dict(l=ROW_LABEL_PX, r=CHART_RIGHT_MARGIN_PX, t=24, b=8),
        yaxis=dict(showgrid=False, zeroline=False, showline=False,
                   ticks="", showticklabels=True,
                   tickformat="~s",
                   tickfont=dict(family=FONT_FAMILY, size=10, color="#333"),
                   range=[0, y_max * 1.05 if y_max else 1],
                   title=dict(text="# Follow-Up Visits",
                              font=dict(family=FONT_FAMILY, size=11, color="#333"),
                              standoff=6)),
        xaxis=dict(showgrid=False, showline=False, ticks="",
                   type="category", showticklabels=False),
    )
    fig.update_layout(**layout)
    return fig
