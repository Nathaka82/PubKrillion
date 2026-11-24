import csv
import gzip
import io
import json
import re
import zipfile

from common import (
    ROOT,
    add,
    fetch,
    grid,
    js,
    packs,
    record,
    soup,
    tables,
    textcell,
    wiki,
)
from screen import person

PARIS = "https://www.kaggle.com/api/v1/datasets/download/piterfm/paris-2024-olympic-summer-games"


def build():
    def q(k, t, p, url, rows, c, common="", rare=""):
        return add("general", k, t, p, url, rows, c, common, rare)

    elements = "https://raw.githubusercontent.com/Bowserinator/Periodic-Table-JSON/master/PeriodicTableJSON.json"
    q(
        "elements",
        "The entire periodic table",
        "Name a chemical element, or give its symbol.",
        elements,
        [
            record(
                {"Aluminum": "Aluminium", "Cesium": "Caesium"}.get(
                    r["name"], r["name"]
                ),
                r["name"],
                r["symbol"],
                *(["Sulphur"] if r["symbol"] == "S" else []),
            )
            for r in js(elements)["elements"]
            if 1 <= r["number"] <= 118
        ],
        "Every element from hydrogen to oganesson. Element symbols count.",
        "Gold|Silver|Oxygen|Hydrogen|Iron|Carbon",
        "Darmstadtium|Meitnerium|Tennessine",
    )
    page = "List of constellations by area"
    q(
        "stars",
        "Connect the stars",
        "Name one of the 88 modern constellations.",
        wiki(page),
        [record(r[2], r[1]) for r in grid(tables(page)[0]) if r[0].isdigit()],
        "All 88 ranked constellations; the three-letter abbreviations also count.",
        "Orion|Leo|Gemini|Taurus|Aries",
    )
    archive = zipfile.ZipFile(io.BytesIO(fetch(PARIS)))

    def paris(file):
        return list(csv.DictReader(io.StringIO(archive.read(file).decode("utf-8-sig"))))

    events = [
        dict(sport=d, event=e)
        for d, e in sorted({(r["discipline"], r["event"]) for r in paris("medals.csv")})
    ]
    assert len(events) == 329
    q(
        "paris-events",
        "The Paris programme",
        "Name a medal event at the Paris 2024 Summer Olympics. Include the discipline or gender where needed.",
        PARIS,
        [
            record(
                r["sport"] + " – " + r["event"],
                r["event"],
                r["event"] + " " + r["sport"],
                r["sport"] + " " + r["event"],
            )
            for r in events
        ],
        "All 329 medal events from the published results. In weight classes, + and − mean over and under. An event name on its own counts where unambiguous.",
    )
    venues = paris("venues.csv")
    q(
        "paris-venues",
        "Where Paris played",
        "Name a venue in the Paris 2024 Olympic venues file.",
        PARIS,
        [r["venue"] for r in venues],
        "Every named venue in the Paris 2024 venue index, including venues outside Paris.",
    )
    nb = "https://api.nobelprize.org/2.1/nobelPrizes?limit=1000"
    prizes = js(nb)["nobelPrizes"]
    for k, cat, lo in [
        ("literature", "Literature", 1901),
        ("peace", "Peace", 1901),
        ("physics", "Physics", 2000),
    ]:
        vals = []
        for prize in prizes:
            if (
                prize["category"]["en"] != cat
                or not lo <= int(prize["awardYear"]) <= 2024
            ):
                continue
            for v in prize.get("laureates", []):
                name = (v.get("knownName") or v.get("orgName") or {}).get("en")
                if name:
                    vals.append(
                        record(
                            name,
                            (v.get("fullName") or {}).get("en"),
                            *(person(name)[1] if "orgName" not in v else []),
                        )
                    )
        q(
            "nobel-" + k,
            "Nobel " + cat,
            "Name a Nobel " + cat + " laureate from " + str(lo) + " through 2024.",
            nb,
            vals,
            "Every awarded laureate in the Nobel Foundation API for the category and period; joint recipients are separate answers.",
        )
    page = "Booker Prize"
    br = [r for r in grid(tables(page)[0]) if r[0].isdigit() and int(r[0]) <= 2025]
    q(
        "booker",
        "The Booker bookshelf",
        "Name a Booker Prize-winning novel from 1969 through 2025.",
        wiki(page),
        [r[2] for r in br],
        "All winning novels, including joint winners, in the main prize table.",
    )
    page = "List of presidents of the United States"
    q(
        "presidents",
        "In the White House",
        "Name a person who served as US president up to the end of 2025.",
        wiki(page),
        [
            person(re.split(r"\s+\(", r[2])[0])
            for r in grid(tables(page)[0])
            if r[0].isdigit()
        ],
        "Each individual counts once, including presidents with non-consecutive terms.",
        "George Washington|Abraham Lincoln|Barack Obama",
    )
    page = "List of prime ministers of the United Kingdom"
    vals = []
    for tr in tables(page)[0].select("tr"):
        # The first linked name in the office-holder cell omits constituencies and life dates.
        for cell in tr.find_all(["th", "td"], recursive=False):
            a = cell.select_one("a[rel~=mw\\:WikiLink]")
            if a and (
                "MP for" in textcell(cell)
                or re.search(r"\(\d{4}[\s–-]", textcell(cell))
            ):
                name = a.get_text(" ", strip=True)
                if vals and vals[-1][0] == "Keir Starmer":
                    break
                name, aliases = person(
                    {"Lord John Russell": "John Russell"}.get(name, name)
                )
                for link in cell.select("a"):
                    text = link.get_text(" ", strip=True)
                    if re.search(r"\b(Earl|Duke|Viscount|Baron|Marquess|Lord)\b", text):
                        aliases.extend(
                            [text, re.sub(r"^\d+(?:st|th|nd|rd) ", "", text)]
                        )
                vals.append(record(name, *aliases))
                break
    q(
        "prime-ministers",
        "Number Ten",
        "Name a British prime minister in the main list from Robert Walpole to Keir Starmer.",
        wiki(page),
        vals,
        "Distinct office-holders in the principal chronological table. Repeat ministries count once.",
    )
    page = "List of Formula One World Drivers' Champions"
    q(
        "f1",
        "Formula One champions",
        "Name a Formula One World Drivers’ Champion through the 2025 season.",
        wiki(page),
        [
            person(r[1])
            for r in grid(tables(page)[1])
            if r[0].isdigit() and int(r[0]) <= 2025 and r[1]
        ],
        "All championship seasons from 1950 to 2025, deduplicated by driver.",
        "Lewis Hamilton|Michael Schumacher|Max Verstappen",
    )
    page = "List of Wimbledon gentlemen's singles champions"
    q(
        "wimbledon",
        "Centre Court",
        "Name a men's singles finalist at Wimbledon in the Open Era, 1968–2025.",
        wiki(page),
        [
            person(re.sub(r"\s*\([^)]*\)", "", r[i]))
            for r in grid(tables(page)[2])
            if r[0].isdigit()
            and 1968 <= int(r[0]) <= 2025
            and len(r) > 5
            and "competition" not in r[2].lower()
            for i in (2, 4)
            if r[i] not in ("", "—")
        ],
        "Both champion and runner-up at each completed championship.",
        "Roger Federer|Andy Murray|Rafael Nadal|Novak Djokovic",
    )
    page = "List of chess grandmasters"
    q(
        "chess",
        "Grandmasters at the board",
        "Name a listed chess grandmaster born before 1950.",
        wiki(page),
        [
            person(" ".join(reversed(r[0].split(", ", 1))))
            for r in grid(tables(page)[0])
            if len(r) > 3 and re.match(r"^(18|19)\d\d", r[2]) and int(r[2][:4]) < 1950
        ],
        "Grandmaster title-holders in the main list with a recorded birth year before 1950. Honorary-only and revoked lists are separate.",
    )
    url = "https://iba-world.com/cocktails/all-cocktails/"
    directory = soup(url)
    pages = max(
        int(x.text) for x in directory.select(".page-numbers") if x.text.isdigit()
    )
    drinks = [
        x.get_text(strip=True)
        for i in range(1, pages + 1)
        for x in (directory if i == 1 else soup(url + f"page/{i}/")).select(
            'a[href*="/iba-cocktail/"] h2'
        )
    ]
    q(
        "cocktails",
        "Behind the bar",
        "Name a cocktail in the International Bartenders Association’s official cocktail directory.",
        url,
        drinks,
        "Every drink card in the IBA directory snapshot across all three categories.",
        "Negroni|Mojito|Old Fashioned|Daiquiri|Margarita",
    )
    page = "List of national parks of the United States"
    q(
        "parks",
        "America’s national parks",
        "Name a US national park in the source’s designated national parks list.",
        wiki(page),
        [
            record(title_clean(r[0]), title_clean(r[0]) + " National Park")
            for r in grid(tables(page)[0])
            if len(r) > 5 and re.search(r"\d{4}", r[3])
        ],
        "Main designated national parks table, including parks in US territories.",
        "Yellowstone|Yosemite|Grand Canyon",
    )
    page = "List of World Heritage Sites in the United Kingdom"
    q(
        "heritage",
        "World heritage",
        "Name an inscribed World Heritage Site in the UK and its overseas territories from the source list.",
        wiki(page),
        [
            title_clean(r[0])
            for r in grid(tables(page)[0])
            if len(r) > 4 and re.search(r"\d{4}", r[3])
        ],
        "Inscribed sites in the main list; tentative and delisted sites have separate tables.",
    )
    page = "List of books of the King James Version"
    books = [r[0] for t in [tables(page)[0], tables(page)[2]] for r in grid(t)[1:]]

    def bible_book(name):
        variants = {
            "The Acts": ["Acts", "Acts of the Apostles"],
            "Song of Solomon": ["Song of Songs", "Canticles"],
            "Revelation": ["Book of Revelation", "Apocalypse"],
        }.get(name, [])
        if name[0].isdigit():
            variants += [
                prefix + name[1:]
                for prefix in {
                    "1": ["First", "I"],
                    "2": ["Second", "II"],
                    "3": ["Third", "III"],
                }[name[0]]
            ]
        return record(name, *variants)

    q(
        "bible",
        "Books of the Bible",
        "Name one of the 66 books in the Protestant Old or New Testament.",
        wiki(page),
        map(bible_book, books),
        "All 39 Old Testament books and 27 New Testament books in the King James Version.",
    )
    sample = json.loads((ROOT / "data/pub-sampler.json").read_text("utf8"))[
        "questions"
    ][1]
    q(
        "currencies",
        "Foreign exchange",
        sample["prompt"],
        sample["source"],
        [record(a["name"], *a["aliases"]) for a in sample["answers"]],
        "The 162 entries in the archived en-GB currency list. Names and three-letter codes count.",
    )
    seed = json.loads((ROOT / "data/questions.json").read_text("utf8"))
    for key in ["tube-lines", "moonwalkers", "allergens", "greek", "national-parks"]:
        s = next(s for s in seed if s["id"] == key)
        packs["general"].append(s)
    url = "https://cdn.rebrickable.com/media/downloads/themes.csv.gz"
    rows = list(
        csv.DictReader(io.StringIO(gzip.decompress(fetch(url)).decode("utf-8-sig")))
    )
    q(
        "lego",
        "Brick by brick",
        "Name a top-level LEGO theme in the Rebrickable theme index.",
        url,
        [r["name"] for r in rows if not r["parent_id"]],
        "Top-level themes have no parent ID. Subthemes alone are outside the scope.",
        "Star Wars|Technic|City|Friends",
    )
    page = "List of constellations by area"
    # Further closed lists cover books, food, chemistry, sport and language.
    q(
        "booker-authors",
        "Booker-winning authors",
        "Name a writer who won the main Booker Prize by the end of 2025.",
        wiki("Booker Prize"),
        [person(r[1]) for r in br],
        "All winners in the main Booker Prize table, including the two joint awards.",
    )
    page = "List of FIFA World Cup finals"
    q(
        "world-final",
        "The World Cup final",
        "Name a national side which reached the men's FIFA World Cup final from 1930 to 2022.",
        wiki(page),
        [
            r[i]
            for r in grid(tables(page)[1])
            if re.fullmatch(r"\d{4}", r[0]) and int(r[0]) <= 2022
            for i in (1, 3)
        ],
        "Both finalists in each tournament. The 1950 deciding match counts.",
    )
    from animals import wordnet_rows

    for key, label, lemma in [
        ("cheese", "Cheese board", "cheese"),
        ("pasta", "Pasta shapes", "pasta"),
        ("tools", "In the toolbox", "hand_tool"),
        ("gemstones", "Precious stones", "gem"),
    ]:
        vals = wordnet_rows(
            lemma, {"cheese": 0, "pasta": 1, "hand_tool": 0, "gem": 1}[lemma]
        )
        q(
            key,
            label,
            "Name a kind of "
            + {
                "cheese": "cheese",
                "pasta": "pasta",
                "tools": "hand tool",
                "gemstones": "gemstone",
            }[key]
            + " in WordNet’s English classification.",
            "https://wordnet.princeton.edu/",
            vals,
            "All named subtypes under the selected dictionary noun sense. Synonyms count together.",
        )


def title_clean(s):
    return s.strip(" *†‡◇♦")
