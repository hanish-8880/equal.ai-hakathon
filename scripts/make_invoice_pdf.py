"""Mock invoice PDF (SIMULATED input) for the PDF → total → email demo.
Run: python -m scripts.make_invoice_pdf [out.pdf]
"""
import sys
from fpdf import FPDF

out = sys.argv[1] if len(sys.argv) > 1 else "media_tmp/invoice_demo.pdf"
rows = [("Cement OPC 53 - 50 bags x 385", 19250), ("TMT Steel 12mm - 0.5 ton", 31500), ("River sand - 2 units", 9000),
        ("Transport charges", 2000), ("Loading / unloading", 750), ("GST 18% on cement", 3465), ("Discount", -1000)]
pdf = FPDF(); pdf.add_page(); pdf.set_font("Helvetica", "B", 16)
pdf.cell(0, 10, "SRI SAI BUILDING MATERIALS (sample)", new_x="LMARGIN", new_y="NEXT")
pdf.set_font("Helvetica", "", 11)
pdf.cell(0, 7, "Invoice No: SSB-2026-0412    Date: 20-11-2026    Bill to: Hanish", new_x="LMARGIN", new_y="NEXT"); pdf.ln(4)
pdf.set_font("Helvetica", "B", 11); pdf.cell(140, 8, "Item", border=1); pdf.cell(40, 8, "Amount (Rs)", border=1, new_x="LMARGIN", new_y="NEXT")
pdf.set_font("Helvetica", "", 11)
for label, amt in rows:
    pdf.cell(140, 8, label, border=1); pdf.cell(40, 8, f"{amt:,}", border=1, align="R", new_x="LMARGIN", new_y="NEXT")
pdf.set_font("Helvetica", "B", 11)
pdf.cell(140, 8, "GRAND TOTAL", border=1); pdf.cell(40, 8, f"{sum(a for _, a in rows):,}", border=1, align="R", new_x="LMARGIN", new_y="NEXT")
pdf.output(out); print("saved", out, "true total", sum(a for _, a in rows))
