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
const packs = new Map<string, Question[]>();
export const builtInQuestions = new Map<string, Question>();
export const packForQuestion = new Map<string, string>();
for (const pack of catalogue) {
  const { questions } = JSON.parse(readFileSync(new URL(pack.id + ".json", root), "utf8"));
  const rows: Question[] = questions.map((q: Question) => validateQuestion(q, q.id));
  packs.set(pack.id, rows);
  for (const question of rows) {
    if (builtInQuestions.has(question.id)) throw new Error("Duplicate question ID " + question.id);
    builtInQuestions.set(question.id, question);
    packForQuestion.set(question.id, pack.title);
  }
}

export function catalogueQuestions(id: string): Question[] {
  const questions = packs.get(id);
  demand(questions, 404, "Question pack not found.");
  return questions;
}

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
