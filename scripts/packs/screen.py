import csv
import io
import re
from urllib.parse import quote

from common import add, clean, fetch, grid, js, record, split, tables, textcell, wiki

OS = "https://raw.githubusercontent.com/DLu/oscar_data/main/oscars.csv"


def awards():
    return list(
        csv.DictReader(io.StringIO(fetch(OS).decode("utf-8-sig")), delimiter="\t")
    )


def title(s):
    s = clean(s).strip(' "“”†‡*#').strip()
    s = {
        "÷": "Divide",
        "×": "Multiply",
        "+": "Plus",
        "=": "Equals",
        "−": "Subtract",
    }.get(s, s)
    s = re.sub(r"\s+([,!?;:])", r"\1", s)
    return s


def film(name):
    us = title(name)
    uk = {
        "My Neighbor Totoro": "My Neighbour Totoro",
        "The Avengers": "Avengers Assemble",
        "Zootopia": "Zootropolis",
        "Zootopia 2": "Zootropolis 2",
        "Moana": "Moana",
        "The Pirates! Band of Misfits": "The Pirates! In an Adventure with Scientists!",
        "Harry Potter and the Sorcerer's Stone": "Harry Potter and the Philosopher's Stone",
        "Ford v Ferrari": "Le Mans '66",
        "The Great Mouse Detective": "Basil the Great Mouse Detective",
        "Pirate Radio": "The Boat That Rocked",
    }
    return record(uk.get(us, us), us)


def person(s):
    s = title(s)
    words = s.split()
    if len(words) < 2 or any(w.lower() in {"and", "&", "featuring"} for w in words):
        return record(s)
    end = len(words)
    if end >= 4 and words[-2].lower() == "the":
        end -= 2
    elif words[-1].rstrip(".,").lower() in {"jr", "sr", "ii", "iii", "iv"}:
        end -= 1
    start = end - 1
    while start > 1 and words[start - 1].lower() in {
        "de",
        "du",
        "del",
        "la",
        "van",
        "von",
        "der",
        "den",
        "da",
        "di",
        "dos",
        "al",
    }:
        start -= 1
    surname = " ".join(words[start:end]).rstrip(",")
    if len(surname) < 2 or not any(c.isalpha() for c in surname):
        return record(s)
    return record(s, surname, " ".join(words[start:]))


