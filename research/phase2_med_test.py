from fpdf import FPDF

pdf = FPDF()
pdf.add_page()
pdf.set_font('Helvetica', size=12)
text = (
    'Medication note: Ibuprofen is a nonsteroidal anti-inflammatory drug used for pain and fever. '
    'Typical adult oral dosing is 200mg to 400mg every 4 to 6 hours, with maximum daily limits depending on guidance. '
    'Contraindications include some kidney, GI, or bleeding risk cases.'
)
for _ in range(6):
    pdf.multi_cell(180, 8, text)
pdf.output('/home/user/Gator/research/medication_test.pdf')
print('wrote medication_test.pdf')
