"""
Coordinate-based extraction for a Surrogate's Court "Affidavit to Dispense
with the Filing of a Bond" (NYSBA / HotDocs template "Dispense_bond").

Design goals (the same ones a government roster/award-list parser needs):

* Rows are rebuilt from word coordinates (vertical bands), never from the
  text stream order, so a label can never be paired with the value of the
  row above or below it.
* Every extracted record carries record-specific evidence: source SHA-256,
  page number, bounding box, and a cropped PNG of that exact row.
* Amounts are typed and labeled (asset value, funeral expense paid, tax
  liability ...). "None", "Exempt", "No" are kept as their own value kinds,
  not silently turned into zero.
* A tie-out step proves the numbers: the sum of asset rows must equal the
  total stated in paragraph 8, every row must pass a same-band integrity
  check, and the extracted rows must match the template's expected rows.

Run:
    python -m roster_extract.extract input/viewer_4.pdf --out out/viewer_4
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import shutil
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Optional

import pdfplumber

# --------------------------------------------------------------------------- #
# layout constants for this template (points, PDF origin top-left via pdfplumber)
# --------------------------------------------------------------------------- #
VALUE_RIGHT_EDGE_MIN = 520      # value column is right-aligned near x=534
VALUE_GAP_MIN = 40              # whitespace between label and value token
ROW_BAND_TOLERANCE = 1.5        # label/value tops must agree within this
ITEM_INDENT_X = (76, 82)        # item rows start at x~78 (paragraphs at x~72)
CROP_RESOLUTION = 150

MONEY_RE = re.compile(r"^\$\d{1,3}(,\d{3})*(\.\d{2})?$")
VALUE_WORDS = {"none", "exempt", "no", "yes"}

# expected debt / liability rows of the template, in order.
# Used to prove "extracted count = template count" (no dropped or extra rows).
EXPECTED_DEBT_ROWS = [
    ("Rents or charges due on any lease or apartment", "lease_charges_due"),
    ("Amount of any outstanding mortgages or notes", "mortgage_or_notes_outstanding"),
    ("Amount of any judgments held against the decedent", "judgments_outstanding"),
    ("Liability of any bond on which the decedent was a", "bond_liability"),
    ("Federal income taxes due", "tax_federal_income_due"),
    ("New York State and/or City income taxes due", "tax_ny_income_due"),
    ("Real estate taxes due", "tax_real_estate_due"),
    ("Federal estate taxes due", "tax_federal_estate_due"),
    ("New York estate taxes due", "tax_ny_estate_due"),
    ("Amount of outstanding doctor, hospital or medical", "medical_expenses_outstanding"),
    ("Amount of funeral expenses paid", "funeral_expenses_paid"),
    ("Amount of funeral expenses still outstanding", "funeral_expenses_outstanding"),
    ("Amount of credit card balances", "credit_card_balances"),
    ("At the time of death, was the decedent engaged in", "business_engaged_flag"),
    ("Amount of any outstanding business liabilities", "business_liabilities_outstanding"),
    ("Amount of any federal, state or city public", "public_assistance_received"),
]


# --------------------------------------------------------------------------- #
# records
# --------------------------------------------------------------------------- #
@dataclass
class Evidence:
    source_sha256: str
    page: int
    bbox: list            # [x0, top, x1, bottom] in PDF points
    crop: Optional[str] = None


@dataclass
class LineItem:
    row_id: str
    section: str          # "assets" | "debts"
    label: str
    amount_type: str
    value_raw: str
    value_kind: str       # amount | none | exempt | yes_no
    amount: Optional[str]  # Decimal as string, None when not monetary
    row_integrity_ok: bool
    evidence: Evidence


@dataclass
class HeaderField:
    name: str
    value: Optional[str]
    status: str           # extracted | blank_on_source | not_found
    evidence: Optional[Evidence] = None
    pii: bool = False


@dataclass
class Result:
    source_file: str
    source_sha256: str
    template: Optional[str]
    extracted_at: str
    header: list = field(default_factory=list)
    line_items: list = field(default_factory=list)
    checks: list = field(default_factory=list)
    completeness: dict = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def r2(v: float) -> float:
    return round(float(v), 2)


def group_rows(words, tol=ROW_BAND_TOLERANCE):
    """Cluster words into rows by their 'top' coordinate (vertical band)."""
    rows = []
    for w in sorted(words, key=lambda w: (round(w["top"], 1), w["x0"])):
        for row in rows:
            if abs(row["top"] - w["top"]) <= tol:
                row["words"].append(w)
                break
        else:
            rows.append({"top": w["top"], "words": [w]})
    for row in rows:
        row["words"].sort(key=lambda w: w["x0"])
        row["bottom"] = max(w["bottom"] for w in row["words"])
        row["x0"] = row["words"][0]["x0"]
        row["x1"] = max(w["x1"] for w in row["words"])
        row["text"] = " ".join(w["text"] for w in row["words"])
    return sorted(rows, key=lambda r: r["top"])


def split_value(row):
    """Return (label_words, value_words) when the row ends in a value column."""
    ws = row["words"]
    if len(ws) < 2:
        return None
    last = ws[-1]
    if last["x1"] < VALUE_RIGHT_EDGE_MIN:
        return None
    gap = last["x0"] - ws[-2]["x1"]
    token = last["text"]
    if gap < VALUE_GAP_MIN:
        return None
    if not (MONEY_RE.match(token) or token.lower() in VALUE_WORDS):
        return None
    return ws[:-1], [last]


def classify_value(raw: str):
    low = raw.lower()
    if MONEY_RE.match(raw):
        return "amount", str(Decimal(raw.replace("$", "").replace(",", "")))
    if low == "none":
        return "none", "0.00"
    if low == "exempt":
        return "exempt", None
    return "yes_no", None


def debt_type_for(label: str) -> str:
    for prefix, slug in EXPECTED_DEBT_ROWS:
        if label.lower().startswith(prefix.lower()[:30]):
            return slug
    return "debt_other"


def find_phrase(page, pattern):
    m = page.search(pattern, regex=True, case=False)
    return m[0] if m else None


def bbox_of(obj):
    return [r2(obj["x0"]), r2(obj["top"]), r2(obj["x1"]), r2(obj["bottom"])]


# --------------------------------------------------------------------------- #
# extraction
# --------------------------------------------------------------------------- #
class AffidavitExtractor:
    def __init__(self, pdf_path: Path, out_dir: Path, save_crops=True):
        self.pdf_path = Path(pdf_path)
        self.out_dir = Path(out_dir)
        self.save_crops = save_crops
        self.sha = sha256_of(self.pdf_path)

    # ---- ingest-time snapshot: the evidence must survive the source moving
    def snapshot(self):
        snap_dir = self.out_dir / "snapshot"
        snap_dir.mkdir(parents=True, exist_ok=True)
        dest = snap_dir / f"{self.sha[:16]}.pdf"
        if not dest.exists():
            shutil.copy2(self.pdf_path, dest)
        return dest

    def _crop(self, page, bbox, name):
        if not self.save_crops:
            return None
        crop_dir = self.out_dir / "evidence"
        crop_dir.mkdir(parents=True, exist_ok=True)
        x0, top, x1, bottom = bbox
        pad = 2
        box = (max(0, x0 - pad), max(0, top - pad),
               min(page.width, x1 + pad), min(page.height, bottom + pad))
        img = page.crop(box).to_image(resolution=CROP_RESOLUTION)
        path = crop_dir / f"{name}.png"
        img.save(path)
        return str(path.relative_to(self.out_dir))

    def _ev(self, page, bbox, crop_name=None):
        crop = self._crop(page, bbox, crop_name) if crop_name else None
        return Evidence(self.sha, page.page_number, bbox, crop)

    # ---- header / entity fields --------------------------------------------
    def _header(self, pdf) -> list:
        p1 = pdf.pages[0]
        last = pdf.pages[-1]
        text1 = p1.extract_text() or ""
        full = "\n".join((pg.extract_text() or "") for pg in pdf.pages)
        out = []

        def add(name, value, page=None, hit=None, pii=False, blank=False):
            if blank:
                out.append(HeaderField(name, None, "blank_on_source",
                                       self._ev(page, bbox_of(hit)) if hit else None, pii))
            elif value:
                ev = self._ev(page, bbox_of(hit)) if (page and hit) else None
                out.append(HeaderField(name, value.strip(), "extracted", ev, pii))
            else:
                out.append(HeaderField(name, None, "not_found", None, pii))

        hit = find_phrase(p1, r"SURROGATE.S COURT OF THE STATE OF [A-Z ]+")
        add("court", hit["text"] if hit else None, p1, hit)

        hit = find_phrase(p1, r"COUNTY OF [A-Z]+")
        add("county", hit["text"].split("OF", 1)[1].strip().title() if hit else None, p1, hit)

        hit = find_phrase(p1, r"\w+ Proceeding, Estate of")
        add("proceeding_type", hit["text"].split()[0] if hit else None, p1, hit)

        # Caption: decedent name (left column) and document title (right column)
        # share one text line. Split them by x, so the title never leaks into
        # the entity field (same idea as project name vs sponsor entity).
        rows = group_rows(p1.extract_words())
        cap_idx = next((i for i, r in enumerate(rows) if "Estate of" in r["text"]), None)
        decedent, title_parts, cap_box, title_box = None, [], None, None
        if cap_idx is not None:
            for r in rows[cap_idx + 1: cap_idx + 4]:
                left = [w for w in r["words"] if w["x1"] < 300]
                right = [w for w in r["words"] if w["x0"] >= 300]
                if left and not decedent and "Deceased" not in r["text"]:
                    decedent = " ".join(w["text"] for w in left)
                    cap_box = [r2(left[0]["x0"]), r2(r["top"]),
                               r2(left[-1]["x1"]), r2(r["bottom"])]
                if right:
                    title_parts.append(" ".join(w["text"] for w in right))
                    rb = [r2(right[0]["x0"]), r2(r["top"]), r2(right[-1]["x1"]), r2(r["bottom"])]
                    title_box = rb if title_box is None else [
                        min(title_box[0], rb[0]), title_box[1], max(title_box[2], rb[2]), rb[3]]
        out.append(HeaderField("decedent", decedent, "extracted" if decedent else "not_found",
                               self._ev(p1, cap_box) if cap_box else None, pii=True))
        title = " ".join(title_parts) or None
        out.append(HeaderField("document_title", title, "extracted" if title else "not_found",
                               self._ev(p1, title_box) if title_box else None))

        hit = find_phrase(p1, r"File No\.\s*_+")
        if hit:
            add("file_no", None, p1, hit, blank=True)
        else:
            hit = find_phrase(p1, r"File No\.\s*\S+")
            add("file_no", hit["text"].split(".", 1)[1] if hit else None, p1, hit)

        m = re.search(r"I,\s+(.+?),\s+being duly sworn", text1)
        hit = find_phrase(p1, r"I,\s+.+?,\s+being duly sworn")
        add("petitioner", m.group(1) if m else None, p1, hit, pii=True)

        roles = []
        if re.search(r"\bdistributee of decedent\b", full):
            roles.append("distributee")
        if re.search(r"\bthe petitioner\b", full):
            roles.append("petitioner")
        m = re.search(r"by .+?,\s+as\s+(executor|administrator|executrix)", full.replace("\n", " "))
        if m:
            roles.append("proposed_" + m.group(1))
        out.append(HeaderField("petitioner_roles", ", ".join(roles) or None,
                               "extracted" if roles else "not_found"))

        m = re.search(r"domiciled at (.+?);", full.replace("\n", " "))
        add("petitioner_address", m.group(1) if m else None, pii=True)

        m = re.search(r"was (\d+) years old", full)
        hit = None
        for pg in pdf.pages:
            hit = find_phrase(pg, r"was \d+ years old")
            if hit:
                add("decedent_age", m.group(1) if m else None, pg, hit, pii=True)
                break
        else:
            add("decedent_age", None)

        hit = find_phrase(last, r"Sworn to before me this .+?\d{4}")
        m = re.search(r"this (\d+)\w* day of (\w+) (\d{4})", hit["text"]) if hit else None
        sworn = None
        if m:
            try:
                sworn = datetime.strptime(" ".join(m.groups()), "%d %B %Y").date().isoformat()
            except ValueError:
                sworn = None
        add("sworn_date", sworn, last, hit)

        return out

    # ---- the table-like part: line items with value column ------------------
    def _line_items(self, pdf) -> list:
        items = []
        section = None
        current = None
        for page in pdf.pages:
            for row in group_rows(page.extract_words()):
                t = row["text"]
                if re.match(r"^3\.\s", t):
                    section = "assets"
                    current = None
                    continue
                if re.match(r"^4\.\s", t):
                    section = "debts"
                    current = None
                    continue
                if re.match(r"^[5-9]\.\s", t) or "Forms ©" in t:
                    current = None
                    if re.match(r"^[5-9]\.\s", t):
                        section = None
                    continue
                if section is None or t.startswith("("):
                    continue
                if not (ITEM_INDENT_X[0] <= row["x0"] <= ITEM_INDENT_X[1]):
                    continue

                split = split_value(row)
                if split:
                    label_ws, value_ws = split
                    label = " ".join(w["text"] for w in label_ws)
                    raw = value_ws[0]["text"]
                    kind, amount = classify_value(raw)
                    same_band = abs(label_ws[0]["top"] - value_ws[0]["top"]) <= ROW_BAND_TOLERANCE
                    bbox = [r2(row["x0"]), r2(row["top"]), r2(row["x1"]), r2(row["bottom"])]
                    current = {
                        "section": section, "label": label, "raw": raw, "kind": kind,
                        "amount": amount, "ok": same_band, "page": page, "bbox": bbox,
                    }
                    items.append(current)
                elif current is not None and current["page"] is page:
                    # continuation line of the previous label (no value of its own)
                    current["label"] += " " + t
                    b = current["bbox"]
                    current["bbox"] = [b[0], b[1], max(b[2], r2(row["x1"])), r2(row["bottom"])]

        out = []
        n = {"assets": 0, "debts": 0}
        for it in items:
            n[it["section"]] += 1
            row_id = f"{it['section']}-{n[it['section']]:02d}"
            amount_type = "asset_value" if it["section"] == "assets" else debt_type_for(it["label"])
            out.append(LineItem(
                row_id=row_id, section=it["section"], label=it["label"],
                amount_type=amount_type, value_raw=it["raw"], value_kind=it["kind"],
                amount=it["amount"], row_integrity_ok=it["ok"],
                evidence=self._ev(it["page"], it["bbox"], crop_name=row_id),
            ))
        return out

    # ---- tie-out and integrity checks ---------------------------------------
    def _checks(self, pdf, header, items) -> list:
        checks = []

        def check(name, passed, detail):
            checks.append({"check": name, "passed": bool(passed), "detail": detail})

        assets = [i for i in items if i.section == "assets" and i.amount is not None]
        asset_sum = sum((Decimal(i.amount) for i in assets), Decimal("0"))
        stated, stated_ev = None, None
        for pg in pdf.pages:
            hit = find_phrase(pg, r"total value of their shares of the estate will be \$[\d,]+\.\d{2}")
            if hit:
                stated = Decimal(re.search(r"\$([\d,]+\.\d{2})", hit["text"]).group(1).replace(",", ""))
                stated_ev = {"page": pg.page_number, "bbox": bbox_of(hit)}
                break
        check("asset_sum_ties_to_stated_total",
              stated is not None and asset_sum == stated,
              {"asset_rows": len(assets), "asset_sum": str(asset_sum),
               "stated_total": str(stated) if stated is not None else None,
               "stated_total_evidence": stated_ev})

        bad = [i.row_id for i in items if not i.row_integrity_ok]
        check("row_integrity_label_and_value_same_band", not bad,
              {"rows_checked": len(items), "misaligned_rows": bad})

        debt_types = [i.amount_type for i in items if i.section == "debts"]
        expected = [slug for _, slug in EXPECTED_DEBT_ROWS]
        missing = [s for s in expected if s not in debt_types]
        extra = [s for s in debt_types if s not in expected]
        check("debt_rows_match_template", not missing and not extra,
              {"expected": len(expected), "extracted": len(debt_types),
               "missing": missing, "unexpected": extra})

        no_ev = [i.row_id for i in items if not i.evidence.crop and self.save_crops]
        check("every_row_has_record_specific_evidence", not no_ev,
              {"rows_without_crop": no_ev})

        h = {f.name: f.value for f in header}
        title = (h.get("document_title") or "").lower()
        dec = (h.get("decedent") or "").lower()
        check("entity_not_copied_from_title",
              bool(dec) and dec not in title and title not in dec,
              {"decedent": h.get("decedent"), "document_title": h.get("document_title")})

        return checks

    def run(self) -> Result:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.snapshot()
        with pdfplumber.open(self.pdf_path) as pdf:
            header = self._header(pdf)
            items = self._line_items(pdf)
            checks = self._checks(pdf, header, items)
            template = (pdf.metadata or {}).get("Title")
        filled = [f for f in header if f.status == "extracted"]
        res = Result(
            source_file=self.pdf_path.name,
            source_sha256=self.sha,
            template=template,
            extracted_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            header=header, line_items=items, checks=checks,
            completeness={
                "header_fields_extracted": len(filled),
                "header_fields_total": len(header),
                "blank_on_source": [f.name for f in header if f.status == "blank_on_source"],
                "not_found": [f.name for f in header if f.status == "not_found"],
            },
        )
        self._write(res)
        return res

    # ---- outputs -------------------------------------------------------------
    def _write(self, res: Result):
        data = asdict(res)
        (self.out_dir / "record.json").write_text(json.dumps(data, indent=2, ensure_ascii=False),
                                                  encoding="utf-8")
        with open(self.out_dir / "line_items.csv", "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["row_id", "section", "amount_type", "label", "value_raw", "value_kind",
                        "amount", "row_integrity_ok", "page", "bbox", "evidence_crop"])
            for i in res.line_items:
                w.writerow([i.row_id, i.section, i.amount_type, i.label, i.value_raw,
                            i.value_kind, i.amount, i.row_integrity_ok, i.evidence.page,
                            i.evidence.bbox, i.evidence.crop])
        (self.out_dir / "tieout_report.md").write_text(render_report(res), encoding="utf-8")


def render_report(res: Result) -> str:
    ok = all(c["passed"] for c in res.checks)
    lines = [
        f"# Tie-out report: {res.source_file}",
        "",
        f"- Source SHA-256: `{res.source_sha256}`",
        f"- Template: `{res.template}`",
        f"- Extracted at: {res.extracted_at}",
        f"- Overall: **{'PASS' if ok else 'FAIL'}**",
        "",
        "## Checks",
        "",
        "| Check | Result | Detail |",
        "|---|---|---|",
    ]
    for c in res.checks:
        lines.append(f"| {c['check']} | {'PASS' if c['passed'] else 'FAIL'} | "
                     f"`{json.dumps(c['detail'], ensure_ascii=False)}` |")
    lines += ["", "## Line items (row-level evidence)", "",
              "| Row | Amount type | Label | Value | Amount | Page | Same band | Evidence |",
              "|---|---|---|---|---|---|---|---|"]
    for i in res.line_items:
        crop = f"![{i.row_id}]({i.evidence.crop})" if i.evidence.crop else "-"
        lines.append(f"| {i.row_id} | {i.amount_type} | {i.label} | {i.value_raw} | "
                     f"{i.amount if i.amount is not None else '-'} | {i.evidence.page} | "
                     f"{'yes' if i.row_integrity_ok else 'NO'} | {crop} |")
    lines += ["", "## Header fields", "", "| Field | Value | Status | Page / bbox |",
              "|---|---|---|---|"]
    for f in res.header:
        ev = f"p{f.evidence.page} {f.evidence.bbox}" if f.evidence else "-"
        lines.append(f"| {f.name} | {f.value if f.value is not None else '-'} | {f.status} | {ev} |")
    return "\n".join(lines) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description="Extract an Affidavit to Dispense with Bond PDF")
    ap.add_argument("pdf")
    ap.add_argument("--out", default=None, help="output folder (default out/<pdf stem>)")
    ap.add_argument("--no-crops", action="store_true")
    a = ap.parse_args(argv)
    pdf = Path(a.pdf)
    out = Path(a.out) if a.out else Path("out") / pdf.stem
    res = AffidavitExtractor(pdf, out, save_crops=not a.no_crops).run()
    for c in res.checks:
        print(f"[{'PASS' if c['passed'] else 'FAIL'}] {c['check']}")
    print(f"{len(res.line_items)} line items -> {out}")
    return 0 if all(c["passed"] for c in res.checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
