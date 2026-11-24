import { randomBytes } from "node:crypto";
import { writeFileSync } from "node:fs";

try {
  writeFileSync(
    ".env",
    "# Set your VPS hostname before starting Docker Compose.\nDOMAIN=quiz.example.org\nHOST_PASSWORD=" +
      randomBytes(24).toString("base64url") +
      "\n",
    { flag: "wx", mode: 0o600 },
  );
  console.log(
    "Created .env. Your private HOST_PASSWORD is in that file. For local play, run npm start.",
  );
} catch (e) {
  if (e.code !== "EEXIST") throw e;
  console.log("Your existing .env is ready to use.");
}
