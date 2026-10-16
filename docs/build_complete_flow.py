"""Create an editable, numbered end-to-end sequence diagram from source behavior."""
from pathlib import Path
from xml.sax.saxutils import escape

HERE = Path(__file__).resolve().parent
W, H = 2400, 3430
parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" role="img" aria-label="Complete question to answer architecture">',
'''<defs><marker id="arrow" markerWidth="9" markerHeight="9" refX="8" refY="4.5" orient="auto-start-reverse"><path d="M0 0 L9 4.5 L0 9Z" fill="context-stroke"/></marker><style>text{font-family:Segoe UI,Arial,sans-serif}</style></defs>''']

def rect(x,y,w,h,fill='#ffffff',stroke='#cbd5e1',rx=12):
    parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" fill="{fill}" stroke="{stroke}" rx="{rx}"/>')

def text(x,y,value,size=21,bold=False,color='#263c57'):
    cls='head' if bold else 'body'
    parts.append(f'<text x="{x}" y="{y}" class="{cls}" font-size="{size}" fill="{color}" font-weight="{700 if bold else 400}">{escape(value)}</text>')

def line(coords,color='#326bc1',arrow=True):
    d='M'+ ' L'.join(f'{x} {y}' for x,y in coords)
    parts.append(f'<path d="{d}" class="line" fill="none" stroke="{color}" stroke-width="2" marker-end="{ "url(#arrow)" if arrow else "none"}"/>')

rect(0,0,W,H,'#f5f8fc','#f5f8fc',0)
text(50,58,'Manufacturing Equipment Copilot',36,True)
text(50,97,'Complete user question → evidence retrieval → validated answer flow',26)
text(50,132,'Numbered in execution order. Blue arrows = calls / forward flow. Green arrows = results / responses. Boxes = processing within a component.',20)
xs=[140,470,930,1390,1810,2210]
names=[('USER','Technician'),('FRONTEND','React / Vite'),('BACKEND','FastAPI + Google ADK'),('MODEL SERVICES','Vertex AI / Gemini'),('KNOWLEDGE STORE','BigQuery'),('ASSET STORE','Cloud Storage')]
for i,(x,(a,b)) in enumerate(zip(xs,names)):
    left=40 if i==0 else x-165
    width=200 if i==0 else 330
    rect(left,166,width,83, '#e9effb' if i<3 else '#e6f4f0')
    text(left+15,197,a,18,True)
    text(left+15,226,b,20)
    line([(x,258),(x,2570)],'#d0dbea',False)

y=290
step=0
def message(a,b,label,result=False):
    global y,step
    step+=1
    color='#188267' if result else '#326bc1'
    lo,hi=sorted([xs[a],xs[b]])
    text(lo+14,y-12,f'{step:02d}  {label}',20,color=color)
    line([(xs[a],y),(xs[b],y)],color)
    y+=67

def action(label,detail=''):
    global y,step
    step+=1
    rect(695,y-29,470,57 if detail else 45,'#eaf0fb','#a9bfdf',8)
    text(710,y-4,f'{step:02d}  {label}',19,True)
    if detail: text(710,y+17,detail,17)
    y+=74

