// Compile trusted release data for offline, first-round semantic search.
import { Worker } from "node:worker_threads";
import { once } from "node:events";
import { rename, rm } from "node:fs/promises";
import { DatabaseSync } from "node:sqlite";
import { fileURLToPath } from "node:url";
import { builtInQuestions } from "../server/catalogue.ts";
const dest = fileURLToPath(
  new URL("../data/minilm/catalogue.sqlite", import.meta.url),
);
const temp = dest + "." + process.pid + ".tmp";
const worker = new Worker(
  new URL("../server/semantic-worker.mjs", import.meta.url),
  { workerData: { cache: temp, build: true } },
);
try {
  for (const q of builtInQuestions.values()) {
    if (q.matching === "full") continue;
    const ready = once(worker, "message");
    worker.postMessage({ type: "prepare", key: q.id, q });
    const [message] = await ready;
    if (message.prepared !== q.id)
      throw Error(message.failed ?? "Unexpected worker reply");
    worker.postMessage({ type: "drop", key: q.id });
  }
  await worker.terminate();
  const db = new DatabaseSync(temp);
  try {
    db.exec("PRAGMA journal_mode=DELETE; VACUUM");
    if (db.prepare("PRAGMA integrity_check").get().integrity_check !== "ok")
      throw Error("Index check failed");
    console.log(
      "Bundled vectors:",
      db.prepare("SELECT COUNT(*) AS n FROM vectors").get().n,
    );
  } finally {
    db.close();
  }
  await rename(temp, dest);
} finally {
  await worker.terminate();
  for (const ext of ["", "-wal", "-shm"]) await rm(temp + ext, { force: true });
}
