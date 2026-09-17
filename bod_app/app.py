"""Dash app entry for the COVID-19 QCIT BoD webapp.

Pre-opens the Snowflake connection during startup (single SSO popup) BEFORE
Flask begins serving, so callback threads never race to open connections.
"""

from __future__ import annotations

import logging
import os

import dash
import dash_bootstrap_components as dbc

from .data.snowflake_client import get_conn
from .layouts.bod import build_layout, register_callbacks

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("bod_app")

# When running behind the Dataiku DSS Code Studio proxy the browser hits
# the app at a long path like `/code-studios/<PROJECT>/<CS>/<PORT>/proxy/8050/`,
# but DSS STRIPS that prefix before forwarding the request to the internal
# Flask server. So we need two DIFFERENT prefixes:
#   - `requests_pathname_prefix` = full DSS path (what the browser must use
#     when fetching /_dash-*, /assets/*, etc.)
#   - `routes_pathname_prefix`   = "/" (what Flask actually mounts routes at
#     after DSS strips the proxy prefix).
# Using `url_base_pathname` alone would force both to the same value, which
# is wrong for this proxy topology.
_url_prefix = os.getenv("DASH_URL_PREFIX", "/")
if not _url_prefix.endswith("/"):
    _url_prefix += "/"

_dash_kwargs = dict(
    title="COVID-19 QCIT — Burden of Disease",
    external_stylesheets=[dbc.themes.BOOTSTRAP],
    suppress_callback_exceptions=True,
    assets_folder="assets",
    prevent_initial_callbacks=False,
)
if _url_prefix != "/":
    _dash_kwargs["requests_pathname_prefix"] = _url_prefix
    _dash_kwargs["routes_pathname_prefix"] = "/"

app = dash.Dash(__name__, **_dash_kwargs)
server = app.server


def build() -> None:
    # Layout uses no live queries at build time; domain dropdowns start empty
    # and populate after the first Apply Filters click.
    app.layout = build_layout()
    register_callbacks(app)


build()


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8050"))
    debug = os.getenv("DEBUG", "false").lower() == "true"

    # Warm the Snowflake connection now (single auth prompt) unless we're
    # running under Dataiku DSS, where SQLExecutor2 opens lazily and warm-up
    # would just log noise.
    if not os.getenv("DKU_BACKEND_HOST"):
        try:
            log.info("Warming up Snowflake connection …")
            conn = get_conn()
            log.info("Snowflake connected as %s", getattr(conn, "user", "?"))
        except Exception as exc:  # noqa: BLE001
            log.warning("Snowflake connection failed at startup (%s). "
                        "Charts will error until connection succeeds.", exc)

    # Serve on a single request-processing thread so the 10 section callbacks
    # queue up sequentially through the (already-serialized) Snowflake layer.
    # This prevents the Windows SSO pipe from being clobbered by concurrent
    # cursor use — matches how Streamlit / most desktop tools behave.
    app.run(
        host="0.0.0.0", port=port, debug=debug,
        threaded=False,
        processes=1,
    )
