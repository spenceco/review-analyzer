from __future__ import annotations

import pytest

from app.csv_validation import CSVValidationError, parse_reviews

LIMITS = {"max_rows": 200, "max_bytes": 2 * 1024 * 1024, "max_review_chars": 1500}


def parse(data: bytes | str, **overrides: int):  # type: ignore[no-untyped-def]
    raw = data.encode() if isinstance(data, str) else data
    return parse_reviews(raw, **{**LIMITS, **overrides})


def test_sample_file_parses(sample_bytes: bytes) -> None:
    reviews = parse(sample_bytes)
    assert len(reviews) == 75
    assert reviews[0].id == 1 and reviews[0].rating == 5 and reviews[0].date == "2026-07-02"
    assert "oat latte" in reviews[0].text


@pytest.mark.parametrize("header", ["Review", "review_text", "Comment", "FEEDBACK", "Review Text"])
def test_recognises_common_header_names(header: str) -> None:
    assert parse(f"{header}\nGreat coffee\n")[0].text == "Great coffee"


def test_semicolon_delimiter_and_bom() -> None:
    data = "﻿rating;review\n4;Nice place, good coffee\n".encode()
    reviews = parse(data)
    assert reviews[0].rating == 4 and reviews[0].text == "Nice place, good coffee"


def test_windows_1252_fallback() -> None:
    data = "review\nCafé was lovely\n".encode("cp1252")
    assert parse(data)[0].text == "Café was lovely"


@pytest.mark.parametrize("raw,expected", [("4/5", 4.0), ("4.5", 4.5), ("★★★", 3.0),
                                          ("great", None), ("", None)])
def test_rating_formats(raw: str, expected: float | None) -> None:
    assert parse(f'rating,review\n"{raw}",Good coffee\n')[0].rating == expected


def test_blank_and_junk_rows_skipped_and_ids_contiguous() -> None:
    reviews = parse("review\nFirst one\n\n,\nok\nx\nSecond one\n")
    assert [(r.id, r.text) for r in reviews] == [(1, "First one"), (2, "Second one")]


def test_long_reviews_truncated() -> None:
    assert len(parse("review\n" + "a" * 5000 + "\n")[0].text) == 1500


def test_single_unnamed_column_is_accepted() -> None:
    assert len(parse("My reviews\nGreat\nBad coffee\n")) == 2


def test_empty_file() -> None:
    with pytest.raises(CSVValidationError, match="empty"):
        parse(b"")


def test_too_large() -> None:
    with pytest.raises(CSVValidationError, match="limit is"):
        parse("review\n" + "x" * 2000 + "\n", max_bytes=1000)


def test_too_many_rows() -> None:
    data = "review\n" + "\n".join(f"Review {i}" for i in range(201))
    with pytest.raises(CSVValidationError, match="more than 200"):
        parse(data)


def test_missing_review_column_lists_found_columns() -> None:
    with pytest.raises(CSVValidationError, match='"name", "stars"'):
        parse("name,stars\nBob,5\n")


def test_excel_file_gets_specific_advice() -> None:
    with pytest.raises(CSVValidationError, match="Excel"):
        parse(b"PK\x03\x04rest-of-zip")


def test_column_present_but_empty() -> None:
    with pytest.raises(CSVValidationError, match="no review text"):
        parse("review,rating\n,5\n,4\n")
