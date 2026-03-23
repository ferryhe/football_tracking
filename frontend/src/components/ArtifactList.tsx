import { api } from "../lib/api";
import { useI18n } from "../lib/i18n";
import type { ArtifactSummary, RunRecord } from "../lib/types";
import { ArrowUpRightIcon, FileIcon, LayersIcon, VideoIcon } from "./Icons";

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

function formatArtifactSize(sizeBytes?: number | null) {
  if (!sizeBytes) {
    return "";
  }
  return `${(sizeBytes / (1024 * 1024)).toFixed(1)} MB`;
}

export function ArtifactList({ run, preferredNames = [] }: ArtifactListProps) {
  const { copy, formatArtifactKind } = useI18n();
  const artifacts = sortArtifacts(run.artifacts, preferredNames);
  const preferredSet = new Set(preferredNames);
  const featuredArtifacts = artifacts.filter((artifact) => preferredSet.has(artifact.name));
  const remainingArtifacts = artifacts.filter((artifact) => !preferredSet.has(artifact.name));

  return (
    <div className="artifact-stack">
      {featuredArtifacts.length ? (
        <section className="output-section">
          <div className="panel-header">
            <div className="title-row">
              <VideoIcon className="section-icon" />
              <div>
                <h3>{copy.workspace.featuredOutputs}</h3>
                <p className="muted">{copy.workspace.featuredOutputsSubtitle}</p>
              </div>
            </div>
          </div>
          <div className="artifact-feature-grid">
            {featuredArtifacts.map((artifact) => {
              const Icon = artifactIcon(artifact.kind);
              return (
                <a
                  key={artifact.name}
                  className="artifact-feature-card"
                  href={api.artifactUrl(run.run_id, artifact.name)}
                  target="_blank"
                  rel="noreferrer"
                >
                  <div className="artifact-feature-head">
                    <div className="artifact-feature-icon">
                      <Icon className="section-icon" />
                    </div>
                    <span className="artifact-kind">{formatArtifactKind(artifact.kind)}</span>
                  </div>
                  <strong>{artifact.name}</strong>
                  <p className="muted">{formatArtifactSize(artifact.size_bytes) || copy.common.notAvailable}</p>
                  <span className="artifact-open-link">
                    <span>{copy.workspace.openArtifact}</span>
                    <ArrowUpRightIcon className="section-icon tiny" />
                  </span>
                </a>
              );
            })}
          </div>
        </section>
      ) : null}

      <section className="output-section">
        <div className="panel-header">
          <div className="title-row">
            <FileIcon className="section-icon" />
            <div>
              <h3>{copy.workspace.allArtifacts}</h3>
              <p className="muted">{copy.workspace.allArtifactsSubtitle}</p>
            </div>
          </div>
        </div>
        <div className="artifact-list">
          {(remainingArtifacts.length ? remainingArtifacts : artifacts).map((artifact) => {
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
                <span className="artifact-size">{formatArtifactSize(artifact.size_bytes)}</span>
              </a>
            );
          })}
        </div>
      </section>
    </div>
  );
}
