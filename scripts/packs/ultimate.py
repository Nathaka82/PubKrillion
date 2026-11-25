import csv
import io
import re

import pymupdf
from common import add, clean, fetch, norm, packs, record, soup, split

RULES = "https://rules.wfdf.sport/wp-content/uploads/2024/12/WFDF-Rules-of-Ultimate-2025-2028.pdf"
COACH = "https://archive.usaultimate.org/assets/1/Page/Skills%20and%20Drills%20manual_revised2019.pdf"
USA = "https://usaultimate.org/discover-ultimate/"
ROB = "https://ultimaterob.com/2013/10/24/uncommon-throws-in-ultimate-frisbee/"
SIGNALS = "https://rules.wfdf.sport/wp-content/uploads/2025/03/WFDF-Rules-of-Ultimate-2025-2028-Hand-Signals-1-page.pdf"
SPIRIT = "https://wfdf.sport/spirit-of-the-game/"
UK = "https://www.ukultimate.com/news/uwmon-2025-winners"
SHEET = "https://docs.google.com/spreadsheets/d/e/2PACX-1vSkJHlwarSwo3qy6py-ph4x_D0B8_983ehYdNckDrgXpmI0UgZFGOJGn7YQfVsqHsOfwRDsrWl_tUkQ/pub?output=csv&gid=897873158"
GAMES = "https://wfdf.sport/2025/08/team-usa-wins-ultimate-gold-medal-in-thrilling-final-at-the-world-games-2025-chengdu/"


