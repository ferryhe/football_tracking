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

  source = source.replace(/`\/api[^`]*`/g, (template) =>
    template.replace(/\/\$\{([A-Za-z_$][\w$]*)\}/g, "/${encodePathSegmented($1)}"),
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

function refineHighlightRenderBodySelection() {
  let source = readText(zodApiPath);
  const startMarker = "export const CreateHighlightRenderBody = zod\n";
  const startIndex = source.indexOf(startMarker);
  if (startIndex < 0) {
    throw new Error("CreateHighlightRenderBody schema not found in generated zod api.ts.");
  }
  const endMarker = "\n\n/**\n * @summary ";
  const endIndex = source.indexOf(endMarker, startIndex);
  if (endIndex < 0) {
    throw new Error("Unable to locate end of CreateHighlightRenderBody schema.");
  }

  const before = source.slice(0, startIndex);
  const block = source.slice(startIndex, endIndex);
  const after = source.slice(endIndex);
  if (block.includes("Highlight render requires exactly one of candidate_id, approved_action_id, or start_frame/end_frame.")) {
    return;
  }
  const refinedBlock = block.replace(
    /\n  \);\s*$/,
    [
      "\n  )",
      "  .superRefine((value, ctx) => {",
      "    const payload = value && typeof value === \"object\" ? (value as Record<string, unknown>) : {};",
      "    const candidateId = Boolean(payload.candidate_id);",
      "    const approvedActionId = Boolean(payload.approved_action_id);",
      "    const hasStart = payload.start_frame !== undefined && payload.start_frame !== null;",
      "    const hasEnd = payload.end_frame !== undefined && payload.end_frame !== null;",
      "    const explicitWindow = hasStart && hasEnd;",
      "    const modeCount = [candidateId, approvedActionId, explicitWindow].filter(Boolean).length;",
      "    if (modeCount !== 1) {",
      "      ctx.addIssue({",
      "        code: zod.ZodIssueCode.custom,",
      '        message: "Highlight render requires exactly one of candidate_id, approved_action_id, or start_frame/end_frame.",',
      "      });",
      "    }",
      "    if (hasStart !== hasEnd) {",
      "      ctx.addIssue({",
      "        code: zod.ZodIssueCode.custom,",
      '        message: "Highlight render frame window requires both start_frame and end_frame.",',
      "      });",
      "    }",
      "    if (hasStart && hasEnd && Number(payload.end_frame) < Number(payload.start_frame)) {",
      "      ctx.addIssue({",
      "        code: zod.ZodIssueCode.custom,",
      '        message: "Highlight render requires end_frame to be greater than or equal to start_frame.",',
      "      });",
      "    }",
      "  });",
    ].join("\n"),
  );
  writeText(zodApiPath, `${before}${refinedBlock}${after}`);
}

encodeGeneratedReactPathParams();
pruneZodParamBarrelCollisions();
refineHighlightRenderBodySelection();
