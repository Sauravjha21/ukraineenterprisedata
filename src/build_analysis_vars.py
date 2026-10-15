"""Layer 3: build analysis variables from the deduplicated financial statements.

Input : deduplicated_data/{year}/micro.csv, small.csv, large.csv
Output: analysis_data/{year}/firms.csv      one row per firm with the key variables
        analysis_data/{year}/_summary.csv   what each cleaning step changed, per variable and size

Amounts are nominal, in thousands of UAH, as reported (no deflation yet).

Steps
  1. Build each key variable for the current and the prior year from the statement lines.
     A blank line inside a statement the firm filed counts as 0 (statement totals only add
     up that way); if the firm filed nothing for that statement column, the value is missing.
     Large-firm Form 2 conventions are harmonised to the micro/small forms: income tax is
     stored negative there, net profit is profit (2350) minus loss (2355), and total
     expenses are the sum of the expense lines.
  2. Negative values -> missing for variables that cannot be negative (not net profit,
     which is signed: negative = loss).
  4. Winsorize current-year levels at the 5th/95th percentile, within each firm size.
  5. Year-over-year % change, current vs prior year from the same filing, using the
     non-winsorized values: classic (missing when prior <= 0), winsorized at 5/95 pooled
     across firms; and symmetric, 200 * (cur - prev) / (|cur| + |prev|), bounded at +-200.
  6. Employees: S_CH (micro, small) or N3 (large). employees_adj sets values below 1 to
     missing for small and large firms.

Usage:
    python src/build_analysis_vars.py --year 2025
"""

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
IN_DIR = PROJECT_ROOT / "deduplicated_data"
OUT_DIR = PROJECT_ROOT / "analysis_data"

BLANK_IS_ZERO_IN_FILED_STATEMENT = True
WINSOR_LOWER, WINSOR_UPPER = 0.05, 0.95
WINSORIZE_LEVELS_WITHIN_SIZE = True

# Balance-sheet lines: column 3 = start of year (= prior year end), column 4 = end of year.
# Income-statement lines: column 3 = current year, column 4 = prior year.
COLUMN = {"balance": {"cur": "_4", "prev": "_3"}, "income": {"cur": "_3", "prev": "_4"}}

# name: (statement, {size: recipe}). A recipe is a line number, or a list of (sign, line)
# terms summed together. A size missing from the dict means the form has no such line.
ALL = ("micro", "small", "large")
VARIABLES = {
    "fixed_assets": ("balance", dict.fromkeys(ALL, "1010")),
    "current_assets": ("balance", dict.fromkeys(ALL, "1195")),
    "registered_capital": ("balance", dict.fromkeys(ALL, "1400")),
    "lt_bank_credits": ("balance", {"large": "1510"}),
    "targeted_financing": ("balance", {"large": "1525"}),
    "lt_liabilities": ("balance", dict.fromkeys(ALL, "1595")),
    "st_bank_credits": ("balance", dict.fromkeys(ALL, "1600")),
    "current_liabilities": ("balance", dict.fromkeys(ALL, "1695")),
    "net_sales": ("income", dict.fromkeys(ALL, "2000")),
    "cogs": ("income", dict.fromkeys(ALL, "2050")),
    "income_tax": ("income", {"micro": "2300", "small": "2300", "large": [(-1, "2300")]}),
    "salary_expenses": ("income", {"large": "2505"}),
    "net_profit": ("income", {"micro": "2350", "small": "2350", "large": [(1, "2350"), (-1, "2355")]}),
    "total_expenses": ("income", {
        "micro": "2285",
        "small": "2285",
        "large": [(1, line) for line in ("2050", "2130", "2150", "2180", "2250", "2255", "2270")],
    }),
}
SIGNED = {"net_profit"}

STATEMENT_PREFIX = {"balance": "A1", "income": "B2"}
EMPLOYEES = {"micro": "S_CH", "small": "S_CH", "large": "N3"}
EMPLOYEES_ADJ_SIZES = {"small", "large"}
ID_COLS = ["firm_id", "firm_size", "FIRM_EDRPOU", "FIRM_NAME", "FIRM_KVED", "FIRM_OPFCD", "KATOTTG", "OBL", "C_REG", "PERIOD_YEAR"]


def load_size_group(path: Path, size: str) -> pd.DataFrame:
    header = pd.read_csv(path, nrows=0).columns
    lines = [c for c in header if c[:2] in ("A1", "B2") and c[2:5].isdigit()]
    usecols = [c for c in ID_COLS if c in header] + [EMPLOYEES[size]] + lines
    df = pd.read_csv(path, usecols=usecols, dtype=str, keep_default_na=False)
    df[lines] = df[lines].apply(lambda s: pd.to_numeric(s.replace("", None), errors="coerce"))
    return df


def statement_filed(df: pd.DataFrame, statement: str, period: str) -> pd.Series:
    """True where the firm reported at least one line of this statement for this period."""
    suffix = COLUMN[statement][period]
    cols = [c for c in df.columns if c.startswith(STATEMENT_PREFIX[statement]) and c.endswith(suffix)]
    return df[cols].notna().any(axis=1)


