from pathlib import Path
import re

root = Path(__file__).parent
lines = (root / "BACKEND_AND_AGENT_GUIDE.md").read_text(encoding="utf-8").splitlines()
pages, page = [], []
for raw in lines:
    text = raw.replace("**", "").replace("`", "")
    text = re.sub(r"^#+\s*", "", text)
    text = text.encode("ascii", "replace").decode("ascii")
    if not text.strip():
        page.append("")
        continue
    while len(text) > 105:
        cut = text.rfind(" ", 0, 105) or 105
        page.append(text[:cut]); text = text[cut:].lstrip()
    page.append(text)
    if len(page) >= 48:
        pages.append(page); page = []
if page: pages.append(page)

objects = []
def add(value):
    objects.append(value)
    return len(objects)

font = add("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
page_ids = []
for page_lines in pages:
    content = ["BT", "/F1 9 Tf", "50 760 Td", "11 TL"]
    for line in page_lines:
        safe = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        content.append(f"({safe}) Tj T*" if safe else "T*")
    content.append("ET")
    stream = "\n".join(content).encode("ascii")
    content_id = add(f"<< /Length {len(stream)} >>\nstream\n{stream.decode('ascii')}\nendstream")
    page_ids.append(add(f"<< /Type /Page /Parent 0 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 {font} 0 R >> >> /Contents {content_id} 0 R >>"))
pages_id = add("<< /Type /Pages /Kids [" + " ".join(f"{i} 0 R" for i in page_ids) + f"] /Count {len(page_ids)} >>")
for i in page_ids:
    objects[i - 1] = objects[i - 1].replace("/Parent 0 0 R", f"/Parent {pages_id} 0 R")
catalog = add(f"<< /Type /Catalog /Pages {pages_id} 0 R >>")
pdf = bytearray(b"%PDF-1.4\n")
offsets = [0]
for number, value in enumerate(objects, 1):
    offsets.append(len(pdf)); pdf += f"{number} 0 obj\n{value}\nendobj\n".encode("ascii")
xref = len(pdf); pdf += f"xref\n0 {len(objects)+1}\n0000000000 65535 f \n".encode("ascii")
for offset in offsets[1:]: pdf += f"{offset:010d} 00000 n \n".encode("ascii")
pdf += f"trailer\n<< /Size {len(objects)+1} /Root {catalog} 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode("ascii")
output = root / "Manufacturing_Equipment_Copilot_Backend_Guide_v2.pdf"
output.write_bytes(pdf)
print(output)
