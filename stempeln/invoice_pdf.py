"""Rechnungs-PDF (DIN-5008-nahes Layout) als PDF/A-3b mit eingebetteter ZUGFeRD/Factur-X-XML."""

from datetime import datetime, timezone
from html import escape
from pathlib import Path

import qrcode
from fpdf import FPDF
from fpdf.enums import OutputIntentSubType, XPos, YPos
from fpdf.output import PDFICCProfile
from PIL import ImageCms

from . import billing, i18n
from .einvoice import build_cii

FONT_DIR = Path(__file__).parent / "pdf_fonts"
INK = (28, 31, 38)
MUTED = (110, 116, 128)
LINE = (220, 224, 232)
LEFT, RIGHT = 20, 190
WIDTH = RIGHT - LEFT
COUNTRY_NAMES = {
    "de": {"DE": "Deutschland", "AT": "Österreich", "CH": "Schweiz", "NL": "Niederlande", "FR": "Frankreich", "IT": "Italien",
           "BE": "Belgien", "LU": "Luxemburg", "DK": "Dänemark", "PL": "Polen", "CZ": "Tschechien", "ES": "Spanien",
           "GB": "Vereinigtes Königreich", "US": "USA"},
    "en": {"DE": "Germany", "AT": "Austria", "CH": "Switzerland", "NL": "Netherlands", "FR": "France", "IT": "Italy",
           "BE": "Belgium", "LU": "Luxembourg", "DK": "Denmark", "PL": "Poland", "CZ": "Czechia", "ES": "Spain",
           "GB": "United Kingdom", "US": "USA"},
}

TYPE_TITLES = {"380": i18n.N_("Rechnung"), "381": i18n.N_("Stornorechnung")}


def _srgb_profile():
    return ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()


def _xmp(title, author, created, conformance="EN 16931"):
    stamp = created.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"""<x:xmpmeta xmlns:x="adobe:ns:meta/">
  <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
    <rdf:Description rdf:about="" xmlns:pdfaid="http://www.aiim.org/pdfa/ns/id/">
      <pdfaid:part>3</pdfaid:part>
      <pdfaid:conformance>B</pdfaid:conformance>
    </rdf:Description>
    <rdf:Description rdf:about="" xmlns:dc="http://purl.org/dc/elements/1.1/">
      <dc:title><rdf:Alt><rdf:li xml:lang="x-default">{escape(title)}</rdf:li></rdf:Alt></dc:title>
      <dc:creator><rdf:Seq><rdf:li>{escape(author)}</rdf:li></rdf:Seq></dc:creator>
    </rdf:Description>
    <rdf:Description rdf:about="" xmlns:xmp="http://ns.adobe.com/xap/1.0/">
      <xmp:CreatorTool>Stempeln</xmp:CreatorTool>
      <xmp:CreateDate>{stamp}</xmp:CreateDate>
      <xmp:ModifyDate>{stamp}</xmp:ModifyDate>
      <xmp:MetadataDate>{stamp}</xmp:MetadataDate>
    </rdf:Description>
    <rdf:Description rdf:about="" xmlns:pdf="http://ns.adobe.com/pdf/1.3/">
      <pdf:Producer>Stempeln (fpdf2)</pdf:Producer>
    </rdf:Description>
    <rdf:Description rdf:about="" xmlns:fx="urn:factur-x:pdfa:CrossIndustryDocument:invoice:1p0#">
      <fx:DocumentType>INVOICE</fx:DocumentType>
      <fx:DocumentFileName>factur-x.xml</fx:DocumentFileName>
      <fx:Version>1.0</fx:Version>
      <fx:ConformanceLevel>{conformance}</fx:ConformanceLevel>
    </rdf:Description>
    <rdf:Description rdf:about="" xmlns:pdfaExtension="http://www.aiim.org/pdfa/ns/extension/"
        xmlns:pdfaSchema="http://www.aiim.org/pdfa/ns/schema#" xmlns:pdfaProperty="http://www.aiim.org/pdfa/ns/property#">
      <pdfaExtension:schemas><rdf:Bag><rdf:li rdf:parseType="Resource">
        <pdfaSchema:schema>Factur-X PDFA Extension Schema</pdfaSchema:schema>
        <pdfaSchema:namespaceURI>urn:factur-x:pdfa:CrossIndustryDocument:invoice:1p0#</pdfaSchema:namespaceURI>
        <pdfaSchema:prefix>fx</pdfaSchema:prefix>
        <pdfaSchema:property><rdf:Seq>
          <rdf:li rdf:parseType="Resource"><pdfaProperty:name>DocumentFileName</pdfaProperty:name><pdfaProperty:valueType>Text</pdfaProperty:valueType><pdfaProperty:category>external</pdfaProperty:category><pdfaProperty:description>Name of the embedded XML invoice file</pdfaProperty:description></rdf:li>
          <rdf:li rdf:parseType="Resource"><pdfaProperty:name>DocumentType</pdfaProperty:name><pdfaProperty:valueType>Text</pdfaProperty:valueType><pdfaProperty:category>external</pdfaProperty:category><pdfaProperty:description>INVOICE</pdfaProperty:description></rdf:li>
          <rdf:li rdf:parseType="Resource"><pdfaProperty:name>Version</pdfaProperty:name><pdfaProperty:valueType>Text</pdfaProperty:valueType><pdfaProperty:category>external</pdfaProperty:category><pdfaProperty:description>Version of the Factur-X XML schema</pdfaProperty:description></rdf:li>
          <rdf:li rdf:parseType="Resource"><pdfaProperty:name>ConformanceLevel</pdfaProperty:name><pdfaProperty:valueType>Text</pdfaProperty:valueType><pdfaProperty:category>external</pdfaProperty:category><pdfaProperty:description>Conformance level of the embedded XML</pdfaProperty:description></rdf:li>
        </rdf:Seq></pdfaSchema:property>
      </rdf:li></rdf:Bag></pdfaExtension:schemas>
    </rdf:Description>
  </rdf:RDF>
</x:xmpmeta>"""


