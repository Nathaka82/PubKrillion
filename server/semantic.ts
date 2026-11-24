import { Worker } from "node:worker_threads";
import { createHash } from "node:crypto";
import type { Question } from "./rules.ts";

export type Suggestion = { name?: string; ambiguous?: boolean; busy?: boolean };
type Request = { finish: (result: Suggestion) => void; timer: NodeJS.Timeout };
export class SemanticMatcher {
  private worker?: Worker;
  private failed = false;
  private keys = new WeakMap<Question, string>();
  private banks = new Set<string>();
  private requests = new Map<number, Request>();
  private guesses = new Map<string, Promise<Suggestion>>();
  private sequence = 0;
  private cache: string;
  constructor(path: string) {
    this.cache = path === ":memory:" ? path : path + ".minilm-v1.sqlite";
  }
  private offline(error: unknown) {
    if (!this.failed)
      console.warn(
        "Semantic suggestions unavailable. Spelling and dictionary matching are active.",
        String(error),
      );
    this.failed = true;
    for (const { finish, timer } of this.requests.values()) {
      clearTimeout(timer);
      finish({});
    }
    this.requests.clear();
    this.banks.clear();
    void this.worker?.terminate();
    this.worker = undefined;
  }
  prepare(q: Question) {
    if (this.failed || process.env.QUIZ_SEMANTIC === "off") return;
    try {
      if (!this.worker) {
        const worker = (this.worker = new Worker(
          new URL("./semantic-worker.mjs", import.meta.url),
          { workerData: { cache: this.cache }, execArgv: [] },
        ));
        worker.on("error", (error) => this.offline(error));
        worker.on("exit", (code) => {
          if (this.worker === worker)
            this.offline("Worker exited (" + code + ")");
        });
        worker.on("message", (msg) => {
          if (msg.failed) {
            this.offline(msg.failed);
            return;
          }
          const job = this.requests.get(msg.id);
          if (job) {
            clearTimeout(job.timer);
            this.requests.delete(msg.id);
            job.finish(msg.result);
          }
        });
        worker.unref();
      }
      let key = this.keys.get(q);
      if (!key) {
        key = createHash("sha256")
          .update(
            JSON.stringify([
              q.topic,
              q.prompt,
              q.answers.map((a) => [a.name, a.wordnetSenses]),
            ]),
          )
          .digest("hex");
        this.keys.set(q, key);
      }
      if (this.banks.delete(key)) {
        this.banks.add(key);
        return key;
      }
      if (this.banks.size >= 6) {
        const oldest = this.banks.values().next().value!;
        this.banks.delete(oldest);
        this.worker.postMessage({ type: "drop", key: oldest });
      }
      this.banks.add(key);
      this.worker.postMessage({
        type: "prepare",
        key,
        q: {
          topic: q.topic,
          prompt: q.prompt,
          answers: q.answers.map(({ name, wordnetSenses }) => ({
            name,
            wordnetSenses,
          })),
        },
      });
      return key;
    } catch (error) {
      this.offline(error);
    }
  }
  async suggest(q: Question, raw: string): Promise<Suggestion> {
    const key = this.prepare(q);
    if (!key || !this.worker) return {};
    const shared = JSON.stringify([key, raw]);
    const running = this.guesses.get(shared);
    if (running) return running;
    if (this.requests.size >= 64) return { busy: true };
    const worker = this.worker,
      id = ++this.sequence;
    const job = new Promise<Suggestion>((finish) => {
      const until = Date.now() + 3000;
      const timer = setTimeout(() => {
        this.requests.delete(id);
        finish({ busy: true });
      }, 3000);
      this.requests.set(id, { finish, timer });
      worker.postMessage({ type: "suggest", key, id, raw, until });
    });
    this.guesses.set(shared, job);
    try {
      return await job;
    } finally {
      this.guesses.delete(shared);
    }
  }
  close() {
    const worker = this.worker;
    this.worker = undefined;
    this.failed = true;
    for (const { timer, finish } of this.requests.values()) {
      clearTimeout(timer);
      finish({});
    }
    this.requests.clear();
    return worker?.terminate();
  }
}
