"""Currencies offered as suggestions in the form (any ISO 4217 code is still accepted)."""

CURRENCIES: dict[str, str] = {
    "CHF": "Swiss franc",
    "EUR": "Euro",
    "USD": "US dollar",
    "GBP": "British pound",
    "AUD": "Australian dollar",
    "BRL": "Brazilian real",
    "CAD": "Canadian dollar",
    "CNY": "Chinese yuan",
    "CZK": "Czech koruna",
    "DKK": "Danish krone",
    "HKD": "Hong Kong dollar",
    "HUF": "Hungarian forint",
    "INR": "Indian rupee",
    "JPY": "Japanese yen",
    "KRW": "South Korean won",
    "MXN": "Mexican peso",
    "NOK": "Norwegian krone",
    "NZD": "New Zealand dollar",
    "PLN": "Polish złoty",
    "SEK": "Swedish krona",
    "SGD": "Singapore dollar",
    "THB": "Thai baht",
    "TRY": "Turkish lira",
    "AED": "UAE dirham",
    "ZAR": "South African rand",
}


def suggest_currencies(query: str, limit: int = 10) -> list[tuple[str, str]]:
    """(code, English name) pairs whose code or name contains the query."""
    term = query.strip().lower()
    matches = [
        (code, name)
        for code, name in CURRENCIES.items()
        if term in code.lower() or term in name.lower()
    ]
    matches.sort(key=lambda item: not item[0].lower().startswith(term))
    return matches[:limit]
