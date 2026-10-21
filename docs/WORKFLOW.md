# prod_nowcast workflow

Practical guide for returning to this project after desk tagging (especially before notebook **06**), and for expanding beyond Spain.

---

## What this project does

Kpler waterborne liquids trades → DuckDB → terminal catalog + Tableau Imp/Exp masters → desk-tagged analysis in notebooks. Notebooks score terminals/complexes by import/export role, owners, facility tags, and import seasonality.

---

## End-to-end data flow

```
Raw Data/Kpler/kpler_YYYY.csv
        │  pull_kpler_trades.py   (full years)
        │  refresh_trades.py      (rolling window → data/incremental/)
        ▼
data/kpler_trades.duckdb          ← bootstrap_duckdb.py / kpler_store
        │
        ├─► build_terminal_catalog.py
        │     → data/derived/terminals*.{parquet,csv}
        │     → upserts scaffold rows into data/manual/terminal_desk_map.csv
        │       (preserves your desk columns: owner, tags, complexes, …)
        │
        ├─► build_tableau_imp_exp.py
        │     → data/derived/tableau/trades_imp_exp_master.{parquet,csv}
        │
        └─► build_tableau_tagged_trades.py   (reads desk map)
              → …/trades_imp_exp_master_tagged.csv

You edit (Excel):  data/manual/terminal_desk_map.csv

Notebooks:
  01  DuckDB explore (ES/FR/PT tracker)
  02  Terminal catalog + desk-map how-to
  03  PyGWalker Imp/Exp profiles (Tableau master)
  04  Roles + monthly cube + entity map  ← Persist feeds 05/06
  05  OD / hub Sankeys & maps            ← needs 04 Persist
  06  Facility-type import seasonality   ← desk map + 04 monthly cube
```

---

## Notebook cheat sheet

| NB | Purpose | Main inputs | Persist outputs? | When to run |
|----|---------|-------------|------------------|-------------|
| **01** | Country product seasonality / Sankeys | `kpler_trades.duckdb` | No | After DB exists; Iberia-oriented by default |
| **02** | Catalog browse + enrichment instructions | `terminals*.parquet` | No | After `build_terminal_catalog.py` |
| **03** | Interactive terminal demand | `trades_imp_exp_master` | No | After `build_tableau_imp_exp.py` |
| **04** | Roles, complexes, OD prep, **monthly cube** | Tableau master + desk map | **Yes** → `data/derived/terminal_product_*.parquet`, `terminal_entity_map.csv`, … | After tagging (and master exists). **Upstream of 05/06** |
| **05** | Flow storytelling | Entity map + OD flows (+ DuckDB) | Optional caches | After 04 Persist; if owners/complexes changed |
| **06** | Seasonality by `primary_tag` / `tag_*` | Desk map + `terminal_product_monthly.parquet` | Charts only | After tags updated **and** 04 Persist |

---

## Desk map: `data/manual/terminal_desk_map.csv`

Single source of truth for desk metadata (`terminal_desk.py`).

### Columns that matter

| Group | Columns | Notes |
|-------|---------|--------|
| Scaffold (machine) | `terminal_key`, `installation_id`, `installation_name`, `country`, `zone`, `zone_id` | Refreshed by catalog upsert; **do not invent keys** |
| Exclusive type | `primary_tag` | One bucket per site (refinery, storage, …) — used when `SEGMENT_MODE="primary"` |
| Multi-label | `tag_*` | Blank = False; `TRUE` / `1` / `yes` = on. Sites can have several flags |
| Complexes | `complex_id`, `complex_name` | Economic unit for NB04 when `AGGREGATE_BY="complex"` |
| Owners / geo | `owner`, `lat`, `lon`, pipelines, `notes` | Owners feed NB05/OD; lat/lon currently often empty |

Starter tags: `tag_refinery`, `tag_storage`, `tag_blending`, `tag_bunkering`, `tag_heavy_industry`, `tag_power_plant`, `tag_airport`, `tag_agriculture`, `tag_military`, `tag_biofuels`, `tag_asphalt` (plus any extra `tag_*` you add, e.g. `tag_petchem`).

### How to edit

1. Open the CSV in Excel; filter to your country.
2. Fill `primary_tag` and/or `tag_*` for sites you care about.
3. Optionally group co-located sites with the same `complex_id` / `complex_name`.
4. Save. Prefer **UTF-8 CSV** if Excel offers it (see §4 encoding note below).

Join key for analysis is usually **`installation_name` ↔ Kpler `Installation`**. Spelling must match or the site falls back to a singleton complex `inst:<name>`.

---

## After updating the desk map → notebook 06

**If only tags / owners / complexes changed (trades unchanged):**

1. **Optional** teammate extract:  
   `python build_tableau_tagged_trades.py`  
   → refreshes `data/derived/tableau/trades_imp_exp_master_tagged.csv`
2. **Notebook 04** — Run from the top through **§8 Persist** (at least setup → monthly/roles → §4 complex map → Persist).  
   Knobs in the setup cell:
   - `FOCUS_COUNTRY` — briefing/plots filter (e.g. `"Spain"`, `"France"`, `"Portugal"`)
   - `AGGREGATE_BY` — `"complex"` (default) or `"installation"`
3. **Notebook 06** — Full re-run. Setup knobs:
   - `FOCUS_COUNTRY` — must match tagged country
   - `SEGMENT_MODE` — `"tags"` (multi-label; sites can appear in several series) or `"primary"` (exclusive partition)
4. **Notebook 05** — Only if you changed complexes/owners and care about flow viz.