def build_variable(df: pd.DataFrame, statement: str, recipe, period: str, filed: pd.Series) -> pd.Series:
    if recipe is None:
        return pd.Series(np.nan, index=df.index)
    terms = [(1, recipe)] if isinstance(recipe, str) else recipe
    suffix = COLUMN[statement][period]
    prefix = STATEMENT_PREFIX[statement][0]
    total = pd.Series(0.0, index=df.index)
    for sign, line in terms:
        col = f"{prefix}{line}{suffix}"
        values = df[col] if col in df else pd.Series(np.nan, index=df.index)
        if BLANK_IS_ZERO_IN_FILED_STATEMENT:
            values = values.fillna(0)
        total = total + sign * values
    return total.where(filed)


def winsorize(s: pd.Series, groups: pd.Series | None = None) -> tuple[pd.Series, pd.DataFrame]:
    """Clip at the 5th/95th percentile of non-missing values (within groups if given)."""
    keys = groups if groups is not None else pd.Series("all", index=s.index)
    lo = s.groupby(keys).transform(lambda x: x.quantile(WINSOR_LOWER))
    hi = s.groupby(keys).transform(lambda x: x.quantile(WINSOR_UPPER))
    cutoffs = pd.DataFrame({"group": keys, "p05": lo, "p95": hi}).drop_duplicates("group")
    return s.clip(lower=lo, upper=hi), cutoffs


def run(year: int) -> None:
    in_dir = IN_DIR / str(year)
    if not in_dir.is_dir():
        sys.exit(f"{in_dir} not found - run deduplicate.py --year {year} first")
    out_dir = OUT_DIR / str(year)
    out_dir.mkdir(parents=True, exist_ok=True)
    summary = []

    # Step 1 (+ step 6): variables per size group, current and prior year.
    frames = []
    for size in ALL:
        df = load_size_group(in_dir / f"{size}.csv", size)
        out = df[[c for c in ID_COLS if c in df]].copy()
        for statement in ("balance", "income"):
            for period in ("cur", "prev"):
                filed = statement_filed(df, statement, period)
                out[f"has_{statement}_{period}"] = filed
                for name, (stmt, recipes) in VARIABLES.items():
                    if stmt == statement:
                        col = name if period == "cur" else f"{name}_prev"
                        out[col] = build_variable(df, statement, recipes.get(size), period, filed)

        employees = pd.to_numeric(df[EMPLOYEES[size]].replace("", None), errors="coerce")
        out["employees"] = employees
        out["employees_adj"] = employees.mask(employees < 1) if size in EMPLOYEES_ADJ_SIZES else employees
        summary.append({"step": "6_employees_below_1_set_missing", "firm_size": size, "variable": "employees",
                        "value": int((employees < 1).sum()) if size in EMPLOYEES_ADJ_SIZES else 0})
        frames.append(out)
        print(f"  {size}: {len(out):,} firms", flush=True)
    firms = pd.concat(frames, ignore_index=True)

    for name in VARIABLES:
        for size in ALL:
            sel = firms["firm_size"] == size
            summary.append({"step": "1_non_missing_current", "firm_size": size, "variable": name,
                            "value": int(firms.loc[sel, name].notna().sum())})

    # Step 2: negative -> missing for variables that cannot be negative.
    for name in VARIABLES:
        if name in SIGNED:
            continue
        for col in (name, f"{name}_prev"):
            neg = firms[col] < 0
            for size in ALL:
                summary.append({"step": f"2_negative_set_missing{'_prev' if col.endswith('_prev') else ''}",
                                "firm_size": size, "variable": name,
                                "value": int((neg & (firms["firm_size"] == size)).sum())})
            firms.loc[neg, col] = np.nan

    # Step 4: winsorize current-year levels.
    for name in VARIABLES:
        groups = firms["firm_size"] if WINSORIZE_LEVELS_WITHIN_SIZE else None
        firms[f"{name}_w"], cutoffs = winsorize(firms[name], groups)
        for _, row in cutoffs.iterrows():
            for bound in ("p05", "p95"):
                summary.append({"step": f"4_level_cutoff_{bound}", "firm_size": row["group"], "variable": name, "value": row[bound]})

    # Step 5: year-over-year % changes from the non-winsorized values.
    for name in VARIABLES:
        cur, prev = firms[name], firms[f"{name}_prev"]
        pct = ((cur - prev) / prev * 100).where(prev > 0)
        firms[f"{name}_pct"] = pct
        firms[f"{name}_pct_w"], cutoffs = winsorize(pct)
        denom = cur.abs() + prev.abs()
        firms[f"{name}_spct"] = (200 * (cur - prev) / denom).where(denom > 0)
        for bound in ("p05", "p95"):
            summary.append({"step": f"5_pct_cutoff_{bound}", "firm_size": "all", "variable": name, "value": cutoffs[bound].iloc[0]})
        for size in ALL:
            sel = firms["firm_size"] == size
            summary.append({"step": "5_pct_change_non_missing", "firm_size": size, "variable": name,
                            "value": int(firms.loc[sel, f"{name}_pct"].notna().sum())})

    # Step 7: write outputs, variables grouped together.
    var_cols = [c for name in VARIABLES for c in (name, f"{name}_prev", f"{name}_w", f"{name}_pct", f"{name}_pct_w", f"{name}_spct")]
    firms = firms[[c for c in firms.columns if c not in var_cols] + var_cols]
    firms.to_csv(out_dir / "firms.csv", index=False)
    pd.DataFrame(summary).to_csv(out_dir / "_summary.csv", index=False)
    print(f"Done: {len(firms):,} firms, {firms.shape[1]} columns -> {out_dir}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--year", type=int, required=True, help="Reporting year, e.g. 2025")
    run(parser.parse_args().year)
