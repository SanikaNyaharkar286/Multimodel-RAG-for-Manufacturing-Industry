"""ReportLab layout for an already authorized, normalized report snapshot.

No retrieval, persistence, identity checks or repair-state inference belong here.
Only inline PNGs in the snapshot's images mapping can become PDF images.
"""

import base64
import binascii
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from html import escape
from io import BytesIO
from pathlib import Path
import re

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen.canvas import Canvas
from reportlab.platypus import (
    Image, LongTable, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
)
from app.responses.maintenance_summary import performed_steps


_IST = timezone(timedelta(hours=5, minutes=30), 'Asia/Calcutta')
_INK = colors.HexColor('#173653')
_BLUE = colors.HexColor('#e9f3fc')
_BORDER = colors.HexColor('#d7e0e8')
_MUTED = colors.HexColor('#536574')
_WIDTH = A4[0] - 84
_MISSING = 'Not recorded'


def _value(value, default=_MISSING):
    """Keep zero, explicit false, table order and all supplied text."""
    if value is None or value == '' or value == [] or value == {}:
        return default
    if isinstance(value, bool):
        return 'Yes' if value else 'No'
    if isinstance(value, dict):
        return '\n'.join(f'{key}: {_value(item)}' for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return '\n'.join(_value(item) for item in value)
    return str(value)


def _safe(value):
    # XML controls are neither printable text nor valid paragraph markup.
    return escape(re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', _value(value)), quote=True)


def _inline(value):
    """Escape first; the only generated markup is our small Markdown subset."""
    text = _safe(value)
    text = re.sub(r'\*\*([^*\n]+)\*\*', r'<b>\1</b>', text)
    text = re.sub(r'`([^`\n]+)`', r'<font color="#354d62">\1</font>', text)
    text = re.sub(r'(?<!\*)\*([^*\n]+)\*(?!\*)', r'<i>\1</i>', text)
    return text.replace('\n', '<br/>')


def _date(value):
    if not value:
        return _MISSING
    if isinstance(value, date) and not isinstance(value, datetime):
        return value.strftime('%d %b %Y')
    try:
        text = str(value)
        if len(text) == 10:
            return date.fromisoformat(text).strftime('%d %b %Y')
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(text.replace('Z', '+00:00'))
        # A naive technician date is local, never silently interpreted as UTC.
        parsed = parsed.replace(tzinfo=_IST) if parsed.tzinfo is None else parsed
        return parsed.astimezone(_IST).strftime('%d %b %Y, %I:%M %p') + ' IST'
    except (ValueError, TypeError, OverflowError):
        return _value(value)


@lru_cache(maxsize=1)
def _fonts():
    candidates = [
        (Path('C:/Windows/Fonts'), ('segoeui.ttf', 'segoeuib.ttf', 'segoeuii.ttf', 'segoeuiz.ttf')),
        (Path('C:/Windows/Fonts'), ('arial.ttf', 'arialbd.ttf', 'ariali.ttf', 'arialbi.ttf')),
        (Path('/usr/share/fonts/truetype/dejavu'), ('DejaVuSans.ttf', 'DejaVuSans-Bold.ttf',
                                                 'DejaVuSans-Oblique.ttf', 'DejaVuSans-BoldOblique.ttf')),
    ]
    for folder, files in candidates:
        if all((folder / name).is_file() for name in files):
            for suffix, filename in zip(('', '-Bold', '-Italic', '-BoldItalic'), files):
                pdfmetrics.registerFont(TTFont('Maintenance' + suffix, str(folder / filename)))
            pdfmetrics.registerFontFamily('Maintenance', normal='Maintenance', bold='Maintenance-Bold',
                                          italic='Maintenance-Italic', boldItalic='Maintenance-BoldItalic')
            return 'Maintenance', 'Maintenance-Bold'
    return 'Helvetica', 'Helvetica-Bold'


class _NumberedCanvas(Canvas):
    """Replay completed pages once the final page count is known."""

    def __init__(self, *args, report_title='', fonts=(), **kwargs):
        super().__init__(*args, **kwargs)
        self._saved_pages = []
        self._report_title = report_title
        self._report_fonts = fonts

    def showPage(self):
        self._saved_pages.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        count = len(self._saved_pages)
        for state in self._saved_pages:
            self.__dict__.update(state)
            self._page_furniture(count)
            super().showPage()
        super().save()

    def _page_furniture(self, count):
        regular, bold = self._report_fonts
        self.saveState()
        self.setStrokeColor(_BORDER)
        self.line(42, A4[1] - 61, A4[0] - 42, A4[1] - 61)
        # Use the full header width now that internal report IDs are omitted.
        style = ParagraphStyle('running', fontName=bold, fontSize=8, leading=10,
                               textColor=_INK, splitLongWords=True)
        title = Paragraph(_safe(self._report_title), style)
        _, height = title.wrap(_WIDTH, 28)
        if height <= 28:
            title.drawOn(self, 42, A4[1] - 28 - height)
        else:
            # Full title is retained in the body and PDF metadata.
            self.setFont(bold, 8)
            self.drawString(42, A4[1] - 39, 'Technical Service & Maintenance Report')
        self.line(42, 39, A4[0] - 42, 39)
        self.setFillColor(_MUTED)
        self.setFont(regular, 8)
        self.drawRightString(A4[0] - 42, 25, f'Page {self._pageNumber} of {count}')
        self.restoreState()


class _Renderer:
    def __init__(self, report):
        self.report = report
        self.story = []
        self.font, self.bold = _fonts()
        common = dict(fontName=self.font, textColor=_INK, alignment=TA_LEFT,
                      splitLongWords=True, allowWidows=0, allowOrphans=0)
        self.body = ParagraphStyle('body', fontSize=9, leading=13, spaceAfter=5, **common)
        self.small = ParagraphStyle('small', parent=self.body, fontSize=8, leading=11,
                                    textColor=_MUTED, spaceAfter=3)
        self.heading = ParagraphStyle('section', parent=self.body, fontName=self.bold,
                                      fontSize=11, leading=15, backColor=_BLUE,
                                      borderPadding=7, spaceBefore=16, spaceAfter=12,
                                      keepWithNext=True)
        self.subheading = ParagraphStyle('subheading', parent=self.body, fontName=self.bold,
                                         fontSize=10, leading=14, spaceBefore=10,
                                         spaceAfter=6, keepWithNext=True)
        self.title_style = ParagraphStyle('title', parent=self.body, fontName=self.bold,
                                          fontSize=19, leading=24, spaceAfter=10)
        self.label = ParagraphStyle('label', parent=self.body, fontName=self.bold,
                                    fontSize=8.5, leading=12, spaceAfter=0)
        self.cell = ParagraphStyle('cell', parent=self.body, spaceAfter=0)
        self.caption = ParagraphStyle('caption', parent=self.small, fontName=self.bold,
                                      keepWithNext=True, spaceBefore=8)
        self.images = report.get('images') or {}
        self.evidence = [item for item in report.get('evidence', []) if isinstance(item, dict)]
        self.rendered_images = set()
        self.figure_numbers = {}
        self.attachment_refs = []
        self.rendered_guidance = set()
        self.steps = report.get('steps') or []
        self.history = self._history()

    def _history(self):
        history = [item for item in self.report.get('history', []) if isinstance(item, dict)]
        case_id = self.report.get('case_id')
        if case_id:
            return [item for item in history if item.get('case_id') == case_id]
        selected = self.report.get('message_id')
        if selected:
            for index, item in enumerate(history):
                if item.get('id') == selected:
                    start = index - 1 if index and history[index - 1].get('role') == 'user' else index
                    return history[start:index + 1]
            return []
        # Legacy snapshots already contain only the selected answer and its question.
        return history

    def p(self, value, style=None):
        style = style or self.body
        status = _value(value).strip().lower()
        tone = None
        if status in ('completed', 'passed', 'resolved', 'operational'):
            tone = '#286544'
        elif status in ('failed', 'unresolved', 'not operational', 'not authorized'):
            tone = '#993d3d'
        elif status in ('pending', 'not performed', 'not tested', 'partially resolved', 'under investigation'):
            tone = '#886319'
        markup = _inline(value)
        if tone:
            markup = f'<font color="{tone}">{markup}</font>'
        return Paragraph(markup, style)

    def text(self, value, style=None):
        self.story.append(self.p(value, style))

    def section(self, number, title):
        self.text(f'{number}. {title}', self.heading)

    def table(self, columns, rows, widths=None):
        """Split only oversized rows internally; repeat headers after every split."""
        columns = list(columns)
        rows = [list(row) if isinstance(row, (tuple, list)) else [row] for row in rows]
        count = max([len(columns)] + [len(row) for row in rows] + [1])
        columns += [f'Column {i + 1}' for i in range(len(columns), count)]
        if not rows:
            self.text('No recorded rows.', self.small)
            return
        if count > 6:
            # Wide source tables become readable record tables, in original row order.
            for index, row in enumerate(rows, 1):
                self.text(f'Table record {index}', self.subheading)
                self.table(['Original column', 'Recorded value'],
                           [(label, row[i] if i < len(row) else '') for i, label in enumerate(columns)],
                           [_WIDTH * .32, _WIDTH * .68])
            return
        if widths is None:
            widths = [_WIDTH / count] * count
        cells = [[self.p(item, self.label) for item in columns]]
        cells += [[self.p(row[i] if i < len(row) else '', self.cell) for i in range(count)] for row in rows]
        table = LongTable(cells, colWidths=widths, repeatRows=1, splitByRow=1, splitInRow=1,
                          hAlign='LEFT', spaceAfter=8)
        table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), _BLUE),
            ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#f7f9fb')]),
            ('GRID', (0, 0), (-1, -1), .45, _BORDER),
            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
            ('LEFTPADDING', (0, 0), (-1, -1), 7),
            ('RIGHTPADDING', (0, 0), (-1, -1), 7),
            ('TOPPADDING', (0, 0), (-1, -1), 6),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ]))
        self.story.append(table)

    def kv(self, pairs):
        self.table(['Field', 'Recorded information'], pairs, [_WIDTH * .30, _WIDTH * .70])

    def fields(self, fields):
        self.kv([(label, self.report.get(key)) for label, key in fields])

    def markdown(self, value):
        """Basic headings/lists/fences and Markdown tables, with no raw HTML."""
        lines = _value(value).splitlines()
        index = 0
        fenced = False
        while index < len(lines):
            line = lines[index]
            if line.strip().startswith('```'):
                fenced = not fenced
                index += 1
                continue
            if (not fenced and index + 1 < len(lines) and '|' in line
                    and re.fullmatch(r'\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*', lines[index + 1])):
                def cells(text):
                    return [cell.strip() for cell in text.strip().strip('|').split('|')]
                columns = cells(line)
                index += 2
                rows = []
                while index < len(lines) and '|' in lines[index] and lines[index].strip():
                    rows.append(cells(lines[index]))
                    index += 1
                self.table(columns, rows)
                continue
            heading = re.match(r'^\s*#{1,6}\s+(.+)', line) if not fenced else None
            if heading:
                self.text(heading[1], self.subheading)
            elif line.strip():
                if not fenced:
                    line = re.sub(r'^\s*[-*]\s+', '\u2022 ', line)
                self.text(line)
            else:
                self.story.append(Spacer(1, 4))
            index += 1

    @staticmethod
    def image_key(block):
        key = block.get('image_id') or block.get('asset_id')
        if key:
            return str(key)
        # Interpret an existing internal reference as a lookup key, never a fetch.
        src = str(block.get('src') or '')
        if src.startswith(('/images/', '/reports/')):
            return src.rsplit('/', 1)[-1].split('?', 1)[0]
        return ''

    def sources(self, sources):
        for source in sources or []:
            if not isinstance(source, dict):
                continue
            name = _value(source.get('document_name'))
            page = source.get('page_number')
            reference = name + (f', PDF page {page}' if page is not None else '')
            self.text('Source: ' + reference, self.small)
            if reference not in self.attachment_refs:
                self.attachment_refs.append(reference)

    def image(self, key, caption='', description='', source_type='', step_id=None, source=None):
        data = self.images.get(key)
        if not isinstance(data, str) or not data.startswith('data:image/png;base64,'):
            self.text('Referenced image unavailable in this report snapshot.', self.small)
            return False
        try:
            raw = base64.b64decode(data.split(',', 1)[1], validate=True)
            if not raw.startswith(b'\x89PNG\r\n\x1a\n'):
                raise ValueError('Not a PNG')
            picture = Image(BytesIO(raw))
            scale = min(_WIDTH / picture.imageWidth, 255 / picture.imageHeight, 1.0)
            picture.drawWidth = picture.imageWidth * scale
            picture.drawHeight = picture.imageHeight * scale
            picture.hAlign = 'LEFT'
        except (ValueError, TypeError, binascii.Error, OSError, ZeroDivisionError):
            self.text('Referenced image unavailable in this report snapshot.', self.small)
            return False
        number = self.figure_numbers.setdefault(key, len(self.figure_numbers) + 1)
        self.text(f'Figure {number}. {_value(caption, "Supporting evidence")}', self.caption)
        self.story.append(picture)
        labels = []
        if source_type:
            labels.append(source_type)
        if step_id is not None and step_id != '':
            labels.append(f'Related step: {step_id}')
        if labels:
            self.text(' | '.join(labels), self.small)
        if description:
            self.markdown(description)
        if source:
            self.sources([source])
        self.rendered_images.add(key)
        self.story.append(Spacer(1, 5))
        return True

    def evidence_image(self, item):
        return self.image(str(item.get('image_id') or ''), item.get('caption'), item.get('description'),
                          item.get('source_type'), item.get('step_id'), item.get('source'))

    def blocks(self, blocks, step_id=None):
        for block in blocks or []:
            if not isinstance(block, dict):
                continue
            kind = block.get('type')
            if block.get('title'):
                self.text(block['title'], self.subheading)
            if kind == 'text':
                self.markdown(block.get('content'))
            elif kind == 'table':
                self.table(block.get('columns') or [], block.get('rows') or [])
            elif kind == 'image':
                key = self.image_key(block)
                evidence = next((item for item in self.evidence if str(item.get('image_id')) == key), {})
                self.image(key, block.get('caption') or evidence.get('caption'),
                           block.get('description') or evidence.get('description'),
                           evidence.get('source_type') or 'Approved manual diagram',
                           step_id or evidence.get('step_id'), evidence.get('source') or block.get('source'))
            elif kind == 'link':
                # Preserve the human-facing label/description, without active URLs.
                label = block.get('label') or block.get('content') or block.get('title')
                if label:
                    self.text(label)
                url = str(block.get('url') or '')
                if re.match(r'^https?://[^\s<>]+$', url):
                    self.text('Manual reference: ' + url, self.small)
                if block.get('description'):
                    self.markdown(block['description'])

    def guidance(self, step_id=None, remaining=False):
        guidance = self.report.get('manual_guidance')
        if guidance is not None:
            for index, item in enumerate(guidance):
                if (not isinstance(item, dict) or index in self.rendered_guidance
                        or (not remaining and item.get('step_id') != step_id)):
                    continue
                recorded = next((step for step in self.steps
                    if step.get('step_id') == item.get('step_id') and step.get('blocks') == item.get('blocks')), None)
                if recorded and recorded.get('blocks'):
                    self.rendered_guidance.add(index)
                    continue
                self.text('Manual-supported guidance (recommendations, not confirmed work)', self.subheading)
                self.blocks(item.get('blocks'), step_id)
                self.sources(item.get('sources'))
                self.rendered_guidance.add(index)
        elif step_id is None:
            for message in self.history:
                if message.get('role') == 'user':
                    self.text('Selected technician question / observation', self.subheading)
                    self.markdown(message.get('text'))
                    key = str(message.get('id') or '')
                    if key in self.images:
                        self.image(key, 'Technician upload', source_type='Technician photograph')
                elif message.get('role') == 'assistant':
                    self.text('Selected manual-supported response (guidance, not confirmed work)', self.subheading)
                    response = message.get('response') or {}
                    if response.get('blocks'):
                        self.blocks(response['blocks'])
                    elif message.get('text'):
                        self.markdown(message['text'])
                    self.sources(response.get('sources'))

    def opening(self, maintenance):
        r = self.report
        self.text('MANUFACTURING MAINTENANCE COPILOT', self.small)
        self.text('TECHNICAL SERVICE & MAINTENANCE REPORT' if maintenance else r.get('title') or 'Machine information',
                  self.title_style)
        if maintenance:
            self.text('Industrial Equipment | Field Service | Breakdown & Repair Documentation', self.small)
            if r.get('title') and r['title'].casefold() != 'technical service & maintenance report':
                self.text(r['title'], self.subheading)
        self.kv([
            ('Generated / created (Asia/Calcutta)', _date(r.get('created_at'))),
            ('Last updated (Asia/Calcutta)', _date(r.get('updated_at'))),
            ('Machine / asset ID', r.get('asset_id')),
            ('Issue resolution status' if maintenance else 'Document status', r.get('status')),
            *([('Machine / model', ' / '.join(filter(None, [r.get('machine'), r.get('model')]))),
               ('Reported fault', r.get('issue')), ('Machine operational status', r.get('operational_status'))]
              if maintenance else []),
        ])

    def information(self):
        self.opening(False)
        self.fields([('Machine', 'machine'), ('Model', 'model'), ('Asset ID', 'asset_id')])
        if self.report.get('issue'):
            self.text('Selected question / subject', self.subheading)
            self.markdown(self.report['issue'])
        self.guidance(remaining=True)
        for label, key in [('Editable documentation', 'content'), ('Technician notes', 'notes')]:
            if self.report.get(key):
                self.text(label, self.heading)
                self.markdown(self.report[key])
        for item in self.evidence:
            if str(item.get('image_id')) not in self.rendered_images:
                self.evidence_image(item)

    def render(self):
        self.information()
        output = BytesIO()
        title = _value(self.report.get('title'), 'Technical Service & Maintenance Report')
        document = SimpleDocTemplate(output, pagesize=A4, leftMargin=42, rightMargin=42,
                                     topMargin=77, bottomMargin=53, title=title,
                                     author=_value(self.report.get('technician_name') or self.report.get('technician_email'), ''),
                                     allowSplitting=True, pageCompression=1)
        def canvas(*args, **kwargs):
            return _NumberedCanvas(*args, report_title=title,
                                   fonts=(self.font, self.bold), **kwargs)
        document.build(self.story, canvasmaker=canvas)
        return output.getvalue()


