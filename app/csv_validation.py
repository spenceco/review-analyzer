"""Turn an uploaded CSV into a list of `Review`s, or explain clearly what is wrong with it."""

from __future__ import annotations

import csv
import io
import re

from .models import Review

# Header names we recognise, checked in order. Matching ignores case, spaces and _/-.
TEXT_COLUMNS = ("review", "reviewtext", "text", "comment", "comments", "feedback", "body",
                "content", "message", "description")
RATING_COLUMNS = ("rating", "stars", "score", "starrating")
DATE_COLUMNS = ("date", "reviewdate", "createdat", "created", "timestamp", "time")

MIN_REVIEW_CHARS = 3


class CSVValidationError(ValueError):
    """Raised with a message that is safe and helpful to show to the end user."""


def _normalise(header: str) -> str:
    return re.sub(r"[\s_\-]+", "", header.strip().lower())


def _find_column(headers: list[str], candidates: tuple[str, ...]) -> int | None:
    normalised = [_normalise(h) for h in headers]
    for name in candidates:
        if name in normalised:
            return normalised.index(name)
    return None


def _decode(data: bytes) -> str:
    """Decode bytes as UTF-8 (with or without BOM), falling back to Windows-1252,
    which is what Excel on Windows often produces."""
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise CSVValidationError(
        "The file isn't readable text. Please save it as 'CSV UTF-8' and try again."
    )


def _cell(row: list[str], col: int | None) -> str:
    """Return a stripped cell, or "" when the column is absent or the row is short."""
    return row[col].strip() if col is not None and col < len(row) else ""


def _parse_rating(raw: str) -> float | None:
    """Accept '4', '4.5', '4/5' or '★★★★'. Anything else is ignored rather than rejected."""
    raw = raw.strip()
    if not raw:
        return None
    if set(raw) <= {"★", "☆"}:
        return float(raw.count("★"))
    match = re.match(r"^(\d+(?:\.\d+)?)\s*(?:/\s*\d+)?$", raw)
    return float(match.group(1)) if match else None


def parse_reviews(
    data: bytes, *, max_rows: int, max_bytes: int, max_review_chars: int
) -> list[Review]:
    """Validate and parse the CSV.

    Rules: must be under ``max_bytes``, have a header row containing a review-text
    column, and contain between 1 and ``max_rows`` non-empty reviews. Over-long
    reviews are truncated to ``max_review_chars`` instead of rejected.
    """
    if not data:
        raise CSVValidationError("The file is empty.")
    if len(data) > max_bytes:
        raise CSVValidationError(
            f"The file is {len(data) / 1_048_576:.1f} MB. The limit is "
            f"{max_bytes / 1_048_576:.0f} MB."
        )
    if data[:4] == b"PK\x03\x04":
        raise CSVValidationError(
            "This looks like an Excel (.xlsx) file. In Excel use File > Save As > "
            "'CSV UTF-8', then upload that."
        )
    if b"\x00" in data[:1024]:
        raise CSVValidationError("This doesn't look like a text CSV file.")

    text = _decode(data)
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel

    try:
        rows = list(csv.reader(io.StringIO(text), dialect))
    except csv.Error as exc:
        raise CSVValidationError(f"The CSV couldn't be read: {exc}.") from exc

    rows = [r for r in rows if any(cell.strip() for cell in r)]
    if not rows:
        raise CSVValidationError("The file has no rows.")

    headers = rows[0]
    text_col = _find_column(headers, TEXT_COLUMNS)
    if text_col is None:
        if len(headers) == 1 and len(rows) > 1:
            text_col = 0  # A single unnamed column: assume it's the reviews.
        else:
            shown = ", ".join(f'"{h.strip()}"' for h in headers[:8] if h.strip()) or "none"
            raise CSVValidationError(
                "Couldn't find a review column. Name the column with the review text "
                f'"review" (or "text", "comment", "feedback"). Columns found: {shown}.'
            )
    rating_col = _find_column(headers, RATING_COLUMNS)
    date_col = _find_column(headers, DATE_COLUMNS)

    reviews: list[Review] = []
    for row in rows[1:]:
        cell = _cell(row, text_col)
        if len(cell) < MIN_REVIEW_CHARS:
            continue  # Skip blank or junk rows silently; they carry no signal.
        if len(reviews) >= max_rows:
            raise CSVValidationError(
                f"The file has more than {max_rows} reviews. This demo analyzes up to "
                f"{max_rows} at a time; please split the file."
            )
        rating_raw = _cell(row, rating_col)
        reviews.append(
            Review(
                id=len(reviews) + 1,
                text=cell[:max_review_chars],
                rating=_parse_rating(rating_raw) if rating_raw else None,
                date=_cell(row, date_col) or None,
            )
        )

    if not reviews:
        raise CSVValidationError(
            f'The "{headers[text_col].strip() or "review"}" column has no review text in it.'
        )
    return reviews
