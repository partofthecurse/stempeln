"""E-Rechnung im UN/CEFACT-CII-Format (EN 16931 für ZUGFeRD/Factur-X, optional XRechnung 3.0)."""

from xml.etree import ElementTree as ET

from . import billing

NS = {
    "rsm": "urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100",
    "ram": "urn:un:unece:uncefact:data:standard:ReusableAggregateBusinessInformationEntity:100",
    "qdt": "urn:un:unece:uncefact:data:standard:QualifiedDataType:100",
    "udt": "urn:un:unece:uncefact:data:standard:UnqualifiedDataType:100",
}
for _prefix, _uri in NS.items():
    ET.register_namespace(_prefix, _uri)

GUIDELINE_EN16931 = "urn:cen.eu:en16931:2017"
GUIDELINE_XRECHNUNG = "urn:cen.eu:en16931:2017#compliant#urn:xeinkauf.de:kosit:xrechnung_3.0"
BUSINESS_PROCESS = "urn:fdc:peppol.eu:2017:poacc:billing:01:1.0"


def _q(tag):
    prefix, name = tag.split(":")
    return f"{{{NS[prefix]}}}{name}"


def _el(parent, tag, text=None, **attributes):
    element = ET.SubElement(parent, _q(tag), {key: str(value) for key, value in attributes.items()})
    if text is not None:
        element.text = str(text)
    return element


def _date(parent, tag, day, qualified=False):
    wrapper = _el(parent, tag)
    _el(wrapper, "qdt:DateTimeString" if qualified else "udt:DateTimeString", day.strftime("%Y%m%d"), format="102")


def _amount(cents):
    return f"{billing.money(cents):f}"


def _quantity(value):
    return f"{billing.dec(value).quantize(billing.dec('0.0001')).normalize():f}"


def _percent(value):
    return f"{billing.dec(value).quantize(billing.CENT):f}"


def _address(parent, party):
    address = _el(parent, "ram:PostalTradeAddress")
    if party.get("zip"):
        _el(address, "ram:PostcodeCode", party["zip"])
    if party.get("street"):
        _el(address, "ram:LineOne", party["street"])
    if party.get("contact") and parent.tag == _q("ram:BuyerTradeParty"):
        _el(address, "ram:LineTwo", party["contact"])
    if party.get("city"):
        _el(address, "ram:CityName", party["city"])
    _el(address, "ram:CountryID", (party.get("country") or "DE").upper())


