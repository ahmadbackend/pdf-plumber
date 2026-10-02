"""
Build synthetic look-alike PDFs of the "Affidavit to Dispense with Bond" layout.

The real court filing used during development (input/viewer_4.pdf) names a
private family, a home address and a bank account fragment, so it stays out of
the repository (.gitignore). These synthetic files reproduce the same layout
(same coordinates, value column, bordered rows, multi-line labels, page break
inside the debts table) with invented data, so the parser and tests can be
published and rerun by anyone.

    python scripts/make_synthetic_sample.py
writes:
    samples/synthetic_affidavit.pdf            totals tie out
    samples/synthetic_affidavit_bad_total.pdf  stated total is wrong (tie-out must FAIL)
"""

from pathlib import Path

from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

H = letter[1]
ROOT = Path(__file__).resolve().parents[1]
FOOTER = "SYNTHETIC SAMPLE - invented data in the layout of a NY Surrogate's Court form - not a real filing"

ASSETS = [
    ("Example Savings Bank Checking Account ending in #0000", "$82,500.00"),
    ("Example Brokerage Account ending in #1111", "$41,250.00"),
]
DEBTS_P1 = [
    (["Rents or charges due on any lease or apartment"], "None"),
    (["Amount of any outstanding mortgages or notes"], "None"),
    (["Amount of any judgments held against the decedent", "or his estate"], "None"),
    (["Liability of any bond on which the decedent was a", "party"], "None"),
    (["Federal income taxes due"], "None"),
    (["New York State and/or City income taxes due"], "$1,204.00"),
    (["Real estate taxes due"], "None"),
    (["Federal estate taxes due"], "Exempt"),
    (["New York estate taxes due"], "Exempt"),
    (["Amount of outstanding doctor, hospital or medical", "expenses due not covered by insurance"], "None"),
    (["Amount of funeral expenses paid"], "$9,840.15"),
    (["Amount of funeral expenses still outstanding"], "None"),
    (["Amount of credit card balances—not paid off by", "credit card insurance"], "None"),
]
DEBTS_P2 = [
    (["At the time of death, was the decedent engaged in", "any business?"], "No"),
    (["Amount of any outstanding business liabilities", "payable by decedent and/or his estate"], "None"),
    (["Amount of any federal, state or city public",
      "assistance which the decedent (or any distributee)", "ever received"], "None"),
]


def text(c, x, top, s, size=12, right=False):
    c.setFont("Times-Roman", size)
    y = H - top - 9.6 * size / 12
    (c.drawRightString if right else c.drawString)(x, y, s)


def row_block(c, top, label_lines, value, step=14.2):
    """Bordered two-column row; label may wrap, value sits on the first line."""
    height = step * len(label_lines) + 2
    c.setLineWidth(0.6)
    c.rect(76, H - top - height + 1, 460, height, stroke=1, fill=0)
    c.line(340, H - top + 1, 340, H - top - height + 1)
    for i, line in enumerate(label_lines):
        text(c, 78, top + i * step, line)
    text(c, 534, top, value, right=True)
    return top + height


def build(path: Path, stated_total: str):
    c = canvas.Canvas(str(path), pagesize=letter)
    c.setTitle("Dispense_bond")
    c.setAuthor("synthetic")
    c.setKeywords("HotDocs Template")

    text(c, 72, 74, "SURROGATE’S COURT OF THE STATE OF NEW YORK")
    text(c, 72, 88, "COUNTY OF KINGS")
    text(c, 78, 108, "Probate Proceeding, Estate of")
    text(c, 78, 136, "Margaret Holloway")
    text(c, 375, 136, "AFFIDAVIT TO DISPENSE WITH")
    text(c, 375, 149, "THE FILING OF A BOND")
    text(c, 266, 163, "Deceased.")
    text(c, 361, 177, "File No. 2026-0000/A")
    text(c, 72, 205, "State of New York )")
    text(c, 258, 219, "SS:")
    text(c, 72, 233, "County of Kings )")
    text(c, 72, 260, "1. I, Daniel R. Holloway, being duly sworn, depose and say that I am over the age of 18")
    text(c, 72, 274, "years and reside and am domiciled at 48 Example Street, Apt 2B, Springfield, IL")
    text(c, 72, 288, "62701;")
    text(c, 72, 316, "2. I am a distributee of decedent, and the petitioner in the captioned proceeding, and have")
    text(c, 72, 329, "personal knowledge as to the assets, debts and liabilities of the estate of the decedent.")
    text(c, 72, 357, "3. The value of the decedent's assets passing through the estate is:")
    top = 385
    for label, value in ASSETS:
        top = row_block(c, top, [label], value) + 1
    top += 12
    text(c, 72, top, "4. The decedent or his estate will be liable for the following debts:")
    top += 28
    text(c, 339, top - 1, "(State amounts due or answer None.)", size=11)
    top += 15
    for label_lines, value in DEBTS_P1:
        top = row_block(c, top, label_lines, value)
    text(c, 72, 737, FOOTER, size=8)
    c.showPage()

    top = 74
    for label_lines, value in DEBTS_P2:
        top = row_block(c, top, label_lines, value)
    text(c, 72, 186, "5. The decedent was 88 years old at the time of her death.")
    text(c, 72, 214, "6. EXCEPT AS OTHERWISE STATED ABOVE, NEITHER THE DECEDENT NOR")
    text(c, 72, 228, "HER ESTATE IS LIABLE FOR ANY OUTSTANDING DEBTS OR CLAIMS.")
    text(c, 72, 269, "7. Attached are consents from all of the interested parties of the estate that the filing of a")
    text(c, 72, 283, "bond be dispensed with by this court. All parties are competent adults.")
    text(c, 72, 310, f"8. The total value of their shares of the estate will be {stated_total}.")
    text(c, 108, 338, "WHEREFORE your deponent prays that the filing of a bond by Daniel R. Holloway, as")
    text(c, 72, 352, "executor be dispensed with.")
    text(c, 324, 407, "Daniel R. Holloway")
    text(c, 72, 433, "Sworn to before me this 3rd day of March 2026.")
    text(c, 72, 490, "Notary Public")
    text(c, 72, 725, FOOTER, size=8)
    c.save()


def main():
    out = ROOT / "samples"
    out.mkdir(exist_ok=True)
    build(out / "synthetic_affidavit.pdf", "$123,750.00")
    build(out / "synthetic_affidavit_bad_total.pdf", "$125,000.00")
    print("wrote", *sorted(p.name for p in out.glob("*.pdf")))


if __name__ == "__main__":
    main()
