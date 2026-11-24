import { createHash } from "node:crypto";
import {
  cp,
  mkdir,
  mkdtemp,
  readFile,
  readdir,
  rename,
  rm,
  stat,
  utimes,
} from "node:fs/promises";
import { join, relative, sep } from "node:path";

const releasePattern = /^[0-9a-f]{64}$/;
const week = 7 * 24 * 60 * 60 * 1000;
async function files(dir: string): Promise<string[]> {
  const entries = await readdir(dir, { withFileTypes: true });
  const groups = await Promise.all(
    entries.map((entry) => {
      const path = join(dir, entry.name);
      return entry.isDirectory() ? files(path) : entry.isFile() ? [path] : [];
    }),
  );
  return groups.flat().sort();
}

// Publish a complete snapshot before its URLs can appear in the entry page.
export async function publishWebAssets(
  source: string,
  store: string,
  revision: string,
) {
  const hash = createHash("sha256").update(revision + "\0");
  for (const file of await files(source)) {
    hash.update(relative(source, file).split(sep).join("/") + "\0");
    hash.update(
      createHash("sha256")
        .update(await readFile(file))
        .digest(),
    );
  }
  const version = hash.digest("hex"),
    destination = join(store, version);
  await mkdir(store, { recursive: true });
  if (!(await stat(destination).catch(() => null))) {
    const staging = await mkdtemp(join(store, ".staging-"));
    try {
      await cp(source, staging, { recursive: true });
      try {
        await rename(staging, destination);
      } catch (error) {
        if (!(await stat(destination).catch(() => null))) throw error;
      }
    } finally {
      await rm(staging, { recursive: true, force: true });
    }
  }
  const now = new Date();
  await utimes(destination, now, now);
  const entries = await readdir(store, { withFileTypes: true });
  const older = await Promise.all(
    entries
      .filter(
        (d) =>
          d.isDirectory() && releasePattern.test(d.name) && d.name !== version,
      )
      .map(async (d) => ({
        path: join(store, d.name),
        time: (await stat(join(store, d.name))).mtimeMs,
      })),
  );
  older.sort((a, b) => b.time - a.time);
  for (const dir of older.slice(2)) {
    if (now.getTime() - dir.time > week)
      await rm(dir.path, { recursive: true, force: true });
  }
  const template = await readFile(join(destination, "index.html"), "utf8");
  const html = Buffer.from(
    template.replace(/\b(src|href)="\.\//g, '$1="/assets/' + version + "/"),
  );
  return { html, store, version };
}