def build():
    def q(key, title, prompt, source, rows, scope, common="", rare=""):
        if isinstance(rows, str):
            rows = [record(*[s.strip() for s in row.split("~")]) for row in split(rows)]
        return add("ultimate", key, title, prompt, source, rows, scope, common, rare)

    fetch(COACH)
    fetch(SPIRIT)
    with pymupdf.open(stream=fetch(SIGNALS)) as signals:
        assert {
            int(v) for v in re.findall(r"(?m)^(\d+)\.", signals[0].get_text())
        } == set(range(1, 25))
    q(
        "throws",
        "Ways to throw a disc",
        "Name a way to release the disc in ultimate frisbee. High and low versions of the same throw count together.",
        ROB,
        "Forehand~Flick~Sidearm~Side arm~High-release forehand~High release flick~Low-release forehand~Inside-out forehand~Outside-in flick~Off-hand forehand|Backhand~High-release backhand~Low-release backhand~Air bounce~Airbounce~Inside-out backhand~Outside-in backhand~Off-hand backhand~Around-the-back backhand|Hammer~Overhead hammer~Helix hammer|Scoober~Scoobie~Helix scoober|Thumber~Helix thumber|Push pass~Push throw|Overhand wrist flip~Chicken wing~Chickenwing~Wrist flip~Overhand wristflip|Reverse overhand wrist flip~Reverse wrist flip~Blind overhand wrist flip|Upside-down backhand~Inverted backhand|Sky hook~Skyhook",
        "Ten release families from USA Ultimate’s beginner terminology and Ultimate Rob’s uncommon-throws collection. Release height, throwing hand and curve share a family. Distance, tactical purpose and trick routines are outside this question.",
        "Forehand|Backhand",
        "Reverse overhand wrist flip|Sky hook",
    )
    # Published variations and release qualifiers are examples of a technique.
    # A team confirms the suggested family, while a true synonym such as flick locks in.
    releases = packs["ultimate"][0]["answers"]
    for answer in releases:
        variants = [
            a
            for a in answer["aliases"]
            if any(
                w in a.lower()
                for w in [
                    "release",
                    "inside-out",
                    "outside-in",
                    "off-hand",
                    "helix",
                    "around-the-back",
                ]
            )
        ]
        if answer["name"] == "Forehand":
            variants.append(
                "Blade"
            )  # USA Ultimate defines this as a steep outside-in flick.
        answer["aliases"] = [a for a in answer["aliases"] if a not in variants]
        words = [answer["name"]] + (
            {"Forehand": ["flick", "sidearm"]}.get(answer["name"], [])
        )
        for variation in soup(ROB).select(".entry-content ul li"):
            name = variation.get_text(" ", strip=True).split(" (")[0]
            if any(norm(name).endswith(" " + norm(w)) for w in words):
                variants.append(name)
        if answer["name"] in ("Forehand", "Backhand"):
            variants.extend(
                prefix + " " + word
                for prefix in [
                    "high release",
                    "low release",
                    "inside out",
                    "outside in",
                    "off hand",
                ]
                for word in words
            )
        answer["examples"] = sorted({norm(v): v for v in variants}.values())
    q(
        "catches",
        "Hands on the disc",
        "Name a standard technique for catching the disc in ultimate frisbee.",
        COACH,
        "Pancake catch~Pancake~Clap catch~Sandwich catch~Two-handed clap catch|Two-handed rim grab~Two handed rim catch~Two hand rim catch~Two-handed claw|One-handed rim grab~One handed rim catch~One hand catch~One-handed catch",
        "All three techniques in Standard Catches, pp. 11–13.",
        "Pancake catch",
    )
    q(
        "cuts",
        "Lose your defender",
        "In ultimate frisbee, name a cut a player can make to get free for a pass.",
        COACH,
        "Straight cut~Straight line cut|Full cut~Full cuts|Two-step juke~Juke~Two step fake|Double cut~Double cuts",
        "The three illustrated changes of direction and the straight cut named in the handler section.",
    )
    q(
        "offence",
        "Seven on the line",
        "Name one of the three attacking roles in ultimate frisbee against a person-to-person defence.",
        COACH,
        "Handler~Handlers|Middle~Middles|Long~Longs~Deep~Deeps~Deep cutter",
        "The manual’s Positions section groups players by their role on offence.",
        "Handler",
    )
    q(
        "forces",
        "Setting the force",
        "In ultimate frisbee, name a force used in person-to-person defence.",
        COACH,
        "Force forehand~Force flick~Forehand~Flick|Force backhand~Backhand|Force middle~Middle~Forcing middle|Straight up~Straight-up force~Force straight up~Flat mark",
        "All four illustrated schemes, figures 9a–9d.",
    )
    q(
        "zone",
        "Inside the zone",
        "In ultimate frisbee, name a defensive position in a zone with a three-player cup.",
        COACH,
        "Point~Points~Point defender|Middle-middle~Middle middle|Wing~Wings|Short deep~Short-deep|Deep-deep~Deep deep",
        "All five role names in the manual’s zone positioning diagrams; the two points and wings each share one answer.",
    )
    q(
        "pass-purpose",
        "Reading a pass",
        "In ultimate frisbee, what do players call a long pass, a pass across the field, a reset, or a pass through the force? Give any one term.",
        USA,
        "Huck~Deep throw~Long throw|Swing~Swing pass|Dump~Dump pass~Reset~Reset pass|Breakmark~Break mark~Break throw~Break pass",
        "One named pass for each of the four descriptions in the prompt.",
    )
    q(
        "spirit",
        "Scoring Spirit",
        "Name a category on the WFDF Spirit of the Game score sheet.",
        SPIRIT,
        "Rules knowledge and use~Rules knowledge~Knowledge and use of the rules|Fouls and body contact~Fouls~Body contact|Fair-mindedness~Fair mindedness|Positive attitude and self-control~Positive attitude~Self control|Communication",
        "The five scored spirit categories. Word variants share the same score.",
    )
    q(
        "mark",
        "On the mark",
        "In ultimate frisbee, name a marking infraction under the 2025–2028 WFDF rules.",
        RULES,
        "Fast count~Fast counting|Straddle~Straddling|Disc space~Discspace|Wrapping~Wrap|Double team~Double teaming|Vision~Vision blocking",
        "All six named marking infractions.",
        "Fast count|Disc space",
    )
    q(
        "live-turns",
        "Live turnovers",
        "Under WFDF ultimate rules, name a turnover after which play continues immediately.",
        RULES,
        "Down~Grounded disc~Incomplete pass~Dropped pass|Interception~Intercepted pass~Intercept|Out-of-bounds~Out of bounds~OB|Dropped pull~Drop the pull~Muffed pull",
        "The four cases in 13.1. The pull must have been touched by the receiving team.",
    )
    q(
        "stopped-turns",
        "Turnovers that stop play",
        "Under WFDF ultimate rules, name a turnover that stops play.",
        RULES,
        "Accepted offensive receiving foul~Offensive receiving foul|Stall-out~Stall~Stalled out|Handover~Hand-off~Hand off|Deflection~Intentional deflection|Self-catch~Self catch|Team-mate assisted catch~Assisted catch~Team assisted catch|Equipment-assisted catch~Using equipment to catch~Object assisted catch",
        "All seven cases in 13.2, including the two distinct forms of assistance.",
    )
    q(
        "fouls",
        "Making the call",
        "Name a type of foul in ultimate frisbee under WFDF rules.",
        RULES,
        "Dangerous play|Receiving foul~Receiving|Strip foul~Strip|Blocking foul~Blocking~Block|Force-out foul~Force out|Defensive throwing foul~Marking foul|Offensive throwing foul~Thrower foul|Indirect foul~Indirect|Offsetting fouls~Offsetting foul",
        "All nine headings in section 17, including dangerous play.",
    )
    q(
        "signals",
        "Say it with your hands",
        "Name a call or decision that has an official WFDF hand signal in ultimate frisbee.",
        SIGNALS,
        "Foul|Violation|Goal~Score|Contest~Contested|Accepted~Uncontested|Retracted~Retract|In or out-of-bounds~In~Out~Out of end zone|Disc down~Down|Disc up~Up|Pick|Travel|Marking infraction|Turnover~Turn|Timing violation|Offside or false start~Offside~False start|Time-out~Timeout|Spirit stoppage|Stoppage|Male-matching ratio~Male matching|Female-matching ratio~Female matching|Play has stopped~Stopped play|Who made the call~Who called it|Did not affect the play~No effect|Match point~Game point",
        "All 24 numbered signals. The shared in/out gesture counts once.",
    )
    # Read the actual first table column. Three labels wrap across printed lines.
    with pymupdf.open(stream=fetch(RULES)) as pdf:
        glossary = []
        for page in (pdf[14], pdf[15]):
            for line in page.get_text(clip=pymupdf.Rect(0, 30, 137, 820)).splitlines():
                term = clean(line)
                if not term or term == "Definitions":
                    continue
                if term in ("disc", "stops"):
                    glossary[-1] += " " + term
                else:
                    glossary.append(term)
    aliases = {
        "Out-of-bounds (OB)": ["OB", "Out of bounds"],
        "Receivers": ["Receiver"],
        "Possession of the disc": ["Possession"],
    }
    q(
        "definitions",
        "The rules glossary",
        "Name a playing term formally defined in the 2025–2028 WFDF rules for ultimate frisbee.",
        RULES,
        [record(term, *aliases.get(term, [])) for term in glossary],
        "Defined terms in the rules glossary, as printed in the 2025–2028 edition.",
    )
    q(
        "lines",
        "Marking out a pitch",
        "Name a type of line that marks the playing area or an end zone on an ultimate frisbee field.",
        RULES,
        "Sideline~Side line~Sidelines|Endline~End line~Endlines|Goal line~Goal lines~Goalline",
        "Figure 1. The brick mark is a point on the field.",
    )
    q(
        "check",
        "Checking the disc in",
        "In ultimate frisbee, name a reason play stops and the disc must be checked before play resumes.",
        RULES,
        "Foul|Violation|Contested turnover~Disputed turnover|Specified turnover~Specified turnover under rule 13.2|Contested goal~Disputed goal|Stoppage~Injury stoppage~Technical stoppage|Discussion~Rules discussion|End of a time-out~Timeout~Time out",
        "The eight listed reasons in rule 10.1.",
    )
    q(
        "safety",
        "Stopping for safety",
        "In ultimate frisbee, name one of the two safety stoppages recognised under WFDF rules.",
        RULES,
        "Injury stoppage~Injury|Technical stoppage~Technical",
        "Section 19 covers injuries and technical issues affecting safety.",
    )
    q(
        "good-spirit",
        "Showing good Spirit",
        "Name an action the WFDF recognises as an example of good Spirit of the Game.",
        RULES,
        "Retract an incorrect call~Retract a call~Retraction|Check in with an opponent~Check in after a disagreement|Compliment an opponent~Compliment good play~Compliment good spirit|Introduce yourself~Introductions|React calmly~Stay calm~Calm reaction",
        "Five example actions identified in rule 1.5.",
    )
    q(
        "poor-spirit",
        "Crossing the Spirit line",
        "Name a behaviour that breaches Spirit of the Game under WFDF rules.",
        RULES,
        "Dangerous or aggressive play~Dangerous play~Aggressive behaviour|Deliberate rule breach~Intentional foul~Intentional fouling|Intimidation~Taunting|Disrespectful celebration~Disrespectful celebrating|Retaliatory call~Revenge call|Calling for an opponent’s pass~Calling for a pass from the opposition|Win-at-all-costs behaviour~Win at all costs",
        "All seven examples in rule 1.6.",
    )
    q(
        "team-spirit",
        "Team responsibilities",
        "Under WFDF rules, name a responsibility teams share for upholding Spirit of the Game.",
        RULES,
        "Teach rules and Spirit~Teach the rules~Teach spirit|Discipline poor Spirit~Discipline team-mates|Give constructive feedback~Feedback to other teams|Call a Spirit stoppage~Spirit timeout~Spirit stoppage",
        "All four team duties in 1.7.",
    )
    q(
        "bad-check",
        "An invalid restart",
        "In ultimate frisbee, what can an opponent do at a restart that allows you to call a check violation?",
        RULES,
        "Pass without a valid check~No check|Restart without verification~No verification|Move before the check~Early movement|Start in the wrong position~Incorrect position~Wrong position",
        "The four opponent actions in 10.7.1–10.7.4.",
    )
    glossary = [e.get_text(strip=True) for e in soup(USA).select("h5")]
    q(
        "sideline",
        "Sideline vocabulary",
        "Name an ultimate frisbee term a commentator might use for a throw, a tactic, a player’s role or a moment on the field.",
        USA,
        glossary[glossary.index("Bid") : glossary.index("Swing") + 1],
        "Every entry in Ultimate Terminology, from Bid through Swing.",
    )
    assert len(packs["ultimate"]) == 22
    sheet = list(csv.reader(io.StringIO(fetch(SHEET).decode("utf-8-sig"))))
    division = ""
    standings = {}
    for row in sheet:
        if len(row) < 4:
            continue
        if not row[0] and row[3]:
            division = row[3]
        elif row[0].isdigit() and row[3]:
            name = re.sub(r"\s+\d+\s+\([^)]+\)$", "", row[3])
            standings.setdefault(division, []).append(
                record(
                    name,
                    "University of " + name,
                    name + " University",
                    {
                        "UCL": "University College London",
                        "King's College": "King's College London",
                        "St Andrews": "Saint Andrews",
                    }.get(name, ""),
                )
            )
    for key, division in [
        ("men", "Men's Championship"),
        ("women", "Women's Championship"),
    ]:
        q(
            "uni-" + key,
            "University " + key + "’s championship",
            "Name a university in the "
            + division.lower()
            + " standings at UK Ultimate’s April 2025 Outdoor Championships.",
            SHEET,
            standings[division],
            "Every university in this championship division’s final standings. Trophy and Shield are separate divisions.",
        )
    winners = [
        p.get_text(strip=True).split(": ", 1)[1].removesuffix(" 1")
        for p in soup(UK).select("p")
        if re.match(
            r"^(Men|Women)[’']s (Championship|Trophy|Shield): ", p.get_text(strip=True)
        )
    ]
    assert len(winners) == 5
    q(
        "uni-winners",
        "University outdoor champions",
        "Name a university that won a division at the April 2025 UK University Outdoor Championships.",
        UK,
        [record(n, "University of " + n, n + " University") for n in winners],
        "Winners of the five Championship, Trophy and Shield divisions.",
    )
    for div in ["open", "mixed", "women"]:
        url = "https://uku-schedule.ultimatefederation.eu/teams/division/" + div
        teams = [
            a.get_text(" ", strip=True).removesuffix(" team page")
            for a in soup(url).select('a[href^="/team/"]')
        ]
        assert len(teams) == 8
        q(
            "clubs-" + div,
            "UKU Nationals · " + div.title(),
            "Name a club team in the " + ("women’s" if div == "women" else div) + " division of the 2026 UKU Nationals.",
            url,
            [record(n, re.sub(r" (Open|Women|Mixed)$", "", n)) for n in teams],
            "All eight entries in the tournament’s division directory. Numbered squads count separately.",
        )
    ranking = (
        soup(GAMES)
        .get_text(" ", strip=True)
        .split("Final Standings", 1)[1]
        .split("Medal Team Rosters", 1)[0]
    )
    ranks = re.findall(r"\b([1-8])[.:]\s*(.+?)(?=\s+[1-8][.:]|$)", ranking)
    assert len(ranks) == 8, ranks
    countries = [
        record("United States", "USA", "US", "United States of America")
        if "USA" in n or "United States" in n
        else n.strip()
        for _, n in ranks
    ]
    q(
        "chengdu",
        "The World Games",
        "Name a country in the ultimate tournament at the 2025 World Games in Chengdu.",
        GAMES,
        countries,
        "All eight teams in WFDF’s published final ranking.",
    )
    q(
        "uni-divisions",
        "The university silverware",
        "Name a division contested at the April 2025 UK University Outdoor Championships.",
        UK,
        [
            r[3]
            for r in sheet
            if len(r) > 3
            and not r[0]
            and re.fullmatch(r"(Men|Women)'s (Championship|Trophy|Shield)", r[3])
        ],
        "The five named divisions in the published final standings.",
    )
