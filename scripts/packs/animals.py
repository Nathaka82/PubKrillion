import io
import re

import openpyxl
from common import ROOT, add, csvrows, fetch, record

BOU = "https://bou.org.uk/wp-content/uploads/2026/02/BOU_British_List_10th-and-58th_AviList_v2025_3Feb.xlsx"
MDD = "https://raw.githubusercontent.com/mammaldiversity/mammaldiversity.github.io/master/assets/data/mdd.csv"
WN = "https://wordnet.princeton.edu/"


def birds():
    book = openpyxl.load_workbook(
        io.BytesIO(fetch(BOU)), read_only=True, data_only=True
    )
    checklist = {
        r[2]: record(r[0], r[1], r[2])
        for r in list(book.worksheets[0].values)[1:]
        if r[0] and r[2]
    }
    tree = []
    order = family = None
    for _, rank, scientific, _, name, _, _ in list(book.worksheets[1].values)[1:]:
        if rank == "Order":
            order = scientific
        if rank == "Family":
            family = scientific
        if rank == "Species" and scientific in checklist:
            tree.append((order, family, scientific))
    assert set(checklist) == {s for _, _, s in tree}
    for key, title, prompt, branches in [
        (
            "british-list",
            "The British List",
            "Name a bird species on the British List, categories A–C.",
            [],
        ),
        (
            "wildfowl",
            "Ducks, geese and swans",
            "Name a duck, goose or swan on the British List.",
            ["Anatidae"],
        ),
        (
            "waders",
            "Sandpipers and allies",
            "Name a bird in the sandpiper family recorded on the British List.",
            ["Scolopacidae"],
        ),
        (
            "gulls",
            "Gulls and terns",
            "Name a member of the gull and tern family on the British List.",
            ["Laridae"],
        ),
        (
            "raptors",
            "Daytime birds of prey",
            "Name a hawk, eagle, osprey or falcon family member on the British List.",
            ["Accipitridae", "Pandionidae", "Falconidae"],
        ),
        (
            "songbirds",
            "Perching birds",
            "Name a passerine on the British List.",
            ["Passeriformes"],
        ),
    ]:
        rows = [
            checklist[s]
            for o, f, s in tree
            if not branches or o in branches or f in branches
        ]
        add(
            "animals",
            key,
            title,
            prompt,
            BOU,
            rows,
            "BOU 10th edition, 58th report, February 2026 correction. British, international English and scientific species names count. "
            + (", ".join(branches) if branches else "All category A–C species."),
            "Robin|Blackbird|Mallard|Mute Swan|Peregrine Falcon|Herring Gull",
        )


def mammals():
    rows = [r for r in csvrows(MDD) if r["extinct"] == "0"]

    def naming(r):
        name = r["mainCommonName"].replace("Gray ", "Grey ").replace("Gray-", "Grey-")
        alt = [x for x in r["otherCommonNames"].split("|") if x not in ("", "NA")]
        return record(name, r["mainCommonName"], r["sciName"].replace("_", " "), *alt)

    specs = [
        (
            "cats",
            "Wild cats",
            "a living member of the cat family",
            lambda r: r["family"] == "Felidae",
        ),
        (
            "canids",
            "Dogs, foxes and wolves",
            "a living canid species",
            lambda r: r["family"] == "Canidae",
        ),
        (
            "cetaceans",
            "Whales, dolphins and porpoises",
            "a living cetacean species",
            lambda r: (
                r["infraorder"] in ("Mysticeti", "Odontoceti")
                or r["order"] == "Cetacea"
            ),
        ),
        (
            "primates",
            "Our primate relatives",
            "a living primate species",
            lambda r: r["order"] == "Primates",
        ),
        (
            "bats",
            "Bats of Europe",
            "a bat species whose recorded distribution includes Europe",
            lambda r: (
                r["order"] == "Chiroptera" and "Europe" in r["continentDistribution"]
            ),
        ),
        (
            "monotremes",
            "Egg-laying mammals",
            "a living monotreme species",
            lambda r: r["order"] == "Monotremata",
        ),
        (
            "marsupials",
            "Australian marsupials",
            "a living marsupial species recorded in Australia",
            lambda r: (
                r["infraclass"] == "Marsupialia"
                and "Australia" in r["countryDistribution"].split("|")
            ),
        ),
        (
            "bears",
            "The bear family",
            "a living species of bear",
            lambda r: r["family"] == "Ursidae",
        ),
        (
            "pinnipeds",
            "Seals, sea lions and walruses",
            "a living seal, sea lion or walrus species",
            lambda r: r["family"] in ["Odobenidae", "Otariidae", "Phocidae"],
        ),
        (
            "mustelids",
            "Otters and their cousins",
            "a living member of the weasel and otter family",
            lambda r: r["family"] == "Mustelidae",
        ),
        (
            "squirrels",
            "Squirrels and chipmunks",
            "a living species in the squirrel family",
            lambda r: r["family"] == "Sciuridae",
        ),
        (
            "antelopes",
            "The bovid family",
            "a living member of the cattle, antelope, sheep and goat family",
            lambda r: r["family"] == "Bovidae",
        ),
    ]
    for key, title, desc, test in specs:
        add(
            "animals",
            key,
            title,
            "Name " + desc + " in the Mammal Diversity Database snapshot.",
            MDD,
            [naming(r) for r in rows if test(r)],
            "All matching species marked extant in the archived MDD checklist. Scientific names and unambiguous listed common names count.",
            "Lion|Tiger|Cheetah|Red Fox|Blue Whale|Brown Bear|Platypus|Human|Sea Otter",
        )


