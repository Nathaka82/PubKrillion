import { readFileSync } from "node:fs";
import { demand, validateQuestion, type Question } from "./rules.ts";

type Pack = {
  id: string;
  title: string;
  description: string;
  count: number;
  answers: number;
};
const root = new URL("../data/packs/", import.meta.url);
export const catalogue: Pack[] = JSON.parse(
  readFileSync(new URL("index.json", root), "utf8"),
);
export function catalogueQuestions(id: string): Question[] {
  demand(
    catalogue.some((pack) => pack.id === id),
    404,
    "Question pack not found.",
  );
  return JSON.parse(readFileSync(new URL(id + ".json", root), "utf8"))
    .questions;
}
const starter: Question[] = JSON.parse(
  readFileSync(new URL("../questions.json", root), "utf8"),
);
export const samplePack: { attribution: string; questions: Question[] } =
  JSON.parse(readFileSync(new URL("../pub-sampler.json", root), "utf8"));
export const published = new Map(
  [
    ...starter,
    ...catalogue.flatMap((p) => catalogueQuestions(p.id)),
    ...samplePack.questions,
  ].map((q) => [q.id, validateQuestion(q, q.id)] as const),
);
// The sample download includes a second edition of the world currency question.
export const builtInQuestions = new Map(published);
// These starter IDs have their own play history but ask for the same answer set.
for (const [starterId, packId] of [["abba", "music-abba-albums"], ["german-borders", "geography-germany"]]) {
  const shared = published.get(packId)!;
  const question = { ...published.get(starterId)!, answers: shared.answers };
  published.set(starterId, question);
  builtInQuestions.set(starterId, question);
}
builtInQuestions.delete("sampler-world-currencies");

export function previewPack(id: string) {
  return catalogueQuestions(id).map(
    ({ title, prompt, coverage, source, answers }) => ({
      title,
      prompt,
      coverage,
      source,
      count: answers.length,
    }),
  );
}
