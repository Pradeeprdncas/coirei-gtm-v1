"""PDF fixtures for the market-research tests, with what each is known to contain.

Every number below is the fixture's own: tests compare against these, worked
out from the fixture text, never against the extractor's output.
"""

from tests.pdfs import Image, Text, make_pdf, paragraph, table

GLOBAL_SENTENCE = ("The global EV charging infrastructure market was valued at EUR 41.5 "
                   "billion in 2023, with Europe accounting for EUR 12.4 billion.")  # fmt: skip
DRIVER_SENTENCE = ("Demand for EV charging infrastructure is driven by binding deployment "
                   "targets for public charging points.")  # fmt: skip
BARRIER_SENTENCE = ("High grid connection costs are a key barrier to EV charging infrastructure "
                    "roll-out in rural areas.")  # fmt: skip
PRICE_SENTENCE = ("Public EV charging infrastructure plans cost EUR 450 per charger per year "
                  "for small operators.")  # fmt: skip
TABLE_CAPTION = "Table 1: European EV charging infrastructure market by country (EUR billion)"
TABLE_ROWS = [["Country", "2022", "2023"], ["Germany", "3.1", "3.6"], ["France", "2.2", "2.5"],
              ["Total", "10.9", "12.4"]]  # fmt: skip


def _wrap(text: str, width: int = 80) -> list[str]:
    lines, line = [], ""
    for word in text.split():
        if line and len(line) + 1 + len(word) > width:
            lines.append(line)
            line = word
        else:
            line = f"{line} {word}".strip()
    return [*lines, line]


def report_pdf(changed: bool = False) -> bytes:
    """Three pages: global and European figures (p.1), a country table (p.2), costs and
    barriers (p.3). ``changed``: a later edition with another European figure."""
    sentence = GLOBAL_SENTENCE.replace("12.4", "13.9") if changed else GLOBAL_SENTENCE
    p1 = [
        Text(56, 780, 16, "EV Charging Infrastructure Outlook 2024", bold=True),
        *paragraph(56, 750, _wrap(sentence)),
        *paragraph(56, 700, _wrap(DRIVER_SENTENCE)),
    ]
    p2 = [
        Text(56, 780, 12, TABLE_CAPTION, bold=True),
        *table(56, 760, TABLE_ROWS, [150, 80, 80]),
        *paragraph(56, 660, ["Source: national statistics offices; figures rounded."], size=9),
    ]
    p3 = [
        Text(56, 780, 14, "Costs and barriers", bold=True),
        *paragraph(56, 750, _wrap(PRICE_SENTENCE)),
        *paragraph(56, 700, _wrap(BARRIER_SENTENCE)),
    ]
    return make_pdf([p1, p2, p3], info={"Title": "EV Charging Infrastructure Outlook 2024",
                                        "CreationDate": "D:20240315120000Z"})  # fmt: skip


def scanned_pdf() -> bytes:
    """One text page and one image-only page (a scan has no text layer)."""
    p1 = paragraph(56, 750, _wrap(DRIVER_SENTENCE))
    return make_pdf([p1, [Image(50, 300, 400, 400)]])


def irregular_table_pdf() -> bytes:
    """A ruled table with an empty cell: not used for figures, and said so."""
    rows = [["Country", "2022", "2023"], ["Germany", "3.1", ""], ["France", "2.2", "2.5"]]
    return make_pdf([[Text(56, 780, 12, TABLE_CAPTION, bold=True),
                      *table(56, 760, rows, [150, 80, 80])]])  # fmt: skip


LEFT_COLUMN = ("The European EV charging infrastructure market grew strongly in 2023 as "
               "public funding was extended to motorway sites.")  # fmt: skip
RIGHT_COLUMN = ("Operators report waiting times of several months for grid connections in "
                "most regions, and hardware prices have stopped falling.")  # fmt: skip


def two_column_pdf() -> bytes:
    """Two text columns side by side: each must read as its own paragraph."""
    return make_pdf([paragraph(56, 750, _wrap(LEFT_COLUMN, 38))
                     + paragraph(310, 750, _wrap(RIGHT_COLUMN, 38))])  # fmt: skip


UNRULED_ROWS = [["Country", "2022", "2023"], ["Germany", "3.1", "3.6"], ["France", "2.2", "2.5"]]


def unruled_table_pdf() -> bytes:
    """A table drawn without lines: pdfplumber's ruled-table finder does not see it."""
    items = [Text(56, 780, 12, TABLE_CAPTION, bold=True)]
    for r, row in enumerate(UNRULED_ROWS):
        items += [Text(56 + 150 * j if j == 0 else 206 + 80 * (j - 1), 750 - 18 * r, 10, c)
                  for j, c in enumerate(row)]  # fmt: skip
    return make_pdf([items])


# Labelled set for the extraction-quality test: (page, exact text expected in the page text).
REPORT_TEXT = [(1, GLOBAL_SENTENCE), (1, DRIVER_SENTENCE), (2, TABLE_CAPTION),
               (2, "Germany | 3.1 | 3.6"), (2, "Total | 10.9 | 12.4"), (3, PRICE_SENTENCE),
               (3, BARRIER_SENTENCE)]  # fmt: skip
# Figures a correct reading yields: (page, value, currency, year, geography).
REPORT_FIGURES = [
    (1, 41.5e9, "EUR", 2023, "Global"),
    (1, 12.4e9, "EUR", 2023, "Europe"),
    (2, 3.1e9, "EUR", 2022, "Germany"),
    (2, 3.6e9, "EUR", 2023, "Germany"),
    (2, 2.2e9, "EUR", 2022, "France"),
    (2, 2.5e9, "EUR", 2023, "France"),
    (2, 10.9e9, "EUR", 2022, "Europe"),
    (2, 12.4e9, "EUR", 2023, "Europe"),
]
