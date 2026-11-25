"""Read name columns, regional names and spelling variants, excluding recipes."""

import re

from common import add, norm, record, soup, textcell, wiki


def build():
    for key, page in [
        ("cheese", "List of cheeses"),
        ("pasta", "List of pasta"),
    ]:
        url = wiki(page)
        rows = []
        for table in soup(url).select("table.wikitable"):
            headers = [
                textcell(c)
                for c in table.select("tr")[0].find_all(["th", "td"], recursive=False)
            ]
            if headers[0] not in ("Name", "Type"):
                continue
            for tr in table.select("tr")[1:]:
                cells = tr.find_all(["td", "th"], recursive=False)
                if len(cells) != len(headers):
                    continue
                name_cell = cells[0]
                variants = [textcell(li) for li in name_cell.select("li")]
                for nested in name_cell.select("ul, ol"):
                    nested.decompose()
                label = textcell(name_cell)
                if not label or label in {
                    "Other PDO cheeses",
                    "Cauliflower Cheese",
                    "Fried Camembert cheese",
                }:
                    continue
                parts = re.split(r"\s+or\s+|\s*/\s*|[()]", label)
                name = parts[0].strip()
                aliases = [p.strip().removeprefix("or ") for p in parts[1:]]
                aliases = [
                    a for a in aliases if a and a.lower() not in {"pdo", "brand", "tr"}
                ]
                if key == "cheese":
                    aliases.extend(name + " " + v for v in variants)
                    if (
                        name.lower().endswith(" cheese")
                        and name[:-7].lower() in SHORT_CHEESES
                    ):
                        name = name[:-7]
                    if not name.lower().endswith(" cheese"):
                        aliases.append(name + " cheese")
                    if name == "Halumi":
                        aliases.append(name)
                        name = "Halloumi"
                if "Synonyms" in headers:
                    synonyms = textcell(cells[headers.index("Synonyms")])
                    for alt in re.split(
                        r"[,;]|\s+or\s+", re.sub(r"\([^)]*\)", "", synonyms)
                    ):
                        alt = alt.strip(' "“”')
                        if (
                            alt
                            and len(alt.split()) <= 4
                            and not re.search(r"[:.!?]", alt)
                        ):
                            aliases.append(alt)
                rows.append(record(name, *aliases))
        # The international cheese tables refer out to national lists for some
        # major producers. Include widely used market names from those lists.
        if key == "cheese":
            for group in [
                "Brie",
                "Camembert",
                "Roquefort",
                "Comté",
                "Reblochon",
                "Morbier",
                "Gruyère",
                "Emmental|Emmenthal",
                "Gouda",
                "Edam",
                "Stilton",
                "Parmigiano Reggiano|Parmesan|Parmigiano-Reggiano",
                "Mozzarella",
                "Ricotta",
                "Gorgonzola",
                "Burrata",
                "Mascarpone",
                "Taleggio",
                "Pecorino Romano",
                "Manchego",
                "Oaxaca|Quesillo de Oaxaca",
                "Queso fresco",
                "Queso blanco",
                "Cotija",
                "Cheddar",
                "Monterey Jack",
                "Cheshire cheese|Cheshire",
                "Double Gloucester",
                "Caerphilly",
                "Lancashire cheese",
                "Danablu|Danish blue",
                "Blue cheese|Bleu cheese",
                "Goat cheese|Chèvre|Goat's cheese",
                "Swiss cheese",
                "Quark",
                "Fontina",
                "Provolone",
                "Scamorza",
                "Asiago",
                "Grana Padano",
                "Castelmagno",
                "Caciocavallo",
                "Robiola",
                "Bel Paese",
                "Stracchino|Crescenza",
            ]:
                name, *aliases = group.split("|")
                rows.append(
                    record(
                        name,
                        *aliases,
                        *(
                            []
                            if name.lower().endswith(" cheese")
                            else [name + " cheese"]
                        ),
                    )
                )
        must_include = (
            {"paneer", "halloumi", "feta"}
            if key == "cheese"
            else {"fusilli", "orecchiette", "bucatini"}
        )
        assert must_include <= {norm(r[0]) for r in rows}
        q = add(
            "general",
            key,
            "Cheese board" if key == "cheese" else "Pasta shapes",
            "Name a type of cheese." if key == "cheese" else "Name an Italian pasta shape.",
            url,
            rows,
            f"Named varieties in the archived {page} tables"
            + (
                " and familiar cheeses from national lists."
                if key == "cheese"
                else ". Sauces and finished dishes are separate."
            ),
            common="Feta|Paneer|Halloumi|Cheddar|Parmesan|Parmigiano Reggiano|Mozzarella|Brie|Fusilli|Penne|Spaghetti|Macaroni|Lasagna|Ravioli",
            rare="Bouhezza|Kemariya|Ferrazzuoli|Fedelini",
            matching="full",
        )
        q["topic"] = "Food & Drink"


SHORT_CHEESES = set(
    "abertam adobera anari asadero añejo baladi chanco cheddar chihuahua colby colby-jack cotija cremoso guayanés heidal herve jarlsberg kalimpong liederkranz limburger maredsous muenster nabulsi oaxaca passendale provel pule samsø serpa stilton teleme tilsit trappista turrialba wensleydale".split()
) | {"pepper jack", "stinking bishop"}
