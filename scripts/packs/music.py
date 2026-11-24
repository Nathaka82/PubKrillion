import re

from animals import wordnet_data, wordnet_rows
from common import add, csvrows, grid, record, soup, split, tables, textcell, wiki
from screen import OS, awards, title


def build():
    def q(k, t, p, u, vals, c, common="", rare=""):
        return add("music", k, t, p, u, vals, c, common, rare)

    euro = "https://raw.githubusercontent.com/rfordatascience/tidytuesday/main/data/2022/2022-05-17/eurovision.csv"
    entries = [
        r
        for r in csvrows(euro)
        if int(r["year"]) <= 2021 and r["section"] in ("grand-final", "final")
    ]
    for key, label, prompt, col, fn in [
        (
            "uk-eurovision",
            "The UK at Eurovision",
            "Name an act which represented the UK in a Eurovision final between 1957 and 2021.",
            "artist",
            lambda r: r["artist_country"] == "United Kingdom",
        ),
        (
            "winning-songs",
            "Douze points",
            "Name a Eurovision-winning song from 1956 through 2021.",
            "song",
            lambda r: r["winner"] == "TRUE",
        ),
        (
            "euro-countries",
            "On the Eurovision scoreboard",
            "Name a country that appeared in a Eurovision final from 1956 through 2021.",
            "artist_country",
            lambda r: True,
        ),
        (
            "euro-hosts",
            "Eurovision host cities",
            "Name a city which hosted a Eurovision final between 1956 and 2021.",
            "host_city",
            lambda r: True,
        ),
    ]:
        q(
            key,
            label,
            prompt,
            euro,
            [r[col] for r in entries if fn(r) and r[col] not in ("", "NA")],
            "Eurovision results archive. All four joint winners in 1969 count in the winners round; cancelled contests are excluded.",
        )
    for decade, field, desc in [
        (1980, 2, "a number-one single"),
        (1990, 1, "a credited number-one act"),
        (2000, 2, "a number-one single"),
        (
            2010,
            2,
            "a single that spent at least four weeks at number one in one chart run",
        ),
    ]:
        page = f"List of UK singles chart number ones of the {decade}s"
        rs = grid(tables(page)[1])
        vals = []
        for r in rs[1:]:
            if len(r) < 6 or r[0] == r[1] or not re.search(r"\d{4}", r[4]):
                continue
            if decade == 2010 and (
                not re.search(r"\d+", r[5]) or int(re.search(r"\d+", r[5])[0]) < 4
            ):
                continue
            val = title(r[field])
            if field == 1:
                # Joint billings count together; allow individual explicitly credited featured artists.
                vals.append(record(val, *re.split(r"\s+featuring\s+", val, flags=re.I)))
            else:
                vals.append(
                    record(val, *[title(v) for v in val.split('" / "')])
                    if '" / "' in val
                    else val
                )
        q(
            "charts-" + str(decade),
            "UK number ones · " + str(decade) + "s",
            "Name "
            + desc
            + " in the UK Singles Chart during the "
            + str(decade)
            + "s.",
            wiki(page),
            vals,
            "Every qualifying entry in the decade's number-one table. Repeat runs of the same entry score once.",
        )
    for key, act, idx in [
        ("beatles", "the Beatles", 1),
        ("abba", "ABBA", 1),
        ("bowie", "David Bowie", 1),
        ("queen", "Queen", 1),
        ("adele", "Adele", 1),
        ("rihanna", "Rihanna", 0),
    ]:
        page = "List of songs recorded by " + act
        rs = grid(tables(page)[idx])
        hdr = rs[0]
        year = next((i for i, s in enumerate(hdr) if s.startswith("Year")), None)
        vals = [
            title(r[0])
            for r in rs[1:]
            if len(r) > 2
            and r[0]
            and r[0] != r[1]
            and (
                year is None
                or year < len(r)
                and re.search(r"\d{4}", r[year])
                and int(re.search(r"\d{4}", r[year])[0]) <= 2025
            )
        ]
        q(
            key,
            act.title() if key == "beatles" else act,
            "Name a song in the main recordings table for "
            + act
            + " released by the end of 2025.",
            wiki(page),
            vals,
            "The main song table supplies the complete scope. Separate archival, guest-only and unreleased-song tables are outside this round.",
        )
    page = "Glastonbury Festival line-ups"
    vals = []
    years = set()
    for t in tables(page):
        stage = t.find_previous(["h2", "h3", "h4", "h5"])
        yr = t.find_previous("h2")
        if not stage or not yr or not re.fullmatch(r"\d{4}", yr.get_text(strip=True)):
            continue
        year = int(yr.get_text(strip=True))
        if (
            not 2009 <= year <= 2025
            or stage.get_text(" ", strip=True) != "Pyramid Stage"
        ):
            continue
        for day in t.select("tr td"):
            if li := day.select_one("li"):
                name = re.sub(r"\s*\(replaced.*", "", textcell(li), flags=re.I)
                alias = {
                    "Rolling Stones": ["The Rolling Stones"],
                    "Bruce Springsteen & the E Street Band": ["Bruce Springsteen"],
                    "Neil Young and the Chrome Hearts": ["Neil Young"],
                    "Florence and the Machine": ["Florence + the Machine"],
                }.get(name, [])
                vals.append(record(name, *alias))
        years.add(year)
    assert len(years) == 13
    q(
        "glastonbury",
        "At the Pyramid Stage",
        "Name a Friday, Saturday or Sunday Pyramid Stage headliner at Glastonbury from 2009 through 2025.",
        wiki(page),
        vals,
        "Top billing on each of the three main days of the 13 held festivals. Replacement headliners count; cancelled performances do not.",
    )
    page = "Mercury Prize"
    winners = [
        re.split(r"\s+[–—]\s+", r[1], maxsplit=1)
        for r in grid(tables(page)[0])[1:]
        if re.match(r"^\d{4}", r[0]) and int(r[0][:4]) <= 2025
    ]
    for field, what in [(1, "album"), (0, "act")]:
        q(
            "mercury-" + what,
            "Mercury-winning " + ("albums" if field else "artists"),
            "Name "
            + ("an album" if field else "an act")
            + " that won the Mercury Prize from 1992 through 2025.",
            wiki(page),
            [w[field] for w in winners if len(w) == 2],
            "All winners in the annual prize table; collaborations use the credited name.",
        )
    page = "List of Rock and Roll Hall of Fame inductees"
    q(
        "hall",
        "Rock & Roll Hall of Fame",
        "Name an act inducted in the Hall of Fame’s Performers category through 2025.",
        wiki(page),
        [
            title(r[2])
            for r in grid(tables(page)[0])
            if len(r) > 3 and r[0].isdigit() and int(r[0]) <= 2025
        ],
        "Performer inductees only. A band is one scoring answer.",
    )
    page = "List of best-selling albums in the United Kingdom"
    q(
        "best-sellers",
        "Multi-platinum in Britain",
        "Name an album in the source’s ranked list of UK best-selling albums.",
        wiki(page),
        [
            title(r[1])
            for r in grid(tables(page)[0])
            if re.search(r"\d", r[0]) and len(r) > 3 and r[0] != r[1]
        ],
        "Every ranked album in the archived table; aliases and reissues of the same album share a score.",
    )
    page = "James Bond music"
    table = tables(page)[1]
    # Separate track names from editorial notes before flattening the table.
    arrangements = {}
    for note in table.select("td small"):
        cell = note.find_parent("td")
        description = textcell(note)
        note.decompose()
        if description.startswith("Arrangement of"):
            arrangements[title(textcell(cell))] = title(
                description.removeprefix("Arrangement of").strip()
            )
    rows = [
        r for r in grid(table) if len(r) >= 6 and r[1].isdigit() and int(r[1]) <= 2021
    ]
    q(
        "bond-music",
        "The sound of Bond",
        "Name an opening title song or instrumental listed for an Eon Bond film through No Time to Die.",
        wiki(page),
        [
            record(
                title(r[3]),
                arrangements.get(title(r[3]), ""),
                *(
                    [
                        title(part)
                        for part in title(r[3])
                        .removeprefix("Opening Titles:")
                        .split("/")
                    ]
                    if title(r[3]).startswith("Opening Titles:")
                    else []
                ),
            )
            for r in rows
        ],
        "Title song column for the Eon series; end-credit songs listed separately do not count.",
    )
    archive = awards()
    for k, t, cat, col in [
        ("oscar-songs", "Oscar-winning songs", "MUSIC (Original Song)", "Detail"),
        ("score", "Oscar-winning composers", "MUSIC (Original Score)", "Nominees"),
    ]:
        vals = [
            title(n)
            for r in archive
            if int(r["Ceremony"]) <= 97
            and r["CanonicalCategory"] == cat
            and r["Winner"] == "True"
            for n in split(r[col] or "")
        ]
        q(
            k,
            t,
            "Name "
            + (
                "an Original Song winner"
                if k == "oscar-songs"
                else "a credited Original Score winner"
            )
            + " at the Academy Awards through the 97th ceremony (2025).",
            OS,
            vals,
            "Winners in the database’s canonical category. Each individually credited composer counts.",
        )
    for key, root, desc in [
        ("strings", "stringed_instrument", "stringed instrument"),
        ("winds", "wind_instrument", "wind instrument"),
        ("percussion", "percussion_instrument", "percussion instrument"),
        ("styles", "musical_style", "musical style"),
    ]:
        q(
            key,
            desc.capitalize() + "s",
            "Name a kind of " + desc + " in WordNet’s English music classification.",
            "https://wordnet.princeton.edu/",
            wordnet_rows(root, 0),
            "Includes every named subtype of the primary WordNet 3.1 noun sense. Synonyms share a score.",
        )
    page = "Grammy Award for Album of the Year"
    vals = []
    for t in tables(page):
        rs = grid(t)
        if rs[0][:3] != ["Year", "Album", "Artist(s)"]:
            continue
        for r in rs[1:]:
            if (
                len(r) > 2
                and (yr := re.search(r"\d{4}", r[0]))
                and 2000 <= int(yr[0]) <= 2025
                and r[1] != r[0]
            ):
                vals.append(title(r[1]))
    q(
        "grammys",
        "Album of the Year",
        "Name a Grammy Album of the Year nominee at the 2000–2025 ceremonies.",
        wiki(page),
        vals,
        "All winners and fellow nominees, grouped by ceremony year.",
    )
    for k, t, p, source, raw in [
        (
            "planets",
            "Holst’s planets",
            "Name a planet with a movement in Gustav Holst’s orchestral suite The Planets.",
            wiki("The Planets"),
            "Mars|Venus|Mercury|Jupiter|Saturn|Uranus|Neptune",
        ),
        (
            "orchestra",
            "Orchestra families",
            "Name one of the four main instrument families in a symphony orchestra.",
            "https://philharmonia.co.uk/resources/instruments/",
            "Strings|Woodwind|Brass|Percussion",
        ),
        (
            "abba-albums",
            "ABBA studio albums",
            "Name one of ABBA’s original nine studio albums through Voyage.",
            wiki("ABBA discography"),
            "Ring Ring|Waterloo|ABBA|Arrival|ABBA: The Album|Voulez-Vous|Super Trouper|The Visitors|Voyage",
        ),
    ]:
        al = {
            "Strings": ["String", "String section"],
            "Woodwind": ["Woodwinds"],
            "ABBA: The Album": ["The Album"],
        }
        rows = [record(x, *al.get(x, [])) for x in split(raw)]
        if k == "orchestra":
            families = {
                "Strings": "stringed_instrument",
                "Brass": "brass_instrument",
                "Woodwind": "woodwind",
                "Percussion": "percussion_instrument",
            }
            senses = wordnet_data()[1]
            rows = [
                (name, aliases, senses[families[name]][0]) for name, aliases in rows
            ]
        question = q(
            k,
            t,
            p,
            source,
            rows,
            "All entries under the scope written in the prompt.",
        )
        if k == "orchestra":
            # The orchestra's instrument guide resolves terms like viola in context.
            by_family = {a["name"]: a for a in question["answers"]}
            for heading in soup(source).select("section h2"):
                family = heading.get_text(strip=True)
                if family in by_family:
                    names = [
                        h.get_text(" ", strip=True)
                        for h in heading.find_parent("section").select("h4")
                    ]
                    by_family[family]["examples"] = [n for n in names if n != family]
            assert all(a.get("examples") for a in question["answers"])
