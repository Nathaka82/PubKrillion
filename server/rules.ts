import {
  britishLabel,
  concept,
  relatedTerms,
  nounSense,
  parentSenses,
} from "./vocabulary.ts";

export type Answer = {
  name: string;
  aliases: string[];
  examples?: string[];
  wordnetSenses?: number[];
  depth: number;
};
export type Matching = "names" | "categories" | "full";
export type Question = {
  id: string;
  title: string;
  topic: string;
  prompt: string;
  source: string;
  coverage?: string;
  context?: string;
  matching: Matching;
  answers: Answer[];
};
export class Fault extends Error {
  status: number;
  suggestion?: string;
  constructor(status: number, message: string, suggestion?: string) {
    super(message);
    this.status = status;
    this.suggestion = suggestion;
  }
}
export function demand(
  condition: unknown,
  status: number,
  message: string,
): asserts condition {
  if (!condition) throw new Fault(status, message);
}
export function clean(value: unknown, max: number, label: string, min = 1) {
  demand(typeof value === "string", 400, "Enter a valid " + label + ".");
  const s = value.trim().replace(/\s+/gu, " ");
  const length = Array.from(s).length;
  demand(
    length >= min && length <= max,
    400,
    label + " must be " + min + "–" + max + " characters.",
  );
  return s;
}
export function normal(s: string) {
  return s
    .toLowerCase()
    .normalize("NFKD")
    .replace(/\p{M}/gu, "")
    .replace(/(^|\s)\+(?=\d)/g, "$1over ")
    .replace(/(^|\s)[−-](?=\d)/g, "$1under ")
    .replace(/&/g, " and ")
    .replace(/['’]/g, "")
    .replace(/[^\p{L}\p{N}]+/gu, " ")
    .trim();
}
export function validateQuestion(raw: any, id: string): Question {
  demand(raw && typeof raw === "object", 400, "Enter a question.");
  demand(
    Array.isArray(raw.answers) &&
      raw.answers.length > 0 &&
      raw.answers.length <= 10000,
    400,
    "Add between 1 and 10,000 accepted answers.",
  );
  const source = clean(raw.source ?? "", 600, "source", 0);
  demand(
    !source || /^https?:\/\//i.test(source),
    400,
    "Use an http or https source URL.",
  );
  const seen = new Map<string, number>();
  const matching: Matching = raw.matching ?? "categories";
  demand(
    matching === "names" || matching === "categories" || matching === "full",
    400,
    "Choose a matching mode.",
  );
  const answers = raw.answers.map((a: any, i: number) => {
    demand(a && typeof a === "object", 400, "Check answer " + (i + 1) + ".");
    const original = clean(a.name, 140, "answer");
    const name = clean(
      a.nameGB ??
        (matching === "categories" ? britishLabel(original) : original),
      140,
      "British answer",
    );
    demand(
      Number.isInteger(a.depth) && a.depth >= 1 && a.depth <= 5,
      400,
      "Choose a depth from 1 to 5 for " + name + ".",
    );
    demand(
      Array.isArray(a.aliases ?? []) && (a.aliases?.length ?? 0) <= 256,
      400,
      "Check the aliases for " + name + ".",
    );
    const aliases: string[] = (a.aliases ?? []).map((x: unknown) =>
      clean(x, 140, "alias"),
    );
    if (original !== name && !aliases.includes(original))
      aliases.push(original);
    demand(
      aliases.length <= 256,
      400,
      "Use up to 256 aliases for " + name + ".",
    );
    for (const term of [name, ...aliases]) {
      const key = normal(term);
      demand(
        key && (!seen.has(key) || seen.get(key) === i),
        400,
        "Conflicting answer or alias: " + term,
      );
      seen.set(key, i);
    }
    demand(
      a.examples === undefined ||
        (Array.isArray(a.examples) && a.examples.length <= 256),
      400,
      "Check the examples for " + name + ".",
    );
    const wordnetSenses = a.wordnetSenses ?? [];
    demand(
      Array.isArray(wordnetSenses) &&
        wordnetSenses.length <= 32 &&
        wordnetSenses.every(
          (id: unknown) =>
            typeof id === "number" &&
            Number.isSafeInteger(id) &&
            id >= 0 &&
            id < 100000000,
        ),
      400,
      "Check the dictionary senses for " + name + ".",
    );
    const examples: string[] = (a.examples ?? []).map((value: unknown) =>
      clean(value, 140, "example"),
    );
    demand(
      examples.every((v) => normal(v)),
      400,
      "Enter a word or name for each example.",
    );
    return {
      name,
      aliases,
      ...(examples.length ? { examples } : {}),
      ...(wordnetSenses.length ? { wordnetSenses } : {}),
      depth: a.depth,
    };
  });
  return {
    id,
    title: clean(raw.title, 100, "title"),
    topic: clean(raw.topic, 60, "topic"),
    prompt: clean(raw.prompt, 400, "prompt"),
    source,
    ...(raw.context ? { context: clean(raw.context, 200, "picker notes") } : {}),
    ...(raw.coverage ? { coverage: clean(raw.coverage, 900, "coverage") } : {}),
    matching,
    answers,
  };
}
// Optimal string alignment counts a neighbouring transposition as a single typo.
function distance(a: string, b: string, limit: number) {
  let prev = Array.from({ length: b.length + 1 }, (_, j) => j);
  let older = prev;
  for (let i = 1; i <= a.length; i++) {
    const row = [i];
    const start = Math.max(1, i - limit);
    if (start > 1) row[start - 1] = limit + 1;
    for (let j = start; j <= Math.min(b.length, i + limit); j++) {
      row[j] = Math.min(
        row[j - 1] + 1,
        (prev[j] ?? limit) + 1,
        prev[j - 1] + Number(a[i - 1] !== b[j - 1]),
      );
      if (i > 1 && j > 1 && a[i - 1] === b[j - 2] && a[i - 2] === b[j - 1])
        row[j] = Math.min(row[j], older[j - 2] + 1);
    }
    older = prev;
    prev = row;
  }
  return prev[b.length];
}
// Null marks a key which belongs to more than one accepted answer.
function addKey<K>(map: Map<K, Answer | null>, key: K, answer: Answer) {
  if (!map.has(key)) map.set(key, answer);
  else if (map.get(key) !== answer) map.set(key, null);
}
const withoutArticle = (key: string) => key.replace(/^(?:a|an|the) /, "");
const writingVariant = (key: string) => withoutArticle(key).replaceAll(" ", "");
const linkingWords = new Set(
  "a an the of to for and in on at by from with".split(" "),
);
const meaningfulWords = (key: string) =>
  key.split(" ").filter((word) => !linkingWords.has(word));
function inOrder(needle: string[], name: string) {
  let i = 0;
  for (const word of name.split(" ")) if (word === needle[i]) i++;
  return i === needle.length;
}
function indexBank(bank: Answer[], mode: Matching) {
  const exact = new Map<string, Answer>();
  const byLength = new Map<number, string[]>();
  const examples = new Map<string, Answer | null>();
  const spellings = new Map<string, Answer | null>();
  const variants = new Map<string, Answer | null>();
  const fragments = new Map<string, Map<Answer, string[]>>();
  const categories = new Map<string, Answer | null>();
  const senses = new Map<number, Answer | null>();
  for (const answer of bank) {
    for (const example of answer.examples ?? [])
      addKey(examples, normal(example), answer);
    // Only the display name defines the category's dictionary meaning. An
    // alias like "keyboard" for "computer keyboard" can have a broader sense.
    if (mode === "categories") {
      const meanings = answer.wordnetSenses ?? [
        nounSense(concept(normal(answer.name))),
      ];
      for (const sense of meanings) {
        if (sense !== undefined) addKey(senses, sense, answer);
      }
    }
    for (const name of [answer.name, ...answer.aliases]) {
      const key = normal(name);
      if (exact.has(key)) continue;
      exact.set(key, answer);
      // Titles and names can be shortened. In category packs, also recognise
      // proper names such as a full athlete's name, keeping common nouns scoped.
      if (
        key.includes(" ") &&
        (mode === "names" ||
          (mode === "categories" &&
            /^(?:\p{Lu}\p{L}+\s+)+\p{Lu}\p{L}+$/u.test(name)))
      ) {
        for (const word of new Set(meaningfulWords(key))) {
          if (
            word.length < 2 ||
            !/\p{L}/u.test(word) ||
            (mode === "categories" && nounSense(word) !== undefined)
          )
            continue;
          if (!fragments.has(word)) fragments.set(word, new Map());
          const owners = fragments.get(word)!;
          if (!owners.has(answer)) owners.set(answer, []);
          owners.get(answer)!.push(key);
        }
      }

      if (mode === "categories") {
        const category = concept(key);
        addKey(categories, category, answer);
      }
    }
  }
  // A declared name wins over an authored example with the same spelling.
  for (const [key, answer] of [...examples, ...exact])
    spellings.set(key, answer);
  const typoKeys = new Map(spellings);
  for (const [word, owners] of fragments) {
    if (!typoKeys.has(word) && word.length >= 4)
      typoKeys.set(
        word,
        owners.size === 1 ? owners.keys().next().value! : null,
      );
  }
  for (const [key, answer] of spellings) {
    const short = withoutArticle(key);
    if (short !== key && !spellings.has(short)) {
      if (!typoKeys.has(short)) typoKeys.set(short, answer);
      else if (typoKeys.get(short) !== answer) typoKeys.set(short, null);
    }
    const variant = writingVariant(key);
    if (variant.length >= 4) {
      if (!variants.has(variant)) variants.set(variant, answer);
      else if (variants.get(variant) !== answer) variants.set(variant, null);
    }
  }
  for (const key of typoKeys.keys()) {
    if (!byLength.has(key.length)) byLength.set(key.length, []);
    byLength.get(key.length)!.push(key);
  }
  return {
    exact,
    fragments,
    examples,
    typoKeys,
    variants,
    byLength,
    categories,
    senses,
    mode,
  };
}
const indices = new WeakMap<Answer[], ReturnType<typeof indexBank>>();
const britishOrder = new Intl.Collator("en-GB", {
  numeric: true,
  sensitivity: "base",
});
export function answerPage(q: Question, params: URLSearchParams) {
  const depth = Number(params.get("depth"));
  const offset = Number(params.get("offset"));
  demand(
    Number.isInteger(depth) &&
      depth >= 0 &&
      depth <= 5 &&
      Number.isSafeInteger(offset) &&
      offset >= 0,
    400,
    "Invalid answer page.",
  );
  const search = normal(clean(params.get("q") ?? "", 140, "search", 0));
  const counts = [0, 0, 0, 0, 0, 0];
  const relevance = new Map<Answer, number>();
  const matches = q.answers
    .filter((a) => {
      counts[a.depth]++;
      if (depth && a.depth !== depth) return false;
      if (!search) return true;
      let best = 3;
      for (const term of [a.name, ...a.aliases, ...(a.examples ?? [])]) {
        const key = normal(term);
        if (key === search) { best = 0; break; }
        if (key.startsWith(search)) best = 1;
        else if (key.includes(search)) best = Math.min(best, 2);
      }
      relevance.set(a, best);
      return best < 3;
    })
    .sort((a, b) =>
      (relevance.get(a) ?? 0) - (relevance.get(b) ?? 0) ||
      a.depth - b.depth || britishOrder.compare(a.name, b.name));
  const start = Math.min(
    offset,
    Math.max(0, Math.floor((matches.length - 1) / 40) * 40),
  );
  return {
    prompt: q.prompt,
    source: q.source,
    coverage: q.coverage,
    counts,
    total: q.answers.length,
    filtered: matches.length,
    offset: start,
    answers: matches.slice(start, start + 40),
  };
}
export function resolveAnswer(
  raw: string,
  bank: Answer[],
  mode: Matching = "categories",
) {
  let index = indices.get(bank);
  if (index?.mode !== mode) {
    index = indexBank(bank, mode);
    indices.set(bank, index);
  }
  const key = normal(raw);
  const literal = index.exact.get(key);
  const words = meaningfulWords(key);
  // A full alternative title (e.g. a regional release name) still identifies
  // the original when a sequel contains it. A shortened surname can be shared.
  if (
    literal &&
    (normal(literal.name) === key || !inOrder(words, normal(literal.name)))
  )
    return { answer: literal, exact: true };
  const pivot = words.find((word) => index.fragments.has(word));
  const owners = pivot ? index.fragments.get(pivot) : undefined;
  if (owners) {
    let found: Answer | undefined = literal;
    for (const [answer, names] of owners) {
      if (!names.some((name) => inOrder(words, name))) continue;
      if (found && found !== answer) return null;
      found = answer;
    }
    if (found) return { answer: found, exact: found === literal };
  }
  if (literal) return { answer: literal, exact: true };
  if (index.examples.has(key)) {
    const answer = index.examples.get(key);
    return answer ? { answer, exact: false } : null;
  }
  const variant = writingVariant(key);
  if (variant.length >= 4 && index.variants.has(variant)) {
    const answer = index.variants.get(variant);
    return answer ? { answer, exact: false } : null;
  }
  if (mode === "categories") {
    const category = concept(key);
    const inferred = new Set<Answer | null>();
    for (const term of relatedTerms(category, distance)) {
      if (index.categories.has(term)) inferred.add(index.categories.get(term)!);
    }
    if (inferred.size) {
      const [answer] = inferred;
      return inferred.size === 1 && answer ? { answer, exact: false } : null;
    }
    const first = nounSense(category);
    let frontier = first === undefined ? [] : [first];
    const visited = new Set(frontier);
    for (let level = 0; frontier.length && level <= 6; level++) {
      const candidates = new Set(
        frontier
          .filter((s) => index.senses.has(s))
          .map((s) => index.senses.get(s)),
      );
      if (candidates.size) {
        const [answer] = candidates;
        return candidates.size === 1 && answer
          ? { answer, exact: false }
          : null;
      }
      frontier = [...new Set(frontier.flatMap(parentSenses))].filter(
        (s) => !visited.has(s),
      );
      frontier.forEach((s) => visited.add(s));
    }
  }
  const spelling = withoutArticle(key);
  const numbers = spelling.match(/\d+/g)?.join(" ");
  const max = spelling.length >= 9 ? 2 : spelling.length >= 4 ? 1 : 0;
  const prefixLimit = mode === "names" && spelling.length >= 5 ? 2 : max;
  if (!max) return;
  let best = Math.max(max, prefixLimit) + 1;
  let winner: Answer | undefined;
  for (
    let len = spelling.length - max;
    len <= spelling.length + Math.max(max, prefixLimit);
    len++
  ) {
    for (const candidate of index.byLength.get(len) ?? []) {
      if (
        mode === "full" &&
        (candidate.length <= 3 ||
          candidate.split(" ").length !== spelling.split(" ").length)
      )
        continue;
      // A whole token omitted from a name is a fragment, not a spelling error.
      if (
        candidate.length > spelling.length &&
        (" " + candidate + " ").includes(" " + spelling + " ")
      )
        continue;
      if (
        candidate.length < spelling.length &&
        (" " + spelling + " ").includes(" " + candidate + " ")
      )
        continue;
      if (numbers !== candidate.match(/\d+/g)?.join(" ")) continue;
      const limit = candidate.startsWith(spelling) ? prefixLimit : max;
      if (Math.abs(spelling.length - candidate.length) > limit) continue;
      const score = distance(spelling, candidate, Math.min(limit, best));
      if (score === undefined || score > limit) continue;
      const answer = index.typoKeys.get(candidate) ?? undefined;
      if (score < best) {
        best = score;
        winner = answer;
      } else if (score === best && winner !== answer) winner = undefined;
    }
  }
  return winner
    ? { answer: winner, exact: false }
    : best <= Math.max(max, prefixLimit)
      ? null
      : undefined;
}
// null distinguishes an ambiguous guess from one eligible for semantic lookup.
export function matchAnswer(
  raw: string,
  bank: Answer[],
  mode: Matching = "categories",
) {
  return resolveAnswer(raw, bank, mode) ?? undefined;
}