def build_cii(doc, xrechnung=False):
    """doc: number, type_code, issue_date, due_date, service_from, service_to, buyer_reference, notes,
    payment_terms, preceding (number, date) | None, seller, buyer, items, tax_mode."""
    seller, buyer, mode = doc["seller"], doc["buyer"], doc["tax_mode"]
    summary = billing.totals(doc["items"], mode)
    root = ET.Element(_q("rsm:CrossIndustryInvoice"))

    context = _el(root, "rsm:ExchangedDocumentContext")
    _el(_el(context, "ram:BusinessProcessSpecifiedDocumentContextParameter"), "ram:ID", BUSINESS_PROCESS)
    _el(_el(context, "ram:GuidelineSpecifiedDocumentContextParameter"), "ram:ID",
        GUIDELINE_XRECHNUNG if xrechnung else GUIDELINE_EN16931)

    header = _el(root, "rsm:ExchangedDocument")
    _el(header, "ram:ID", doc["number"])
    _el(header, "ram:TypeCode", doc["type_code"])
    _date(header, "ram:IssueDateTime", doc["issue_date"])
    for note in doc.get("notes", []):
        if note:
            _el(_el(header, "ram:IncludedNote"), "ram:Content", note)
    if seller.get("company"):
        # Pflichtangaben des Verkäufers als Freitext (BT-22, Code REG)
        lines = [seller["company"], seller.get("street", ""), f"{seller.get('zip', '')} {seller.get('city', '')}".strip()]
        tax = f"USt-IdNr. {seller['vat_id']}" if seller.get("vat_id") else f"Steuernummer {seller.get('tax_number', '')}"
        note = _el(header, "ram:IncludedNote")
        _el(note, "ram:Content", ", ".join(line for line in lines if line) + ", " + tax)
        _el(note, "ram:SubjectCode", "REG")

    transaction = _el(root, "rsm:SupplyChainTradeTransaction")
    for position, item in enumerate(doc["items"], start=1):
        category, rate = billing.line_category(mode, item["vat_rate"])
        line = _el(transaction, "ram:IncludedSupplyChainTradeLineItem")
        _el(_el(line, "ram:AssociatedDocumentLineDocument"), "ram:LineID", position)
        product = _el(line, "ram:SpecifiedTradeProduct")
        _el(product, "ram:Name", item["description"][:200])
        if item.get("detail"):
            _el(product, "ram:Description", item["detail"])
        agreement = _el(line, "ram:SpecifiedLineTradeAgreement")
        _el(_el(agreement, "ram:NetPriceProductTradePrice"), "ram:ChargeAmount", _amount(item["price_cents"]))
        _el(_el(line, "ram:SpecifiedLineTradeDelivery"), "ram:BilledQuantity", _quantity(item["quantity"]), unitCode=item["unit"])
        settlement = _el(line, "ram:SpecifiedLineTradeSettlement")
        tax = _el(settlement, "ram:ApplicableTradeTax")
        _el(tax, "ram:TypeCode", "VAT")
        _el(tax, "ram:CategoryCode", category)
        if category != "O":
            _el(tax, "ram:RateApplicablePercent", _percent(rate))
        _el(_el(settlement, "ram:SpecifiedTradeSettlementLineMonetarySummation"), "ram:LineTotalAmount", _amount(item["line_cents"]))

    agreement = _el(transaction, "ram:ApplicableHeaderTradeAgreement")
    _el(agreement, "ram:BuyerReference", doc.get("buyer_reference") or buyer.get("leitweg_id") or buyer.get("number") or "n/a")

    party = _el(agreement, "ram:SellerTradeParty")
    if mode == "non_eu" or not seller.get("vat_id"):
        # BR-CO-26: ohne übermittelte USt-IdNr. braucht der Verkäufer eine Kennung (BT-29)
        _el(party, "ram:ID", seller.get("tax_number") or seller.get("vat_id") or seller["company"])
    _el(party, "ram:Name", seller["company"])
    if seller.get("contact_name") or seller.get("phone") or seller.get("email"):
        contact = _el(party, "ram:DefinedTradeContact")
        _el(contact, "ram:PersonName", seller.get("contact_name") or seller["company"])
        if seller.get("phone"):
            _el(_el(contact, "ram:TelephoneUniversalCommunication"), "ram:CompleteNumber", seller["phone"])
        if seller.get("email"):
            _el(_el(contact, "ram:EmailURIUniversalCommunication"), "ram:URIID", seller["email"])
    _address(party, seller)
    if seller.get("email"):
        _el(_el(party, "ram:URIUniversalCommunication"), "ram:URIID", seller["email"], schemeID="EM")
    # Bei "nicht steuerbar" (O) darf keine USt-IdNr. des Verkäufers übermittelt werden (BR-O-02)
    if seller.get("vat_id") and mode != "non_eu":
        _el(_el(party, "ram:SpecifiedTaxRegistration"), "ram:ID", seller["vat_id"], schemeID="VA")
    if seller.get("tax_number"):
        _el(_el(party, "ram:SpecifiedTaxRegistration"), "ram:ID", seller["tax_number"], schemeID="FC")

    party = _el(agreement, "ram:BuyerTradeParty")
    if buyer.get("number"):
        _el(party, "ram:ID", buyer["number"])
    _el(party, "ram:Name", buyer["name"])
    _address(party, buyer)
    if buyer.get("email"):
        _el(_el(party, "ram:URIUniversalCommunication"), "ram:URIID", buyer["email"], schemeID="EM")
    if buyer.get("vat_id") and mode != "non_eu":
        _el(_el(party, "ram:SpecifiedTaxRegistration"), "ram:ID", buyer["vat_id"], schemeID="VA")

    delivery = _el(transaction, "ram:ApplicableHeaderTradeDelivery")
    # Leistungsdatum (BT-72): Ende des Leistungszeitraums bzw. Rechnungsdatum
    _date(_el(delivery, "ram:ActualDeliverySupplyChainEvent"), "ram:OccurrenceDateTime", doc.get("service_to") or doc["issue_date"])

    settlement = _el(transaction, "ram:ApplicableHeaderTradeSettlement")
    _el(settlement, "ram:PaymentReference", doc["number"])
    _el(settlement, "ram:InvoiceCurrencyCode", "EUR")
    if seller.get("iban"):
        means = _el(settlement, "ram:SpecifiedTradeSettlementPaymentMeans")
        _el(means, "ram:TypeCode", "58")
        account = _el(means, "ram:PayeePartyCreditorFinancialAccount")
        _el(account, "ram:IBANID", seller["iban"].replace(" ", "").upper())
        _el(account, "ram:AccountName", seller.get("account_holder") or seller["company"])
        if seller.get("bic"):
            _el(_el(means, "ram:PayeeSpecifiedCreditorFinancialInstitution"), "ram:BICID", seller["bic"].replace(" ", "").upper())
    else:
        _el(_el(settlement, "ram:SpecifiedTradeSettlementPaymentMeans"), "ram:TypeCode", "1")
    for group in summary["groups"]:
        tax = _el(settlement, "ram:ApplicableTradeTax")
        _el(tax, "ram:CalculatedAmount", _amount(group["tax"]))
        _el(tax, "ram:TypeCode", "VAT")
        if group["reason"]:
            _el(tax, "ram:ExemptionReason", group["reason"])
        _el(tax, "ram:BasisAmount", _amount(group["basis"]))
        _el(tax, "ram:CategoryCode", group["category"])
        if group["reason_code"]:
            _el(tax, "ram:ExemptionReasonCode", group["reason_code"])
        # BT-119 auch bei "O" mit 0 % (XRechnung BR-DE-14); auf Positionsebene bleibt er bei "O" weg (BR-O-05)
        _el(tax, "ram:RateApplicablePercent", _percent(group["rate"]))
    if doc.get("service_from") and doc.get("service_to"):
        period = _el(settlement, "ram:BillingSpecifiedPeriod")
        _date(period, "ram:StartDateTime", doc["service_from"])
        _date(period, "ram:EndDateTime", doc["service_to"])
    due = doc.get("due_date") if doc["type_code"] == "380" else None
    if doc.get("payment_terms") or due:
        terms = _el(settlement, "ram:SpecifiedTradePaymentTerms")
        if doc.get("payment_terms"):
            _el(terms, "ram:Description", doc["payment_terms"])
        if due:
            _date(terms, "ram:DueDateDateTime", due)
    sums = _el(settlement, "ram:SpecifiedTradeSettlementHeaderMonetarySummation")
    _el(sums, "ram:LineTotalAmount", _amount(summary["net"]))
    _el(sums, "ram:TaxBasisTotalAmount", _amount(summary["net"]))
    _el(sums, "ram:TaxTotalAmount", _amount(summary["tax"]), currencyID="EUR")
    _el(sums, "ram:GrandTotalAmount", _amount(summary["gross"]))
    _el(sums, "ram:DuePayableAmount", _amount(summary["gross"]))
    if doc.get("preceding"):
        reference = _el(settlement, "ram:InvoiceReferencedDocument")
        _el(reference, "ram:IssuerAssignedID", doc["preceding"][0])
        _date(reference, "ram:FormattedIssueDateTime", doc["preceding"][1], qualified=True)

    ET.indent(root)
    return b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="utf-8")
