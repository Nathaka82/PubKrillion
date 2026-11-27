// One-time upgrade for installations that previously ran the question builder.
import { existsSync, rmSync } from "node:fs";
import { join, dirname, resolve } from "node:path";
import { DatabaseSync } from "node:sqlite";
import { activeLegacyRounds } from "../server/upgrade.ts";

const file = resolve(process.env.DB_PATH || "storage/quiz.sqlite");
const mode = process.argv[2];
if (!["check", "clean"].includes(mode)) throw new Error("Expected check or clean");
if (existsSync(file)) {
  const db = new DatabaseSync(file, {readOnly:true});
  try {
    const version = db.prepare("PRAGMA user_version").get().user_version;
    if (mode === "check") {
      if (version < 8 && activeLegacyRounds(db)) process.exitCode = 42;
    } else {
      if (version < 8) throw new Error("Upgrade the game database before removing builder data.");
      const directory = dirname(file);
      // Only these builder-owned paths are disposable. Never remove the game
      // database, its sidecars, the answer matcher cache or the backup volume.
      for (const name of ["knowledge-sources", "knowledge-requests", "knowledge-catalogue.sqlite",
        "knowledge-catalogue.sqlite.build", "knowledge-catalogue.sqlite-wal",
        "knowledge-catalogue.sqlite-shm", "knowledge-catalogue.sqlite.lock", "world"]) {
        const target = join(directory, name);
        if (target !== file) rmSync(target, {recursive:true, force:true});
      }
      rmSync(file + ".knowledge", {recursive:true, force:true});
    }
  } finally { db.close(); }
}