def build():
    rows = awards()
    specs = [
        (
            "best-picture",
            "Best Picture nominees",
            "a film nominated for Best Picture",
            "BEST PICTURE",
            "Film",
            False,
            72,
            97,
        ),
        (
            "directors",
            "In the director’s chair",
            "a Best Director winner",
            "DIRECTING",
            "Nominees",
            True,
            1,
            97,
        ),
        (
            "leading-ladies",
            "Leading performances",
            "a Best Actress winner",
            "ACTRESS IN A LEADING ROLE",
            "Nominees",
            True,
            1,
            97,
        ),
        (
            "supporting",
            "Supporting actors",
            "a Best Supporting Actor winner",
            "ACTOR IN A SUPPORTING ROLE",
            "Nominees",
            True,
            9,
            97,
        ),
        (
            "animation",
            "Oscar animation",
            "a film nominated for Best Animated Feature",
            "ANIMATED FEATURE FILM",
            "Film",
            False,
            74,
            97,
        ),
        (
            "international",
            "World cinema",
            "a film that won Best International Feature Film, formerly Best Foreign Language Film,",
            "INTERNATIONAL FEATURE FILM",
            "Film",
            True,
            29,
            97,
        ),
        (
            "docs",
            "True stories",
            "a film nominated for Best Documentary Feature",
            "DOCUMENTARY (Feature)",
            "Film",
            False,
            72,
            97,
        ),
        (
            "vfx",
            "Movie magic",
            "a film that won Best Visual Effects",
            "VISUAL EFFECTS",
            "Film",
            True,
            50,
            97,
        ),
        (
            "costumes",
            "Dressed for the Oscars",
            "a film nominated for Costume Design",
            "COSTUME DESIGN",
            "Film",
            False,
            72,
            97,
        ),
    ]
    ceremony_year = {1: 1929, 9: 1937, 29: 1957, 50: 1978, 72: 2000, 74: 2002}
    for key, label, what, cat, col, won, lo, hi in specs:
        vals = [
            v
            for r in rows
            if lo <= int(r["Ceremony"]) <= hi
            and r["CanonicalCategory"] == cat
            and (not won or r["Winner"] == "True")
            for v in split(r[col] or "")
        ]
        add(
            "screen",
            key,
            label,
            "Name "
            + what
            + f" at the Oscars between {ceremony_year[lo]} and 2025.",
            OS,
            [person(v) if col == "Nominees" else film(v) for v in vals],
            "All qualifying competitive records in the Academy database transcription. Ceremony numbers refer to presentation years, not a film’s release year.",
        )
    for key, studio, page, idx, n, d in [
        (
            "disney",
            "Walt Disney Animation Studios",
            "List of Walt Disney Animation Studios films",
            0,
            0,
            1,
        ),
        ("pixar", "Pixar", "List of Pixar films", 0, 1, 2),
        (
            "dreamworks",
            "DreamWorks Animation",
            "List of DreamWorks Animation productions",
            0,
            0,
            1,
        ),
    ]:
        films = [
            film(r[n])
            for r in grid(tables(page)[idx])
            if len(r) > d
            and (y := re.search(r"\b(19\d\d|20\d\d)\b", r[d]))
            and int(y[1]) <= 2025
            and r[n] != r[d]
        ]
        add(
            "screen",
            key,
            studio + " features",
            "Name a "
            + studio
            + " feature film released by the end of 2025.",
            wiki(page),
            films,
            "Uses the main feature-film table and its release dates. Future releases and television spin-offs are excluded.",
            "Toy Story|Shrek|Frozen|The Lion King|Finding Nemo",
        )
    page = "List of James Bond films"
    bond = grid(tables(page)[0])
    add(
        "screen",
        "bond",
        "Bond on the big screen",
        "Name an Eon-produced James Bond film released through No Time to Die.",
        wiki(page),
        [
            film(r[0])
            for r in bond
            if len(r) > 1 and re.fullmatch(r"\d{4}", r[1]) and int(r[1]) <= 2021
        ],
        "All 25 Eon series films through 2021; unofficial adaptations have separate credits.",
    )
    page = "List of Marvel Cinematic Universe films"
    movies = []
    for t in tables(page):
        hdr = textcell(t.select_one("tr"))
        if not hdr.startswith("Film U.S. release date"):
            continue
        for r in grid(t)[1:]:
            if (
                len(r) > 1
                and (y := re.search(r"\b(20\d\d)\b", r[1]))
                and int(y[1]) <= 2025
            ):
                movies.append(film(r[0]))
    add(
        "screen",
        "marvel",
        "The Marvel Cinematic Universe",
        "Name a Marvel Cinematic Universe feature film released by the end of 2025.",
        wiki(page),
        movies,
        "Uses the US theatrical dates in the phase tables. Series and unrelated Marvel adaptations are excluded.",
    )
    page = "List of Studio Ghibli works"
    add(
        "screen",
        "ghibli",
        "Studio Ghibli at the cinema",
        "Name a Studio Ghibli feature film released by the end of 2023.",
        wiki(page),
        [
            film(r[1])
            for r in grid(tables(page)[0])
            if re.fullmatch(r"\d{4}", r[0]) and int(r[0]) <= 2023
        ],
        "Main feature table including co-productions; TV and short-film tables are separate.",
    )
    page = "List of Blue Peter presenters"
    vals = [
        person(r[1])
        for t in tables(page)
        for r in grid(t)
        if r[0].isdigit()
        and len(r) > 2
        and (y := re.search(r"\d{4}", r[2]))
        and int(y[0]) <= 2025
    ]
    add(
        "screen",
        "blue-peter",
        "Here’s one I made earlier",
        "Name a regular Blue Peter presenter who joined the show by the end of 2025.",
        wiki(page),
        vals,
        "All qualifying numbered presenters in the past and present tables.",
    )
    page = "List of The Great British Bake Off contestants"
    vals = [
        person(r[0])
        for t in tables(page)
        for r in grid(t)
        if len(r) > 7
        and r[1].isdigit()
        and (m := re.fullmatch(r"Series (\d+)", r[-1]))
        and int(m[1]) <= 15
    ]
    add(
        "screen",
        "bake-off",
        "In the Bake Off tent",
        "Name a baker who competed in one of the first 15 series of The Great British Bake Off.",
        wiki(page),
        vals,
        "Ages and series identify competition rows. Holiday specials and celebrity spin-offs are separate.",
    )
    page = "List of Strictly Come Dancing contestants"
    vals = [
        person(r[0])
        for r in grid(tables(page)[1])
        if len(r) > 3 and r[4].isdigit() and int(r[4]) <= 22
    ]
    add(
        "screen",
        "strictly",
        "On the Strictly dance floor",
        "Name a celebrity who competed in one of the first 22 series of the UK’s Strictly Come Dancing.",
        wiki(page),
        vals,
        "Celebrity contestants in the main-series table; excludes Christmas specials and professional partners.",
    )
    for key, name, what in [
        ("inside-nine", "Inside No. 9", "episode"),
        ("black-mirror", "Black Mirror", "episode"),
        ("red-dwarf", "Red Dwarf", "episode"),
        ("who", "Doctor Who", "episode"),
        ("friends", "Friends", "character"),
        ("office", "The Office", "character"),
        ("simpsons", "The Simpsons", "character"),
        ("buffy", "Buffy the Vampire Slayer", "character"),
    ]:
        show = js("https://api.tvmaze.com/singlesearch/shows?q=" + quote(name))
        if key == "who":
            show = js("https://api.tvmaze.com/shows/210")
        if key == "office":
            show = js("https://api.tvmaze.com/shows/526")  # US adaptation
        addr = (
            "https://api.tvmaze.com/shows/"
            + str(show["id"])
            + ("/episodes" if what == "episode" else "/cast")
        )
        records = js(addr)
        label = show["name"] + (
            " (US)" if key == "office" else " (2005)" if key == "who" else ""
        )
        if what == "episode":
            vals = [
                title(r["name"])
                for r in records
                if r["type"] == "regular"
                and r["airdate"]
                and r["airdate"] <= "2025-12-31"
            ]
            prompt = "Name a regular episode of " + label + " aired by the end of 2025."
            scope = "All regular episodes in TVmaze's episode index for this series. Specials are separate."
        else:
            vals = [r["character"]["name"] for r in records]
            prompt = (
                "Name a character credited to a main-cast performer in "
                + label
                + "."
            )
            scope = "All character credits in the archived main-cast endpoint; guest appearances have a separate list."
        add("screen", key, label, prompt, addr, vals, scope)
    page = "British Academy Television Award for Best Drama Series"
    vals = []
    for t in tables(page):
        rs = grid(t)
        if "Title" not in rs[0] or "Year" not in rs[0]:
            continue
        idx = rs[0].index("Title")
        vals.extend(
            title(r[idx])
            for r in rs[1:]
            if len(r) > idx
            and (yr := re.search(r"\d{4}", r[0]))
            and 2000 <= int(yr[0]) <= 2025
            and r[idx]
            and r[idx] != "Title"
        )
    add(
        "screen",
        "bafta-tv",
        "BAFTA television drama",
        "Name a Best Drama Series nominee at the BAFTA Television Awards from 2000 through 2025.",
        wiki(page),
        vals,
        "Includes winners and fellow nominees in the series award's year tables.",
    )
    # Finite cast lists from the show's own credits as indexed by TVmaze.
    for key, name in [
        ("thrones", "Game of Thrones"),
        ("crown", "The Crown"),
        ("lost", "Lost"),
    ]:
        show = js("https://api.tvmaze.com/singlesearch/shows?q=" + quote(name))
        url = "https://api.tvmaze.com/shows/" + str(show["id"]) + "/cast"
        add(
            "screen",
            key,
            "The cast of " + name,
            "Name an actor in the main cast of " + name + ".",
            url,
            [person(r["person"]["name"]) for r in js(url)],
            "Every credited performer in the archived main cast, including actors sharing a role; guest-only credits are separate.",
        )
