"""Render a CV dict (config/resume.json shape) to a one-page A4 PDF that matches the original CV's layout.

Layout measured from the original CV PDF: Carlito (metric-compatible with Calibri), blue top band,
30 pt navy name, blue headline, grey contact line, navy section headings with a light rule, blue bullets.
Needs the `reportlab` package (installed by run_jobradar.bat).
"""
from __future__ import annotations

import io
from pathlib import Path
from xml.sax.saxutils import escape

NAVY, BLUE, GREY, RULE = "#14284B", "#2F6DB5", "#555555", "#C9D3E0"
FONT_DIR = Path(__file__).resolve().parent / "fonts"
_FONTS: tuple[str, str] | None = None


def _fonts() -> tuple[str, str]:
    global _FONTS
    if _FONTS:
        return _FONTS
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    for reg, bold in ((FONT_DIR / "Carlito-Regular.ttf", FONT_DIR / "Carlito-Bold.ttf"),
                      (Path("C:/Windows/Fonts/calibri.ttf"), Path("C:/Windows/Fonts/calibrib.ttf"))):
        if reg.exists() and bold.exists():
            pdfmetrics.registerFont(TTFont("CV-Regular", str(reg)))
            pdfmetrics.registerFont(TTFont("CV-Bold", str(bold)))
            _FONTS = ("CV-Regular", "CV-Bold")
            return _FONTS
    _FONTS = ("Helvetica", "Helvetica-Bold")
    return _FONTS


def _story(cv: dict, k: float):
    from reportlab.lib.colors import HexColor
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.platypus import HRFlowable, KeepTogether, Paragraph, Spacer
    reg, bold = _fonts()

    def st(name, font=reg, size=10.5, lead=13.8, color="#000000", **kw):
        return ParagraphStyle(name, fontName=font, fontSize=size * k, leading=lead * k, textColor=HexColor(color), **kw)

    s_name = st("name", bold, 30, 34, NAVY)
    s_head = st("head", bold, 13.5, 16, BLUE, spaceBefore=1.7 * k)
    s_contact = st("contact", reg, 10.5, 14, GREY, spaceBefore=3 * k)
    s_avail = st("avail", bold, 10.5, 14, BLUE)
    s_sec = st("sec", bold, 12, 14, NAVY, spaceBefore=9 * k)
    s_body = st("body")
    s_skill = st("skill", spaceAfter=2.5 * k)
    s_job = st("job", bold, 11.8, 14, "#000000", spaceBefore=6.2 * k)
    s_date = st("date", reg, 10.3, 13, GREY, spaceAfter=2.5 * k)
    s_bul = st("bul", reg, 10.5, 13.4, leftIndent=13 * k, bulletIndent=0, spaceBefore=1.5 * k,
               bulletFontName="Helvetica", bulletFontSize=10 * k, bulletColor=HexColor(BLUE))
    s_edu = st("edu", spaceAfter=3 * k)
    e = escape

    def section(title):
        return [Paragraph(e(title), s_sec), HRFlowable(width="100%", thickness=0.8, color=HexColor(RULE),
                                                       spaceBefore=1.5 * k, spaceAfter=5 * k)]

    out = [Paragraph(e(cv["name"]), s_name), Paragraph(e(cv["headline"]), s_head),
           Paragraph("&nbsp;&nbsp;|&nbsp;&nbsp;".join(e(c) for c in cv["contact"]), s_contact)]
    if cv.get("availability"):
        out.append(Paragraph(e(cv["availability"]), s_avail))
    summ = cv["summary"]
    exp = summ["expert_in"]
    expert = ", ".join(exp[:-1]) + " and " + exp[-1] if len(exp) > 1 else (exp[0] if exp else "")
    out += section("Summary") + [Paragraph(e(summ["lead"] + (f" Expert in {expert}." if expert else "")), s_body)]
    out += section("Skills") + [Paragraph(f'<font name="{bold}">{e(s["label"])}:</font> {e(", ".join(s["items"]))}', s_skill)
                                for s in cv["skills"]]
    out += section("Experience")
    for job in cv["experience"]:
        head = [Paragraph(f'{e(job["title"])} | {e(job["company"])}', s_job),
                Paragraph(f'{e(job["dates"])} | {e(job["location"])}', s_date)]
        bullets = [Paragraph(e(b), s_bul, bulletText="•") for b in job["bullets"]]
        out += [KeepTogether(head + bullets[:1])] + bullets[1:]
    out += section("Education") + [Paragraph(f'<font name="{bold}">{e(d["degree"])}</font>, {e(d["rest"])}', s_edu)
                                   for d in cv["education"]]
    out += section("Certifications") + [Paragraph(e(c), s_body) for c in cv["certifications"]]
    if cv.get("languages"):
        out += section("Languages") + [Paragraph(e(cv["languages"]), s_body)]
    out.append(Spacer(1, 1))
    return out


def render_pdf(cv: dict, subject: str = "") -> bytes:
    """One A4 page; shrinks type slightly (down to 88 %) if added keywords would spill onto a second page."""
    from reportlab.lib.colors import HexColor
    from reportlab.lib.pagesizes import A4
    from reportlab.platypus import SimpleDocTemplate

    def band(canvas, _doc):
        canvas.saveState()
        canvas.setFillColor(HexColor(BLUE))
        canvas.rect(0, A4[1] - 14.2, A4[0], 14.2, stroke=0, fill=1)
        canvas.restoreState()

    pdf = b""
    for k in (1.0, 0.97, 0.94, 0.91, 0.88):
        buf = io.BytesIO()
        doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=39.7, rightMargin=39.7, topMargin=39.7 * k, bottomMargin=24,  # frame adds 6 pt padding
                                title=f'{cv["name"]} - {cv["headline"]}', author=cv["name"], subject=subject or cv["headline"])
        doc.build(_story(cv, k), onFirstPage=band, onLaterPages=band)
        pdf = buf.getvalue()
        if doc.page == 1:
            break
    return pdf
