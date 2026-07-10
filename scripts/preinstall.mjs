import { rmSync } from "node:fs";
import path from "node:path";

const userAgent = process.env.npm_config_user_agent ?? "";

if (!userAgent.startsWith("pnpm/")) {
  console.error("Use pnpm instead");
  process.exit(1);
}

const root = path.resolve(import.meta.dirname, "..");

for (const filename of ["package-lock.json", "yarn.lock"]) {
  rmSync(path.join(root, filename), { force: true });
}
