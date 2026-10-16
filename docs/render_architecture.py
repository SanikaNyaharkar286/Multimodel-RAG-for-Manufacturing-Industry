"""Render this project's code-native architecture SVG to a high-resolution PNG."""
from pathlib import Path
import sys
import math
import re
import xml.etree.ElementTree as ET
from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent
SCALE = 2
source = HERE / (sys.argv[1] if len(sys.argv) > 1 else "system-architecture.svg")
root = ET.parse(source).getroot()
image = Image.new("RGB", (int(root.attrib['width']) * SCALE, int(root.attrib['height']) * SCALE), "#f5f8fc")
draw = ImageDraw.Draw(image)
styles = {
    "title": (32, True, "#172b46"), "subtitle": (17, False, "#52647c"),
    "section": (16, True, "#172b46"), "head": (19, True, "#172b46"),
    "body": (15, False, "#465b75"), "small": (14, False, "#52647c"),
    "note": (16, False, "#465b75"),
}
for element in root:
    tag = element.tag.rsplit("}", 1)[-1]
    a = element.attrib
    if tag == "rect":
        x, y, w, h = [float(a.get(k, 0)) for k in ("x", "y", "width", "height")]
        card = a.get("class") == "card"
        draw.rounded_rectangle(
            (x*SCALE, y*SCALE, (x+w)*SCALE, (y+h)*SCALE),
            radius=float(a.get("rx", 14 if card else 0))*SCALE,
            fill=a.get("fill", "white" if card else "black"),
            outline=a.get("stroke", "#ccdae9" if card else None),
            width=3 if card or "stroke" in a else 1,
        )
    elif tag == "text":
        size, bold, color = styles[a["class"]]
        size, color = int(a.get('font-size', size)), a.get('fill', color)
        font = ImageFont.truetype("C:/Windows/Fonts/" + ("segoeuib.ttf" if bold else "segoeui.ttf"), size*SCALE)
        draw.text((float(a["x"])*SCALE, float(a["y"])*SCALE), element.text or "", font=font, fill=color, anchor="ls")
    elif tag == "path" and a.get("class") in ("line", "dash"):
        tokens = re.findall(r"[MHVL]|-?\d+(?:\.\d+)?", a["d"])
        points, i, x, y = [], 0, 0., 0.
        while i < len(tokens):
            command = tokens[i]
            i += 1
            if command in ("M", "L"):
                x, y = float(tokens[i]), float(tokens[i+1])
                i += 2
            elif command == "H":
                x = float(tokens[i]); i += 1
            elif command == "V":
                y = float(tokens[i]); i += 1
            points.append((x*SCALE, y*SCALE))
        color = a.get('stroke', '#64748b')
        draw.line(points, fill=color, width=2*SCALE)
        if a.get('marker-end') == 'none':
            continue
        px, py = points[-2]
        ex, ey = points[-1]
        angle = math.atan2(ey-py, ex-px)
        length, half = 8*SCALE, 4*SCALE
        bx, by = ex-length*math.cos(angle), ey-length*math.sin(angle)
        draw.polygon([(ex,ey), (bx-half*math.sin(angle),by+half*math.cos(angle)), (bx+half*math.sin(angle),by-half*math.cos(angle))], fill=color)

target = source.with_suffix('.png')
image.save(target)
print(target)
