# Krill Club · The Pub Dive

An independent, Krillion-inspired multiplayer pub quiz for a society night. One room lasts the whole crawl. The host opens one general-knowledge question at a time and decides when to finish. Teams use their phones and need no accounts.

## Run locally

Use Node.js 24.12 or later.

```sh
npm ci
npm run setup
npm start
```

Open **http://127.0.0.1:3000**. Select **Host console** and enter the generated `HOST_PASSWORD` from the project's private `.env`. Create an event, open **Invite teams**, then open the join link in a second browser profile to rehearse. Local QR links to loopback work on the same computer. For a LAN rehearsal set `BIND_ADDRESS=0.0.0.0` and `PUBLIC_ORIGIN=http://<computer-LAN-IP>:3000` in the local environment and use that address on both phones.

## Generate questions from linked data

In the host picker, a year such as **1999** can suggest questions across subjects. Entering **films from 1999** offers the specific release-date rule. The answer bank shows each qualifying film’s recorded dates and links to the source statements. Regional releases and re-releases qualify. The independent builder regularly samples its own graph for new date questions, and hosts can ask for subjects and periods it has not yet collected.

In **Start round**, select **Generate from a prompt**. Enter the wording you want players to see. The preview shows how the system interpreted it, verified answers, sources, unresolved cases and the scoring rule. Browse the answers, then save the edition to the room library. Regenerating the same prompt updates the library entry. Rounds already opened keep their original edition.

For topic suggestions, select **Draft questions from an idea**. Enter a topic such as “Nintendo”, or choose **Surprise me** to draw questions from the local publication. Each suggestion shows how many source records match. **Only use complete source registers** limits suggestions to an inventory checked against its publisher’s recorded total. For example, “Nobel Physics 1990s” uses the Nobel Prize register for that category and period. An unrecognised topic in this mode returns no questions.

The separate **knowledge-builder** service reads the general knowledge graph and specialist sources, links records by entity ID, and publishes one SQLite file. The game uses that file offline to suggest questions, compile answer banks and check guesses. A topic such as “bridge” can generate a comparison with a named bridge. A host can also enter a precise question such as “Name a river longer than the Thames” or “Name a film directed by Steven Spielberg and starring Tom Hanks”. The picker shows each published rule’s answer count and prepares the selected bank. The Compose builder checks for newly used graph questions every minute and refreshes their current Wikidata records for subsequent editions. For a local installation, build once with `python scripts/knowledge/catalogue.py` or run continuously with `--watch`. [Publisher and source settings](scripts/knowledge/README.md)

The published graph keeps individual source links, including relationships shared by only one or two entities. A host can type “bridges in France” to see a source-backed question and a narrower one based on the lengths of those bridges. A request such as “Name a bridge in France longer than 2 km” checks the type, country and measurement against the same entity ID. The builder refreshes the class hierarchy so new or corrected links can affect many different questions in the next edition.

The builder can turn a release year into several different questions by finding directors, cast members and other relationships shared by films in that year. The same process works for other dated subjects, such as museums with a recorded founding year and location. A host can also ask for a particular combination. For “Name a film with a recorded release in 2001 and directed by Hayao Miyazaki”, every answer needs a release date in that year and a director credit on the same film. A regional release qualifies. The answer browser links to the evidence for both facts.

The builder can learn names that appeared after the original graph was published. For example, a host can ask for a work performed by a recent artist. The builder looks up the artist, verifies the spelling and the performer credit, and publishes questions under the subject types the source actually records. A host asking for songs may also see a question specifically about singles. While the online builder checks a new name, the open picker waits for publication and then shows the results automatically. The builder independently looks for new topics by sampling current relationships across the subjects in its catalogue.

The builder also reads official names, native names, short names and titles recorded for the same entity. A player can enter a former stadium name or a museum’s local name. The answer earns the score of the underlying venue or museum. The host’s answer browser links these names to the individual source statements. When an updated source verifies an alternate spelling during an open round, subsequent teams can use that spelling for the same score.

