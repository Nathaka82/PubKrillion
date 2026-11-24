import { DatabaseSync, backup } from "node:sqlite";
import {
  existsSync,
  mkdirSync,
  mkdtempSync,
  copyFileSync,
  renameSync,
  rmSync,
} from "node:fs";
import { resolve, dirname, join } from "node:path";

const [action, given] = process.argv.slice(2);
if (!["backup", "restore"].includes(action) || (action === "restore" && !given))
  throw new Error("Use backup [filename] or restore filename.");
const root = resolve(process.env.BACKUP_DIR || "backups");
const filename =
  given || "quiz-" + new Date().toISOString().replace(/[:.]/g, "-") + ".sqlite";
if (!/^[\w.-]+\.sqlite$/.test(filename))
  throw new Error("Use a filename such as crawl.sqlite.");
const dbPath = resolve(process.env.DB_PATH || "storage/quiz.sqlite"),
  saved = join(root, filename);
const source = action === "backup" ? dbPath : saved;
const dest = action === "backup" ? saved : dbPath;
if (!existsSync(source)) throw new Error("Database source does not exist.");
if (action === "backup" && existsSync(dest))
  throw new Error("Choose a new backup filename.");
mkdirSync(dirname(dest), { recursive: true });
const tmp = dest + "." + process.pid + ".tmp";
try {
  const db = new DatabaseSync(source, { readOnly: true });
  try {
    if (
      db.prepare("PRAGMA quick_check").get().quick_check !== "ok" ||
      !db
        .prepare(
          "SELECT 1 FROM sqlite_master WHERE type='table' AND name='rooms'",
        )
        .get()
    )
      throw new Error("Source failed the quiz database check.");
    await backup(db, tmp);
  } finally {
    db.close();
  }
  const test = new DatabaseSync(tmp);
  try {
    test.exec("PRAGMA journal_mode=DELETE");
    if (test.prepare("PRAGMA integrity_check").get().integrity_check !== "ok")
      throw new Error("Backup failed its integrity check.");
  } finally {
    test.close();
  }
  // The operator stops the app first. Preserve even unreadable database files
  // without asking SQLite to open or checkpoint the damaged destination.
  const suffixes = ["-wal", "-shm", "-journal"];
  const moved = [];
  if (
    action === "restore" &&
    ["", ...suffixes].some((s) => existsSync(dest + s))
  ) {
    const archive = mkdtempSync(dest + ".before-restore-");
    if (existsSync(dest)) copyFileSync(dest, join(archive, "database.sqlite"));
    try {
      for (const suffix of suffixes) {
        if (!existsSync(dest + suffix)) continue;
        const path = join(archive, "database.sqlite" + suffix);
        renameSync(dest + suffix, path);
        moved.push([path, dest + suffix]);
      }
      renameSync(tmp, dest);
    } catch (error) {
      for (const [path, original] of moved.reverse())
        renameSync(path, original);
      throw error;
    }
    console.log("Previous database files preserved in " + archive);
  } else renameSync(tmp, dest);
  console.log(
    (action === "backup" ? "Saved backup " : "Restored database ") + dest,
  );
} finally {
  rmSync(tmp, { force: true });
}