**You do not need** pull/bootstrap/catalog/imp_exp rebuild, nor notebooks 01–03, unless underlying trades changed.

**If trades also refreshed:**

```text
pull / refresh → (bootstrap if full rebuild) → build_terminal_catalog
  → build_tableau_imp_exp → build_tableau_tagged_trades
  → NB04 Persist → NB05 / NB06
```

Catalog upsert **preserves** desk columns; still worth checking that new installs appeared and your tags were not lost.

### What 06 actually needs

| Input | Source | Why |
|-------|--------|-----|
| `data/manual/terminal_desk_map.csv` | You | Tags for `FOCUS_COUNTRY` |
| `data/derived/terminal_product_monthly.parquet` | NB04 Persist | Monthly import kbd by installation × product |

If monthly is stale relative to the Tableau master, re-run 04 Persist. If tags changed but monthly is fine, 06 still re-reads the desk map on each run — but 04 Persist is still needed when **complexes** change (entity map / roles).

---

## Notebook 04 §4 encoding note

Excel on Windows often re-saves the desk map as Latin-1 (e.g. `Bolloré`). `load_desk_map` tries UTF-8 then falls back (`cp1252` / `latin-1`); `write_desk_map` always writes UTF-8.

If §4 still fails after an Excel save, normalize once from the project root:

```text
python terminal_desk.py --rewrite-utf8
```

Prefer saving as **CSV UTF-8** from Excel when available.

---

## Spain vs reusable (expanding countries)

The **pipeline and role/seasonality math are country-agnostic**. Spain shaped the **defaults, examples, and desk depth** — not the core algorithms.

### Reusable by changing `FOCUS_COUNTRY` (and desk data)

| Area | Notes |
|------|--------|
| NB04 role scores, monthly cube, Persist | Filters use `FOCUS_COUNTRY`; §4 complex rollup is **global** (all countries with matching desk `complex_*`) |
| NB06 seasonality | Fully driven by `FOCUS_COUNTRY` + tagged rows |
| NB05 flows | Works for any focus country **if** OD data and (for maps) zone coords exist |
| NB02 catalog slice | Change `FOCUS` list |
| Build scripts (`build_*`, `facility_seasonality`, store/pull) | No Spain filter |

### Still Spain-flavored (update when expanding)

| Location | What to change |
|----------|----------------|
| NB04 setup: `EXAMPLE_COMPLEXES`, `EXAMPLE_INSTALLATIONS` | Spain names (Algeciras, Bilbao, Barcelona…). Empty example panels if wrong country / name mismatch. Desk uses `"Bilbao refinery & port"`; example list historically had `"Bilbao Ref+Port"`. |
| NB04 setup: `OD_HUB_ENTITY`, `OD_ISLAND_ZONES` | Spain hubs / Canaries–Balearics |
| NB04 cell exporting `spain_terminals_unique.csv` | Hardcoded `Country == "Spain"` |
| NB01 | Built as ES/FR/PT tracker (`COUNTRIES = [...]`) |
| NB03 previews | Hardcoded Spain row counts / tables (logic is global) |
| NB05 `FLOW_SCOPE = "all_spain_touched"` | Name is Spain; behavior = legs touching `FOCUS_COUNTRY` |
| `viz_flows.py` map bbox + `DEFAULT_ISLAND_ZONES` | Iberia / near-Atlantic frame |
| `terminal_desk.py` `OTHER_PRIMARY` | Two Spain install name overrides |
| Desk **data** depth | Complexes mostly ES (+ some FR/PT); tags denser for Iberia; `lat`/`lon` largely empty; `zone_coords.csv` Iberia-heavy |

### Checklist for a new country (e.g. France)

1. Tag installs in the desk map (`primary_tag` / `tag_*`; complexes if you want port+refinery rollups).
2. Ensure `installation_name` matches Kpler.
3. If Excel left the CSV as Latin-1 and anything still fails: `python terminal_desk.py --rewrite-utf8`.
4. Set `FOCUS_COUNTRY` in **04** and **06** (and **05** if used).
5. Replace EXAMPLE / HUB / ISLAND lists for that country (or ignore empty example cells).
6. For maps in 05: add missing zones to `data/derived/zone_coords.csv`; expect Iberia bbox to clip until `viz_flows` geo ranges are adjusted.
7. Re-run 04 Persist → 06.

With **no complexes** for that country, `AGGREGATE_BY="complex"` still runs: each install becomes `inst:<name>` (same as installation grain). Tags alone are enough for **06**.

---

## Important paths

| Path | Role |
|------|------|
| `data/manual/terminal_desk_map.csv` | Desk SSOT |
| `data/kpler_trades.duckdb` | Trade store |
| `data/derived/tableau/trades_imp_exp_master.*` | Imp/Exp master |
| `data/derived/tableau/trades_imp_exp_master_tagged.csv` | Master + desk tags |
| `data/derived/terminal_product_monthly.parquet` | **06 input** (from 04) |
| `data/derived/terminal_entity_map.csv` | Install → complex (from 04) |
| `data/derived/zone_coords.csv` | Map arcs (05) |
| `docs/spain.docx` | Spain domain notes |
| `docs/notes_on_columns_grouping.docx` | Kpler column taxonomy |

---

## Quick “I just tagged Portugal / France” path

```text
1. Save desk map (UTF-8 if possible)
2. Open notebooks/04_… → set FOCUS_COUNTRY → Run All through Persist (§8)
3. Open notebooks/06_… → set FOCUS_COUNTRY (+ SEGMENT_MODE) → Run All
4. Optional: 05 for owner/complex flows; build_tableau_tagged_trades.py for Tableau
```
