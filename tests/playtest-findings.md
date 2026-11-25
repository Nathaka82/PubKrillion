# Krillion playtest findings

Three independent player agents joined through the game UI. The host managed rooms, reveals and rulings. Players chose their own names and answers and could message the host. Coral Chaos played spontaneously on a 360 × 740 phone, Kelp Wanted sought unusual answers on a 1366 × 768 laptop, and Coral Atlas approached the game as a newcomer on a 768 × 1024 tablet. The tablet used reduced motion.

The first two games used the existing cheese and element rounds. The third used a generated fish comparison. A fourth game repeated the cheese and fish prompts using the revised sources and interface.

| Severity | Player observation | Change and verification |
| --- | --- | --- |
| High | Cornish Yarg and Gjetost both needed manual credit. Two players asked the host how to request it. | Generated cheese banks incorporate Wikidata labels and regional names. Both players independently retried their original guesses and received immediate credit as Yarg and Brunost. For unresolved names, the review button records the submitted wording. |
| High | The first generated fish bank missed Oarfish and Coelacanth. | Added regional names and a conservative adult-size lower bound from documented maturity measurements. Both players retried the same guesses and locked them in immediately. Requests made during the earlier game remained eligible for review. |
| Medium | “Bigger” left one player wondering which measurement counted. | An optional “What counts?” disclosure explains that the comparison uses usual length. The tablet player read it and chose trumpetfish based on that rule. |
| Medium | Everyone tied in two rounds. Players could see the points and depth but could not explain the tie. | The help text explains the five bands and when scores are set. The newcomer reported that ties then made sense. The generator distinguishes source familiarity from recall samples, and limits the highest tier. |
| Medium | The depth column clipped another crew on the small phone. After fitting three lanes, long fish names touched across columns. | Three lanes now share the available width, with space between wrapped answer labels. The phone player confirmed all three lanes were visible. Chromium and WebKit have a regression check for the final label spacing at 360 px. |
| Low | After entering Gjetost, a player read “You submitted Brunost.” | The reveal shows the original submission and names its scored equivalent separately. |

These sessions exercised spelling variants, accented and regional names, keyboard submission, touch controls, the clarification disclosure, appeals, the dive and standings. They were simulated playtests. They do not establish how frequently real people in different age, language or cultural groups would recall an answer.
