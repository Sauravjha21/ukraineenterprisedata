"""Layer 1: parse the raw data.gov.ua financial-statement archives into CSV.

Input : rawdata/{year}_data/*.zip
        Each outer zip contains one inner zip per report form (e.g. ..._S0111007.zip),
        and each inner zip contains one XML file per enterprise filing.
Output: scrapped_data/{year}/{form_family}_{label}.csv   (one row per XML filing)
        scrapped_data/{year}/_parse_report.csv          (per-form counts and issues)
        scrapped_data/{year}/_parse_errors.csv          (files that could not be parsed)

No values are transformed at this layer: every field is kept as the raw string from
the XML (xsi:nil elements become empty cells). Cleaning happens in later layers.

Usage:
    python src/parse_raw_to_csv.py --year 2024
"""

import argparse
import csv
import io
import json
import re
import sys
import tempfile
import time
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = PROJECT_ROOT / "rawdata"
OUT_DIR = PROJECT_ROOT / "scrapped_data"

# Form code = C_DOC (S01) + C_DOC_SUB (3 digits) + C_DOC_VER (2 digits), e.g. S0111007.
# The version changes between years, so outputs are named by family (C_DOC + C_DOC_SUB)
# to stay stable year to year.
FORM_FAMILIES = {
    "S01001": "balance_sheet_f1",
    "S01002": "financial_results_f2",
    "S01003": "cash_flow_direct_f3",
    "S01033": "cash_flow_indirect_f3n",
    "S01040": "equity_statement_f4",
    "S01050": "notes_f5",
    "S01060": "segment_info_f6",
    "S01100": "small_enterprise_f1m_f2m",
    "S01110": "micro_enterprise_f1ms_f2ms",
}

FORM_CODE_RE = re.compile(r"(S\d{7})")
XML_DECL_RE = re.compile(r"^\s*<\?xml[^>]*\?>")
# Inner XML names look like: {EDRPOU}_{DPS file name}.XML_{YYYY-MM-DD HH:MM:SS}.xml
XML_NAME_RE = re.compile(r"^(?P<edrpou>[^_]+)_(?P<dps_name>.+?\.XML)_(?P<published>.+)\.xml$", re.I)

META_COLS = ["source_zip", "source_xml", "xml_published_at", "form_code", "form_family", "xml_encoding_used"]
HEAD_TAG = "DECLARHEAD"
BODY_TAG = "DECLARBODY"


def form_label(form_code: str) -> tuple[str, str]:
    """Return (family, label) for a form code like S0103355."""
    family = form_code[:6]
    return family, FORM_FAMILIES.get(family, "unknown_form")


def decode_name(info: zipfile.ZipInfo) -> str:
    """Zip entries here store UTF-8 names without the UTF-8 flag; undo cp437 mojibake."""
    if info.flag_bits & 0x800:
        return info.filename
    try:
        return info.filename.encode("cp437").decode("utf-8")
    except UnicodeError:
        return info.filename


def parse_xml(raw: bytes) -> tuple[dict, str]:
    """Parse one filing into a flat {tag: text} dict.

    Some files declare WINDOWS-1251 but are actually UTF-8, so we decode the bytes
    ourselves (UTF-8 first, then cp1251) and drop the declaration before parsing.
    """
    try:
        text, encoding = raw.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        text, encoding = raw.decode("cp1251"), "cp1251"
    root = ET.fromstring(XML_DECL_RE.sub("", text.lstrip("\ufeff"), count=1))

    record = {}
    for section in (HEAD_TAG, BODY_TAG):
        node = root.find(section)
        if node is None:
            raise ValueError(f"missing <{section}>")
        for el in node:
            if len(el):
                raise ValueError(f"unexpected nested element <{el.tag}>")
            if el.tag in record:
                raise ValueError(f"duplicate element <{el.tag}>")
            record[el.tag] = (el.text or "").strip()
    return record, encoding


