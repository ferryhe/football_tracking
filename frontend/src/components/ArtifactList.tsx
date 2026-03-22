import { api } from "../lib/api";
import { useI18n } from "../lib/i18n";
import type { ArtifactSummary, RunRecord } from "../lib/types";
import { FileIcon, LayersIcon, VideoIcon } from "./Icons";

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

function artifactIcon(kind: string) {
  if (kind === "video") {
    return VideoIcon;
  }
  if (kind === "csv") {
    return LayersIcon;
  }
  return FileIcon;
}

export function ArtifactList({ run, preferredNames = [] }: ArtifactListProps) {
  const { formatArtifactKind } = useI18n();
  const artifacts = sortArtifacts(run.artifacts, preferredNames);

  return (
    <div className="artifact-list">
      {artifacts.map((artifact) => {
        const Icon = artifactIcon(artifact.kind);
        return (
          <a
            key={artifact.name}
            className="artifact-card"
            href={api.artifactUrl(run.run_id, artifact.name)}
            target="_blank"
            rel="noreferrer"
          >
            <div className="artifact-copy">
              <div className="title-row compact">
                <Icon className="section-icon tiny" />
                <p className="artifact-kind">{formatArtifactKind(artifact.kind)}</p>
              </div>
              <strong>{artifact.name}</strong>
            </div>
            <span className="artifact-size">
              {artifact.size_bytes ? `${(artifact.size_bytes / (1024 * 1024)).toFixed(1)} MB` : ""}
            </span>
          </a>
        );
      })}
    </div>
  );
}
