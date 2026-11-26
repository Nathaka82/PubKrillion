# Running the knowledge worker

## Publishing the shared catalogue

When a host names an unfamiliar subject, the builder checks its recorded spellings and whether Wikidata uses it as a class or occupation. A verified identity enrolls the requested answer list for collection. If the source lists only subclasses, the host can select an explicit “Name a type of …” question after its answers have been verified. A request about a creator keeps that creator as a condition when the builder learns the subject.

Run the publisher independently of the game. The default Compose configurations include it as the `knowledge-builder` service. These commands also work in a local checkout with Python 3.11 or later.

```sh
python scripts/knowledge/catalogue.py
python scripts/knowledge/catalogue.py --watch
python scripts/knowledge/catalogue.py --offline
```

The first command builds once. Watch mode checks daily and retries a failed build after five minutes. Offline mode reads the installed world index, bundled sources, local feeds and previously downloaded releases. Use `--world`, `--measurements`, `--feeds` and `--output` to override the publisher’s inputs and output. Live FishBase imports and Parquet feeds require the DuckDB and wordfreq dependencies included in the builder image.

To publish an independent collection, run `python scripts/knowledge/catalogue.py --feeds-only --feeds path/to/manifests --output path/to/catalogue.sqlite`. The builder derives question rules from classes and relationships reported by these feeds. Separate providers can supply the subject’s class and its relationship using the same stable subject ID. Search for the class or the named target to find the resulting questions. **Surprise me** also samples these rules. Each qualifying answer carries the evidence for its membership and the requested relationship.

The game opens `storage/knowledge-catalogue.sqlite` read-only. To use a copy published on another machine, set `KNOWLEDGE_CATALOGUE` to its path in the game environment. The game needs no raw world index to search, preview and play these questions. The publisher stages the next file separately, checks it, then replaces the publication atomically. Unchanged inputs reuse the current edition. The game refreshes saved catalogue questions in active rooms after a new edition arrives. A round already opened retains its scoring edition.

The builder checks alternate answer names against labels and aliases in every language on the same Wikidata entity. The online publisher reads the page index of a completed, dated Wikidata dump and matches page titles to IDs in the local knowledge graph. It downloads groups with more relevant entities per byte first. Each group passes range, compression, entity ID and revision text checksum checks before its names and checkpoint are saved together. Interrupted captures resume from the last completed group. The source cache retains the names, dates and revision IDs. The XML content is streamed and discarded. A new dump can withdraw a missing entity's previous dump observation after a complete scan. [Wikimedia multistream format](https://meta.wikimedia.org/wiki/Data_dumps/Dump_format#Multistream_dumps)

Watch mode advances this scan between publications. To advance it manually, run `python scripts/knowledge/bulk_names.py --world storage/world/wikidata5m-v1.sqlite --storage storage --megabytes 100`. The byte allowance rounds up to a complete group. If a group fails content checks, the builder isolates the affected compressed member, saves it for retry, and continues with other members and shards. A transfer failure or a provider rate limit pauses downloads. The command reports validated compressed bytes and outstanding ranges. A gap preserves previous observations until a complete release can establish a withdrawal.

An offline build incorporates locally captured names into the game's read-only catalogue. The field `checkedNameEntities` counts distinct IDs with a name observation, including redirects and explicit empty records. The field `dumpNameEntities` counts those whose winning observation came from a dump. Name evidence links to the exact source revision where available.

Direct lookups also refresh subjects from requested banks. Each pass makes space for popular answers and for the owners of top-scoring names, whose spellings can matter most to a team's score. Every qualifying public ID enters the queue, including distinct works sharing one display name. Questions with the same set of IDs share one queue entry. Only public entity IDs go to the name service. A newer direct lookup overrides an older dump observation. The game can check answers and suggest completions locally throughout the refresh.

