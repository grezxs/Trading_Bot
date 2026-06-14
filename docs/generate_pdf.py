#!/usr/bin/env python3
"""
Generate a CEO-grade PDF from the trading-bot system report.

Usage:
    python docs/generate_pdf.py
Output:
    docs/system_report.pdf
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.colors import HexColor, white, black
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import cm, mm
from reportlab.pdfgen import canvas as pdf_canvas
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    HRFlowable,
    KeepTogether,
    ListFlowable,
    ListItem,
    NextPageTemplate,
    PageBreak,
    PageTemplate,
    Paragraph,
    Preformatted,
    Spacer,
    Table,
    TableStyle,
)
from reportlab.platypus.flowables import Flowable

# ── Palette ──────────────────────────────────────────────────────────────────
NAVY    = HexColor('#1B2A4A')
NAVY_LT = HexColor('#243559')
GOLD    = HexColor('#C5952A')
GOLD_LT = HexColor('#E8B84B')
LGRAY   = HexColor('#F5F6FA')
MGRAY   = HexColor('#DDE0EC')
DGRAY   = HexColor('#3C4260')
CBKG    = HexColor('#ECEEF5')   # code-block background
CBRD    = HexColor('#CED3E8')   # code-block border
WHITE   = white
BLACK   = black

# ── Typography ────────────────────────────────────────────────────────────────
BODY  = 'Helvetica'
BOLD  = 'Helvetica-Bold'
ITLC  = 'Helvetica-Oblique'
CODE  = 'Courier'
CBLD  = 'Courier-Bold'

# ── Page geometry ─────────────────────────────────────────────────────────────
PW, PH = A4
ML = MR = 2.5 * cm
MT = 3.2 * cm   # space for running header
MB = 2.4 * cm   # space for footer
BODY_W = PW - ML - MR

# ── Styles ────────────────────────────────────────────────────────────────────
def _styles():
    base = ParagraphStyle('base', fontName=BODY, fontSize=10, leading=15,
                          textColor=DGRAY, spaceAfter=6)
    return {
        'body': base,
        'body_j': ParagraphStyle('body_j', parent=base, alignment=TA_JUSTIFY),
        'h1': ParagraphStyle('h1', fontName=BOLD, fontSize=22, leading=28,
                             textColor=NAVY, spaceBefore=18, spaceAfter=4),
        'h2': ParagraphStyle('h2', fontName=BOLD, fontSize=15, leading=20,
                             textColor=NAVY, spaceBefore=22, spaceAfter=4),
        'h3': ParagraphStyle('h3', fontName=BOLD, fontSize=12, leading=16,
                             textColor=NAVY, spaceBefore=14, spaceAfter=4),
        'h4': ParagraphStyle('h4', fontName=BOLD, fontSize=10.5, leading=14,
                             textColor=DGRAY, spaceBefore=10, spaceAfter=2),
        'code': ParagraphStyle('code', fontName=CODE, fontSize=7.8, leading=11,
                               backColor=CBKG, textColor=HexColor('#2A2F50'),
                               leftIndent=10, rightIndent=10,
                               spaceBefore=8, spaceAfter=8,
                               borderPad=8),
        'caption': ParagraphStyle('caption', fontName=ITLC, fontSize=8.5,
                                  textColor=HexColor('#6A6E85'),
                                  alignment=TA_CENTER, spaceAfter=10),
        'toc_h': ParagraphStyle('toc_h', fontName=BOLD, fontSize=10,
                                textColor=NAVY, spaceBefore=4),
        'toc_s': ParagraphStyle('toc_s', fontName=BODY, fontSize=9,
                                textColor=DGRAY, leftIndent=14),
        'meta':  ParagraphStyle('meta', fontName=BODY, fontSize=9.5,
                                textColor=HexColor('#5A5F7A'), leading=14),
        'kv_key': ParagraphStyle('kv_key', fontName=BOLD, fontSize=9.5,
                                 textColor=NAVY),
        'kv_val': ParagraphStyle('kv_val', fontName=BODY, fontSize=9.5,
                                 textColor=DGRAY),
        'table_hdr': ParagraphStyle('table_hdr', fontName=BOLD, fontSize=9,
                                    textColor=WHITE, leading=12),
        'table_cell': ParagraphStyle('table_cell', fontName=BODY, fontSize=8.8,
                                     textColor=DGRAY, leading=12),
        'bullet': ParagraphStyle('bullet', fontName=BODY, fontSize=10,
                                 textColor=DGRAY, leading=15,
                                 leftIndent=16, bulletIndent=4),
        'conf': ParagraphStyle('conf', fontName=BOLD, fontSize=8,
                               textColor=WHITE, alignment=TA_CENTER),
        'note': ParagraphStyle('note', fontName=ITLC, fontSize=9,
                               textColor=HexColor('#6A6E85'), spaceAfter=4),
    }


# ── Canvas with header / footer / total-page count ───────────────────────────
class _NumberedCanvas(pdf_canvas.Canvas):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._saved = []

    def showPage(self):
        self._saved.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        n = len(self._saved)
        for state in self._saved:
            self.__dict__.update(state)
            self._draw_chrome(n)
            pdf_canvas.Canvas.showPage(self)
        pdf_canvas.Canvas.save(self)

    def _draw_chrome(self, total: int):
        pg = self._pageNumber
        if pg == 1:         # cover — no chrome
            return
        self.saveState()

        # ── header bar ──
        self.setFillColor(NAVY)
        self.rect(ML, PH - 1.9 * cm, PW - ML - MR, 1.5, fill=1, stroke=0)
        self.setFont(BOLD, 8)
        self.setFillColor(WHITE)
        self.drawString(ML, PH - 1.7 * cm,
                        'CRYPTO TRADING BOT  ·  SYSTEM DESIGN REPORT')
        self.setFont(BODY, 8)
        self.setFillColor(GOLD)
        self.drawRightString(PW - MR, PH - 1.7 * cm, 'CONFIDENTIAL')

        # ── footer ──
        self.setStrokeColor(MGRAY)
        self.setLineWidth(0.4)
        self.line(ML, 1.9 * cm, PW - MR, 1.9 * cm)
        self.setFont(BODY, 8)
        self.setFillColor(HexColor('#6A6E85'))
        self.drawString(ML, 1.35 * cm,
                        'Algorithmic Trading Systems  ·  June 2026')
        self.drawRightString(PW - MR, 1.35 * cm,
                             f'Page {pg} of {total}')
        # centre dot
        self.drawCentredString(PW / 2, 1.35 * cm, '◆')

        self.restoreState()


# ── Cover-page canvas callback ────────────────────────────────────────────────
def _draw_cover(canv: pdf_canvas.Canvas, _doc):
    W, H = PW, PH

    # ── navy top band (55% of page) ──
    band_h = H * 0.55
    canv.setFillColor(NAVY)
    canv.rect(0, H - band_h, W, band_h, fill=1, stroke=0)

    # subtle diagonal accent stripe inside the band
    canv.saveState()
    canv.setFillColor(NAVY_LT)
    canv.setStrokeColor(NAVY_LT)
    canv.setLineWidth(0)
    path = canv.beginPath()
    path.moveTo(W * 0.62, H)
    path.lineTo(W, H * 0.72)
    path.lineTo(W, H)
    path.close()
    canv.drawPath(path, fill=1, stroke=0)
    canv.restoreState()

    # ── top label ──
    canv.setFont(BOLD, 9)
    canv.setFillColor(GOLD)
    canv.drawCentredString(W / 2, H - 1.8 * cm,
                           'A L G O R I T H M I C   T R A D I N G   S Y S T E M S')

    # ── main title ──
    canv.setFont(BOLD, 38)
    canv.setFillColor(WHITE)
    canv.drawCentredString(W / 2, H - 5.8 * cm, 'CRYPTO')
    canv.drawCentredString(W / 2, H - 7.4 * cm, 'TRADING BOT')

    # ── gold rule ──
    canv.setStrokeColor(GOLD)
    canv.setLineWidth(1.5)
    canv.line(ML + 2 * cm, H - 8.5 * cm, W - MR - 2 * cm, H - 8.5 * cm)

    # ── subtitle ──
    canv.setFont(BODY, 16)
    canv.setFillColor(GOLD_LT)
    canv.drawCentredString(W / 2, H - 9.5 * cm, 'System Design Report')

    # ── asset line ──
    canv.setFont(BODY, 11)
    canv.setFillColor(HexColor('#B0BAD8'))
    canv.drawCentredString(W / 2, H - 10.8 * cm,
                           'BTC/USDT  ·  ETH/USDT  ·  Binance Paper Trading')

    # ── metadata card (white section) ──
    card_y = H - band_h - 0.5 * cm
    card_h = 7.2 * cm
    card_x = ML + 0.5 * cm
    card_w = W - card_x - MR - 0.5 * cm

    canv.setFillColor(LGRAY)
    canv.roundRect(card_x, card_y - card_h, card_w, card_h, 6, fill=1, stroke=0)
    canv.setStrokeColor(MGRAY)
    canv.setLineWidth(0.5)
    canv.roundRect(card_x, card_y - card_h, card_w, card_h, 6, fill=0, stroke=1)

    rows = [
        ('Prepared for',   'Executive Leadership / CEO Review'),
        ('Classification', 'CONFIDENTIAL — Internal Use Only'),
        ('Platform',       'Binance Testnet & Demo Trading (Paper Orders Only)'),
        ('Instruments',    'BTC/USDT  ·  ETH/USDT  (Spot)'),
        ('Development Team',
         'Cheng  ·  Gilbert  ·  Grace  ·  ShiYi  ·  sookoon'),
        ('Date',           'June 14, 2026'),
    ]
    row_h = (card_h - 1.0 * cm) / len(rows)
    for i, (key, val) in enumerate(rows):
        y_row = card_y - 0.6 * cm - i * row_h
        canv.setFont(BOLD, 8.5)
        canv.setFillColor(NAVY)
        canv.drawString(card_x + 0.6 * cm, y_row, key.upper())
        canv.setFont(BODY, 8.5)
        canv.setFillColor(DGRAY)
        canv.drawString(card_x + 0.6 * cm + 4.8 * cm, y_row, val)
        if i < len(rows) - 1:
            canv.setStrokeColor(MGRAY)
            canv.setLineWidth(0.3)
            canv.line(card_x + 0.3 * cm, y_row - 0.35 * cm,
                      card_x + card_w - 0.3 * cm, y_row - 0.35 * cm)

    # ── bottom gold bar ──
    canv.setFillColor(GOLD)
    canv.rect(0, 0, W, 0.7 * cm, fill=1, stroke=0)
    canv.setFont(BOLD, 7.5)
    canv.setFillColor(NAVY)
    canv.drawCentredString(W / 2, 0.22 * cm,
                           'CONFIDENTIAL  ·  DO NOT DISTRIBUTE WITHOUT AUTHORISATION')


# ── Table-of-contents builder ─────────────────────────────────────────────────
_TOC_DATA = [
    ('1',  'Executive Summary', ''),
    ('2',  'System Architecture', ''),
    ('3',  'Data Layer', ''),
    ('4',  'Event Engine', ''),
    ('5',  'Regime Detector', ''),
    ('6',  'Strategy Layer', ''),
    ('7',  'Risk Manager', ''),
    ('8',  'Order Management System', ''),
    ('9',  'Portfolio', ''),
    ('10', 'Live Dashboard', ''),
    ('11', 'Backtester', ''),
    ('12', 'Key Design Decisions', ''),
    ('13', 'Current Status and Roadmap', ''),
]


def _build_toc(S: dict) -> list:
    items: list = []
    items.append(Paragraph('Table of Contents', S['h1']))
    items.append(HRFlowable(width=BODY_W, thickness=1.2,
                            color=GOLD, spaceAfter=16))
    for num, title, _ in _TOC_DATA:
        label = f'{num}. &nbsp; {title}'
        items.append(Paragraph(label, S['toc_h']))
    items.append(Spacer(1, 0.8 * cm))
    items.append(PageBreak())
    return items


# ── Markdown parser → platypus flowables ──────────────────────────────────────
def _escape(text: str) -> str:
    """Escape special XML chars for Paragraph, then restore inline tags."""
    text = text.replace('&', '&amp;')
    text = text.replace('<', '&lt;')
    text = text.replace('>', '&gt;')
    return text


def _inline(text: str) -> str:
    """Convert **bold**, `code`, *italic* inline marks to reportlab XML tags."""
    text = _escape(text)
    # **bold**
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
    # *italic*
    text = re.sub(r'\*(.+?)\*', r'<i>\1</i>', text)
    # `code`
    text = re.sub(r'`([^`]+)`',
                  r'<font name="Courier" color="#2A2F50">\1</font>', text)
    return text


def _parse_table(lines: list[str], S: dict) -> Table | None:
    """Convert GFM table lines to a styled reportlab Table."""
    rows = []
    for line in lines:
        line = line.strip()
        if not line or set(line.replace('|', '').replace('-', '').replace(':', '').replace(' ', '')) == set():
            continue
        cells = [c.strip() for c in line.strip('|').split('|')]
        rows.append(cells)

    if len(rows) < 2:
        return None

    header = rows[0]
    data_rows = rows[1:]

    col_n = len(header)
    col_w = BODY_W / col_n

    def cell(txt, style):
        return Paragraph(_inline(txt), style)

    table_data = [[cell(h, S['table_hdr']) for h in header]]
    for i, row in enumerate(data_rows):
        while len(row) < col_n:
            row.append('')
        bg = LGRAY if i % 2 == 0 else WHITE
        table_data.append([cell(c, S['table_cell']) for c in row[:col_n]])

    col_widths = [col_w] * col_n
    # Give the last column more room if there are only 2 columns
    if col_n == 2:
        col_widths = [BODY_W * 0.32, BODY_W * 0.68]

    tbl = Table(table_data, colWidths=col_widths, repeatRows=1)
    style = TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), NAVY),
        ('TEXTCOLOR',  (0, 0), (-1, 0), WHITE),
        ('FONTNAME',   (0, 0), (-1, 0), BOLD),
        ('FONTSIZE',   (0, 0), (-1, 0), 9),
        ('BOTTOMPADDING', (0, 0), (-1, 0), 8),
        ('TOPPADDING',    (0, 0), (-1, 0), 8),
        ('LEFTPADDING',   (0, 0), (-1, -1), 8),
        ('RIGHTPADDING',  (0, 0), (-1, -1), 8),
        ('FONTSIZE',   (0, 1), (-1, -1), 8.8),
        ('TOPPADDING', (0, 1), (-1, -1), 6),
        ('BOTTOMPADDING', (0, 1), (-1, -1), 6),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [LGRAY, WHITE]),
        ('GRID', (0, 0), (-1, -1), 0.4, MGRAY),
        ('LINEBELOW', (0, 0), (-1, 0), 1.5, GOLD),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
    ])
    tbl.setStyle(style)
    return tbl


def _code_box(code_text: str) -> Table:
    """Wrap a Preformatted code block in a styled box."""
    pre = Preformatted(code_text, ParagraphStyle(
        'pre', fontName=CODE, fontSize=7.5, leading=10.5,
        textColor=HexColor('#2A2F50'),
    ))
    tbl = Table([[pre]], colWidths=[BODY_W])
    tbl.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), CBKG),
        ('BOX',        (0, 0), (-1, -1), 0.8, CBRD),
        ('LEFTPADDING',  (0, 0), (-1, -1), 12),
        ('RIGHTPADDING', (0, 0), (-1, -1), 12),
        ('TOPPADDING',   (0, 0), (-1, -1), 10),
        ('BOTTOMPADDING',(0, 0), (-1, -1), 10),
    ]))
    return tbl


def _section_rule(S: dict) -> list:
    return [
        Spacer(1, 4),
        HRFlowable(width=BODY_W, thickness=1.0, color=GOLD, spaceAfter=10),
    ]


class _GoldBar(Flowable):
    """Left-edge gold accent bar beside a section heading."""
    def __init__(self, height=22):
        super().__init__()
        self._height = height
        self.width  = 4
        self.height = height

    def draw(self):
        self.canv.setFillColor(GOLD)
        self.canv.rect(0, 0, 4, self._height, fill=1, stroke=0)


def md_to_flowables(md_text: str, S: dict) -> list:
    """Parse markdown source into a list of reportlab flowables."""
    flowables: list = []
    lines = md_text.splitlines()
    i = 0
    n = len(lines)

    # skip YAML front matter if any
    if lines and lines[0].strip() == '---':
        i = 1
        while i < n and lines[i].strip() != '---':
            i += 1
        i += 1

    bullet_buf: list[str] = []
    table_buf:  list[str] = []
    in_code  = False
    code_buf: list[str] = []

    def flush_bullets():
        nonlocal bullet_buf
        if not bullet_buf:
            return
        items = [ListItem(Paragraph(_inline(b), S['bullet']), leftIndent=20,
                          bulletColor=GOLD, bulletType='bullet')
                 for b in bullet_buf]
        flowables.append(ListFlowable(items, bulletType='bullet',
                                      leftIndent=10, bulletFontSize=8))
        flowables.append(Spacer(1, 4))
        bullet_buf = []

    def flush_table():
        nonlocal table_buf
        if not table_buf:
            return
        tbl = _parse_table(table_buf, S)
        if tbl:
            flowables.append(Spacer(1, 6))
            flowables.append(tbl)
            flowables.append(Spacer(1, 10))
        table_buf = []

    while i < n:
        raw = lines[i]
        stripped = raw.strip()

        # ── code fence ──────────────────────────────────────────────────────
        if stripped.startswith('```'):
            flush_bullets()
            flush_table()
            if not in_code:
                in_code = True
                code_buf = []
            else:
                in_code = False
                flowables.append(Spacer(1, 6))
                flowables.append(_code_box('\n'.join(code_buf)))
                flowables.append(Spacer(1, 6))
                code_buf = []
            i += 1
            continue

        if in_code:
            code_buf.append(raw)
            i += 1
            continue

        # ── horizontal rule ─────────────────────────────────────────────────
        if re.match(r'^-{3,}$', stripped) or re.match(r'^\*{3,}$', stripped):
            flush_bullets()
            flush_table()
            i += 1
            continue

        # ── headings ─────────────────────────────────────────────────────────
        m = re.match(r'^(#{1,4})\s+(.*)', stripped)
        if m:
            flush_bullets()
            flush_table()
            level = len(m.group(1))
            text  = _inline(m.group(2))

            if level == 1:
                # Document title — only for the section right after the cover;
                # we skip the very first # heading (it becomes the cover).
                flowables.append(Paragraph(text, S['h1']))
                flowables.extend(_section_rule(S))
            elif level == 2:
                # numbered section — add page-break before section 2+
                if text and not text.startswith('1.') and text != 'Table of Contents':
                    # minor breathing room instead of hard break for sub-sections
                    pass
                flowables.append(Spacer(1, 8))
                flowables.append(Paragraph(text, S['h2']))
                flowables.append(
                    HRFlowable(width=BODY_W, thickness=1.5,
                               color=GOLD, spaceAfter=8))
            elif level == 3:
                flowables.append(Spacer(1, 6))
                flowables.append(Paragraph(text, S['h3']))
                flowables.append(
                    HRFlowable(width=BODY_W * 0.25, thickness=0.8,
                               color=GOLD, spaceAfter=4))
            else:
                flowables.append(Paragraph(text, S['h4']))
            i += 1
            continue

        # ── table row ────────────────────────────────────────────────────────
        if stripped.startswith('|'):
            flush_bullets()
            table_buf.append(stripped)
            i += 1
            continue
        else:
            flush_table()

        # ── bullet / list item ────────────────────────────────────────────────
        m_bullet = re.match(r'^[-*]\s+(.*)', stripped)
        if m_bullet:
            bullet_buf.append(m_bullet.group(1))
            i += 1
            continue
        else:
            flush_bullets()

        # ── blank line ────────────────────────────────────────────────────────
        if not stripped:
            i += 1
            continue

        # ── normal paragraph ─────────────────────────────────────────────────
        para_lines = [stripped]
        i += 1
        while i < n:
            nxt = lines[i].strip()
            if (not nxt or nxt.startswith('#') or nxt.startswith('|')
                    or nxt.startswith('```') or nxt.startswith('-')
                    or nxt.startswith('*')):
                break
            para_lines.append(nxt)
            i += 1
        text = ' '.join(para_lines)
        if text:
            flowables.append(Paragraph(_inline(text), S['body_j']))

    flush_bullets()
    flush_table()
    return flowables


# ── Document assembly ─────────────────────────────────────────────────────────
def build(src: Path, out: Path) -> None:
    md_text = src.read_text(encoding='utf-8')
    S = _styles()

    doc = BaseDocTemplate(
        str(out),
        pagesize=A4,
        leftMargin=ML, rightMargin=MR,
        topMargin=MT,  bottomMargin=MB,
        title='Crypto Trading Bot — System Design Report',
        author='Algorithmic Trading Systems Team',
        subject='System Architecture & Design',
    )

    # page templates
    cover_frame = Frame(0, 0, PW, PH, leftPadding=0, rightPadding=0,
                        topPadding=0, bottomPadding=0)
    body_frame  = Frame(ML, MB, BODY_W, PH - MT - MB)

    doc.addPageTemplates([
        PageTemplate('Cover', frames=[cover_frame],
                     onPage=_draw_cover),
        PageTemplate('Body',  frames=[body_frame],
                     onPage=lambda c, d: None),  # chrome added by _NumberedCanvas
    ])

    # ── build story ──────────────────────────────────────────────────────────
    story: list = []

    # 1. Cover page
    story.append(NextPageTemplate('Body'))
    story.append(PageBreak())

    # 2. TOC
    story.extend(_build_toc(S))

    # 3. Body content
    body_flowables = md_to_flowables(md_text, S)

    # Insert a page break before each major section (## N.)
    enhanced: list = []
    skip_first_break = True
    for item in body_flowables:
        if isinstance(item, Paragraph) and item.style.name == 'h2':
            if skip_first_break:
                skip_first_break = False
            else:
                enhanced.append(PageBreak())
        enhanced.append(item)
    story.extend(enhanced)

    doc.build(story, canvasmaker=_NumberedCanvas)
    print(f'PDF written → {out}')


if __name__ == '__main__':
    here  = Path(__file__).resolve().parent
    src   = here / 'system_report.md'
    out   = here / 'system_report.pdf'
    if not src.exists():
        print(f'ERROR: source not found: {src}', file=sys.stderr)
        sys.exit(1)
    build(src, out)
