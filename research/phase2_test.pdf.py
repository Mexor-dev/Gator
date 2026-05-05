from fpdf import FPDF

pdf = FPDF()
pdf.set_auto_page_break(auto=True, margin=15)
pdf.add_page()
pdf.set_font("Helvetica", size=12)
text = (
    "Gator Scholar test corpus. "
    "Vector Pivot combines graph central nodes with semantic retrieval. "
    "Graphify builds structural relations while LanceDB stores embedding chunks. "
    "The system caps retrieval context to 768 tokens to preserve VRAM headroom. "
    "CPU and SSD paths handle indexing while the 1.5B chassis handles inference."
)
for _ in range(8):
    pdf.multi_cell(180, 8, text)

pdf.output("/home/user/Gator/research/phase2_test.pdf")
print("wrote /home/user/Gator/research/phase2_test.pdf")
