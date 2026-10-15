"""Layer 2: remove duplicate submissions from the parsed financial statements.

Input : scrapped_data/{year}/*.csv          (output of parse_raw_to_csv.py)
Output: deduplicated_data/{year}/
          micro.csv, small.csv, large.csv    one row per firm, per size group
          combined.csv                       micro + small + large stacked
          large_f3_cash_flow_direct.csv ...  other large-firm forms (F3-F6), one row per firm
          _dedup_log.csv                     rows/firms remaining after each step
          _dropped_firms.csv                 firms removed, with the reason

Firms can submit the same report several times (e.g. to correct it). Within each form,
for each firm (firm_id = TIN zero-padded to 8 digits) the steps are:
  1. Keep the submission(s) with the most recent D_FILL.
  2. Drop perfect duplicates (same value in every XML field except C_DOC_CNT).
  3. Keep the largest C_DOC_CNT.
  4. Keep the most recent MY_DATE (only forms that have it).
  5. Keep the most recent PERIOD_YEAR + PERIOD_MONTH.
  6. Drop firms that still have more than one record.
Large firms = Form 1 (balance) joined with Form 2 (financial results); their columns are
renamed to the micro/small convention. After combining micro, small and large for the
year, firms appearing in more than one size group are dropped everywhere.

Usage:
    python src/deduplicate.py --year 2025
"""

import argparse
import re
import sys
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
IN_DIR = PROJECT_ROOT / "scrapped_data"
OUT_DIR = PROJECT_ROOT / "deduplicated_data"

# Columns added by parse_raw_to_csv.py (not part of the filing itself).
PARSE_META = ["source_zip", "source_xml", "xml_published_at", "form_code", "form_family", "xml_encoding_used"]
# Fields describing the submission rather than the firm; kept per form when F1 and F2 are joined.
SUBMISSION_COLS = PARSE_META + ["C_DOC", "C_DOC_SUB", "C_DOC_VER", "C_DOC_TYPE", "C_DOC_CNT", "D_FILL", "SOFTWARE"]

FORMS = {
    "micro": "S01110",
    "small": "S01100",
    "large_f1": "S01001",
    "large_f2": "S01002",
    "large_f3_cash_flow_direct": "S01003",
    "large_f3n_cash_flow_indirect": "S01033",
    "large_f4_equity": "S01040",
    "large_f5_notes": "S01050",
    "large_f6_segments": "S01060",
}
SIZE_GROUPS = ["micro", "small", "large"]

LINE_COL_RE = re.compile(r"^([AB])(\d{4})$")


def read_form(year_dir: Path, family: str) -> pd.DataFrame:
    matches = list(year_dir.glob(f"{family}_*.csv"))
    if len(matches) != 1:
        sys.exit(f"Expected one {family}_*.csv in {year_dir}, found {len(matches)}")
    df = pd.read_csv(matches[0], dtype=str, keep_default_na=False).replace("", pd.NA)
    df.insert(0, "firm_id", df["TIN"].str.strip().str.zfill(8))
    return df


class Deduplicator:
    """Applies the six-step rule to one form and records what each step did."""

    def __init__(self, year: int, form: str):
        self.year, self.form = year, form
        self.log, self.dropped = [], []

    def record(self, step: str, df: pd.DataFrame) -> None:
        sizes = df.groupby("firm_id").size()
        self.log.append({
            "year": self.year, "form": self.form, "step": step, "rows": len(df),
            "firms": len(sizes), "firms_with_duplicates": int((sizes > 1).sum()),
        })

    @staticmethod
    def keep_max(df: pd.DataFrame, key: pd.Series) -> pd.DataFrame:
        """Within each firm keep rows where key equals the firm's max (all rows if key is all missing)."""
        firm_max = key.groupby(df["firm_id"]).transform("max")
        return df[(key == firm_max).fillna(False) | firm_max.isna()]

    def run(self, df: pd.DataFrame) -> pd.DataFrame:
        self.record("0_raw", df)

        d_fill = pd.to_datetime(df["D_FILL"], format="%d%m%Y", errors="coerce")
        df = self.keep_max(df, d_fill)
        self.record("1_latest_D_FILL", df)

        content_cols = [c for c in df.columns if c not in PARSE_META + ["C_DOC_CNT"]]
        df = df.drop_duplicates(subset=content_cols)
        self.record("2_drop_perfect_duplicates", df)

        df = self.keep_max(df, pd.to_numeric(df["C_DOC_CNT"], errors="coerce"))
        self.record("3_largest_C_DOC_CNT", df)

        if "MY_DATE" in df.columns:
            df = self.keep_max(df, pd.to_datetime(df["MY_DATE"], format="%d%m%Y", errors="coerce"))
            self.record("4_latest_MY_DATE", df)
        else:
            self.record("4_latest_MY_DATE (field absent, skipped)", df)

        period = pd.to_numeric(df["PERIOD_YEAR"], errors="coerce") * 100 + pd.to_numeric(df["PERIOD_MONTH"], errors="coerce")
        df = self.keep_max(df, period)
        self.record("5_latest_PERIOD", df)

        dup = df["firm_id"].duplicated(keep=False)
        for firm in df.loc[dup, "firm_id"].unique():
            self.dropped.append({"year": self.year, "firm_id": firm, "form": self.form, "reason": "duplicates_unresolved_after_step_5"})
        df = df[~dup]
        self.record("6_drop_unresolved_firms", df)
        return df