On its first online run, the builder installs a local Wikidata5M index of 20.6 million factual links. Allow 6 GB of free disk space for initial installation. It joins named records to a separate input containing over two million length and mass measurements. The game only needs the resulting publication for indexed questions. Running the game alone without a publication can also install the world input for its research worker. A host can ask for a class, an occupation, a type of instrument, a relationship such as “Name a film directed by Akira Kurosawa”, or a measurement comparison. The publisher can refresh host-used graph questions from current Wikidata records, including labels and aliases in any source language. The preview identifies the source edition and its coverage. [Installation and data model](scripts/knowledge/README.md)

**Local requirements.** Generated questions use Python 3.11 or later. The bundled projections and JSON, JSONL, CSV and expanded JSON-LD imports run with the standard library. Install `duckdb==2.0.1` for Parquet imports and `wordfreq==3.1.1` for refreshing name familiarity. Set `KNOWLEDGE_PYTHON` to that interpreter if needed. The application image includes both dependencies.

**Draft a question with OpenAI.** Create a project key and add these lines to the private `.env` next to `package.json`. Restart `npm start` or rebuild and recreate the Compose app after updating the code and environment. [Project API keys](https://platform.openai.com/api-keys)

```dotenv
OPENAI_API_KEY=replace-with-your-project-key
OPENAI_MODEL=gpt-5.4-mini-2026-03-17
```

OpenAI wording suggestions are off by default. A host can select **Ask OpenAI for wording if the catalogue has no match** on a topic search. With a configured key, an unmatched open-coverage topic can then make one model request. The compiler checks each candidate against the sources. The publisher, complete-register mode, player submissions and scoring all work without OpenAI. Billing depends on the account’s current terms. [OpenAI data controls](https://platform.openai.com/settings/organization/data-controls/sharing) · [Offer terms](https://help.openai.com/en/articles/10306912-sharing-feedback-evaluation-and-fine-tuning-data-and-api-inputs-and-outputs-with-openai)

New generated editions score automatically. An exact answer missing from an open bank can qualify through the room’s updated published edition when it verifies the same rule. Krillion keeps the proof for other teams in that round. Unknown names can be retried. Only the first unaided, verified choice per team and source edition counts towards gameplay calibration. Misses and prompted corrections earn no calibration weight. Closed rounds contribute once at least 200 teams across three rooms have supplied choices. Scores are recalibrated when the sample doubles. Each round keeps the scores it opened with.

**Optional local interpretation.** Standard prompt forms run directly. For paraphrases, the optional Compose overlay runs quantized Qwen2.5 1.5B through Ollama. The model selects a typed query, and the local compiler checks the source evidence. The first start downloads the model. Model and application containers have a combined memory limit below 8 GiB. The model port stays inside the Docker network.

```sh
docker compose -f compose.yaml -f compose.knowledge.yaml up -d --build
```

An existing local Ollama installation can be selected with `KNOWLEDGE_MODEL_URL` and `KNOWLEDGE_MODEL`. The VPS overlay also composes with `compose.vps.yaml`. Use the same overlay when bringing up or updating the model service. [Knowledge storage and import format](scripts/knowledge/README.md) · [Dataset attribution](data/knowledge/README.md) · [Blind playtest findings](tests/playtest-findings.md)

## Deploy on a VPS

For **krillion.duckdns.org** on the VPS that already runs Stackimals, follow [the Docker installation and automatic deployment guide](INSTALL.md). It uses your existing Caddy proxy and includes GitHub Actions, backups and rollback.

### Standalone VPS with its own Caddy

Use a VPS with Docker Engine and Docker Compose. Point a public hostname at it and allow inbound TCP ports 80 and 443. Copy the project to the server. Create a private `.env` next to `compose.yaml` with the following two entries. Set `HOST_PASSWORD` to your chosen value. To change it later, edit `.env` and restart the local server or run `docker compose up -d` on the VPS.

```dotenv
DOMAIN=quiz.your-society.org
HOST_PASSWORD=your-chosen-password
```

```sh
chmod 600 .env
docker compose up --build -d
docker compose ps
```

Open the HTTPS address for your domain. Caddy handles TLS and proxies to the app on the private Compose network. The health endpoint is `/healthz`. Inspect startup errors with `docker compose logs --tail=80 app caddy`. Rooms and host/team sessions survive app restarts in the `quiz_data` volume. Keep the same Compose project name when deploying an update.

## Host the frontend on GitHub Pages

GitHub Pages can serve the host console and team screens. The VPS runs the quiz API and SQLite database. To put the whole app on the VPS, use the preceding section.

1. Deploy the VPS with an HTTPS domain. Set `FRONTEND_URL` in its `.env` to the full Pages URL, including the repository path, for example `https://your-society.github.io/quiz/`. Run `docker compose up -d` to apply it.
2. In your GitHub repository, select **Settings → Pages → Source → GitHub Actions**. Under **Settings → Secrets and variables → Actions → Variables**, create `PAGES_API_ORIGIN` with the VPS origin, for example `https://quiz.your-society.org`.
3. Run **Actions → Publish quiz frontend → Run workflow**, then open the Pages URL. The invite link and QR code point teams back to that site. The phone remembers its team across reloads. A host login lasts for the browser tab's session.

The API trusts the configured frontend origin. All repositories served under one `github.io` hostname share that origin, so use a dedicated Pages domain if others can publish content there. To build the static files locally, run `npm run build:pages -- https://quiz.your-society.org`. The output is in `dist/pages/`. [GitHub Pages workflow documentation](https://docs.github.com/en/pages/getting-started-with-github-pages/using-custom-workflows-with-github-pages)

## Run the event

Each phone plays short sound effects after its first interaction. The speaker button in the header or during a dive remembers that browser’s mute setting. Reduced motion shows the dive results immediately.

1. Create a room and share its six-character code, join link or QR. One phone represents a team. **Transfer a team** generates a five-character recovery code for a replacement phone. It expires in ten minutes. If a join or transfer response is lost, retry on the same phone within ten minutes to recover the acknowledgement.
2. Use **Questions** to edit the library, or open **Answers** beside a question to browse its accepted answers by rarity. The host can also open **Accepted answers** during a round or from its history. The built-in questions are available in every room. The five rarity tiers earn **10, 15, 60, 85 and 100 points**.
3. Tap **Start round**, choose a category or search for a question, then select a countdown or **No time limit**. Open **Team status** on the host’s phone to see who is still thinking and who has submitted. The host can lock early while teams are still thinking. As soon as every team has an accepted answer, the host sees **Reveal answers**. Each round takes its own copy of the question. Previously played questions stay available for a deliberate replay.
4. Teams tap **Dive** to submit. The first accepted answer is final. An invalid guess can be retried, and a spelling suggestion only counts when confirmed. A disconnected phone retries its pending submission while the round is open. At lock, eligible teams with no accepted answer receive **Miss · 0 points · 0 m**.
5. Reveal the dive to see teams start side by side with their previous totals, descend together to their round depths and collect their points. Scroll sideways to see more teams. A phone returning during the dive catches up. After it ends, **Watch this round’s dive** plays a missed reveal from the start while that round is current. The host can correct a ruling after locking or in **Round history**. Select a missed team to see its last submitted wording. Choose a listed answer, award an unlisted answer and tier, or score a miss. An unlisted award becomes available at the same value to other teams in that round. Ruling history records the team and decision.
6. Leave standings on screen between pubs. **Finish quiz** closes the room and displays the final podium. To remove an old event, open **Your rooms**, choose **Delete**, and type its exact name. This permanently removes that room’s teams, scores and question library.

When upgrading an existing database, open events use the new scoring scale for every round. Finished events retain their original point totals. Misses are recorded for the eligible teams from the first lock after upgrading. Earlier reveals show the results recorded at the time.

## Question categories

Open **Start round** to browse by category or search across the library. Select **Answers** beside a question to see its accepted answers, scope and source. **Questions** opens the same categories for editing. A room keeps its own edits, play counts and past round snapshots.

| Category | Example rounds |
| --- | --- |
| General Knowledge | British prime ministers, the Greek alphabet and LEGO themes |
| Sport | Olympic venues, Wimbledon finalists, Formula One champions and ultimate frisbee |
| Geography | Countries, capitals, national parks and world currencies |
| Film & TV | Pixar films, Oscar nominees and sitcom characters |
| Music | UK number ones, Eurovision songs and orchestral instruments |
| Animals | British birds, butterflies and living mammals |
| Science | Chemical elements, constellations and the Solar System |
| Books & Literature | Booker Prize winners, Narnia and the Fellowship of the Ring |
| Food & Drink | Cocktails, cheeses and pasta shapes |
| Games | PC gaming devices |

Each question defines its own scope. For example, the British bird round covers all 636 species on the cited edition of the BOU checklist. The Bond round covers the 25 Eon films through *No Time to Die*. The release-techniques round groups variants by throw family. The generator ranks familiar spellings and synonyms together. Corpus frequency supplies the first four tiers. In the standard release the 100-point tier requires an editorial rarity selection and is limited to one slot per 20 answers, with one possible slot in smaller banks. A round can have no 100-point answer. Equivalent ABBA and German-border editions share their scoring banks. Hosts can change the tiers in their room.

### How answers are matched

Teams type a guess and tap **Dive**. A full answer or unambiguous declared alias locks immediately. An inferred shortened name asks the team to confirm. A shared surname asks for more detail. Currency rounds accept full currency names, recognised unit names such as **yen**, and codes such as **JPY**. Entering **Japan** leaves the team free to try another answer. The receipt shows the full accepted answer.

A close spelling, a specific example or a paraphrase can produce **Did you mean?**. The team can confirm or edit. In a football positions round, **the player in goal** can suggest **Goalkeeper** even when the host entered only the position names. A suggestion carries no points until confirmed. An unanswered round scores a miss.

The server first checks the current round’s immutable answer bank, spelling variants, aliases and dictionary relationships. For names and category questions with unresolved guesses, it compares their meaning with the accepted names and relevant WordNet definitions. The prompt and topic help select dictionary senses. Two local MiniLM models retrieve candidates and check borderline descriptions. The matcher uses both the strength of the evidence and the lead over other answers. Some valid descriptions will need rewording. Host edits and custom packs use the same matcher.

Models and the built-in catalogue’s search vectors ship with the server. Phones send the guess only on Dive. Selecting a custom question prepares its index in the background. New vectors are cached next to the quiz database in a regenerable file ending in `.minilm-v1.sqlite`. Very large new packs can take longer on a small VPS. Exact, spelling and dictionary matches work during preparation and if model inference is unavailable. Set `QUIZ_SEMANTIC=off` to run only those matchers. After changing the bundled packs or encoder, rebuild the shipped index with `npm run build:answer-index`. [Model sources and licences](data/minilm/README.md)

### Generating and calibrating questions

Build from an identifiable source and edition. Derive the prompt’s eligibility rule and the answer rows from the same selection. For example, the women’s finals generator takes the first and last tournament years from the results and includes both finalists for every completed edition. The food generator reads name and synonym columns, and the currency generator selects tender codes across territories from Unicode CLDR 47. Regional names and accents resolve to the same scoring entry. Hosts can search the picker by region or era and use its filters to hide rounds marked “Specialist” or “Explicit titles”.

To calibrate a generated question with a playtest, give at least 200 independent participants its exact prompt, the same time, and an opportunity to name as many answers as they can without seeing the bank. Count each participant once per canonical answer, combining aliases. Pass a JSON file to `uv run --script scripts/packs/build.py --recall-data survey.json` and use the same file with `--check`. The file maps a generated question ID to an object with `prompt`, `participants` and `counts`. Include every canonical name in `counts`, with an explicit zero for an unmentioned answer. The generator rejects incomplete samples and prompts from a different edition.

With calibration, the generator reserves 100 points for the least-recalled answers whose upper 95% recall estimate is below 2%. Existing editorial tiers break ties in observed recall. A tie that still crosses the slot limit receives 85 points. These thresholds measure recall in the surveyed audience. The bundled catalogue uses editorial estimates until a suitable survey is supplied.

### Custom packs and sources

**Questions → Import questions** accepts JSON files up to 16 MiB, with up to 1,000 questions per upload and 10,000 answers per question. **Download example JSON** provides an editable sample. Each question has a stable `id`, `title`, `topic`, `prompt` and `answers`. Optional fields include `source`, `coverage`, `context` (searchable picker notes) and `matching` (`names`, `categories` or `full` for full names and codes). Each answer has a British display `name` and a `depth` from 1 to 5. Use `aliases` for interchangeable names and `examples` for specific variants the team should confirm. Use `nameGB` to supply a British display name for an imported entry. Film titles and other proper names retain their credited spelling.

The six [released packs](data/packs/) ship with the server. The [build scripts](scripts/packs/) read source tables, spreadsheets, dictionaries and APIs, and add reviewed aliases and rules questions. Each question links to its source. The [source manifest](data/packs/sources.json) records SHA-256 hashes for fetched build inputs. Tables and sports results reflect the edition or period in the question. TVmaze cast and episode data are attributed to [TVmaze](https://www.tvmaze.com/api#licensing) under CC BY-SA. WFDF rules and signals are attributed to the [World Flying Disc Federation](https://rules.wfdf.sport/). The dictionary uses the [Princeton WordNet licence](data/WordNet-LICENSE.txt).

After `npm ci`, a maintainer can reconstruct the packs with `uv run --script scripts/packs/build.py`. Add `--check` to compare canonical identities, aliases, examples, rarity, sources and the vocabulary against the released files. Source downloads are cached. Set `KRILL_PACK_CACHE` to a fresh directory to check upstream changes. Review differences before releasing an updated pack. Normal hosting and play use the bundled JSON and require no Python or external data service.

## Back up and restore

Take an online, integrity-checked backup in the backup volume, then copy it off the VPS. Choose a fresh filename each time.

```sh
docker compose exec app node scripts/database.mjs backup crawl.sqlite
docker compose cp app:/app/backups/crawl.sqlite ./crawl.sqlite
```

To restore, copy the backup into the backup volume if required. Stop all processes using the database before running restore. The command validates the incoming backup, preserves the previous database and its SQLite sidecar files in an adjacent `.before-restore-…` directory, then installs the verified copy. This also works when the previous database is unreadable.

```sh
docker compose cp ./crawl.sqlite app:/app/backups/crawl.sqlite
docker compose stop app
docker compose run --rm --no-deps app node scripts/database.mjs restore crawl.sqlite
docker compose up -d app
```

Local installs can use the same script with `node scripts/database.mjs backup crawl.sqlite` and, after stopping the server, `node scripts/database.mjs restore crawl.sqlite`. Backups contain event records and login sessions. Store them privately.

## Checks

```sh
npm run build
npm test
npx playwright install chromium webkit
npm run test:browser
```

Browser checks use isolated databases and emulated phone viewports. The split-hosting phone checks exercise a Pages-style repository path with a separate API origin. The artwork and interface run locally in the browser and have no runtime dependency on krillion.io. VT323 is distributed under the SIL Open Font License in `public/fonts/OFL.txt`.