For checked subjects, the checker and autocomplete share the recorded spellings. A historical title with a parenthetical qualifier can also qualify when the title and qualifier are independently recorded for that subject. The answer evidence identifies these spellings. The English article title and specialist registers still supply names. Subjects awaiting their first check retain historical alternatives, which can include related topics. Ambiguous names shared by several subjects are checked against the question’s membership before scoring.

When an exact title is also an alternate name for another eligible answer and every known owner qualifies, players earn the shared score. The host’s answer list shows how many points that spelling earns. A more specific recorded title can earn the individual entry’s score. A spelling suggestion for that entry uses a name the checker can identify when the player confirms it.

A host can enter a name such as “Celine Song” or a subject and credit such as “songs by PinkPantheress”. For a named creator or group already in the catalogue, the game can offer questions directly from local credit records. The picker spells out the credit, such as director, screenwriter or performer. A separate form or genre recorded on the same work can supply another explicitly worded question. Current verified withdrawals apply to these suggestions. The game queues further research while local questions are playable. When a complete name search returns many namesakes, the builder checks which identities have relevant creative credits. That finding applies only to the requested subject. The builder verifies the membership of each proposed bank independently. On separate machines, configure `KNOWLEDGE_REQUESTS` as a shared directory and deliver each new edition to the game.

The builder also reads names and aliases for newly encountered classes and item-valued properties. For example, a novel may be catalogued as a literary work with a separate statement for its form. When a direct class search finds too few works, the builder can verify the author and the form or genre on the same work. The displayed question names both conditions. A source count can suggest a question. Its answer list becomes playable after the builder verifies the individual members. When a source requests a pause, the builder honours its retry interval and keeps the last verified data.

Plain prompts such as “Name a board game” and “Name an architect” follow the general graph’s class and occupation records, including subclasses. “Name a type of woodwind instrument” searches for the subclasses themselves. The historical edition stores each qualifying membership and subclass path. Selecting one of these rules enrolls it for a current Wikidata check. The publisher commits up to two 200-record pages per class per pass and resumes at the saved cursor. Only a completed, verified capture replaces the preceding class membership. Each current answer carries its source question as an executable Wikidata query. Both historical and current graph questions have open coverage.

When an input feed changes, the publisher can copy the previously verified world index into the new publication. Host setup shows a notice while a saved question is being checked against a new edition. A withdrawn rule stays unavailable for new rounds until a source supplies it again. A temporary verification failure retries automatically. A preview opened before a source update must be regenerated before saving.

The publisher joins length and mass records to named entities, their classes and their aliases. A host can enter “Name a bridge at least 2 kilometres long”, compare two kinds of things, or search for a measured place such as the Golden Gate Bridge. The game derives suggestions from the available measurements. The picker shows the matching record count before building the selected answer bank. A comparison with a named reference uses its recorded range. For “longer than”, a candidate’s lower bound must exceed the reference’s upper bound. “At least” includes equality. A verified class census with enough evidence to decide every member can produce a complete comparison.

