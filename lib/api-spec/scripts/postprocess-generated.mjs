import fs from "node:fs";
import path from "node:path";

const root = path.resolve(import.meta.dirname, "..", "..");
const reactApiPath = path.join(root, "api-client-react", "src", "generated", "api.ts");
const zodGeneratedRoot = path.join(root, "api-zod", "src", "generated");
const zodApiPath = path.join(zodGeneratedRoot, "api.ts");
const zodTypesIndexPath = path.join(zodGeneratedRoot, "types", "index.ts");

function readText(filePath) {
  if (!fs.existsSync(filePath)) {
    throw new Error(`Generated file is missing: ${filePath}`);
  }
  return fs.readFileSync(filePath, "utf8");
}

function writeText(filePath, content) {
  fs.writeFileSync(filePath, content);
}

function capitalize(value) {
  return value ? `${value[0].toUpperCase()}${value.slice(1)}` : "";
}

function encodeGeneratedReactPathParams() {
  let source = readText(reactApiPath);
  const helper = [
    "function encodePathSegmented(path: string): string {",
    '  return path.split("/").map((segment) => encodeURIComponent(segment)).join("/");',
    "}",
    "",
  ].join("\n");

  source = source.replace(/return `\/api[^`]*\$\{[^`]+`;/g, (statement) =>
    statement.replace(/\$\{([A-Za-z_$][\w$]*)\}/g, "${encodePathSegmented($1)}"),
  );

  if (source.includes("encodePathSegmented(") && !source.includes("function encodePathSegmented")) {
    source = source.replace(/type SecondParameter[^\n]+;\n\n/, (match) => `${match}${helper}`);
  }

  writeText(reactApiPath, source);
}

function pruneZodParamBarrelCollisions() {
  const apiSource = readText(zodApiPath);
  const exportedConsts = new Set(
    Array.from(apiSource.matchAll(/^export const ([A-Za-z_$][\w$]*)\b/gm), (match) => match[1]),
  );

  const indexSource = readText(zodTypesIndexPath);
  const exportLine = /^export \* from "\.\/([^"']+)";$/;
  const lines = indexSource.split(/\r?\n/).filter((line) => {
    const match = line.match(exportLine);
    if (!match) return true;

    // Orval can export both a generated type barrel and a generated zod const
    // with the same PascalCase name. Keep the zod const as the authoritative
    // runtime schema export and drop only the colliding type barrel line.
    return !exportedConsts.has(capitalize(match[1]));
  });

  writeText(zodTypesIndexPath, lines.join("\n"));
}

encodeGeneratedReactPathParams();
pruneZodParamBarrelCollisions();
