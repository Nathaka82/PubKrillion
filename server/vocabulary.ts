import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { join } from "node:path";

// British display spellings for everyday categories. Official names are authored verbatim.
const spellings = new Map(
  Object.entries({
    color: "colour",
    colors: "colours",
    colored: "coloured",
    coloring: "colouring",
    aluminum: "aluminium",
    colorless: "colourless",
    flavor: "flavour",
    flavors: "flavours",
    favorite: "favourite",
    favorites: "favourites",
    center: "centre",
    centers: "centres",
    theater: "theatre",
    theaters: "theatres",
    liter: "litre",
    liters: "litres",
    centimeter: "centimetre",
    centimeters: "centimetres",
    gray: "grey",
    yogurt: "yoghurt",
    eggplant: "aubergine",
    eggplants: "aubergines",
    zucchini: "courgette",
    zucchinis: "courgettes",
    cilantro: "coriander",
    soybean: "soya bean",
    soybeans: "soya beans",
    mollusk: "mollusc",
    mollusks: "molluscs",
    airplane: "aeroplane",
    airplanes: "aeroplanes",
    pajamas: "pyjamas",
    jewelry: "jewellery",
    tire: "tyre",
    tires: "tyres",
  }),
);
export function britishLabel(label: string) {
  return label.replace(/[a-z]+/gi, (word) => {
    const uk = spellings.get(word.toLowerCase());
    if (!uk) return word;
    if (word === word.toUpperCase()) return uk.toUpperCase();
    return word[0] === word[0].toUpperCase()
      ? uk[0].toUpperCase() + uk.slice(1)
      : uk;
  });
}

// The released graph is compiled from linked source records. Relations point
// from a named example to its class and are never followed in reverse.
const graph: { equivalents: string[][]; parents: [string, string][] } =
  JSON.parse(
    readFileSync(new URL("../data/vocabulary.json", import.meta.url), "utf8"),
  );
const concepts = new Map(
  graph.equivalents.flatMap((group) =>
    group.map((term) => [term, group[0]] as const),
  ),
);
export function concept(term: string) {
  const bare = britishLabel(term).replace(/^(?:a|an|the) /, "");
  return concepts.get(bare) ?? bare;
}
const parents = new Map<string, Set<string>>();
for (const [child, parent] of graph.parents) {
  const key = concept(child);
  if (!parents.has(key)) parents.set(key, new Set());
  parents.get(key)!.add(concept(parent));
}
export function isBroaderTerm(broad: string, narrow: string) {
  const target = concept(broad);
  let frontier = [concept(narrow)];
  const visited = new Set(frontier);
  for (let i = 0; i < 6 && frontier.length; i++) {
    const next = [];
    for (const term of frontier)
      for (const parent of parents.get(term) ?? []) {
        if (parent === target && target !== concept(narrow)) return true;
        if (!visited.has(parent)) {
          visited.add(parent);
          next.push(parent);
        }
      }
    frontier = next;
  }
  return false;
}
const entityLengths = new Map<number, string[]>();
for (const key of parents.keys()) {
  if (!entityLengths.has(key.length)) entityLengths.set(key.length, []);
  entityLengths.get(key.length)!.push(key);
}
export function relatedTerms(
  term: string,
  distance: (a: string, b: string, max: number) => number,
) {
  let frontier = [term];
  // Correct a single typo in a complete known entity. No suffix or brand guesswork.
  if (!parents.has(term) && term.length >= 6) {
    const near = [];
    for (let len = term.length - 1; len <= term.length + 1; len++) {
      for (const candidate of entityLengths.get(len) ?? []) {
        if (distance(term, candidate, 1) <= 1) near.push(candidate);
      }
    }
    frontier.push(...near);
  }
  const visited = new Set(frontier);
  for (let depth = 0; depth < 6 && frontier.length; depth++) {
    const next = new Set<string>();
    for (const node of frontier) {
      for (const parent of parents.get(node) ?? []) {
        if (!visited.has(parent)) {
          visited.add(parent);
          next.add(parent);
        }
      }
    }
    frontier = [...next];
  }
  return visited;
}

// WordNet orders senses by usage. Using the primary noun sense at both ends
// avoids mapping a perfectly ordinary word through an obscure secondary sense.
let dictionary: { nouns: Map<string, number[]>; data: Buffer } | undefined;
function wordnet() {
  if (dictionary) return dictionary;
  const dir = createRequire(import.meta.url)("wordnet-db").path;
  const nouns = new Map<string, number[]>();
  for (const line of readFileSync(join(dir, "index.noun"), "utf8").split(
    "\n",
  )) {
    if (!line || line.startsWith(" ")) continue;
    const fields = line.trimEnd().split(/ +/);
    nouns.set(
      fields[0].replaceAll("_", " "),
      fields.slice(6 + Number(fields[3])).map(Number),
    );
  }
  dictionary = { nouns, data: readFileSync(join(dir, "data.noun")) };
  return dictionary;
}
const usTerms = new Map([...spellings].map(([us, uk]) => [uk, us]));
export function nounSenses(term: string): number[] {
  const { nouns } = wordnet();
  const word = term.replace(/[a-z]+/g, (w) => usTerms.get(w) ?? w);
  // Try regular singulars when the spelling has no dictionary entry.
  const forms = [
    word,
    word.replace(/ies$/, "y"),
    word.replace(/(ch|sh|ss|x|z)es$/, "$1"),
    word.replace(/([^s])s$/, "$1"),
  ];
  for (const form of forms) {
    const sense = nouns.get(form);
    if (sense !== undefined) return sense;
  }
  return [];
}
export function nounSense(term: string): number | undefined {
  return nounSenses(term)[0];
}
// Imported dictionary sense IDs may come from a different WordNet edition.
export function definition(offset: number) {
  const { data } = wordnet();
  if (
    !Number.isInteger(offset) ||
    offset < 0 ||
    offset >= data.length ||
    (offset > 0 && data[offset - 1] !== 10)
  )
    return "";
  const record = data.toString("utf8", offset, data.indexOf(10, offset));
  return (record.split(" | ")[1] ?? "").split('; "')[0].trim();
}
export function parentSenses(offset: number) {
  const { data } = wordnet();
  const fields = data
    .toString("utf8", offset, data.indexOf(10, offset))
    .split(" | ")[0]
    .trim()
    .split(/ +/);
  const first = 4 + 2 * parseInt(fields[3], 16);
  const result: number[] = [];
  for (
    let i = first + 1, end = i + 4 * Number(fields[first]);
    i < end;
    i += 4
  ) {
    if (fields[i] === "@" || fields[i] === "@i")
      result.push(Number(fields[i + 1]));
  }
  return result;
}
