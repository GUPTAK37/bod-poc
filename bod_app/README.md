# COVID-19 QCIT — Burden of Disease Webapp

A Plotly Dash port of the **BoD (Burden of Disease)** tab from the Tableau
workbook `COVID-19 QCIT Enhancements v1 (Prod) 2.twbx`. Queries Snowflake
live against
`VAW_AMER_PUB.PUB_VAW_AMER_COVID_QCIT.PAXLOVIDCOIVDQCITLAADETL_FINAL_ENCOUNTERS_MSA_HR_UNPIVOT_CLEAN_TRUNCATED_DEV_PUB_SF`.

## Contents

```
bod_app/
  app.py                       # Dash entry point
  assets/tableau_theme.css     # Trebuchet MS + Tableau palette styling
  data/
    snowflake_client.py        # Connection + LRU-cached run_query
    params.py                  # 11-parameter model + where_clause builder
    queries.py                 # SQL for each BoD worksheet (E&E, BoD1V-5V, BOD*T, benchmarks)
  charts/
    theme.py                   # Plotly template + trend_figure factory
    kpi.py                     # KPI tile / header pill widgets
  layouts/bod.py               # Layout composition + all callbacks
  requirements.txt
  .env.example
```

## Setup

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r bod_app/requirements.txt
copy bod_app/.env.example bod_app/.env    # then fill in credentials
```

SSO users can leave `SNOWFLAKE_PASSWORD` blank; the connector will fall back to
`externalbrowser` auth using the same account as the Cortex Code
`AMERPROD01` connection (`PFE-AMERPROD01`).

## Run

```powershell
$env:PYTHONPATH="."
python -m bod_app.app
```

Open http://localhost:8050.

## Design notes

- **Cohort integrity filters** (`MEDICAL_PATIENT_COHORT_FLAG=1`,
  `INCOMPLETE_EPISODE_FLAG=0`, `EPISODE_PER_QUARTER_FLAG='1'`) are baked into
  every SQL builder via `params.where_clause`. They are never exposed in the UI.
- **Fanout guard** — the source table is unpivoted on high-risk conditions, so
  every patient/episode/encounter count uses `COUNT(DISTINCT ...)`.
- **Small-cohort mask** (Tableau's `NAT_PT_FLAG`) is applied as a
  `HAVING MIN(patients) >= 5` mask over cohort per time bucket, mirroring the
  workbook's ≥5 threshold in both Treated and Untreated cohorts.
- **Encounter window** — the `Encounter Window` parameter picks between
  `ENCOUNTER_IN_30_D_WINDOW` and `ENCOUNTER_IN_90_D_WINDOW`.
- **Reference line** — pulls `MONTHLY / QUARTERLY / YEARLY BENCHMARK` (or the
  `_NUMBERS` variants when `# Treatment Utilization` mode is selected).
- **Live queries** — every filter change fires SQL through
  `snowflake_client.run_query`. Repeat combinations hit an in-process
  256-entry LRU cache to keep interactions snappy.

## Verification checklist

- `only_compile=true` SQL check on every builder in `data/queries.py`.
- 5-combination parity check against Tableau CSV exports (default, Treated /
  Age 65+, MSA=NY, State=CA + 90-day window, HR=Diabetes).
- Regression tests that patient/episode counts match a raw
  `COUNT(DISTINCT PATIENT_ID)` on the fact table (guards HR-fanout).
- Small-cohort mask assertions: no time bucket with <5 patients per cohort is
  rendered.
- Manual visual QA vs the Tableau BoD tab at 1920x1080.