class InvoicePDF(FPDF):
    def __init__(self, doc, draft):
        super().__init__(format="A4", unit="mm", enforce_compliance="PDF/A-3B")
        self.doc, self.draft = doc, draft
        self.language = doc.get("language") or "de"
        self.layout = billing.layout_values(doc.get("layout"))
        self.accent = billing.hex_to_rgb(self.layout["accent"])
        family = self.layout["font"] if self.layout["font"] in billing.PDF_FONTS else "dmsans"
        for style, weight in (("", "400"), ("B", "700")):
            if family == "dmsans":
                self.add_font("Body", style, FONT_DIR / f"dmsans-latin-{weight}.ttf")
            else:
                self.add_font("Body", style, FONT_DIR / f"{family}-{weight}.ttf")
            self.add_font("DMX", style, FONT_DIR / f"dmsans-latin-ext-{weight}.ttf")
        self.set_fallback_fonts(["DMX"])
        self.set_margins(LEFT, 18, 210 - RIGHT)
        self.set_auto_page_break(True, margin=34 if self.layout["footer"] else 22)
        self.c_margin = 1

    def multi_cell(self, *args, **kwargs):
        kwargs.setdefault("align", "L")
        return super().multi_cell(*args, **kwargs)

    def t(self, text, **values):
        return i18n.gettext(text, self.language, **values)

    def font(self, size, bold=False, color=INK):
        self.set_font("Body", "B" if bold else "", size)
        self.set_text_color(*color)

    @property
    def bottom(self):
        return 297 - (36 if self.layout["footer"] else 24)

    def header(self):
        seller, layout = self.doc["seller"], self.layout
        if self.draft:
            self.font(56, True, tuple(int(c + (255 - c) * 0.88) for c in self.accent))
            with self.rotation(35, 105, 160):
                self.text(38, 175, self.t("ENTWURF"))
        if layout["fold_marks"]:
            self.set_draw_color(*MUTED)
            self.set_line_width(0.2)
            for y in (105, 148.5, 210):
                self.line(4, y, 9 if y != 148.5 else 7, y)
        top = 14
        logo = self.doc.get("logo")
        logo_bottom = top
        if logo:
            height = layout["logo_height"]
            width = _logo_width(logo, height)
            x = {"left": LEFT, "center": 105 - width / 2}.get(layout["logo_position"], RIGHT - width)
            self.image(logo, x=x, y=top, h=height)
            logo_bottom = top + height + 3
        if layout["header_text"]:
            right_side = not logo or layout["logo_position"] != "right"
            y = top if right_side and layout["logo_position"] != "center" else logo_bottom
            self.font(13 if logo else 15, True, self.accent)
            self.set_xy(LEFT, y)
            self.cell(WIDTH, 6, seller.get("company") or "", align="R")
            self.font(8.5, False, MUTED)
            lines = [seller.get("street"), f"{seller.get('zip', '')} {seller.get('city', '')}".strip(), seller.get("phone"),
                     seller.get("email"), seller.get("website")]
            self.set_xy(LEFT, y + 7)
            self.multi_cell(WIDTH, 4, "\n".join(line for line in lines if line), align="R")
        if self.page_no() > 1:
            self.set_y(max(self.get_y() + 6, logo_bottom + 4, 40))

    def footer(self):
        seller, layout = self.doc["seller"], self.layout
        if layout["page_numbers"]:
            self.font(7.5, False, MUTED)
            self.set_xy(LEFT, (272 if layout["footer"] else 284) - 5)
            self.cell(WIDTH, 4, f"{self.page_no()}/{{nb}}", align="R")
        if not layout["footer"]:
            return
        self.set_draw_color(*LINE)
        self.line(LEFT, 272, RIGHT, 272)
        self.font(7.2, False, MUTED)
        country = COUNTRY_NAMES.get(self.language, {}).get((seller.get("country") or "DE").upper(), seller.get("country"))
        columns = [
            [seller.get("company"), seller.get("street"), f"{seller.get('zip', '')} {seller.get('city', '')}".strip(), country],
            [self.t("Tel. %(value)s", value=seller["phone"]) if seller.get("phone") else "",
             self.t("E-Mail %(value)s", value=seller["email"]) if seller.get("email") else "",
             self.t("Web %(value)s", value=seller["website"]) if seller.get("website") else ""],
            [self.t("USt-IdNr. %(value)s", value=seller["vat_id"]) if seller.get("vat_id") else "",
             self.t("Steuer-Nr. %(value)s", value=seller["tax_number"]) if seller.get("tax_number") else "",
             self.t("Inhaber/-in %(value)s", value=seller["contact_name"]) if layout["footer_owner"] and seller.get("contact_name") else ""],
            [seller.get("bank_name"), f"IBAN {format_iban(seller.get('iban'))}" if seller.get("iban") else "",
             f"BIC {seller['bic']}" if seller.get("bic") else ""],
        ]
        # Breiten nach typischem Inhalt: Bank/IBAN braucht am meisten Platz
        weights = [0.20, 0.27, 0.24, 0.29]
        used = [(column, weight) for column, weight in zip(columns, weights) if any(column)]
        total = sum(weight for _, weight in used) or 1
        x = LEFT
        for column, weight in used:
            width = WIDTH * weight / total
            self.set_xy(x, 274)
            self.multi_cell(width - 2, 3.5, "\n".join(line for line in column if line))
            x += width


