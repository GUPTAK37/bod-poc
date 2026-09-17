"""Snowflake connection + query helper.

Two execution paths, tried in order:

  1. **Dataiku DSS** — when running inside a DSS Code Studio / Web App
     the `dataiku` module is importable and points at a pre-configured
     Snowflake connection. `SQLExecutor2` handles auth, warehouse, and
     credentials centrally. This is the preferred path on DSS.

  2. **Direct snowflake-connector** — used locally (Cortex Code / dev
     laptop) via the named connection from `~/.snowflake/connections.toml`
     (SSO with `client_store_temporary_credential=true`) or via
     env-var / password fallback for headless deploys.

Both paths surface a single `run_query(sql, binds)` returning a pandas
DataFrame, cached in an LRU (by sql+binds hash) so repeat filter
combinations don't hit the warehouse twice.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from functools import lru_cache
from typing import Any, Mapping

import pandas as pd
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger(__name__)

FACT_TABLE = (
    "VAW_AMER_PUB.PUB_VAW_AMER_COVID_QCIT."
    "PAXLOVIDCOIVDQCITLAADETL_FINAL_ENCOUNTERS_MSA_HR_UNPIVOT_CLEAN_TRUNCATED_DEV_PUB_SF"
)

# Name of the Dataiku DSS Snowflake connection (overridable via env var
# for portability between DSS instances).
DSS_CONNECTION_NAME = os.getenv(
    "DSS_SNOWFLAKE_CONNECTION", "VAW_SF_COMMERCIAL_AMER_DESIGN_RW"
)

_query_lock = threading.Lock()

# --------------------------------------------------------------------------
# Dataiku DSS path
# --------------------------------------------------------------------------

_dss_executor = None
_dss_available: bool | None = None


def _try_dss_executor():
    """Return a `dataiku.core.sql.SQLExecutor2` instance if the DSS runtime
    is importable, else `None`. Result is cached across calls."""
    global _dss_executor, _dss_available
    if _dss_available is not None:
        return _dss_executor
    try:
        from dataiku.core.sql import SQLExecutor2  # type: ignore
        _dss_executor = SQLExecutor2(connection=DSS_CONNECTION_NAME)
        _dss_available = True
        log.info("Using Dataiku DSS Snowflake connection '%s'", DSS_CONNECTION_NAME)
    except Exception as exc:  # noqa: BLE001
        _dss_available = False
        _dss_executor = None
        log.info("Dataiku DSS runtime not available (%s); "
                 "falling back to direct Snowflake connector", exc)
    return _dss_executor


def _run_via_dss(executor, sql: str,
                 binds: Mapping[str, Any] | None) -> pd.DataFrame:
    """DSS SQLExecutor2 doesn't support pyformat bind params — inline
    them safely (single-quoting strings, ISO-formatting dates)."""
    if binds:
        materialised = sql
        for k, v in binds.items():
            placeholder = "%(" + k + ")s"
            if isinstance(v, str):
                lit = "'" + v.replace("'", "''") + "'"
            elif hasattr(v, "isoformat"):  # date / datetime
                lit = "'" + v.isoformat() + "'"
            elif v is None:
                lit = "NULL"
            else:
                lit = str(v)
            materialised = materialised.replace(placeholder, lit)
    else:
        materialised = sql
    return executor.query_to_df(materialised)


# --------------------------------------------------------------------------
# Direct snowflake-connector fallback (local / non-DSS deploys)
# --------------------------------------------------------------------------

_conn_lock = threading.Lock()
_conn = None  # snowflake.connector.SnowflakeConnection | None


def _direct_connect():
    import snowflake.connector
    conn_name = os.getenv("SNOWFLAKE_CONNECTION_NAME", "AMERPROD01")
    try:
        return snowflake.connector.connect(
            connection_name=conn_name,
            client_session_keep_alive=True,
            client_prefetch_threads=1,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("Named connection '%s' failed (%s); falling back to env vars",
                    conn_name, exc)

    params: dict[str, Any] = {
        "account": os.environ["SNOWFLAKE_ACCOUNT"],
        "user": os.environ["SNOWFLAKE_USER"],
        "role": os.getenv("SNOWFLAKE_ROLE") or None,
        "warehouse": os.getenv("SNOWFLAKE_WAREHOUSE") or None,
        "database": os.getenv("SNOWFLAKE_DATABASE") or None,
        "schema": os.getenv("SNOWFLAKE_SCHEMA") or None,
        "client_session_keep_alive": True,
        "client_prefetch_threads": 1,
    }
    if os.getenv("SNOWFLAKE_PASSWORD"):
        params["password"] = os.environ["SNOWFLAKE_PASSWORD"]
    else:
        params["authenticator"] = os.getenv("SNOWFLAKE_AUTHENTICATOR", "externalbrowser")
    return snowflake.connector.connect(**{k: v for k, v in params.items() if v})


def get_conn():
    """Return the shared direct Snowflake connection (opens on first call).
    Only used when the DSS runtime is not available."""
    global _conn
    with _conn_lock:
        if _conn is None or _conn.is_closed():
            log.info("Opening Snowflake connection (direct connector)")
            _conn = _direct_connect()
        return _conn


def _reset_conn() -> None:
    global _conn
    with _conn_lock:
        if _conn is not None:
            try:
                _conn.close()
            except Exception:  # noqa: BLE001
                pass
        _conn = None


def _run_via_direct(sql: str,
                    binds: Mapping[str, Any] | None) -> pd.DataFrame:
    with get_conn().cursor() as cur:
        cur.execute(sql, dict(binds) if binds else None)
        return (cur.fetch_pandas_all()
                if cur.rowcount
                else pd.DataFrame(columns=[c.name for c in cur.description or []]))


# --------------------------------------------------------------------------
# Unified entry point
# --------------------------------------------------------------------------

_RETRY_MARKERS = (
    "WinError 233", "10054", "10053", "Broken pipe",
    "Connection is closed", "Session no longer exists",
    "Session token is invalid", "Authentication token has expired",
    "No process is on the other",
)


def _run_once(sql: str, binds: Mapping[str, Any] | None) -> pd.DataFrame:
    with _query_lock:
        executor = _try_dss_executor()
        if executor is not None:
            return _run_via_dss(executor, sql, binds)
        return _run_via_direct(sql, binds)


def _key(sql: str, binds: Mapping[str, Any] | None) -> str:
    payload = json.dumps({"sql": sql, "binds": binds or {}}, sort_keys=True, default=str)
    return hashlib.sha1(payload.encode()).hexdigest()


@lru_cache(maxsize=256)
def _cached(key: str, sql: str, binds_json: str) -> pd.DataFrame:  # noqa: ARG001
    binds = json.loads(binds_json)
    try:
        return _run_once(sql, binds)
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)
        if any(marker in msg for marker in _RETRY_MARKERS):
            log.warning("Snowflake pipe error, reconnecting once: %s",
                        msg.splitlines()[0][:120])
            _reset_conn()
            return _run_once(sql, binds)
        raise


def run_query(sql: str, binds: Mapping[str, Any] | None = None) -> pd.DataFrame:
    binds_json = json.dumps(binds or {}, sort_keys=True, default=str)
    try:
        return _cached(_key(sql, binds), sql, binds_json).copy()
    except Exception:
        _cached.cache_clear()
        raise


def clear_cache() -> None:
    _cached.cache_clear()
