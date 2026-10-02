# pdf-plumber: row-level PDF extraction with evidence and tie-out

A small, tested example of extracting structured records from an official
PDF form with `pdfplumber`, built around the problems that matter in
government roster and award-list parsing:

| Problem | How this repo handles it |
|---|---|
| Row misalignment (a label paired with the value from the row above/below) | Rows are rebuilt from word **coordinates** (vertical bands), never from text-stream order. A value is accepted only if it sits in the same band as its label and in the right-aligned value column. |
| Multi-line labels and page breaks inside a table | Continuation lines are merged into the previous row; the debts table continues across page 1 → 2 without losing rows. |
| "Amount" columns that mix different things | Every value is typed: `amount_type` (e.g. `asset_value`, `funeral_expenses_paid`, `tax_ny_income_due`) and `value_kind` (`amount`, `none`, `exempt`, `yes_no`). "Exempt" is never turned into 0. |
| Proving the numbers | Tie-out: sum of asset rows must equal the total stated in paragraph 8. A wrong total makes the run **fail** (see `samples/synthetic_affidavit_bad_total.pdf`). |
| Dropped or extra rows | Extracted debt rows are checked against the template's 16 expected rows. |
| Evidence for one record inside a shared document | Each row stores source SHA-256, page, bounding box and a cropped PNG of that exact row. |
| Source documents that move or expire | The PDF is snapshotted at ingest, named by its hash. |
| Entity vs title | The caption line holds the decedent name and the document title side by side; they are split by x-coordinate so the title never leaks into the entity field. |
| Missing data vs low data | Fields are `extracted`, `blank_on_source` (e.g. an empty "File No. ____") or `not_found`, with a completeness summary. |

## Run

```bash
pip install -r requirements.txt
python scripts/make_synthetic_sample.py                 # builds samples/*.pdf
python -m roster_extract.extract samples/synthetic_affidavit.pdf --out examples/synthetic_affidavit
python -m unittest tests.test_extract -v
```

Outputs per document:

- `record.json` - header fields, line items, checks, completeness, all with evidence
- `line_items.csv` - one row per extracted line item
- `tieout_report.md` - checks + every row next to its cropped source image
- `evidence/*.png` - one crop per row
- `snapshot/<sha>.pdf` - ingest-time copy of the source

See [`examples/synthetic_affidavit/tieout_report.md`](examples/synthetic_affidavit/tieout_report.md).

## About the sample data

The parser was developed against a real NY Surrogate's Court filing
("Affidavit to Dispense with the Filing of a Bond", file `input/viewer_4.pdf`),
which the test suite also checks when the file is present (17 rows, amounts
tie out, blank file number detected).

That filing names a private family, a home address, a bank account fragment
and a signature, so it is **not committed** (`.gitignore`). The published
samples are synthetic PDFs with the same layout and invented data, generated
by `scripts/make_synthetic_sample.py`. Private source documents and extracted
personal data stay out of public repositories.

## Layout

```
roster_extract/extract.py        extractor, checks, report writer
scripts/make_synthetic_sample.py synthetic look-alike PDFs (good + bad total)
samples/                         synthetic input PDFs
examples/synthetic_affidavit/    extractor output for the synthetic sample
tests/test_extract.py            unit tests (alignment, tie-out, entity split, evidence)
```
