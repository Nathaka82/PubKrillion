"""Compile sourced names and directional class links; no live lookup during a game."""

import re

from common import fetch, norm, soup, textcell, wiki

CONTROLLERS = (
    "https://www.wikidata.org/wiki/Wikidata:WikiProject_Video_games/Lists/Controllers"
)
APPLE = "https://support.apple.com/en-gb/108044"


def build():
    edges = set()

    def relates(child, parent):
        if norm(child) != norm(parent):
            edges.add((norm(child), norm(parent)))

    # The source is a list of hardware, including buttons and accessories. Read
    # actual instance/subclass statements rather than categorising every row alike.
    for row in soup(CONTROLLERS).select("table.wikitable")[0].select("tr")[1:]:
        cells = row.find_all("td", recursive=False)
        if len(cells) < 5:
            continue
        name = textcell(cells[1])
        for c in [cells[3], cells[4]]:
            for a in c.select("a"):
                term = a.get_text(strip=True)
                if term and name and not re.fullmatch(r"Q\d+", term):
                    relates(name, term)
    for node in soup(APPLE).select("h2"):
        name = textcell(node)
        if name.startswith("iPhone"):
            relates(name, "smartphone")
    # Editorial terms link gaps between regional vocabulary and source ontologies.
    # These are general lexical records; the same graph traversal serves every bank.
    taxonomy = {
        wiki("Game controller"): {
            "game controller": [
                "gamepad",
                "video game controller model",
                "wired game controller",
                "wireless game controller",
                "motion controller",
                "arcade controller",
                "guitar controller",
                "sim racing wheel",
                "dance pad",
                "light gun",
                "joystick",
            ]
        },
        wiki("Smartphone"): {
            "mobile phone": ["smartphone"],
            "smartphone": ["iPhone", "Google Pixel"],
        },
        wiki("Video game console"): {
            "games console": [
                "Nintendo Switch",
                "PlayStation 5",
                "Xbox Series X",
                "Xbox Series S",
                "home video game console",
            ]
        },
    }
    for url, branches in taxonomy.items():
        fetch(url)
        for parent, children in branches.items():
            for child in children:
                relates(child, parent)
    # Derive familiar descriptions from the platform names printed in hardware
    # articles. Shared tokens in the product title are included only once.
    platforms = {
        "Xbox Wireless Controller": ["Xbox One", "Xbox Series X and Series S"],
        "Nintendo Switch Pro Controller": ["Nintendo Switch"],
        "DualSense": ["PlayStation 5"],
    }
    for model, articles in platforms.items():
        page = soup(wiki(model))
        for article in articles:
            anchor = page.find("a", title=article)
            if not anchor:
                continue
            label = anchor.get_text(" ", strip=True)
            names = [label]
            if m := re.fullmatch(r"(.+ )(\w+)/(\w+)", label):
                names.extend([m[1] + m[2], m[1] + m[3]])
            for name in names:
                relates(name + " controller", model)
                suffix = [w for w in norm(model).split() if w not in norm(name).split()]
                if suffix and suffix[-1] == "controller":
                    relates(name + " " + " ".join(suffix), model)
    equivalents = [
        [
            "games controller",
            "game controller",
            "games controllers",
            "video game controller",
            "gaming controller",
        ],
        ["gamepad", "game pad", "joypad", "control pad"],
        ["mobile phone", "mobile phones", "cell phone", "cellphone"],
        ["trainers", "sneakers", "training shoes"],
        ["games console", "game console", "video game console"],
        ["memory stick", "usb stick", "usb flash drive", "thumb drive", "pen drive"],
        ["smartphone", "smart phone"],
        ["joy con", "joy cons"],
    ]
    return {
        "sources": [CONTROLLERS, APPLE, *taxonomy, *map(wiki, platforms)],
        "equivalents": equivalents,
        "parents": [list(edge) for edge in sorted(edges)],
    }
