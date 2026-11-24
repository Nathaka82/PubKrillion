import { cp, mkdir, readFile, writeFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
const raw = process.argv[2] || process.env.PAGES_API_ORIGIN;
if (!raw)
  throw Error(
    "Supply the public HTTPS quiz server as PAGES_API_ORIGIN or as an argument.",
  );
const url = new URL(raw);
if (
  url.protocol !== "https:" ||
  url.username ||
  url.password ||
  url.pathname !== "/" ||
  url.search ||
  url.hash
)
  throw Error("Use an HTTPS origin such as https://quiz.example.org.");
const out = new URL("../dist/pages/", import.meta.url);
await mkdir(out, { recursive: true });
await cp(new URL("../public/", import.meta.url), out, { recursive: true });
await writeFile(
  new URL("config.js", out),
  "export const apiOrigin = " + JSON.stringify(url.origin) + ";\n",
);
const index = new URL("index.html", out);
const policy =
  "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; font-src 'self'; connect-src 'self' " +
  url.origin +
  "; object-src 'none'; base-uri 'none'; form-action 'self'";
await writeFile(
  index,
  (await readFile(index, "utf8")).replace(
    "<head>",
    '<head>\n    <meta http-equiv="Content-Security-Policy" content="' +
      policy +
      '">',
  ),
);
await writeFile(new URL(".nojekyll", out), "");
console.log("Pages frontend ready in " + fileURLToPath(out));
