import json
import re

from common import (
    ROOT,
    add,
    clean,
    grid,
    js,
    norm,
    packs,
    record,
    soup,
    tables,
    textcell,
    wiki,
)


def build():
    url = "https://raw.githubusercontent.com/mledoze/countries/master/countries.json"
    countries = js(url)
    # The upstream flag erroneously includes the Holy See, a UN observer.
    states = [c for c in countries if c["unMember"] and c["cca3"] != "VAT"]
    assert len(states) == 193
    uk = {
        "TUR": "Türkiye",
        "CIV": "Côte d’Ivoire",
        "CPV": "Cabo Verde",
        "SWZ": "Eswatini",
    }
    alternatives = {
        "GBR": ["UK", "Britain", "Great Britain"],
        "USA": ["US", "USA", "America"],
        "CZE": ["Czech Republic"],
        "TUR": ["Turkey"],
        "COD": ["DR Congo", "DRC"],
        "COG": ["Congo-Brazzaville"],
        "MMR": ["Burma"],
        "KOR": ["South Korea"],
        "PRK": ["North Korea"],
        "CIV": ["Ivory Coast"],
        "SWZ": ["Swaziland"],
        "TLS": ["East Timor"],
        "MKD": ["Macedonia"],
    }

    def country(c):
        code = c["cca3"]
        return record(
            uk.get(code, c["name"]["common"]),
            c["name"]["common"],
            c["name"]["official"],
            code,
            c["cca2"],
            *(s for native in c["name"].get("native", {}).values()
              for s in native.values() if s and len(s) <= 140),
            *(s for s in c.get("altSpellings", []) if len(s) <= 140),
            *(["Nippon", "Nihon", "日本"] if code == "JPN" else []),
            *alternatives.get(code, []),
        )

    def subset(key, title, prompt, items, field=country):
        add(
            "geography",
            key,
            title,
            prompt,
            url,
            [field(c) for c in items],
            "UN membership uses the 193 member states. Region, borders and capital fields follow the frozen Countries atlas. ISO codes and unambiguous alternate names count.",
        )

    subset("un", "The UN roll call", "Name a United Nations member state.", states)
    for key, region in [
        ("africa", "Africa"),
        ("asia", "Asia"),
        ("europe", "Europe"),
        ("americas", "Americas"),
        ("oceania", "Oceania"),
    ]:
        subset(
            key,
            region + " on the map",
            "Name a UN member state in "
            + ("the " if key == "americas" else "")
            + region
            + ".",
            [c for c in states if c["region"] == region],
        )
    subset(
        "landlocked",
        "No way to the sea",
        "Name a landlocked UN member state.",
        [c for c in states if c["landlocked"]],
    )
    subset(
        "no-borders",
        "No land neighbours",
        "Name a UN member state that shares no land border with another country.",
        [c for c in states if not c["borders"]],
    )
    subset(
        "one-neighbour",
        "One neighbour",
        "Name a UN member state that shares a land border with exactly one country.",
        [c for c in states if len(c["borders"]) == 1],
    )
    subset(
        "million-km",
        "A million square kilometres",
        "Name a UN member state whose area is at least one million square kilometres.",
        [c for c in states if c["area"] >= 1000000],
    )
    subset(
        "small",
        "Small countries",
        "Name a UN member state with an area under 30,000 square kilometres.",
        [c for c in states if c["area"] < 30000],
    )
    subset(
        "french",
        "Francophone world",
        "Name a UN member state where French is an official language.",
        [c for c in states if "fra" in c["languages"]],
    )
    subset(
        "equator",
        "Across the equator",
        "Name a UN member state whose land or territorial waters are crossed by the equator.",
        [
            c
            for c in states
            if c["cca3"]
            in "STP GAB COG COD UGA KEN SOM MDV IDN KIR ECU COL BRA".split()
        ],
    )
    cap_alias = {
        "Kyiv": ["Kiev"],
        "Beijing": ["Peking"],
        "Astana": ["Nur-Sultan"],
        "Rome": ["Roma"],
        "Vienna": ["Wien"],
        "Warsaw": ["Warszawa"],
        "Prague": ["Praha"],
        "Copenhagen": ["København"],
        "Lisbon": ["Lisboa"],
        "Athens": ["Athina"],
        "Mexico City": ["Ciudad de México"],
        "Washington, D.C.": ["Washington DC", "Washington"],
        "Ulaanbaatar": ["Ulan Bator"],
        "Brasília": ["Brasilia"],
    }
    for key, title, region in [
        ("capitals", "Capital cities", None),
        ("african-capitals", "Capitals of Africa", "Africa"),
    ]:
        add(
            "geography",
            key,
            title,
            "Name a capital city of "
            + ("an African " if region else "a ")
            + "UN member state.",
            url,
            [
                record(x, *cap_alias.get(x, []))
                for c in states
                if not region or c["region"] == region
                for x in c["capital"]
            ],
            "All capital entries for qualifying states in the source snapshot; joint capitals each count.",
        )
    add(
        "geography",
        "languages",
        "Languages on the map",
        "Name an official language of a UN member state.",
        url,
        [v for c in states for v in c["languages"].values()],
        "Repeated languages count once; uses the atlas’s language fields.",
    )
    cityurl = (
        "https://www.gov.uk/government/publications/list-of-cities/list-of-cities-html"
    )
    doc = soup(cityurl)
    cities = []
    for h in doc.select("h4"):
        if h.get_text(strip=True) in [
            "England",
            "Scotland",
            "Wales",
            "Northern Ireland",
        ]:
            cities.extend(
                clean(li.get_text(strip=True).replace("*", ""))
                for li in h.find_next("ul").select("li")
            )
    assert len(cities) == 76
    city_alias = {
        "Kingston-upon-Hull": ["Hull", "Kingston upon Hull"],
        "Londonderry": ["Derry"],
        "Newcastle-upon-Tyne": ["Newcastle"],
        "Brighton & Hove": ["Brighton"],
        "St Davids": ["St David's"],
    }
    add(
        "geography",
        "uk-cities",
        "City status",
        "Name a place in the UK that officially held city status by the end of 2022.",
        cityurl,
        [record(x, *city_alias.get(x, [])) for x in cities],
        "Includes the Platinum Jubilee awards. Either Bangor scores as Bangor. Crown Dependencies and Overseas Territories are outside this round.",
    )

    def sourced(key, title, prompt, page, idx, fn, coverage):
        rows = grid(tables(page)[idx])
        out = [v for r in rows if (v := fn(r))]
        add("geography", key, title, prompt, wiki(page), out, coverage)

    sourced(
        "counties",
        "Ceremonial counties",
        "Name a ceremonial county of England.",
        "List of English counties by area",
        0,
        lambda r: r[0] if len(r) > 2 and re.fullmatch(r"[\d,.]+", r[1]) else None,
        "All ceremonial counties in the location column, including the City of London.",
    )
    sourced(
        "departments",
        "French départements",
        "Name a French département, including overseas départements.",
        "List of French departments",
        0,
        lambda r: r[3] if re.fullmatch(r"\d{2,3}|2[AB]|69D", r[0]) else None,
        "All 101 départements. Lyon Metropolis has a separate territorial status.",
    )
    sourced(
        "prefectures",
        "Japan by prefecture",
        "Name one of Japan’s 47 prefectures.",
        "List of Japanese prefectures by area",
        0,
        lambda r: (
            record(r[1], r[2], r[1] + " Prefecture")
            if r[0].isdigit() and int(r[0]) <= 47
            else None
        ),
        "All 47 ranked prefectures, including Tokyo and Hokkaido. Japanese names also count.",
    )

    def ranked(r, col=1, limit=100):
        num = re.fullmatch(r"(\d+)\.?", r[0])
        if num and int(num[1]) <= limit:
            val = re.split(r"\s+\(", r[col])[0]
            bits = val.split(" / ")
            return record(bits[0], *bits[1:])

    sourced(
        "islands",
        "Island giants",
        "Name one of the world's 100 largest islands by area.",
        "List of islands by area",
        1,
        lambda r: (
            record(
                r[1] + " (" + r[4] + ")",
                "Prince of Wales Island (Alaska)"
                if r[4] == "United States"
                else "Canadian Prince of Wales Island",
            )
            if r[1] == "Prince of Wales Island"
            else record("Grande Terre (New Caledonia)", "Grande Terre")
            if r[1] == "Grande Terre"
            else r[1]
            if r[1] == "Grande Terre (Kerguelen)"
            else ranked(r)
        ),
        "The ranked islands table excludes continental landmasses. Use the main island name.",
    )
    sourced(
        "peaks",
        "A mountain above the rest",
        "Name one of the world’s 100 most prominent mountain peaks.",
        "List of mountain peaks by prominence",
        0,
        ranked,
        "Prominence measures a peak's rise above its key col. Alternative names in the ranked table count.",
    )
    sourced(
        "rivers",
        "Great river systems",
        "Name the main river in one of the world’s 100 longest river systems.",
        "List of rivers by length",
        1,
        lambda r: (
            re.split(r"\s+[–—]\s+|\s+\(", r[1])[0]
            if re.fullmatch(r"\d+\.?", r[0]) and int(r[0].rstrip(".")) <= 100
            else None
        ),
        "Uses the first named river in each ranked system; tributaries sharing a system do not automatically count.",
    )
    minimum_population = city_population_cutoff()
    sourced(
        "us-cities",
        "Big American cities",
        "Name one of the 100 largest US cities by estimated population in 2025.",
        "List of United States cities by population",
        1,
        lambda r: (
            r[0].split(" (")[0]
            if len(r) > 3
            and re.fullmatch(r"[\d,]+", r[2])
            and int(r[2].replace(",", "")) >= minimum_population
            else None
        ),
        "Top 100 by the 2025 municipal estimate; cities sharing a name score once.",
    )
    st = "List of capitals in the United States"
    state_rows = next(
        grid(t)
        for t in tables(st)
        if "State" in textcell(t.select_one("tr"))
        and "Capital" in textcell(t.select_one("tr"))
        and len(t.select("tr")) >= 51
    )
    hdr = state_rows[0]
    ix = hdr.index("State")
    ic = hdr.index("Capital")
    names = [
        r
        for r in state_rows[1:]
        if len(r) > ic
        and r[ix] != "State"
        and re.match(r"^[A-Z]", r[ix])
        and r[ic] != "Capital"
        and r[ix] not in ["United States", "Total"]
    ]
    assert len(set(r[ix] for r in names)) == 50
    for key, title, field in [
        ("states", "Stars and stripes", ix),
        ("state-capitals", "US state capitals", ic),
    ]:
        add(
            "geography",
            key,
            title,
            "Name "
            + (
                "one of the 50 US states."
                if field == ix
                else "the capital city of any of the 50 US states."
            ),
            wiki(st),
            [r[field] for r in names],
            "All 50 states. Federal districts and territories are outside this round.",
        )
    canada = next(
        q
        for q in json.loads((ROOT / "data/questions.json").read_text("utf8"))
        if q["id"] == "canada"
    )
    packs["geography"].append(canada)
    add(
        "geography",
        "germany",
        "On Germany’s border",
        "Name a country sharing a land border with Germany.",
        url,
        [country(c) for c in states if "DEU" in c["borders"]],
        "Nine land neighbours.",
    )
    atl = "List of countries and territories bordering the Atlantic Ocean"
    doc = soup(wiki(atl))
    names = set()
    for h in doc.select("h2,h3"):
        if h.get_text(strip=True) in [
            "Eurasia",
            "North and Baltic Seas",
            "Mediterranean and Black Seas",
            "Africa",
            "North America",
            "Caribbean",
            "South America",
        ]:
            for li in h.find_next("ul").find_all("li", recursive=False):
                a = li.find("a")
                if a:
                    names.add(norm(a.get_text(strip=True)))
    fixes = {
        "CPV": "Cape Verde",
        "GMB": "Gambia",
        "COD": "Democratic Republic of the Congo",
        "COG": "Republic of the Congo",
        "TUR": "Turkey",
        "CIV": "Ivory Coast",
        "GBR": "United Kingdom",
        "USA": "United States",
        "STP": "São Tomé and Príncipe",
    }
    atlrows = [
        country(c)
        for c in states
        if any(
            norm(n) in names
            for n in [
                c["name"]["common"],
                c["name"]["official"],
                fixes.get(c["cca3"], ""),
            ]
        )
    ]
    # Kaliningrad is listed separately in the Baltic section.
    if not any(a[0] == "Russia" for a in atlrows):
        atlrows.append(country(next(c for c in states if c["cca3"] == "RUS")))
    add(
        "geography",
        "atlantic",
        "Atlantic shores",
        "Name a UN member state with a coast on the Atlantic Ocean or one of its seas, such as the Mediterranean or Caribbean.",
        wiki(atl),
        atlrows,
        "Uses the regional coastal lists; overseas dependencies are not separate scoring countries. Russia qualifies through Kaliningrad.",
    )
    sourced(
        "lakes",
        "Great lakes of the world",
        "Name one of the world’s 47 largest lakes by surface area.",
        "List of lakes by area",
        1,
        lambda r: (
            record(r[2].split(" (")[0], "Lake " + r[2].split(" (")[0])
            if len(r) > 3 and re.fullmatch(r"\d+\.?", r[1])
            else None
        ),
        "Includes the Caspian Sea and every ranked entry in the main large-lakes table. Use the listed lake name.",
    )


def city_population_cutoff():
    rows = grid(tables("List of United States cities by population")[1])
    counts = sorted(
        (
            int(r[2].replace(",", ""))
            for r in rows
            if len(r) > 3 and re.fullmatch(r"[\d,]+", r[2])
        ),
        reverse=True,
    )
    return counts[99]
