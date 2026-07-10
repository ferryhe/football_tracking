import { existsSync } from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";

function findExecutable(name) {
  const extensions = process.platform === "win32" ? [".cmd", ".exe", ".bat", ""] : [""];
  for (const directory of (process.env.PATH ?? "").split(path.delimiter)) {
    if (!directory) continue;
    for (const extension of extensions) {
      const candidate = path.join(directory, `${name}${extension}`);
      if (existsSync(candidate)) return candidate;
    }
  }
  return null;
}

const pnpm = findExecutable("pnpm");
const corepack = pnpm ? null : findExecutable("corepack");

if (!pnpm && !corepack) {
  console.error("[ERROR] pnpm was not found. Install pnpm or enable Corepack.");
  process.exit(1);
}

const executable = pnpm ?? corepack;
const args = [...(pnpm ? [] : ["pnpm"]), ...process.argv.slice(2)];
let command = executable;
let commandArgs = args;
if (process.platform === "win32" && /\.(cmd|bat)$/i.test(executable)) {
  command = process.env.ComSpec ?? "cmd.exe";
  commandArgs = ["/d", "/s", "/c", "call", executable, ...args];
}
const result = spawnSync(command, commandArgs, {
  env: process.env,
  stdio: "inherit",
});

if (result.error) {
  console.error(`[ERROR] Unable to start pnpm: ${result.error.message}`);
  process.exit(1);
}

process.exit(result.status ?? 1);
