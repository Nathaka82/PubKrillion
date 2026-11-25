import { clean, demand, Fault } from "./rules.ts";
import { execFile } from "node:child_process";
import { fileURLToPath } from "node:url";
import { existsSync } from "node:fs";
import { dirname, join } from "node:path";

const instructions = [
  "Turn a host's idea into up to three distinct, short English pub quiz questions, each starting with Name.",
  "Respect every explicit condition. For a loose theme, try a familiar entry point and a more surprising angle.",
  "Each question must have many possible answers and an objective membership rule a database can check.",
  "Use a named class, a class with one named relationship and target, or a length or mass comparison with a named reference.",
  "For relationships use natural wording such as 'directed by' or the form 'whose [property] is [target]'.",
  "Use everyday language. Specify the country, era or meaning when needed. Avoid subjective labels, stereotypes and answer spoilers.",
  "Return an empty list if no supported rule preserves the host's conditions. Only return question wordings.",
].join(" ");

let quotaRetryAt = 0;
const quotaNotice = "OpenAI reports that this project has exhausted its API credit.";

async function requestDrafts(idea: string, model: string, key: string, sourceIdeas: string[]): Promise<string[]> {
  if (Date.now() < quotaRetryAt) throw new Fault(503, quotaNotice);
  let response;
  try {
    response = await fetch("https://api.openai.com/v1/responses", {
      method: "POST",
      redirect: "error",
      signal: AbortSignal.timeout(25000),
      headers: { Authorization: "Bearer " + key, "Content-Type": "application/json" },
      body: JSON.stringify({
        model, instructions, input: sourceIdeas.length
          ? JSON.stringify({ hostIdea: idea, relatedQuestionsFromIndex: sourceIdeas })
          : idea, store: false, service_tier: "default", max_output_tokens: 600,
        reasoning: { effort: "low" },
        text: { format: {
          type: "json_schema", name: "question_drafts", strict: true,
          schema: {
            type: "object", additionalProperties: false, required: ["questions"],
            properties: { questions: { type: "array", maxItems: 3, items: { type: "string" } } },
          },
        } },
      }),
    });
  } catch {
    throw new Fault(503, "OpenAI could not be reached. Try again or enter a question directly.");
  }
  if (response.status === 401 || response.status === 403)
    throw new Fault(503, "Check the server's OpenAI project key and model permissions.");
  if (response.status === 429) {
    const payload = await response.json().catch(() => ({}));
    if (payload.error?.type === "insufficient_quota" || payload.error?.code === "credit_balance_exhausted") {
      quotaRetryAt = Date.now() + 60000;
      throw new Fault(503, quotaNotice);
    }
    throw new Fault(503, "OpenAI has limited this request. Check the project's quota or try again later.");
  }
  demand(response.ok, 503, "OpenAI could not generate drafts for this request.");
  try {
    const result = await response.json();
    const message = result.output?.find((item: { type: string }) => item.type === "message");
    if (result.status === "completed" && message?.content?.length === 1 &&
        message.content[0].type === "output_text") {
      const { questions } = JSON.parse(message.content[0].text);
      if (Array.isArray(questions) && questions.length <= 3 && questions.every(
        q => typeof q === "string" && q.length <= 240 && /^Name\s+\S/i.test(q) && !/[\r\n]/.test(q),
      )) return [...new Set<string>(questions.map(q => q.trim()))];
    }
  } catch {
    // A partial response or malformed draft cannot be offered for compilation.
  }
  throw new Fault(422, "No usable wording came back. Try a more specific idea or enter a question directly.");
}

// Keeping the promise also coalesces simultaneous requests for the same room and idea.
const recent = new Map<string, Promise<string[]>>();

export async function questionDrafts(room: string, raw: unknown, sourceIdeas: string[] = []) {
  const idea = clean(raw, 450, "idea");
  const key = process.env.OPENAI_API_KEY?.trim();
  demand(key, 503, "Set OPENAI_API_KEY on the server to enable question drafts.");
  const model = process.env.OPENAI_MODEL?.trim() || "gpt-5.4-mini-2026-03-17";
  const cacheKey = JSON.stringify([room, model, idea, sourceIdeas]);
  const hit = recent.get(cacheKey);
  if (hit) return hit;
  if (recent.size >= 128) recent.delete(recent.keys().next().value!);
  const pending = requestDrafts(idea, model, key, sourceIdeas);
  recent.set(cacheKey, pending);
  try {
    return await pending;
  } catch (error) {
    recent.delete(cacheKey);
    throw error;
  }
}

export type CatalogueRule = { key: string; edition: string; prompt: string; records: number; complete: boolean };

export async function discoverDrafts(file: string, room: string, raw: unknown, completeOnly = false, useModel = false, surprise = false, waitForSource = false) {
  const idea = surprise ? "" : clean(raw, 450, "idea");
  const direct = !completeOnly && /^(?:please )?(?:name|list|identify|give me|can you name)\s/i.test(idea);
  if (direct && !existsSync(process.env.KNOWLEDGE_CATALOGUE || join(dirname(file), "knowledge-catalogue.sqlite")))
    return { questions: [idea], rules: [], note: "" };
  const script = fileURLToPath(new URL("../scripts/knowledge/ideas.py", import.meta.url));
  const { local, rules, awaitingEdition, sourceNote } = await new Promise<{ local: string[]; rules: CatalogueRule[]; awaitingEdition?: string; sourceNote?: string }>((resolve, reject) => {
    const child = execFile(process.env.KNOWLEDGE_PYTHON || (process.platform === "win32" ? "python" : "python3"),
      [script, file], { timeout: 6000, windowsHide: true, maxBuffer: 32768 }, (err, out) => {
        if (err) return reject(new Fault(503, "The local question index could not be searched."));
        try {
          const result = JSON.parse(out);
          if (waitForSource && typeof result.awaitingEdition === "string")
            resolve({ local: [], rules: [], awaitingEdition: result.awaitingEdition });
          else if (typeof result.error === "string") reject(new Fault(422, result.error));
          else resolve(result);
        } catch { reject(new Fault(503, "The local index returned no usable result.")); }
      });
    child.stdin?.on("error", () => {});
    child.stdin?.end(JSON.stringify({ idea, completeOnly, surprise, direct }));
  });
  if (awaitingEdition) return {questions: [], rules: [], note: "", awaitingEdition};
  if (rules.length || completeOnly || surprise) return {
    rules, questions: rules.map(rule => rule.prompt),
    note: rules.length ? sourceNote || "" : surprise ? "There are no published questions to sample yet." : "No complete published register matches this topic.",
  };
  if (direct) return { questions: [idea], rules: [], note: "" };
  let drafts: string[] = [], note = "";
  if (useModel && process.env.OPENAI_API_KEY?.trim()) {
    try { drafts = await questionDrafts(room, idea, local); }
    catch (e) { if (!(e instanceof Fault)) throw e; note = e.message; }
  }
  const options = [...local.slice(0, 1), ...drafts, ...local.slice(1)];
  if (!options.length) options.push("Name " + idea);
  return { questions: [...new Set(options)].slice(0, 3), rules: [], note };
}