def rename_large(df: pd.DataFrame, statement: str) -> pd.DataFrame:
    """Rename Form 1/2 line columns to the micro/small convention.

    Form 1: A1000 -> A1000_3 (start of year), B1000 -> A1000_4 (end of year)
    Form 2: A2000 -> B2000_3 (current year),  B2000 -> B2000_4 (prior year)
    """
    prefix = {"f1": "A", "f2": "B"}[statement]
    mapping = {}
    for col in df.columns:
        m = LINE_COL_RE.match(col)
        if m:
            mapping[col] = f"{prefix}{m.group(2)}_{3 if m.group(1) == 'A' else 4}"
    return df.rename(columns=mapping)


def build_large(f1: pd.DataFrame, f2: pd.DataFrame) -> pd.DataFrame:
    """Outer-join deduplicated Form 1 and Form 2 on firm_id.

    Submission fields are kept per form (suffix _f1/_f2); firm descriptors present in
    both forms are taken from Form 1, falling back to Form 2.
    """
    f1, f2 = rename_large(f1, "f1").set_index("firm_id"), rename_large(f2, "f2").set_index("firm_id")
    sub1 = f1[[c for c in SUBMISSION_COLS if c in f1]].add_suffix("_f1")
    sub2 = f2[[c for c in SUBMISSION_COLS if c in f2]].add_suffix("_f2")
    f1, f2 = f1.drop(columns=SUBMISSION_COLS, errors="ignore"), f2.drop(columns=SUBMISSION_COLS, errors="ignore")

    shared = [c for c in f1.columns if c in f2.columns]
    line_cols = [c for c in shared if re.match(r"^[AB]\d{4}_\d$", c)]
    if line_cols:
        raise RuntimeError(f"Form 1 and Form 2 line columns collide after renaming: {line_cols[:10]}")

    firms = f1.index.union(f2.index)
    f1, f2 = f1.reindex(firms), f2.reindex(firms)
    large = pd.concat([
        f1[shared].combine_first(f2[shared]),
        f1.drop(columns=shared),
        f2.drop(columns=shared),
        sub1.reindex(firms),
        sub2.reindex(firms),
    ], axis=1)
    large.insert(0, "has_f2", f2.index.isin(f2.dropna(how="all").index))
    large.insert(0, "has_f1", f1.index.isin(f1.dropna(how="all").index))
    return large.rename_axis("firm_id").reset_index()


def run(year: int) -> None:
    year_dir = IN_DIR / str(year)
    if not year_dir.is_dir():
        sys.exit(f"{year_dir} not found - run parse_raw_to_csv.py --year {year} first")
    out_dir = OUT_DIR / str(year)
    out_dir.mkdir(parents=True, exist_ok=True)

    logs, dropped, deduped = [], [], {}
    for form, family in FORMS.items():
        dd = Deduplicator(year, form)
        deduped[form] = dd.run(read_form(year_dir, family))
        logs += dd.log
        dropped += dd.dropped
        print(f"  {form}: {dd.log[0]['rows']:,} rows -> {len(deduped[form]):,} firms", flush=True)

    groups = {
        "micro": deduped.pop("micro"),
        "small": deduped.pop("small"),
        "large": build_large(deduped.pop("large_f1"), deduped.pop("large_f2")),
    }

    # Firms reporting in more than one size group in the same year are dropped everywhere.
    membership = pd.concat([pd.DataFrame({"firm_id": g["firm_id"], "group": name}) for name, g in groups.items()])
    multi = membership.groupby("firm_id")["group"].agg(lambda s: "+".join(sorted(s)))
    multi = multi[multi.str.contains(r"\+")]
    for firm, combo in multi.items():
        dropped.append({"year": year, "firm_id": firm, "form": combo, "reason": "in_multiple_size_groups"})
    print(f"  dropping {len(multi):,} firms that appear in more than one size group")

    for name, g in groups.items():
        before = len(g)
        g = g[~g["firm_id"].isin(multi.index)]
        g.insert(1, "firm_size", name)
        groups[name] = g
        logs.append({"year": year, "form": name, "step": "7_drop_firms_in_multiple_size_groups",
                     "rows": len(g), "firms": g["firm_id"].nunique(), "firms_with_duplicates": 0})
        g.to_csv(out_dir / f"{name}.csv", index=False)
        print(f"  {name}.csv: {before:,} -> {len(g):,} firms")

    for form, g in deduped.items():  # F3-F6
        g = g[~g["firm_id"].isin(multi.index)]
        logs.append({"year": year, "form": form, "step": "7_drop_firms_in_multiple_size_groups",
                     "rows": len(g), "firms": g["firm_id"].nunique(), "firms_with_duplicates": 0})
        g.to_csv(out_dir / f"{form}.csv", index=False)

    combined = pd.concat(groups.values(), ignore_index=True)
    assert combined["firm_id"].is_unique, "combined dataset still has duplicate firms"
    combined.to_csv(out_dir / "combined.csv", index=False)
    print(f"  combined.csv: {len(combined):,} firms, {combined.shape[1]} columns")

    pd.DataFrame(logs).to_csv(out_dir / "_dedup_log.csv", index=False)
    pd.DataFrame(dropped, columns=["year", "firm_id", "form", "reason"]).to_csv(out_dir / "_dropped_firms.csv", index=False)
    print(f"Done: outputs in {out_dir}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--year", type=int, required=True, help="Reporting year, e.g. 2025")
    run(parser.parse_args().year)