message(0,1,'Enter question + optional equipment photo')
message(1,2,'POST /sessions — create chat when needed')
message(2,1,'Session ID + expiry returned',True)
message(1,2,'POST /chat — session_id + message + image (multipart)')
action('Validate session and input','Question ≤ 6000 chars; PNG / JPEG / WebP ≤ 5 MB')
action('Normalize photo; acquire session lock','Load recent history + topic + guided workflow state')
action('Route inventory / technical question','Inventory shortcut shown below; continue for technical Q')
message(2,3,'Planner request: question + recent context + optional photo')
message(3,2,'QueryPlan: search text, topic, follow-up, media needs',True)
action('Check plan; prepare retrieval query','Clarify / reject scope when needed; preserve follow-up topic')
message(2,3,'Embed search text — RETRIEVAL_QUERY / 768 dimensions')
message(3,2,'gemini-embedding-001 → query vector',True)
message(2,4,'Parameterized SQL: document_chunks + cosine distance + keyword bonus')
message(4,2,'Ranked seed chunks; retain candidates per explicit machine',True)
message(2,4,'Expand selected document pages; enrich images / tables / links / sections')
message(4,2,'Selected chunks + geometry + relationships + media metadata',True)
message(2,4,'Read verified QR links for selected machines / documents / pages')
message(4,2,'Relevant saved QR destinations (when available)',True)
message(2,5,'Load relevant manual images / original PDF when required')
message(5,2,'Asset bytes for image normalization / PDF-page rendering',True)
action('Prepare approved evidence + allowed media','Validate relevant URLs; keep provenance and load warnings')
action('Build compact agent payload','Short evidence IDs + workflow + recent context + image bytes')
message(2,3,'ADK agent: approved evidence + allowed media + optional user photo')
message(3,2,'Structured AgentAnswer JSON + evidence IDs',True)
action('Validate JSON and evidence identifiers','Reject unknown IDs; map short IDs to real chunk IDs')
action('Assemble final response blocks','Text / tables / images + descriptions / links / sources')
action('Save state and conversation history','Session memory stores workflow, history and response assets')
message(2,1,'JSON: blocks + warnings + session_id + expiry + elapsed time',True)
message(1,2,'GET /images/{asset_id} — when displaying image blocks')
action('Look up session-owned image bytes','Image route serves assets from backend memory')
message(2,1,'PNG image bytes (Cache-Control: no-store)',True)
message(1,0,'Render final answer: text, tables, images, video / QR / source links',True)

panel_y=y+10
rect(35,panel_y,2330,267,'#ffffff','#cbd5e1',16)
text(58,panel_y+35,'ALTERNATE PATHS — controlled by the backend',23,True)
def branch_row(yy,items):
    positions=[58,640,1222,1804]
    widths=[520,520,520,535]
    for i,label in enumerate(items):
        rect(positions[i],yy,widths[i],47,'#eef3fb','#c8d7eb',7)
        text(positions[i]+12,yy+30,label,18)
        if i<len(items)-1: line([(positions[i]+widths[i],yy+24),(positions[i+1],yy+24)])
branch_row(panel_y+54,['Inventory request (no photo)','GROUP BY machine + document in BigQuery','Build direct response; skip models','Record history → JSON → user'])
branch_row(panel_y+119,['Planner requests clarification / rejects scope','Build clarification / scope reply','Record direct reply in session','JSON → frontend → user'])
branch_row(panel_y+184,['No chunks / unsupported answer / failure','Unavailable reply or HTTP error','Frontend displays message / error','User can clarify or retry'])

off=panel_y+296
rect(35,off,2330,270,'#eaf3ff','#c4d7ee',16)
text(58,off+36,'OFFLINE KNOWLEDGE PREPARATION — populates the stores used above',23,True)
positions=[58,530,1002,1474,1946]
labels=[('PDF + machine metadata','Manual ingestion CLI'),('Extract + normalize','PyMuPDF / tables / relationships'),('Document embeddings','RETRIEVAL_DOCUMENT / 768 values'),('Upload processed artifacts','Cloud Storage: JSON + images'),('Validate + load tables','Read GCS artifacts → BigQuery')]
for i,(x,(a,b)) in enumerate(zip(positions,labels)):
    rect(x,off+60,420,84,'#ffffff','#c4d7ee',10)
    text(x+14,off+90,a,21,True)
    text(x+14,off+120,b,18)
    if i<4:line([(x+420,off+102),(positions[i+1],off+102)])
text(58,off+185,'Separate optional QR path:',20,True)
text(350,off+185,'PDF page / image → Document AI → review extracted URLs → save only verified QR destinations in BigQuery.',20)
text(58,off+228,'Shared embedding model: gemini-embedding-001. Chat performs cloud reads; offline ingestion and verified QR utilities write persistent data.',20)

footer=off+302
text(50,footer,'BOUNDARIES: user photos support observation; manuals provide technical evidence. The agent receives prepared evidence, not arbitrary database tools.',19)
text(50,footer+30,'Sessions / response images live in backend memory, expire within one hour, and clear on restart. /health is a process check. QR decoding remains unverified.',19)
text(50,footer+60,'Source verified against frontend/src, backend/app, src/simple_ingestion and backend/scripts · 16 October 2026 · Implemented flow, not deployment topology.',18)
assert footer+65 < H, (footer,H)
parts.append('</svg>')
target=HERE/'complete-question-to-answer-flow.svg'
target.write_text('\n'.join(parts),encoding='utf-8')
print(target)
