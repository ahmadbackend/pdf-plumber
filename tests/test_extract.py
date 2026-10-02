import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from roster_extract.extract import AffidavitExtractor, group_rows, split_value  # noqa: E402

SAMPLE = ROOT / "samples" / "synthetic_affidavit.pdf"
BAD = ROOT / "samples" / "synthetic_affidavit_bad_total.pdf"
REAL = ROOT / "input" / "viewer_4.pdf"   # private, gitignored; tests skip without it


def ensure_samples():
    if not SAMPLE.exists() or not BAD.exists():
        subprocess.run([sys.executable, str(ROOT / "scripts" / "make_synthetic_sample.py")], check=True)


def run(pdf, crops=False):
    out = Path(tempfile.mkdtemp())
    return AffidavitExtractor(pdf, out, save_crops=crops).run(), out


def checks(res):
    return {c["check"]: c for c in res.checks}


class TestSynthetic(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ensure_samples()
        cls.res, cls.out = run(SAMPLE, crops=True)
        cls.items = {i.row_id: i for i in cls.res.line_items}
        cls.header = {h.name: h for h in cls.res.header}

    def test_all_checks_pass(self):
        failed = [c["check"] for c in self.res.checks if not c["passed"]]
        self.assertEqual(failed, [])

    def test_row_counts_match_template(self):
        self.assertEqual(sum(i.section == "assets" for i in self.res.line_items), 2)
        self.assertEqual(sum(i.section == "debts" for i in self.res.line_items), 16)

    def test_amounts_tie_to_source_row(self):
        self.assertEqual(self.items["assets-01"].amount, "82500.00")
        self.assertEqual(self.items["assets-02"].amount, "41250.00")
        self.assertEqual(self.items["debts-06"].amount_type, "tax_ny_income_due")
        self.assertEqual(self.items["debts-06"].amount, "1204.00")
        self.assertEqual(self.items["debts-11"].amount_type, "funeral_expenses_paid")
        self.assertEqual(self.items["debts-11"].amount, "9840.15")

    def test_value_kinds_are_not_flattened(self):
        self.assertEqual(self.items["debts-08"].value_kind, "exempt")
        self.assertIsNone(self.items["debts-08"].amount)
        self.assertEqual(self.items["debts-14"].value_kind, "yes_no")
        self.assertEqual(self.items["debts-01"].value_kind, "none")

    def test_multiline_labels_are_joined(self):
        self.assertEqual(self.items["debts-16"].label,
                         "Amount of any federal, state or city public assistance which the "
                         "decedent (or any distributee) ever received")
        self.assertEqual(self.items["debts-16"].evidence.page, 2)

    def test_entity_split_from_title(self):
        self.assertEqual(self.header["decedent"].value, "Margaret Holloway")
        self.assertEqual(self.header["document_title"].value,
                         "AFFIDAVIT TO DISPENSE WITH THE FILING OF A BOND")
        self.assertEqual(self.header["petitioner"].value, "Daniel R. Holloway")
        self.assertIn("proposed_executor", self.header["petitioner_roles"].value)
        self.assertEqual(self.header["sworn_date"].value, "2026-03-03")

    def test_every_row_has_its_own_crop(self):
        crops = [i.evidence.crop for i in self.res.line_items]
        self.assertTrue(all(crops))
        self.assertEqual(len(set(crops)), len(crops))
        for c in crops:
            self.assertTrue((self.out / c).exists())

    def test_snapshot_stored_with_hash(self):
        snap = list((self.out / "snapshot").glob("*.pdf"))
        self.assertEqual(len(snap), 1)
        self.assertTrue(snap[0].name.startswith(self.res.source_sha256[:16]))


class TestTieOutFailure(unittest.TestCase):
    def test_wrong_stated_total_fails(self):
        ensure_samples()
        res, _ = run(BAD)
        c = checks(res)["asset_sum_ties_to_stated_total"]
        self.assertFalse(c["passed"])
        self.assertEqual(c["detail"]["asset_sum"], "123750.00")
        self.assertEqual(c["detail"]["stated_total"], "125000.00")


class TestRowAlignment(unittest.TestCase):
    def w(self, text, x0, x1, top):
        return {"text": text, "x0": x0, "x1": x1, "top": top, "bottom": top + 12}

    def test_value_on_its_own_band_is_not_paired(self):
        # value drifted 6pt below its label: it must NOT be merged into that row
        words = [self.w("Real", 78, 100, 569.0), self.w("estate", 103, 130, 569.0),
                 self.w("$500.00", 495, 534, 575.0)]
        rows = group_rows(words)
        self.assertEqual(len(rows), 2)
        self.assertIsNone(split_value(rows[0]))

    def test_value_in_band_is_paired(self):
        words = [self.w("Real", 78, 100, 569.0), self.w("estate", 103, 130, 569.0),
                 self.w("$500.00", 495, 534, 569.4)]
        rows = group_rows(words)
        self.assertEqual(len(rows), 1)
        label, value = split_value(rows[0])
        self.assertEqual(value[0]["text"], "$500.00")


@unittest.skipUnless(REAL.exists(), "private source PDF not present")
class TestRealFiling(unittest.TestCase):
    def test_real_filing_ties_out(self):
        res, _ = run(REAL)
        self.assertTrue(all(c["passed"] for c in res.checks), json.dumps(res.checks, indent=1))
        items = {i.row_id: i for i in res.line_items}
        self.assertEqual(len(res.line_items), 17)
        self.assertEqual(items["assets-01"].amount, "150000.00")
        self.assertEqual(items["debts-11"].amount, "13366.77")
        file_no = next(h for h in res.header if h.name == "file_no")
        self.assertEqual(file_no.status, "blank_on_source")


if __name__ == "__main__":
    unittest.main(verbosity=2)
