# Ukraine enterprise financial statements

Turns the annual financial statements that Ukrainian enterprises file with the State Tax Service
into one clean row per firm per year, ready for analysis. The source is the dataset
[7436ae83-dfc1-4836-9962-8af3e831c522](https://data.gov.ua/dataset/7436ae83-dfc1-4836-9962-8af3e831c522)
on data.gov.ua. Every script takes a `--year` argument, so the same code runs for any reporting year.

The pipeline has three stages. Each one reads the previous stage's folder and writes its own:

| Stage | Script | Reads | Writes |
| --- | --- | --- | --- |
| 1. Parse XML to CSV | `src/parse_raw_to_csv.py` | `rawdata/{year}_data/*.zip` | `scrapped_data/{year}/` |
| 2. Remove duplicate submissions | `src/deduplicate.py` | `scrapped_data/{year}/` | `deduplicated_data/{year}/` |
| 3. Build analysis variables | `src/build_analysis_vars.py` | `deduplicated_data/{year}/` | `analysis_data/{year}/` |

## Setup

Requires Python 3.10 or later and pandas:

```
pip install pandas
```

Run all commands from the repository root.

## Running a year

### 1. Download the raw data

data.gov.ua blocks scripted downloads (Cloudflare), so download the archive in a browser:

1. Open the [dataset page](https://data.gov.ua/dataset/7436ae83-dfc1-4836-9962-8af3e831c522).
2. Download the annual financial statements archive for the reporting year
   (file name like `fin_zvit_2025_ric_-....zip`).
3. Save it, unchanged and unzipped, in `rawdata/{year}_data/`, e.g. `rawdata/2026_data/`.

Keep only one archive per year in that folder. The parser stops with an error if two archives
contain the same report form.

### 2. Run the three stages

Replace `2026` with the reporting year:

```
python src/parse_raw_to_csv.py --year 2026
python src/deduplicate.py --year 2026
python src/build_analysis_vars.py --year 2026
```

For 2025 (about 590,000 filings) the stages take roughly 2.5 minutes, 1.5 minutes and 40 seconds.
Rerunning a stage overwrites its output files for that year.

To run several years:

```
for y in 2024 2025; do
  python src/parse_raw_to_csv.py --year $y &&
  python src/deduplicate.py --year $y &&
  python src/build_analysis_vars.py --year $y
done
```

### 3. Check the results

After each stage, open its log files before using the data:

| After | Check | Look for |
| --- | --- | --- |
| Parsing | `scrapped_data/{year}/_parse_report.csv` | Files and rows per form; `parse_errors` should be 0 |
| Parsing | `scrapped_data/{year}/_parse_errors.csv` | Should be empty |
| Parsing | `form_label` column of `_parse_report.csv` | `unknown_form` means the portal added a new form: add it to `FORM_FAMILIES` in `src/parse_raw_to_csv.py` |
| Dedup | `deduplicated_data/{year}/_dedup_log.csv` | Rows and firms left after each step, per form |
| Dedup | `deduplicated_data/{year}/_dropped_firms.csv` | Every firm dropped and why |
| Analysis | `analysis_data/{year}/_summary.csv` | Values built, negatives removed, winsorizing cut-offs, per variable and firm size |

Compare the counts with previous years: a large drop usually means an incomplete download.

## Outputs

### `scrapped_data/{year}/` — raw filings as CSV

One CSV per report form, one row per XML filing. Values are exactly as filed (text, no conversion).

| File | Form |
| --- | --- |
| `S01110_micro_enterprise_f1ms_f2ms.csv` | Micro-enterprise statements |
| `S01100_small_enterprise_f1m_f2m.csv` | Small-enterprise statements |
| `S01001_balance_sheet_f1.csv` | Form 1, balance sheet (medium and large firms) |
| `S01002_financial_results_f2.csv` | Form 2, financial results |
| `S01003_cash_flow_direct_f3.csv` | Form 3, cash flow (direct method) |
| `S01033_cash_flow_indirect_f3n.csv` | Form 3-n, cash flow (indirect method) |
| `S01040_equity_statement_f4.csv` | Form 4, statement of equity |
| `S01050_notes_f5.csv` | Form 5, notes |
| `S01060_segment_info_f6.csv` | Form 6, segment information |

Line columns follow the official form: `A1300_3` is balance-sheet line 1300, column 3 (start of
year); `_4` is end of year. `B2000_3` is income-statement line 2000, column 3 (current year); `_4`
is prior year.

### `deduplicated_data/{year}/` — one row per firm

| File | Contents |
| --- | --- |
| `combined.csv` | Micro, small and large firms stacked, with a `firm_size` column |
| `micro.csv`, `small.csv`, `large.csv` | Each size group separately; `large.csv` joins Form 1 and Form 2 |
| `large_f3_*.csv` … `large_f6_*.csv` | Forms 3 to 6 for large firms |

Firms are identified by `firm_id` (the filer's tax ID, zero-padded to 8 digits). Large-firm columns
are renamed to the micro/small naming above, so all three groups share the same column names.

### `analysis_data/{year}/firms.csv` — analysis variables

One row per firm: ID, size group, name, industry (`FIRM_KVED`), location (`KATOTTG`, `OBL`,
`C_REG`), `employees`, `employees_adj`, and 14 key variables (fixed assets, current assets,
registered capital, long- and short-term bank credits, targeted financing, long-term and current
liabilities, net sales, cost of goods sold, income tax, salary expenses, net profit, total
expenses). Each variable has six columns, e.g. for net sales:

| Column | Meaning |
| --- | --- |
| `net_sales` | Current year |
| `net_sales_prev` | Prior year, from the same filing |
| `net_sales_w` | Current year, winsorized at 5/95 within firm size |
| `net_sales_pct` | Year-over-year % change |
| `net_sales_pct_w` | % change, winsorized at 5/95 |
| `net_sales_spct` | Symmetric % change (bounded at ±200) |

Amounts are nominal thousands of UAH; conversion to constant 2017 euros is not implemented yet.

When loading in pandas, read IDs as text so leading zeros are kept:

```python
import pandas as pd
firms = pd.read_csv("analysis_data/2025/firms.csv", dtype={"firm_id": str, "FIRM_EDRPOU": str})
```

## Cleaning rules in brief

- **Duplicates:** for each firm and form, keep the latest filing date (`D_FILL`), then drop exact
  duplicates, keep the largest submission number (`C_DOC_CNT`), the latest `MY_DATE`, the latest
  period, and finally drop firms still duplicated. Firms filing in more than one size group in a
  year are dropped.
- **Blank lines** in a statement the firm filed count as 0; if the statement was not filed, the
  value is missing.
- **Negative values** are set to missing for variables that cannot be negative (net profit is
  kept signed).
- **Employees below 1** are set to missing in `employees_adj` for small and large firms.

The settings are constants at the top of `src/build_analysis_vars.py`. The full method, with the
checks behind each choice, is in the project's scraping and cleaning log.

## Known limitations

- The current 2024 download (date range `2026-01-01_2026-10-08` in its file name) holds about 3%
  of the expected firms; filings published earlier are probably in a separate download. Year-over-year changes are therefore computed from the prior-year figures inside each
  2025 filing, which match the firms' own 2024 filings in 90–98% of cases.
- Data folders (`rawdata/`, `scrapped_data/`, `deduplicated_data/`, `analysis_data/`) are excluded
  from git; rerun the pipeline to rebuild them.
