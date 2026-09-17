"""KPI tile helper for the E&E Count row and header widgets."""

from __future__ import annotations

import dash_bootstrap_components as dbc
from dash import html


def kpi_tile(label: str, value: str, *, accent: str = "#0000C9") -> dbc.Card:
    return dbc.Card(
        dbc.CardBody([
            html.Div(label, className="kpi-label"),
            html.Div(value, className="kpi-value", style={"color": accent}),
        ]),
        className="kpi-card",
    )


def header_pill(label: str, value: str) -> html.Div:
    return html.Div([
        html.Span(label + ": ", className="pill-label"),
        html.Span(value, className="pill-value"),
    ], className="header-pill")
