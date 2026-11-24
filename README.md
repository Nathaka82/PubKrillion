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
2. Use **Questions** to edit the library, or open **Answers** beside a question to browse its accepted answers by rarity. The host can also open **Accepted answers** during a round or from its history. Each event starts with 20 categories. The five rarity tiers earn **10, 15, 60, 85 and 100 points**.
3. Tap **Start round**, pick a question and select a countdown or manual lock. Open **Team status** on the host’s phone to see who is still thinking and who has submitted. The manual lock button shows how many teams are outstanding. Each round takes its own copy of the question. Previously played questions stay available for a deliberate replay.
4. Teams tap **Dive** to submit. The first accepted answer is final. An invalid guess can be retried, and a spelling suggestion only counts when confirmed. A disconnected phone retries its pending submission while the round is open. At lock, eligible teams with no accepted answer receive **Miss · 0 points · 0 m**.
5. Reveal the dive to see teams start side by side with their previous totals, descend together to their round depths and collect their points. Scroll sideways to see more teams. A phone returning during the dive catches up. After it ends, **Watch this round’s dive** plays a missed reveal from the start while that round is current. The host can correct a ruling after locking or in **Round history**. Select a missed team to award its accepted answer individually or return it to zero. Ruling history records the team and decision.
6. Leave standings on screen between pubs. **Finish quiz** closes the room and displays the final podium. To remove an old event, open **Your rooms**, choose **Delete**, and type its exact name. This permanently removes that room’s teams, scores and question library.

When upgrading an existing database, open events use the new scoring scale for every round. Finished events retain their original point totals. Misses are recorded for the eligible teams from the first lock after upgrading. Earlier reveals show the results recorded at the time.

## Question packs and British English

On the host’s phone, open **Questions → Browse packs**, choose a pack, then tap **Add 30 questions to room**. Tap a question title in the preview to see its prompt, answer count, scope and source. The same catalogue is available from **Start round**. Reinstalling a pack keeps the room’s edits and skips questions already present. Six pack questions also feature in the starter library and share their score tables and play counts. Upgrading combines the old copies, preserves round snapshots and keeps separately edited versions when both copies were customised.

| Pack | Examples |
| --- | --- |
| General Knowledge | Chemical elements and symbols, Booker winners, cocktails and Paris 2024 medal events |
| Film & TV | Oscar nominees, Bake Off bakers, Strictly contestants, Doctor Who episodes and studio films |
| Music | UK number ones, Glastonbury headliners, Eurovision, recorded songs and instruments |
| Geography | UN member states, British cities, mountain peaks, Japanese prefectures and river systems |
| Animals | The British List, living mammal species, sharks, butterflies and more |
| Ultimate Frisbee | 22 rounds on play, coaching and rules, plus eight on university, club and international competition |

Each question defines its own scope. For example, the British bird round covers all 636 species on the cited edition of the BOU checklist. The Bond round covers the 25 Eon films through *No Time to Die*. The release-techniques round groups variants by throw family. Rarity starts with English word frequency and uses editorial adjustments for familiar or obscure answers. Hosts can change the tiers in their room.

### How answers are matched

Teams type a guess and tap **Dive**. A full answer, an exact alias or a distinctive shortened name locks immediately. For example, **Nadal** identifies Rafael Nadal in a tennis round with one Nadal. A shared surname asks the team for more detail. The receipt shows the full accepted answer.

A close spelling, a specific example or a paraphrase can produce **Did you mean?**. The team can confirm or edit. In a football positions round, **the player in goal** can suggest **Goalkeeper** even when the host entered only the position names. A suggestion carries no points until confirmed. An unanswered round scores a miss.

The server first checks the current round’s immutable answer bank, spelling variants, aliases and dictionary relationships. For unresolved guesses it compares their meaning with the accepted names and relevant WordNet definitions. The prompt and topic help select dictionary senses. Two local MiniLM models retrieve candidates and check borderline descriptions. The matcher uses both the strength of the evidence and the lead over other answers. Some valid descriptions will need rewording. Host edits and custom packs use the same matcher.

Models and the built-in catalogue’s search vectors ship with the server. Phones send the guess only on Dive. Selecting a custom question prepares its index in the background. New vectors are cached next to the quiz database in a regenerable file ending in `.minilm-v1.sqlite`. Very large new packs can take longer on a small VPS. Exact, spelling and dictionary matches work during preparation and if model inference is unavailable. Set `QUIZ_SEMANTIC=off` to run only those matchers. After changing the bundled packs or encoder, rebuild the shipped index with `npm run build:answer-index`. [Model sources and licences](data/minilm/README.md)

### Custom packs and sources

**Questions → Import pack** still accepts JSON files up to 16 MiB, with up to 1,000 questions per upload and 10,000 answers per question. **Download example JSON** provides an editable sample. Each question has a stable `id`, `title`, `topic`, `prompt` and `answers`. Optional fields include `source`, `coverage` and `matching` (`names` or `categories`). Each answer has a British display `name` and a `depth` from 1 to 5. Use `aliases` for interchangeable names and `examples` for specific variants the team should confirm. Use `nameGB` to supply a British display name for an imported entry. Film titles and other proper names retain their credited spelling.

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
