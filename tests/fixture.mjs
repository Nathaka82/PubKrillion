import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { spawn } from "node:child_process";
import { once } from "node:events";
import { randomBytes } from "node:crypto";

export async function fixture(
  password = randomBytes(18).toString("hex"),
  extraEnv = {},
) {
  const dir = await mkdtemp(join(tmpdir(), "krill-test-"));
  const file = join(dir, "quiz.sqlite");
  let child, port;
  const f = {
    dir,
    file,
    password,
    url: "",
    async start() {
      child = spawn(process.execPath, ["server/main.ts"], {
        cwd: resolve("."),
        env: {
          ...process.env,
          NODE_ENV: "test",
          PUBLIC_ORIGIN: "",
          FRONTEND_URL: "",
          ...extraEnv,
          DB_PATH: file,
          BIND_ADDRESS: "127.0.0.1",
          PORT: String(port || 0),
          HOST_PASSWORD: password,
        },
        stdio: ["ignore", "pipe", "pipe"],
        windowsHide: true,
      });
      let err = "";
      child.stderr.on("data", (v) => (err += v));
      await new Promise((done, fail) => {
        const timer = setTimeout(() => {
          child.kill();
          fail(new Error("Startup timed out. " + err));
        }, 15000);
        child.once("exit", () => {
          clearTimeout(timer);
          fail(new Error("Server exited. " + err));
        });
        child.stdout.on("data", (v) => {
          const found = v.toString().match(/listening on 127\.0\.0\.1:(\d+)/);
          if (found) {
            clearTimeout(timer);
            port = Number(found[1]);
            f.url = "http://127.0.0.1:" + port;
            done();
          }
        });
      });
    },
    async stop() {
      if (child && child.exitCode === null && child.signalCode === null) {
        const end = once(child, "exit");
        child.kill();
        await end;
      }
    },
    async close() {
      await f.stop();
      await rm(dir, {
        recursive: true,
        force: true,
        maxRetries: 4,
        retryDelay: 150,
      });
    },
  };
  await f.start();
  return f;
}
export function client(f) {
  const cookies = new Map();
  return async function request(path, method = "GET", data) {
    const resp = await fetch(f.url + "/api" + path, {
      method,
      headers: {
        cookie: [...cookies].map(([k, v]) => k + "=" + v).join("; "),
        ...(data ? { "content-type": "application/json" } : {}),
      },
      body: data ? JSON.stringify(data) : undefined,
    });
    for (const c of resp.headers.getSetCookie()) {
      const pair = c.split(";")[0],
        i = pair.indexOf("=");
      cookies.set(pair.slice(0, i), pair.slice(i + 1));
    }
    return { status: resp.status, data: await resp.json() };
  };
}
