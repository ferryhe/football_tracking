import { readFile } from "node:fs/promises";
import path from "node:path";

const root = path.resolve(import.meta.dirname, "..");
const output = path.join(root, "dist", "public");
const manifest = JSON.parse(
  await readFile(path.join(output, ".vite", "manifest.json"), "utf8"),
);
const entries = Object.entries(manifest).filter(([, chunk]) => chunk.isEntry);

if (entries.length !== 1) {
  throw new Error(`Expected one web entry chunk, found ${entries.length}.`);
}

function closure(seedKeys, edge) {
  const seen = new Set();
  const pending = [...seedKeys];
  while (pending.length > 0) {
    const key = pending.pop();
    if (!key || seen.has(key)) continue;
    const chunk = manifest[key];
    if (!chunk) throw new Error(`Manifest references missing chunk ${key}.`);
    seen.add(key);
    pending.push(...(chunk[edge] ?? []));
  }
  return seen;
}

function dynamicClosure(seedKeys) {
  const seen = new Set();
  const pending = [...seedKeys];
  while (pending.length > 0) {
    const key = pending.pop();
    if (!key || seen.has(key)) continue;
    const chunk = manifest[key];
    if (!chunk) throw new Error(`Manifest references missing chunk ${key}.`);
    seen.add(key);
    pending.push(...(chunk.imports ?? []), ...(chunk.dynamicImports ?? []));
  }
  return seen;
}

async function closureSource(keys) {
  return (
    await Promise.all(
      [...keys].map(async (key) => {
        const chunk = manifest[key];
        return `${key}\n${await readFile(path.join(output, chunk.file), "utf8")}`;
      }),
    )
  ).join("\n");
}

const [entryKey] = entries[0];
const initialKeys = closure([entryKey], "imports");
const initialSource = await closureSource(initialKeys);
const konvaSignature =
  /react-konva|Konva warning|HitCanvas|SceneCanvas|node_modules[\\/]konva/i;

if (konvaSignature.test(initialSource)) {
  throw new Error(
    `Konva/calibration code entered the initial static import closure: ${[
      ...initialKeys,
    ].join(", ")}`,
  );
}

const productionKey = "src/pages/production.tsx";
const fieldEditorKey =
  "src/components/production/FieldPolygonEditor.tsx";
const productionChunk = manifest[productionKey];
const fieldEditorChunk = manifest[fieldEditorKey];

if (!productionChunk?.isDynamicEntry || !fieldEditorChunk?.isDynamicEntry) {
  throw new Error(
    "Production and FieldPolygonEditor must remain independently identifiable dynamic entries.",
  );
}

const productionStaticKeys = closure([productionKey], "imports");
const productionStaticSource = await closureSource(productionStaticKeys);
if (konvaSignature.test(productionStaticSource)) {
  throw new Error(
    `Konva entered the Production page static import closure: ${[
      ...productionStaticKeys,
    ].join(", ")}`,
  );
}
if (productionStaticKeys.has(fieldEditorKey)) {
  throw new Error("FieldPolygonEditor became a static Production dependency.");
}
if (!(productionChunk.dynamicImports ?? []).includes(fieldEditorKey)) {
  throw new Error(
    "Production must directly retain FieldPolygonEditor as a dynamic import.",
  );
}
if (productionChunk.file === fieldEditorChunk.file) {
  throw new Error("FieldPolygonEditor must be emitted as a distinct chunk.");
}

const dynamicRoots = [...initialKeys].flatMap(
  (key) => manifest[key].dynamicImports ?? [],
);
const dynamicKeys = dynamicClosure(dynamicRoots);
const dynamicSource = await closureSource(dynamicKeys);

if (!konvaSignature.test(dynamicSource)) {
  throw new Error(
    "The dynamic import closure does not contain the retained Konva calibration editor.",
  );
}

process.stdout.write(
  `Cutover bundle verified: ${initialKeys.size} initial chunks, ${productionStaticKeys.size} Production-static chunks, ${dynamicKeys.size} dynamic chunks; FieldPolygonEditor is a distinct Production dynamic import and Konva is dynamic-only.\n`,
);
