"""Short, searchable host notes. Use subject matter rather than audience stereotypes."""

import re

TOPICS = {
    "UK television": "screen-blue-peter screen-bake-off screen-strictly screen-inside-nine screen-who screen-red-dwarf screen-bafta-tv screen-crown",
    "UK music": "music-glastonbury music-mercury-album music-mercury-act music-best-sellers",
    "Europe · Eurovision": "music-winning-songs music-uk-eurovision music-euro-countries music-euro-hosts",
    "UK · Tennis": "general-wimbledon general-women-wimbledon",
    "International · Football": "general-world-final general-women-world-final world-cup",
    "International · Food": "general-cheese general-pasta general-cocktails",
    "International · Money": "general-currencies",
    "Japan · Animation": "screen-ghibli",
    "UK": "general-prime-ministers general-heritage general-booker general-booker-authors geography-uk-cities geography-counties national-parks tube-lines allergens",
    "USA": "general-parks general-presidents geography-us-cities geography-states geography-state-capitals",
    "Canada": "canada",
    "Japan": "geography-prefectures",
    "France": "geography-departments",
    "Europe": "geography-germany german-borders animals-bats",
}


def picker_context(question):
    key = question["id"]
    labels = [label for label, keys in TOPICS.items() if key in keys.split()]
    if key.startswith("ultimate-"):
        labels.append("Specialist · Ultimate frisbee")
        if "-uni-" in key or "-clubs-" in key:
            labels.append("UK competition")
    elif key.startswith("music-charts-"):
        labels.append("UK singles chart")
    elif key.startswith("geography-") and not labels:
        labels.append(
            {
                "africa": "Africa",
                "african-capitals": "Africa",
                "asia": "Asia",
                "europe": "Europe",
                "americas": "Americas",
                "oceania": "Oceania",
            }.get(key.removeprefix("geography-"), "World geography")
        )
    years = re.findall(
        r"(?<!\d)(?:1[789]\d{2}|20\d{2})(?:s)?(?!\d)", question["prompt"]
    )
    if years:
        labels.append("–".join(dict.fromkeys([years[0], years[-1]])))
    if any(
        re.search(r"\b(fuck\w*|bitch\w*|whore\w*|cunt\w*)\b", a["name"], re.I)
        for a in question["answers"]
    ):
        labels.append("Explicit titles")
    return " · ".join(labels)
