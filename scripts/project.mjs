import { existsSync } from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { childExitCode } from "./child-process-result.mjs";

const root = path.resolve(import.meta.dirname, "..");
const python =
  process.platform === "win32"
    ? path.join(root, ".venv", "Scripts", "python.exe")
    : path.join(root, ".venv", "bin", "python");

if (!existsSync(python)) {
  console.error(`[ERROR] Missing root virtual environment Python: ${python}`);
  process.exit(1);
}

const result = spawnSync(
  python,
  [
    path.join(root, "python_backend", "scripts", "project.py"),
    ...process.argv.slice(2),
  ],
  { cwd: root, env: process.env, stdio: "inherit" },
);

if (result.error) {
  console.error(
    `[ERROR] Unable to start project entrypoint: ${result.error.message}`,
  );
  process.exit(1);
}

process.exit(childExitCode(result));
