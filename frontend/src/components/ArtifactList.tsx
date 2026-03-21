import { api } from "../lib/api";
import type { ArtifactSummary, RunRecord } from "../lib/types";

interface ArtifactListProps {
  run: RunRecord;
  preferredNames?: string[];
}

function sortArtifacts(artifacts: ArtifactSummary[], preferredNames: string[]) {
  const rank = new Map(preferredNames.map((name, index) => [name, index]));
  return [...artifacts].sort((left, right) => {
    const leftRank = rank.get(left.name) ?? 999;
    const rightRank = rank.get(right.name) ?? 999;
    if (leftRank !== rightRank) {
      return leftRank - rightRank;
    }
    return left.name.localeCompare(right.name);
  });
}

export function ArtifactList({ run, preferredNames = [] }: ArtifactListProps) {
  const artifacts = sortArtifacts(run.artifacts, preferredNames);
  return (
    <div className="artifact-list">
      {artifacts.map((artifact) => (
        <a
          key={artifact.name}
          className="artifact-card"
          href={api.artifactUrl(run.run_id, artifact.name)}
          target="_blank"
          rel="noreferrer"
        >
          <div>
            <p className="artifact-kind">{artifact.kind}</p>
            <strong>{artifact.name}</strong>
          </div>
          <span className="artifact-size">
            {artifact.size_bytes ? `${(artifact.size_bytes / (1024 * 1024)).toFixed(1)} MB` : ""}
          </span>
        </a>
      ))}
    </div>
  );
}