class _CompactMaintenance:
    """Three fixed page frames; overflowing text is visibly abbreviated, never clipped.

    Full records remain in the existing report snapshot. This print projection
    never reads the conversation or turns a recommendation into completed work.
    """

    def __init__(self, report):
        self.report = report
        self.steps = performed_steps(report)
        self.font, self.bold = _fonts()
        self.abbreviated = False
        self.output = BytesIO()
        self.canvas = Canvas(self.output, pagesize=A4, pageCompression=1)
        self.canvas.setTitle('Technical Service & Maintenance Report')
        self.canvas.setAuthor(str(report.get('technician_name') or report.get('technician_email') or ''))
        self.style = ParagraphStyle('compact-body', fontName=self.font, fontSize=9.2,
                                    leading=12, textColor=colors.HexColor('#293642'), splitLongWords=True)
        self.label_style = ParagraphStyle('compact-label', parent=self.style,
                                          fontName=self.bold, fontSize=8.8, leading=12)
        self.y = 0

    def fit(self, value, width, lines=2, label=False):
        text = re.sub(r'\s+', ' ', re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', _value(value))).strip()
        style = self.label_style if label else self.style
        def paragraph(value):
            result = Paragraph(escape(value, quote=True), style)
            _, height = result.wrap(width, A4[1])
            return result, height
        result, height = paragraph(text)
        if height <= lines * style.leading + .1:
            return result, height
        self.abbreviated = True
        low, high = 0, len(text)
        while low < high:
            middle = (low + high + 1) // 2
            _, trial = paragraph(text[:middle].rstrip() + '...')
            if trial <= lines * style.leading + .1:
                low = middle
            else:
                high = middle - 1
        return paragraph(text[:low].rstrip() + '...')

    def page(self, subtitle):
        c = self.canvas
        c.setFillColor(_MUTED)
        c.setFont(self.bold, 8)
        c.drawString(42, 805, 'MANUFACTURING MAINTENANCE COPILOT')
        c.setFillColor(_INK)
        c.setFont(self.bold, 16)
        c.drawString(42, 781, 'TECHNICAL SERVICE & MAINTENANCE REPORT')
        c.setFont(self.font, 9)
        c.drawString(42, 762, subtitle)
        c.setStrokeColor(_BORDER)
        c.line(42, 751, A4[0] - 42, 751)
        self.y = 737

    def section(self, title):
        c = self.canvas
        c.setFillColor(_BLUE)
        c.rect(42, self.y - 22, _WIDTH, 22, stroke=0, fill=1)
        c.setFillColor(_INK)
        c.setFont(self.bold, 10)
        c.drawString(50, self.y - 15, title)
        self.y -= 29

    def make_table(self, headers, rows, widths, limits=2, row_limits=None):
        body, heights = [], []
        if headers:
            body.append([self.fit(value, width - 12, 2, True)[0]
                         for value, width in zip(headers, widths)])
            heights.append(29)
        for row_index, row in enumerate(rows):
            cells, sizes = [], []
            for index, (value, width) in enumerate(zip(row, widths)):
                lines = (row_limits[row_index][index] if row_limits else
                         limits[index] if isinstance(limits, (tuple, list)) else limits)
                cell, height = self.fit(value, width - 12, lines, not headers and index == 0)
                cells.append(cell); sizes.append(height)
            body.append(cells)
            heights.append(max(sizes, default=12) + 10)
        table = Table(body, colWidths=widths, rowHeights=heights, hAlign='LEFT')
        table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), _BLUE if headers else colors.HexColor('#f5f8fb')),
            ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#f7f9fb')]),
            ('GRID', (0, 0), (-1, -1), .45, _BORDER),
            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
            ('LEFTPADDING', (0, 0), (-1, -1), 6),
            ('RIGHTPADDING', (0, 0), (-1, -1), 6),
            ('TOPPADDING', (0, 0), (-1, -1), 5),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
        ]))
        table.wrap(_WIDTH, A4[1])
        return table, sum(heights)

    def draw_table(self, table, height):
        if self.y - height < 65:
            raise ValueError('Compact report layout exceeded its reserved page frame.')
        table.drawOn(self.canvas, 42, self.y - height)
        self.y -= height + 9

    def kv(self, pairs, lines=2):
        row_limits = [(2, limit) for limit in lines] if isinstance(lines, (list, tuple)) else None
        table, height = self.make_table(None, pairs, [_WIDTH * .29, _WIDTH * .71],
                                       (2, lines if row_limits is None else 2), row_limits)
        self.draw_table(table, height)

    def note(self, value, lines=2):
        paragraph, height = self.fit(value, _WIDTH, lines)
        paragraph.drawOn(self.canvas, 42, self.y - height)
        self.y -= height + 8

    def finish(self, number):
        c = self.canvas
        if self.abbreviated:
            c.setFillColor(_MUTED)
            c.setFont(self.font, 7.8)
            c.drawString(42, 53, 'Abbreviated PDF: shortened text/steps; full records remain in the saved report.')
        c.setStrokeColor(_BORDER)
        c.line(42, 41, A4[0] - 42, 41)
        c.setFont(self.font, 8)
        c.setFillColor(_MUTED)
        identifier = str(self.report.get('report_number') or self.report.get('report_id') or _MISSING)
        footer, _ = self.fit(identifier, _WIDTH - 90, 1)
        footer.drawOn(c, 42, 24)
        c.drawRightString(A4[0] - 42, 25, f'Page {number} of 3')
        c.showPage()

    def first_page(self):
        r = self.report
        self.page('Machine, technician and reported issue')
        self.kv([('Report ID', r.get('report_number') or r.get('report_id')),
                 ('Report generated', _date(r.get('updated_at') or r.get('created_at'))),
                 ('Issue status', r.get('status'))], [2, 1, 1])
        self.section('1. Machine Details')
        fields = [('Machine / asset ID', r.get('asset_id')), ('Machine name', r.get('machine')),
                  ('Machine model', r.get('model')), ('Machine type', r.get('machine_type'))]
        if r.get('machine_location'):
            fields.append(('Machine location', r['machine_location']))
        self.kv(fields, [1, 1, 1, 1] + ([2] if r.get('machine_location') else []))
        self.section('2. Technician Details')
        fields = [('Technician name', r.get('technician_name')), ('Technician email', r.get('technician_email'))]
        if r.get('employee_id'):
            fields.append(('Technician / employee ID', r['employee_id']))
        service_date = r.get('service_start') or r.get('repair_date')
        if not service_date:
            confirmed_ids = {step['step_id'] for step in self.steps}
            recorded = r.get('steps') if r.get('steps') is not None else (r.get('workflow') or {}).get('completed_steps', [])
            service_date = next((step.get('completed_at') or step.get('confirmed_at') for step in recorded
                                 if step.get('step_id') in confirmed_ids and
                                 (step.get('completed_at') or step.get('confirmed_at'))), None)
        fields.append(('Service date', _date(service_date)))
        if r.get('fault_started_at') and r.get('fault_duration_days') is not None:
            fields.append(('Issue duration', f'{r["fault_duration_days"]} calendar day(s); downtime is separate'))
        self.kv(fields, [2, 2] + ([1] if r.get('employee_id') else []) + [1] +
                ([1] if r.get('fault_started_at') and r.get('fault_duration_days') is not None else []))
        self.section('3. Reported Issue')
        self.kv([('Issue title', r.get('issue_title') or r.get('issue'))], 2)
        description = r.get('fault_description') or r.get('issue')
        if description != (r.get('issue_title') or r.get('issue')):
            self.kv([('Brief problem description', description)], 3)
        fields = []
        if r.get('machine_state'):
            fields.append(('Initial machine status', r['machine_state']))
        initial = r.get('initial_observation') or r.get('initial_technician_observation')
        if initial:
            fields.append(('Initial observation', initial))
        fields.append(('Reported date and time', _date(r.get('fault_reported_at'))))
        self.kv(fields, ([1] if r.get('machine_state') else []) + ([2] if initial else []) + [1])
        self.finish(1)

    def step_table(self):
        headers = ['Step', 'Action Taken', 'Technician Observation', 'Status']
        widths = [32, 225, 176, _WIDTH - 433]
        def rows(steps):
            return [(step['number'], step['action'], step['observation'], step['status']) for step in steps]
        budget = 385
        if not self.steps:
            self.note('No technician-confirmed performed steps recorded. Agent suggestions are excluded.', 2)
            return
        for lines in ((4, 3, 2) if len(self.steps) <= int((budget - 29) // 22) else ()):
            table, height = self.make_table(headers, rows(self.steps), widths, (1, lines, lines, 2))
            if height <= budget:
                self.draw_table(table, height)
                return
        # Keep a chronological selection, prioritizing failed attempts, and
        # explicitly account for every omitted record. Never change storage.
        size = min(10, len(self.steps))
        while size:
            positions = {0, len(self.steps) - 1}
            positions.update(index for index, step in enumerate(self.steps)
                             if step['status'] == 'Failed' and len(positions) < size)
            for index in range(len(self.steps)):
                if len(positions) >= size:
                    break
                positions.add(index)
            chosen = [self.steps[index] for index in sorted(positions)]
            omitted = len(self.steps) - len(chosen)
            summary = ('Other', f'{omitted} additional confirmed performed steps not individually shown.',
                       'Full chronology, observations and results remain in the saved report.', 'Abbreviated')
            table, height = self.make_table(headers, [*rows(chosen), summary], widths, (1, 2, 2, 2))
            if height <= budget:
                self.abbreviated = True
                self.draw_table(table, height)
                return
            size -= 1
        raise ValueError('Unable to fit the abbreviated troubleshooting summary.')

    def second_page(self):
        self.page('Technician-confirmed work record')
        self.section('4. Troubleshooting & Corrective Actions')
        self.step_table()
        self.section('5. Troubleshooting Summary')
        completed = sum(step['status'] == 'Completed' for step in self.steps)
        failed = sum(step['status'] == 'Failed' for step in self.steps)
        findings = list(dict.fromkeys(step['observation'] for step in self.steps
                                     if step['observation'] != 'Not provided'))
        corrective = self.report.get('action_taken') if self.report.get('action_taken_source') == 'technician_recorded' else None
        if str(corrective or '').strip().casefold() in ('not recorded', 'not confirmed'):
            corrective = None
        fields = [('Performed steps', f'{completed} completed; {failed} attempted unsuccessfully'),
                  ('Key findings', '; '.join(findings[:3]) or 'No technician observations recorded.'),
                  ('Corrective actions recorded', corrective or
                   ('Confirmed actions are listed in Section 4.' if self.steps else 'No confirmed work recorded.'))]
        if len(findings) > 3:
            self.abbreviated = True
        cause = self.report.get('cause')
        if cause and str(cause).casefold() not in ('not confirmed', 'not recorded', 'unknown'):
            fields.append(('Confirmed root cause', cause))
        precautions = list(dict.fromkeys(precaution for step in self.steps for precaution in step.get('precautions', [])))
        if precautions:
            fields.append(('Safety precautions (guidance)', '; '.join(precautions)))
        self.kv(fields, 2)
        self.evidence()
        self.finish(2)

    def evidence(self):
        step_ids = {step['step_id'] for step in self.steps}
        records = [item for item in self.report.get('evidence', [])
                   if item.get('step_id') and item['step_id'] in step_ids]
        room = self.y - 80
        if room < 110:
            return
        shown = 0
        for record in records:
            if shown == 2:
                break
            data = self.report.get('images', {}).get(record.get('image_id'))
            if not isinstance(data, str) or not data.startswith('data:image/png;base64,'):
                continue
            try:
                raw = base64.b64decode(data.split(',', 1)[1], validate=True)
                picture = ImageReader(BytesIO(raw))
                width, height = picture.getSize()
                scale = min(243 / width, min(100, room - 36) / height)
                x = 42 + shown * 267
                self.canvas.drawImage(picture, x, self.y - height * scale,
                                      width=width * scale, height=height * scale, mask='auto')
                source = record.get('source') or {}
                caption = str(record.get('caption') or 'Recorded evidence')
                if isinstance(source, dict) and source.get('document_name'):
                    caption += f' | {source["document_name"]}, PDF page {source.get("page_number", _MISSING)}'
                paragraph, caption_height = self.fit(caption, 243, 2)
                paragraph.drawOn(self.canvas, x, self.y - 104 - caption_height)
                shown += 1
            except (ValueError, TypeError, binascii.Error, OSError, ZeroDivisionError):
                continue

    def third_page(self):
        r = self.report
        self.page('Final status, remarks and signatures')
        self.section('6. Final Maintenance Status')
        fields = [('Reported issue', r.get('issue')), ('Issue resolution status', r.get('status')),
                  ('Machine operational status', r.get('operational_status') or 'Not Recorded'),
                  ('Functional test result', r.get('functional_test') or 'Not tested')]
        cause = r.get('cause')
        if cause and str(cause).casefold() not in ('not confirmed', 'not recorded', 'unknown'):
            fields.append(('Confirmed root cause', cause))
        remaining = r.get('remaining_work') or r.get('remaining_issues')
        if remaining:
            fields.append(('Remaining issue / pending work', remaining))
        if r.get('notes'):
            fields.append(('Final technician remarks', r['notes']))
        fields.append(('Return-to-service authorization', 'Not authorized'))
        self.kv(fields, 2)
        self.section('7. Recommendations')
        recommendations = list(dict.fromkeys(value for value in
            (r.get('preventive_recommendations'), r.get('next_action')) if value))
        if recommendations:
            self.note('Recorded follow-up recommendations, not completed work:', 1)
            self.note('; '.join(recommendations), 4)
        else:
            self.note('No follow-up recommendations recorded.', 1)
        # Signature frame is fixed independently of the final-status text.
        if self.y < 316:
            raise ValueError('Final status exceeded the reserved signature frame.')
        self.y = 300
        self.section('8. Approval & Signatures')
        widths = [155, 134, 145, _WIDTH - 434]
        rows = [('Maintenance Technician', r.get('technician_name') or '', '', ''),
                ('Maintenance Supervisor / Admin', r.get('supervisor_name') or '', '', ''),
                ('Customer Representative', '', '', '')]
        cells = [[self.fit(value, width - 12, 2, True)[0] for value, width in
                  zip(['Role', 'Name', 'Signature', 'Date'], widths)]]
        cells.extend([[self.fit(value, width - 12, 2)[0] if value else '' for value, width in zip(row, widths)] for row in rows])
        table = Table(cells, colWidths=widths, rowHeights=[29, 42, 42, 42])
        table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), _BLUE), ('GRID', (0, 0), (-1, -1), .5, _BORDER),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'), ('LEFTPADDING', (0, 0), (-1, -1), 6),
        ]))
        table.wrap(_WIDTH, 160)
        self.draw_table(table, 155)
        self.note('Report Prepared By: ' + str(r.get('technician_name') or r.get('technician_email') or _MISSING), 1)
        self.note('Report Reviewed By: ' + str(r.get('reviewed_by_name') or 'Pending'), 1)
        self.note('Approval Status: Pending. Signatures are to be completed by the responsible people.', 1)
        self.finish(3)

    def render(self):
        self.first_page()
        self.second_page()
        self.third_page()
        self.canvas.save()
        return self.output.getvalue()


def render_maintenance_pdf(report) -> bytes:
    """Keep information exports intact; maintenance service reports are three pages."""
    if report.get('workflow') is not None or report.get('case_id') or report.get('steps'):
        return _CompactMaintenance(report).render()
    return _Renderer(report).render()
