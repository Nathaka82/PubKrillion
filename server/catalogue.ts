import { readFileSync } from "node:fs";
import { demand, type Question } from "./rules.ts";

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
