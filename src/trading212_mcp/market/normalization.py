"""Shared scalar normalization, without cross-provider metric substitution."""

import math
from typing import TypeGuard, cast


def number(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = float(str(value))
    except (ValueError, TypeError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def text(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def is_mapping(value: object) -> TypeGuard[dict[str, object]]:
    return isinstance(value, dict) and all(
        isinstance(key, str) for key in cast(dict[object, object], value)
    )


def is_list(value: object) -> TypeGuard[list[object]]:
    return isinstance(value, list)


COUNTRIES = {
    "United States": "US",
    "United Kingdom": "GB",
    "Canada": "CA",
    "Australia": "AU",
    "Japan": "JP",
    "China": "CN",
    "Hong Kong": "HK",
    "Germany": "DE",
    "France": "FR",
    "Switzerland": "CH",
    "Netherlands": "NL",
    "Ireland": "IE",
    "Sweden": "SE",
    "Denmark": "DK",
    "Finland": "FI",
    "Norway": "NO",
    "Spain": "ES",
    "Italy": "IT",
    "India": "IN",
    "South Korea": "KR",
    "Taiwan": "TW",
    "Singapore": "SG",
    "Israel": "IL",
    "Brazil": "BR",
    "Austria": "AT",
    "Belgium": "BE",
    "Portugal": "PT",
    "New Zealand": "NZ",
    "Mexico": "MX",
    "South Africa": "ZA",
    "Malaysia": "MY",
    "Indonesia": "ID",
    "Thailand": "TH",
    "Poland": "PL",
    "Saudi Arabia": "SA",
    "Turkey": "TR",
    "Greece": "GR",
    "Argentina": "AR",
    "Chile": "CL",
    "Vietnam": "VN",
    "Pakistan": "PK",
    "Czech Republic": "CZ",
    "Hungary": "HU",
    "United Arab Emirates": "AE",
    "Romania": "RO",
    "Colombia": "CO",
    "Peru": "PE",
    "Egypt": "EG",
}


def country_code(value: str) -> str:
    return next(
        (
            code
            for name, code in COUNTRIES.items()
            if name.casefold() == value.strip().casefold()
        ),
        value.strip(),
    ).upper()