def process_form(inner_path: Path, outer_name: str, form_code: str, out_dir: Path) -> tuple[dict, list]:
    family, label = form_label(form_code)
    out_csv = out_dir / f"{family}_{label}.csv"
    columns: dict[str, None] = {}  # insertion-ordered set
    errors = []
    stats = {"rows": 0, "cp1251": 0, "firms": set()}

    # Spool parsed rows to JSON lines so the column union is known before writing,
    # without holding a large form in memory.
    with tempfile.TemporaryFile("w+", encoding="utf-8") as spool, zipfile.ZipFile(inner_path) as inner:
        for info in inner.infolist():
            if info.is_dir():
                continue
            xml_name = decode_name(info)
            try:
                record, encoding = parse_xml(inner.read(info))
            except Exception as exc:  # keep going; report at the end
                errors.append({"form_code": form_code, "source_xml": xml_name, "error": str(exc)})
                continue
            m = XML_NAME_RE.match(xml_name)
            row = {
                "source_zip": outer_name,
                "source_xml": xml_name,
                "xml_published_at": m.group("published") if m else "",
                "form_code": form_code,
                "form_family": family,
                "xml_encoding_used": encoding,
                **record,
            }
            for col in record:
                columns.setdefault(col, None)
            spool.write(json.dumps(row, ensure_ascii=False) + "\n")
            stats["rows"] += 1
            stats["cp1251"] += encoding == "cp1251"
            stats["firms"].add(record.get("FIRM_EDRPOU") or record.get("TIN"))

        collisions = set(META_COLS) & set(columns)
        if collisions:
            raise RuntimeError(f"{form_code}: XML tags collide with metadata columns: {collisions}")

        spool.seek(0)
        with open(out_csv, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=META_COLS + list(columns), restval="")
            writer.writeheader()
            for line in spool:
                writer.writerow(json.loads(line))

    report = {
        "form_code": form_code,
        "form_family": family,
        "form_label": label,
        "output_csv": out_csv.name,
        "xml_files": stats["rows"] + len(errors),
        "rows_written": stats["rows"],
        "parse_errors": len(errors),
        "unique_firms": len(stats["firms"]),
        "n_columns": len(META_COLS) + len(columns),
        "utf8_decode_failed_used_cp1251": stats["cp1251"],
    }
    return report, errors


def run(year: int) -> None:
    year_dir = RAW_DIR / f"{year}_data"
    zips = sorted(year_dir.glob("*.zip"))
    if not zips:
        sys.exit(f"No zip files found in {year_dir}")
    out_dir = OUT_DIR / str(year)
    out_dir.mkdir(parents=True, exist_ok=True)

    reports, all_errors, seen_forms = [], [], set()
    for outer_path in zips:
        with zipfile.ZipFile(outer_path) as outer:
            for info in outer.infolist():
                name = decode_name(info)
                m = FORM_CODE_RE.search(name)
                if not name.lower().endswith(".zip") or not m:
                    print(f"  skipping unexpected entry: {name}")
                    continue
                form_code = m.group(1)
                if form_code in seen_forms:
                    raise RuntimeError(f"Form {form_code} appears in more than one archive for {year}")
                seen_forms.add(form_code)

                t0 = time.time()
                print(f"  {form_code}: extracting {info.file_size / 1e6:,.1f} MB ...", flush=True)
                with tempfile.TemporaryDirectory() as tmp:
                    inner_path = Path(tmp) / "inner.zip"
                    with outer.open(info) as src, open(inner_path, "wb") as dst:
                        while chunk := src.read(16 * 1024 * 1024):
                            dst.write(chunk)
                    report, errors = process_form(inner_path, outer_path.name, form_code, out_dir)
                reports.append(report)
                all_errors.extend(errors)
                print(
                    f"  {form_code}: {report['rows_written']:,} rows, {report['n_columns']} cols, "
                    f"{report['parse_errors']} errors -> {report['output_csv']} ({time.time() - t0:,.0f}s)",
                    flush=True,
                )

    reports.sort(key=lambda r: r["form_code"])
    with open(out_dir / "_parse_report.csv", "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(reports[0]))
        writer.writeheader()
        writer.writerows(reports)
    with open(out_dir / "_parse_errors.csv", "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=["form_code", "source_xml", "error"])
        writer.writeheader()
        writer.writerows(all_errors)
    print(f"Done: {len(reports)} forms written to {out_dir} ({len(all_errors)} parse errors)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--year", type=int, required=True, help="Reporting year, e.g. 2024")
    run(parser.parse_args().year)