from functools import cache


@cache
def wordnet_data():
    path = ROOT / "node_modules/wordnet-db/dict"
    records = {}
    for line in (path / "data.noun").read_text("utf8").splitlines():
        if not line or line[0] == " ":
            continue
        fields = line.partition(" | ")[0].split()
        n = int(fields[3], 16)
        start = 4 + 2 * n
        words = [fields[i].replace("_", " ") for i in range(4, start, 2)]
        children = [
            int(fields[i + 1])
            for i in range(start + 1, start + 1 + 4 * int(fields[start]), 4)
            if fields[i] in ("~", "~i")
        ]
        records[int(fields[0])] = (words, children)
    senses = {}
    for line in (path / "index.noun").read_text("utf8").splitlines():
        if line and line[0] != " ":
            f = line.split()
            senses[f[0]] = list(map(int, f[-int(f[2]) :]))
    return records, senses


def wordnet_rows(word, sense):
    records, senses = wordnet_data()
    root = senses[word][sense]
    stack = list(records[root][1])
    seen = set()
    rows = []
    while stack:
        s = stack.pop()
        if s in seen:
            continue
        seen.add(s)
        words, children = records[s]
        stack.extend(children)
        name = words[0].replace("gray", "grey").replace("mollusk", "mollusc")
        name = re.sub(r"\bladybug\b", "ladybird", name)
        uk = {
            "adjustable wrench": "adjustable spanner",
            "Allen wrench": "Allen key",
            "socket wrench": "socket spanner",
            "open-end wrench": "open-ended spanner",
            "box wrench": "ring spanner",
            "wrench": "spanner",
            "vise": "vice",
            "ball-peen hammer": "ball-pein hammer",
        }
        rows.append((uk.get(name, name), words, s))
    return rows


def dictionary():
    # Root lemma and sense are fixed so these scopes are reproducible.
    for key, title, word, sense in [
        ("sharks", "Sharks", "shark", 0),
        ("snakes", "Snakes", "snake", 0),
        ("lizards", "Lizards", "lizard", 0),
        ("turtles", "Turtles", "turtle", 1),
        ("frogs", "Frogs and toads", "frog", 0),
        ("butterflies", "Butterflies", "butterfly", 0),
        ("beetles", "Beetles", "beetle", 0),
        ("crustaceans", "Crustaceans", "crustacean", 0),
        ("molluscs", "Molluscs", "mollusk", 0),
        ("dogs", "Domestic dogs", "dog", 0),
        ("spiders", "Spiders", "spider", 0),
        ("fish", "Bony fish", "bony_fish", 0),
    ]:
        root = wordnet_data()[1][word][sense]
        rows = wordnet_rows(word, sense)
        label = word.replace("_", " ").replace("mollusk", "mollusc")
        add(
            "animals",
            key,
            title,
            "Name a kind of "
            + label
            + " listed in WordNet’s English animal classification.",
            WN,
            rows,
            "All named descendants of the WordNet 3.1 noun sense "
            + str(root)
            + ". Includes groups and specific kinds; synonymous names share a score.",
            "Great white shark|Cobra|Bullfrog|Poodle|Labrador retriever|Ladybird|Tarantula",
        )


def build():
    birds()
    mammals()
    dictionary()