_LOGO_SIZES = {}


def _logo_width(path, height):
    from PIL import Image
    key = (str(path), Path(path).stat().st_mtime if Path(path).exists() else 0)
    if key not in _LOGO_SIZES:
        with Image.open(path) as image:
            _LOGO_SIZES[key] = image.width / image.height
    return min(height * _LOGO_SIZES[key], WIDTH)


def format_iban(iban):
    iban = (iban or "").replace(" ", "").upper()
    return " ".join(iban[i:i + 4] for i in range(0, len(iban), 4))


def _period(doc):
    first, last = doc.get("service_from"), doc.get("service_to")
    language = doc.get("language")
    if not first or not last:
        return None
    if first == last:
        return i18n.fmt_date(first, language)
    return f"{i18n.fmt_date(first, language)} – {i18n.fmt_date(last, language)}"


def girocode_image(seller, cents, reference):
    """EPC-QR-Code (GiroCode) nach EPC069-12, Version 002."""
    payload = billing.epc_payload(seller, cents, reference)
    if not payload:
        return None
    code = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, border=0, box_size=10)
    code.add_data(payload.encode("utf-8"))
    code.make(fit=True)
    return code.make_image(fill_color="black", back_color="white").get_image().convert("RGB")


def render(doc, draft=False, timesheet=None, xml=None):
    """Erzeugt das PDF; xml (CII) wird bei abgeschlossenen Rechnungen eingebettet."""
    seller, buyer = doc["seller"], doc["buyer"]
    summary = billing.totals(doc["items"], doc["tax_mode"])
    sign = -1 if doc["type_code"] == "381" else 1
    language = doc.get("language") or "de"

    def t(text, **values):
        return i18n.gettext(text, language, **values)

    def money(cents):
        return i18n.fmt_money(cents, language)

    def day(value):
        return i18n.fmt_date(value, language)

    number = doc["number"] if doc["number"] != "Entwurf" else t("Entwurf")
    title = f"{t(TYPE_TITLES[doc['type_code']])} {number}"
    pdf = InvoicePDF(doc, draft)
    layout, accent = pdf.layout, pdf.accent
    created = datetime.now(timezone.utc)
    pdf.set_creation_date(created)
    pdf.set_title(title)
    pdf.set_author(seller.get("company") or "Stempeln")
    pdf.set_lang("en-GB" if language == "en" else "de-DE")
    pdf.add_output_intent(OutputIntentSubType.PDFA, "sRGB IEC61966-2.1", "sRGB", "http://www.color.org",
                          PDFICCProfile(contents=_srgb_profile(), n=3, alternate="DeviceRGB"), "sRGB IEC61966-2.1")
    pdf.set_xmp_metadata(_xmp(title, seller.get("company") or "Stempeln", created))
    pdf.alias_nb_pages()
    pdf.add_page()

    # Absenderzeile + Anschriftfeld (DIN 5008 Form B: Fenster ab 45 mm)
    if layout["sender_line"]:
        pdf.font(7, False, MUTED)
        pdf.set_xy(LEFT, 47)
        sender = " · ".join(part for part in (seller.get("company"), seller.get("street"),
                                              f"{seller.get('zip', '')} {seller.get('city', '')}".strip()) if part)
        pdf.cell(85, 4, sender)
        pdf.set_draw_color(*LINE)
        pdf.line(LEFT, 51.5, LEFT + 85, 51.5)
    pdf.font(10)
    pdf.set_xy(LEFT, 54)
    address = [buyer.get("name"), buyer.get("contact"), buyer.get("street"),
               f"{buyer.get('zip', '')} {buyer.get('city', '')}".strip()]
    country = (buyer.get("country") or "DE").upper()
    if country != "DE" or layout["address_country"]:
        address.append(COUNTRY_NAMES.get(language, {}).get(country, country))
    pdf.multi_cell(85, 5, "\n".join(line for line in address if line))

    # Infoblock rechts
    info = [(t("Rechnungsdatum") if sign > 0 else t("Datum"), day(doc["issue_date"]))]
    reference = doc.get("buyer_reference") or ""
    if buyer.get("leitweg_id"):
        info.append((t("Leitweg-ID"), buyer["leitweg_id"]))
    elif reference:
        info.append((t("Referenz"), reference))
    if _period(doc):
        single = doc["service_from"] == doc["service_to"]
        info.append((t("Leistungsdatum") if single else t("Leistungszeitraum"), _period(doc)))
    if doc.get("due_date") and sign > 0 and layout["info_due_date"]:
        info.append((t("Fällig am"), day(doc["due_date"])))
    if doc.get("preceding"):
        info.append((t("Zu Rechnung"), t("%(number)s vom %(date)s", number=doc["preceding"][0], date=day(doc["preceding"][1]))))
    if buyer.get("number") and layout["info_customer_number"]:
        info.append((t("Ihre Kundennummer"), buyer["number"]))
    if buyer.get("vat_id"):
        info.append((t("USt-IdNr. Kunde"), buyer["vat_id"]))
    if seller.get("contact_name") and layout["info_contact"]:
        info.append((t("Ihr Ansprechpartner"), seller["contact_name"]))
    pdf.set_xy(122, 50)
    pdf.font(11, True, accent)
    pdf.cell(26, 6, t("Rechnungs-Nr.") if sign > 0 else t("Storno-Nr."))
    pdf.cell(42, 6, number, align="R")
    y = 57
    for label, value in info:
        pdf.set_xy(122, y)
        pdf.font(8.5, False, MUTED)
        pdf.cell(30, 4.6, label)
        pdf.font(8.5, False)
        pdf.cell(38, 4.6, value, align="R")
        y += 4.6

    pdf.set_y(max(100, y + 10))
    pdf.font(16, True, accent if layout["title_accent"] else INK)
    pdf.cell(WIDTH, 9, title, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(3)
    pdf.font(10)
    if layout["salutation"] and doc.get("salutation"):
        pdf.multi_cell(WIDTH, 5, doc["salutation"], new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.ln(2.5)
    if doc.get("intro"):
        pdf.multi_cell(WIDTH, 5, doc["intro"], new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.ln(4)

    # Positionen
    multi_rate = len({str(item["vat_rate"]) for item in doc["items"]}) > 1 and doc["tax_mode"] == "standard"
    columns = [(t("Pos."), 11, "L"), (t("Beschreibung"), 79 if multi_rate else 92, "L"), (t("Menge"), 20, "R"),
               (t("Einzelpreis"), 24, "R")] + ([(t("USt."), 13, "R")] if multi_rate else []) + [(t("Gesamt"), 23, "R")]
    head_fill = tuple(int(c + (255 - c) * 0.92) for c in accent)

    def table_header():
        pdf.font(8, True, accent if layout["table_accent"] else MUTED)
        pdf.set_fill_color(*(head_fill if layout["table_accent"] else (244, 246, 250)))
        for label, width, align in columns:
            pdf.cell(width, 7, label, align=align, fill=True)
        pdf.ln(7)

    table_header()
    pdf.set_draw_color(*LINE)
    for position, item in enumerate(doc["items"], start=1):
        description_width = columns[1][1]
        pdf.font(9.5, True)
        lines = pdf.multi_cell(description_width, 4.6, item["description"], dry_run=True, output="LINES")
        detail_lines = []
        if item.get("detail"):
            pdf.font(8.5, False, MUTED)
            detail_lines = pdf.multi_cell(description_width, 4.2, item["detail"], dry_run=True, output="LINES")
        height = len(lines) * 4.6 + len(detail_lines) * 4.2 + 3
        if pdf.get_y() + height > pdf.bottom:
            pdf.add_page()
            table_header()
        top = pdf.get_y() + 1.5
        pdf.set_xy(LEFT, top)
        pdf.font(9.5)
        pdf.cell(columns[0][1], 4.6, f"{position}.")
        x = LEFT + columns[0][1]
        pdf.set_xy(x, top)
        pdf.font(9.5, True)
        pdf.multi_cell(description_width, 4.6, item["description"])
        if detail_lines:
            pdf.set_x(x)
            pdf.font(8.5, False, MUTED)
            pdf.multi_cell(description_width, 4.2, item["detail"])
        x += description_width
        quantity = f"{billing.fmt_quantity(item['quantity'], language)} {billing.unit_label(item['unit'], item['quantity'], language)}"
        values = [quantity, money(item["price_cents"])] + ([billing.fmt_rate(item["vat_rate"], language)] if multi_rate else []) \
            + [money(sign * item["line_cents"])]
        pdf.font(9.5)
        for (label, width, align), value in zip(columns[2:], values):
            pdf.set_xy(x, top)
            pdf.cell(width, 4.6, value, align=align)
            x += width
        pdf.set_y(top + height - 1.5)
        pdf.line(LEFT, pdf.get_y(), RIGHT, pdf.get_y())

    # Summen
    rows = [(t("Summe netto"), summary["net"])] if doc["tax_mode"] == "standard" else []
    for group in summary["groups"]:
        if doc["tax_mode"] == "standard":
            rows.append((t("Umsatzsteuer %(rate)s auf %(basis)s", rate=billing.fmt_rate(group["rate"], language),
                           basis=money(sign * group["basis"])), group["tax"]))
    if pdf.get_y() + 14 + len(rows) * 6 > pdf.bottom:
        pdf.add_page()
    pdf.ln(3)
    for label, cents in rows:
        pdf.font(9.5, False, MUTED)
        pdf.set_x(100)
        pdf.cell(65, 6, label, align="R")
        pdf.font(9.5)
        pdf.cell(25, 6, money(sign * cents), align="R", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_draw_color(*accent)
    pdf.line(110, pdf.get_y() + 1, RIGHT, pdf.get_y() + 1)
    pdf.ln(2.5)
    pdf.set_x(100)
    pdf.font(11, True)
    pdf.cell(65, 7, t("Gesamtbetrag") if doc["tax_mode"] != "standard" else t("Rechnungsbetrag"), align="R")
    pdf.font(11, True, accent)
    pdf.cell(25, 7, money(sign * summary["gross"]), align="R", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_draw_color(*LINE)
    pdf.ln(7)

    notes = [t(group["reason"]) for group in summary["groups"] if group["reason"]]
    if doc.get("footer"):
        notes.append(doc["footer"])
    pdf.font(9.5)
    for note in dict.fromkeys(notes):
        pdf.multi_cell(WIDTH, 5, note, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.ln(1.5)
    closing = doc.get("closing") or ""
    if layout["signature"] and seller.get("contact_name"):
        closing = f"{closing}\n{seller['contact_name']}".strip()
    if closing:
        pdf.ln(2)
        pdf.multi_cell(WIDTH, 5, closing, new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    # GiroCode: nur für Rechnungen mit offenem Betrag in Euro und IBAN
    if layout["girocode"] and sign > 0 and summary["gross"] > 0:
        image = girocode_image(seller, summary["gross"], number if doc["number"] != "Entwurf" else "")
        if image is not None:
            size = 24
            if pdf.get_y() + size + 10 > pdf.bottom:
                pdf.add_page()
            pdf.ln(5)
            y = pdf.get_y()
            pdf.image(image, x=LEFT, y=y, w=size, h=size)
            pdf.set_xy(LEFT, y + size + 0.5)
            pdf.font(6.5, False, MUTED)
            pdf.cell(size, 3, "GiroCode", align="C")
            pdf.set_xy(LEFT + size + 4, y + 2)
            pdf.font(8, False, MUTED)
            pdf.multi_cell(80, 4, t("Zum Bezahlen mit der Banking-App scannen."))

    if timesheet:
        pdf.add_page()
        pdf.font(14, True, accent if layout["title_accent"] else INK)
        pdf.cell(WIDTH, 8, t("Leistungsnachweis"), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.font(8.5, False, MUTED)
        pdf.cell(WIDTH, 5, t("Anlage zu %(title)s", title=title) + (f" · {_period(doc)}" if _period(doc) else ""), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.ln(3)
        sheet_columns = [(t("Datum"), 22, "L"), (t("Zeit"), 24, "L"), (t("Dauer"), 16, "R"), (t("Projekt"), 40, "L"), (t("Tätigkeit"), 68, "L")]

        def sheet_header():
            pdf.font(8, True, accent if layout["table_accent"] else MUTED)
            pdf.set_fill_color(*(head_fill if layout["table_accent"] else (244, 246, 250)))
            for label, width, align in sheet_columns:
                pdf.cell(width, 6.5, label, align=align, fill=True)
            pdf.ln(6.5)

        sheet_header()
        total = 0
        for row in timesheet:
            pdf.font(8.5)
            note_lines = pdf.multi_cell(68, 4.2, row["note"] or "–", dry_run=True, output="LINES") if row["note"] else ["–"]
            height = max(1, len(note_lines)) * 4.2 + 1.6
            if pdf.get_y() + height > pdf.bottom:
                pdf.add_page()
                sheet_header()
            top = pdf.get_y() + 0.8
            x = LEFT
            for (label, width, align), value in zip(sheet_columns[:4], (row["day"], row["time"], row["duration"], row["project"])):
                pdf.set_xy(x, top)
                pdf.cell(width, 4.2, value, align=align)
                x += width
            pdf.set_xy(x + 2, top)
            pdf.multi_cell(66, 4.2, row["note"] or "–")
            pdf.set_y(top + height - 0.8)
            pdf.line(LEFT, pdf.get_y(), RIGHT, pdf.get_y())
            total += row["seconds"]
        pdf.ln(2)
        pdf.font(9, True)
        hours, minutes = divmod(round(total / 60), 60)
        pdf.cell(62, 6, t("Summe %(time)s h", time=f"{hours}:{minutes:02d}"), align="R")

    if xml is not None:
        pdf.embed_file(bytes=xml, basename="factur-x.xml", mime_type="text/xml", desc="Factur-X/ZUGFeRD Rechnung",
                       associated_file_relationship="Alternative", modification_date=created)
    return bytes(pdf.output())


def render_final(doc, timesheet=None):
    xml = build_cii(doc)
    return render(doc, draft=False, timesheet=timesheet, xml=xml), xml