FishBase reports common length, size at maturity and recorded maximum length separately. For an unqualified fish size question, the player rule uses the common length when available and allows size at maturity to establish a lower bound when common length is missing. “Usually longer than” requires common length. “With a recorded maximum length over” uses the maximum records. The standalone publisher checks FishBase’s release directory daily, validates the projected species count against the source export and retains a checksum-verified capture for offline use. [Species fields](https://fishbase.se/manual/fishbasethe_species_table.htm) · [Maturity fields](https://fishbase.se/manual/FishBaseThe_MATURITY_Table.htm)

Hosts can combine two published relationships for the same class in one question, for example “Name a film directed by Steven Spielberg and starring Tom Hanks”. The compiler intersects entity IDs and stores evidence for both conditions with each answer. Two validated properties of the same finite register produce a complete intersection for that edition. A graph intersection retains open coverage. A request with zero supported matches reports that to the host.

Geographic questions follow the recorded location through administrative areas. “A museum in London” can qualify through a district. “An architect born in London” follows the birthplace of a person whose source occupation qualifies. Filming locations and story settings have separate rules. “A bridge whose country is France” requires a direct country statement. A location question can include historical institutions. Extra wording such as “currently open” needs independent evidence.

When a source distinguishes a city from its surrounding region, the draft picker can offer each with its own name and answer count. The publisher checks the provider’s total, collects names in the source languages, and saves the statements that connect each named answer to its place. A missing link or incomplete page defers the new release. The game can replay the published paths offline.

Generating a graph question enrolls its numeric class, property and target IDs with the publisher. The publisher checks for requests every minute in watch mode and collects up to twelve due queries per build. It checks Wikidata for instances of the class or its subclasses that meet every condition, pages through their names and aliases, and embeds the resulting answers in the next catalogue. Current records replace historical membership for that exact rule, including withdrawals. A failed request retains the last capture and retries after fifteen minutes. An offline build replays verified captures. The game continues serving its pinned round during collection. A game and a publisher on different machines can share the small request directory by setting `KNOWLEDGE_REQUESTS` on both. Player guesses and room details stay out of those requests.

The host can also request a relationship with zero matches in the local edition. When the builder can identify its class, relationship and target, the game registers the same source check from either a topic or a fully worded question. The question becomes playable after the publisher obtains named matches. Ambiguous or unsupported wording requires clarification. Choosing complete registers only keeps this search within the published registers.

Each publication indexes the resulting questions under both their subject and their named targets. Searching for a place, person or category can offer a newly verified question to another host. **Surprise me** can also draw from these questions once they have five named matches.

The builder also collects dated statements. A host can enter “Name a film released in 1999”, “Name a museum founded in the 1990s” or “Name an aircraft whose first flight was before 1950”. The source supplies the names and aliases of its time properties. A host can use “whose [property name] was” or a Wikidata property number for properties without a conversational phrase. Years, decades and inclusive ranges are supported, along with strict before and after boundaries. A bare subject and year offers the available date questions to choose from.

The publisher retains the original statement link, calendar and precision alongside each date. These rules use best-ranked Gregorian dates precise to a year or finer. For publications, any recorded release in the period qualifies, including regional releases and re-releases. Label batches and large statement pages survive restarts. The publisher checks both the statement count and the entity count before installing a capture. The game can derive narrower years or combine adjacent published ranges locally. The newest capture supplies each overlapping period, including withdrawals. Type a year in the host picker to discover questions across subjects.

For each dated group with at least eight named entities, the builder groups the entities’ outgoing links by property and target. It publishes up to three refinements using different properties when each group has at least three named answers and narrows the original question. Hosts can discover them through the original question, year, subject or target. A host can also specify a date and a relationship that the publisher has never suggested. The compiler joins the two sets of evidence by entity ID. For places it can follow an evidenced chain of geographic relationships.

The online builder uses the published question index to pick up to two of its own dated relationships for current-source checks per pass. Host requests have a separate allowance of twelve. Within each group, new questions and the oldest due checks are serviced first. A host request registers that precise combination with the builder. A background refresh can verify it directly without first importing every member of the larger date range. It reads the matching source facts twice and compares the results before publishing. For smaller geographic groups it follows place links forward from the dated entities. Failed refreshes retain the last verified capture. A successful check finding zero members withdraws the question from future editions. The builder checks withdrawn rules again when they come due. A published bank keeps the recorded date and relationship evidence for each answer.

Online builds also discover date questions without a host request. The builder cycles through classes in its world graph, samples members, and asks which date properties those entities actually have. It checks the size of promising groups and subscribes to up to two new groups per hour. It prefers groups with 5–250 members for this discovery pass. Hosts can request groups of any size and their requests take priority. Offline builds replay completed captures. If a host expands a year range, only gaps in the published coverage need to be collected.

Published date questions use the current source name of the class they check. When the original graph label has changed, the picker offers the updated wording and the preview includes the source description. The downloaded date-subject dictionary is part of the builder’s offline inputs.

The publisher keeps the individual graph relationships, indexed by property, target and subject ID, alongside separate class, occupation and subclass indexes. Popular groups are precomputed for browsing. A host can also ask about a group containing a single named member, use a source property name or alias, or specify entity and property IDs to resolve a naming ambiguity. For topics outside the precomputed groups, the picker can discover questions from the target's incoming links. Each proposed question is counted against the full index.

The builder also refreshes the class hierarchy independently. It looks up the current direct parent classes of every type, occupation and subclass in the world input, then follows newly discovered parent IDs. Completed batches survive interruptions. Only a fully collected hierarchy can replace the previous one, including classes whose source now lists no parents. Online watch mode performs this work in batches and starts a new capture after seven days. To collect immediately, run `python scripts/knowledge/taxonomy.py`, then `python scripts/knowledge/catalogue.py --offline` to publish it. Both commands use the same storage directory as the game by default.

A host can combine a relationship and a measurement in either order, such as “Name a bridge in France longer than 2 km”. Both must apply to the same entity. Searching for “bridges in France” can also offer a length question with a threshold derived from the measured bridges in that group. The preview reports the number supported by the current edition. The game stores the source evidence, accepted names and scores for the round. A finite register with an exhaustive property can establish complete coverage when every potentially eligible member has a decisive measurement. General graph questions have open coverage.

A complete register has a narrower promise. Its membership is limited to one source, scope and edition. The publisher checks that the recorded member count agrees with the source declaration before publishing. The Nobel adapter pages the official export, checks award counts, non-awards and prize shares, then derives scopes by category, year and decade from the award rows. New awards appear on the next successful refresh. A manifest for another finite source can declare its own `census`. [Official Nobel Prize data](https://api.nobelprize.org/2.1/nobelPrizes?limit=1000)

The periodic table register comes from PubChem. Each row must have a unique name, symbol and atomic number. Numbers must form an uninterrupted sequence starting at one. Each refresh checks for previously published atomic numbers. A new element gets its name, symbol and identity from that export. The shipped table also works offline. Sports finalist registers are checked against the winning and runner-up sides in the dated results. [PubChem periodic table export](https://pubchem.ncbi.nlm.nih.gov/rest/pug/periodictable/JSON)

The answer index retains significant symbols and writing-system marks. For example, C, C# and C++ have separate entries. Exact source names take precedence over secondary aliases. If a shared alias could refer to a candidate that fails the rule or lacks evidence, the player can enter a more specific name. A confirmed alternative spelling uses the same score as the primary name. The checker requests confirmation for spelling suggestions. Once a cohort reaches the calibration threshold, the scoring calculation includes eligible answers that received zero choices and keeps ties together at the top-score cutoff.

In the answer browser, **Also accepts** lists spellings that earn the displayed answer’s score. **Other recorded names** identifies ambiguous spellings and shows the actual score for names shared by several qualifying answers.

The independent builder checks Wikidata name statements for recently collected classes, date ranges and relationships. A separate cursor also walks the general entity index. Each batch reads the recorded names and independently verifies the number of statements for each requested entity. Completed batches survive interruptions. The publication keeps the entity ID, statement ID, language, property and spelling. It accepts best-ranked names and excludes statements marked uncertain or scoped to a part of the entity. A verified refresh can withdraw a name. The compiler joins these names to entities that meet the question’s rule and checks additional spellings for known competing owners. An accepted spelling and its ruling stay available to other teams in that round, including after a later source change. [Official name](https://www.wikidata.org/wiki/Property:P1448) · [Native label](https://www.wikidata.org/wiki/Property:P1705) · [Short name](https://www.wikidata.org/wiki/Property:P1813) · [Title](https://www.wikidata.org/wiki/Property:P1476)

## Compiling an individual prompt

An unfamiliar name in a recognised relationship creates a durable lookup request. The builder sends the target name to Wikidata entity search and also considers names already attested in the local catalogue. It reads each candidate’s source labels and aliases, checks the complete spelling, and verifies a relationship to a member of the requested class. A date in the question stays in that check. The resulting IDs and names become part of the same offline publication the game already reads. Ambiguous or truncated searches can be narrowed with the intended Wikidata Q number.

When the source uses another class for works credited to the same person or organisation, the builder can prepare up to two independently verified questions with those class names. The picker shows the wording and count for each. The regular background cycle also samples up to 48 current entities of one class at a time and rotates across classes and entities. A relationship shared by at least two sampled entities can nominate a new question. The builder then checks the whole class and relationship. For a watched relationship it counts the members and reads the ordered IDs twice, including unnamed members. A changed ID invalidates the capture even when the count stays the same. These lookups and checks use public Wikidata services and work without an LLM.

Node serves the room and player requests. It runs one Python research process at a time. The source graph lives in `<DB_PATH>.knowledge/graph.sqlite`. Published banks, indexed aliases, review receipts and round references live in the existing quiz database. The existing quiz backup therefore includes everything required to play or restore a saved edition. Include the knowledge directory in a volume backup to preserve imported source records and local feed files.

## Updating a source

The server also installs the full Wikidata5M transductive graph in `world/wikidata5m-v1.sqlite` beside the quiz database. The initial background job downloads 366 MB, verifies both archive checksums and publishes the index atomically. Allow 6 GB of free disk during installation. The completed index is approximately 2.55 GB. Later starts reuse it. To provision before launch, run `python scripts/knowledge/world_index.py --db storage/quiz.sqlite`. An existing read-only copy can be shared by setting `KNOWLEDGE_WORLD_DB` to its absolute path. Published round editions stay in the quiz database.

The compiler traverses subclass, instance and occupation links and can intersect them with any indexed object-valued property. Use “with [property] [item]” or “whose [property] is [item]” for a published property name. If a term remains ambiguous after matching its English article title and the question's constraints, the error lists candidate Wikidata IDs. An ID such as `Q11424` can be used in a prompt. The relationship snapshot is joined to the bundled Wikidata length and mass index by entity ID. Missing measurements can request current Wikidata statements. Relationship questions check live statements on first use and reuse a completed scope for subsequent previews. Live class requests page through membership and fetch multilingual names in batches. A failed page preserves the preceding published release. Source updates revisit recently used queries on a rotating schedule. Broad categories take longer to materialize and snapshot coverage is open.

Place a data file and a matching `*.manifest.json` in `<DB_PATH>.knowledge/feeds/`. Publish the completed data file before its manifest and give every changed release a new revision. Alternatively, import a manifest directly.

```sh
python scripts/knowledge/worker.py import --db storage/quiz.sqlite --manifest /path/to/dairy.manifest.json
```

A manifest identifies the publisher, revision, source URL, file and vocabulary.

For a remote structured feed, add a `download` field containing its HTTPS export URL. The publisher fetches it on each source check. The `file` suffix selects the parser. A change in the downloaded contents creates a new source revision even when the manifest stays the same. Offline builds use the last validated download. An optional `sha256` pins a specific export. Open classes can grow through matching entity IDs and subclass links across feeds.

To follow a live Wikidata topic, supply a `*.manifest.json` like this one. This example follows musical instruments and their country of origin. Omit `propertyId` and `relation` to follow membership alone.

```json
{"wikidata":{"classId":"Q34379","label":"musical instrument","propertyId":"P495","relation":"from"}}
```

The publisher refreshes registered scopes daily, including the bundled Wikidata scopes. It pages through class membership, fetches labels and aliases, then looks up each enrolled property for those members. New named members become eligible when the source reports them. Withdrawals disappear from the next edition. Membership and dependent properties share one cache transaction. On a failed request the builder reports the retained capture or defers publication if the remaining sources describe incompatible editions. These scopes have open coverage.

Native CSV, JSON, JSONL and Parquet exports can use `rowMapping` to name their ID, display-name, alias, relationship and measurement columns. For a nested JSON export, `rowsPath` locates the records and `countPath` identifies the publisher’s independently reported total. For example, the following fields describe a collection with a single recorded material for each exhibit.

```json
{
  "rowsPath": ["catalogue", "pieces"],
  "countPath": ["total"],
  "rowMapping": {
    "id": "inventoryNumber", "prefix": "museum:", "name": "title", "class": "exhibit",
    "aliases": ["localTitle"], "relations": {"material": "made from"}
  },
  "completeFacets": {"exhibit": {"made from": "Name an exhibit made from {value}"}}
}
```

The publisher checks every entry and requires exactly one value for each declared complete property. A qualifying property value shared by at least two entries generates a complete question and answer list. Missing values or a discrepancy in the reported total prevent the faulty edition from replacing the working one.

A register can use `suggestFor` to associate broad host wording with a more precise question. Its keys identify classes in the declared census and its values list phrases. For example, “country in South America” offers the UN M49 country-or-area scope. The host sees the definition before play. For names joined from a second provider, an entity’s `nameSources` entries record the publisher, URL, matching identifier and spellings. The answer browser includes the citation with that entity.

A register can attach a verified Wikidata identity through `readershipItem` with an `item` and evidence `url`. The UN M49 adapter checks the two ISO letter codes and the numeric code together. Each triple must identify one Wikidata item and each item must have one triple. The compiler uses this link for readership and graph familiarity. UN identifiers continue to determine membership. The answer browser links to the matching item and the readership release. Missing or conflicting links leave the entity eligible with unknown readership.

```json
{
  "source": "dairy-register",
  "revision": "edition-2",
  "url": "https://example.org/register",
  "file": "varieties.jsonl",
  "terms": {"cheese": ["cheese variety"]}
}
```

Each line contributes assertions about a stable entity. The following is a fictional example.

```json
{"id":"register:42","name":"Tomorrow Wheel","types":["cheese"],"aliases":["Roue de demain"]}
```

Records from different sources may use the same entity ID to contribute names, measurements or relationships. Each release replaces that publisher’s assertions atomically. Omitted rows withdraw the publisher’s support. The worker queues saved questions for a new edition when active source revisions change. Open and historical rounds keep their original answers, aliases and scores. Local feeds are checked during compilation and scheduled refresh. A host can explicitly request live discovery in the preview form.

The single worker checks public releases daily while the application is running. To trigger a source check manually, run the following command when the worker is idle.

```sh
python scripts/knowledge/worker.py refresh --db storage/quiz.sqlite
```

## Data model

- `terms` maps class or reference IDs to phrases. Optional `parents` pairs link subclasses to their parents. Entity IDs are the join key across publishers.
- A verified compound Wikidata question also supplies evidence for its subject class and each direct relationship on the same item. Broader open banks combine those records with the historical graph. An exact source check takes precedence for the classification route it checked, including withdrawals.
- A work can have a broad primary type and a more precise creative form. When Wikidata records the form and its taxonomy connects that form to the verified work class, the builder can generate a question about the form itself. It combines distinct entity IDs, retains evidence for each answer, and refreshes the form and primary-type routes independently when a host uses the question. [Wikidata’s form of creative work property](https://www.wikidata.org/wiki/Property:P7937)
- On first use of a class that the source places under “work”, a class question starts a resumable check of all works carrying that form. A question with a relationship starts a narrower check for the requested form and relationship together. Both routes work before any examples have been collected for that class. [Wikidata’s work class](https://www.wikidata.org/wiki/Q386724)
- Before replacing a class roster, the builder reads its ordered IDs twice and checks the source count at the end of each pass. The two membership fingerprints must agree, including for entities without names. Both passes resume after interruptions. Only the first fetches multilingual names from the [Wikidata entity API](https://www.wikidata.org/w/api.php?action=help&modules=wbgetentities).
- Records can include `edges` such as `[["written by","person:42"]]`. Register names for target IDs in `terms`. A measurement in `facts` has a property, lower and upper values, unit, evidence basis and optional source URL. Supported dimensions are length, mass and year. For example, `{"property":"length","low":8,"high":12,"unit":"cm","basis":"typical"}`.
- A `typical-lower-bound` or `record-lower-bound` can establish a greater-than comparison. Overlapping intervals appear in the borderline count. Native exports can map an interval with, for example, `"measures":{"minLength":{"highColumn":"maxLength","property":"length","unit":"cm","basis":"typical"}}`. Lower and upper columns must contain paired values. Regional names shared by several qualified members receive a conservative shared score.
- A finite source can declare a `census` of separately known class counts. A tournament manifest can contain a `competition` object in the format of `data/knowledge/finals.json`. The importer checks the edition calendar and both participants. Open classes, bounded live searches and subjects with missing traits carry an open-coverage label.
- CSV, JSON, JSONL and Parquet files accept the same fields. Encode nested CSV fields as JSON arrays. An expanded JSON-LD file uses manifest `mapping` keys `label`, `types`, `relations` and `measures` to map source predicates to quiz concepts. Linked identifiers are read locally.
- A record can include `recall` observations with the exact player prompt, participant total and count. Samples need at least 200 participants. The compiler uses a 95% upper bound and protects ties at the cutoff. At most 5% of a bank can earn the highest score. Without recall evidence, the top 1% budget requires two measured familiarity signals. Missing popularity data defaults to a middle-low tier. Newly compiled editions also use verified first choices once at least 200 teams across three rooms have contributed. An exact new answer from a compatible open edition can earn up to tier two in the original round. The proof and the accepted score are retained.
- When several eligible entities share a display name, each could prompt the same answer. The compiler adds their separate survey bounds, capped at one, before awarding a rarity bonus. A team’s actual first choice of the shared name counts once, including when the source titles differ in capitalisation.

## Capacity and verification

The catalogue builder checks the published SHA-256 and size of each QRank release. Each Wikidata ID carries its readership across Wikimedia projects over 52 weeks. A question preview reports how many answers have readership data. For a title shared by several qualifying entities, the most-read owner establishes a minimum familiarity. Before awarding a rarity bonus, the compiler compares all of the title's owners' combined readership with that minimum for other names. Missing observations count against the bonus. Surveys and observed team choices take precedence. [QRank methodology](https://github.com/brawer/wikidata-qrank/blob/main/doc/design.md)

For a readership-based top score, every owner must have measured readership and one to six recorded incoming graph links. Even assigning every missing observation a lower readership and including all ties, the name must rank in the least-read 2% of this question's answers. All estimated methods together can award the top tier to at most 1% of names. A tie that would exceed the remaining allowance receives the lower tier. The answer evidence includes the readership, its worst-case position and the source release. These signals estimate player familiarity until teams supply enough first-choice observations.

The independent builder allows a publication up to 50 GB. It stages a new edition on the same volume before replacing the existing file, so allow space for both editions, inputs and temporary indexes. The per-game research graph and its quiz database connection each cap their SQLite files at roughly 8.2 GB. The worker checks local storage and free space before starting. Its feed input has a 2 GB aggregate budget, bounded individual files and a lower limit for formats decoded as a single JSON document. DuckDB uses one thread and a 384 MB memory limit.

Run `npm test`, `npm run test:browser`, `npm run test:knowledge-load` and `python -m unittest discover -s tests` for game, browser, performance and evidence checks. The load test submits answers from thirty teams in an open round while a second room builds a 100,000-member bank. It then reopens the cached preview and times thirty concurrent submissions against the new bank.

Source attribution and collection methods accompany the projections. [Dataset notes](../../data/knowledge/README.md)
