"""Generate the released currency round and its downloadable edition from CLDR."""

import re

from common import add, js, norm, record, split

CLDR = "https://raw.githubusercontent.com/unicode-org/cldr-json/47.0.0/cldr-json/"
REGIONS = CLDR + "cldr-core/supplemental/currencyData.json"
NAMES = CLDR + "cldr-numbers-full/main/en/currencies.json"
ALIASES = {
    "BDT": "Taka",
    "BRL": "Real",
    "CHF": "Swiss francs",
    "CNY": "Chinese renminbi|Renminbi|Yuan",
    "DEM": "Deutsche Mark|Deutschmark",
    "ETB": "Birr",
    "EUR": "Euros",
    "GBP": "Pound sterling|Sterling|British pound sterling",
    "GTQ": "Quetzal",
    "HUF": "Forint",
    "IDR": "Rupiah",
    "ILS": "Shekel|New shekel",
    "INR": "Indian rupees",
    "JPY": "Yen",
    "KRW": "Republic of Korea won",
    "KZT": "Tenge",
    "MMK": "Kyat",
    "MNT": "Tugrik",
    "MYR": "Ringgit",
    "NGN": "Naira",
    "PLN": "Zloty",
    "THB": "Baht",
    "UAH": "Hryvnia",
    "USD": "United States dollar|American dollar",
    "VND": "Dong",
    "VUV": "Vatu",
    "ZAR": "Rand",
    "FRF": "Franc français",
    "ITL": "Lira italiana",
}


def build():
    regions = js(REGIONS)["supplemental"]["currencyData"]["region"]
    names = js(NAMES)["main"]["en"]["numbers"]["currencies"]
    codes = sorted(
        {
            code
            for entries in regions.values()
            for item in entries
            for code, dates in item.items()
            if dates.get("_tender") != "false"
        }
    )
    rows = []
    for code in codes:
        english = names[code]["displayName"]
        label = {
            "KGS": "Kyrgyz Som",
            "BMD": "Bermudian Dollar",
            "SHP": "St Helena Pound",
        }.get(code, english.replace("Ruble", "Rouble"))
        rows.append(record(label, english, code, *split(ALIASES.get(code, ""))))
    q = add(
        "general",
        "currencies",
        "Foreign exchange",
        "Name a present or historical currency recorded as legal tender in the Unicode currency list. Give its full name or three-letter code.",
        REGIONS,
        rows,
        f"All {len(codes)} tender codes across territories in Unicode CLDR 47. Fund, metal and test codes are excluded.",
        common="Euro|British Pound|Japanese Yen|US Dollar|French Franc|German Mark|Italian Lira",
        rare="Andorran Peseta|Equatorial Guinean Ekwele",
        matching="full",
    )
    q["topic"] = "Geography"
    # A shared unit needs a country or an identifying code. Keep it out of the
    # exact alias index, including when a formerly unique unit gains a namesake.
    for unit in [
        "lira",
        "dollar",
        "franc",
        "pound",
        "peso",
        "rupee",
        "dinar",
        "mark",
        "real",
        "zloty",
        "shekel",
        "dong",
        "rouble",
        "guilder",
        "won",
    ]:
        owners = [
            a
            for a in q["answers"]
            if re.search(r"\b" + unit + r"$", norm(a["name"].split("(")[0]))
        ]
        if len(owners) > 1:
            for a in owners:
                a["aliases"] = [v for v in a["aliases"] if norm(v) != unit]
                a.setdefault("examples", []).append(unit)
    return q
