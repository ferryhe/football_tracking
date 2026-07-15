import { useEffect, useMemo } from "react";
import { Circle, Layer, Line, Stage, Text } from "react-konva";
import type Konva from "konva";

import {
  clampDisplayPointToSource,
  computeContainTransform,
  isDisplayPointInsideContent,
  sourcePointToDisplay,
  type FieldPoint,
  type FieldResolution,
} from "@/lib/fieldGeometry";

interface FieldPolygonEditorProps {
  displaySize: FieldResolution;
  sourceResolution: FieldResolution;
  suggestion: FieldPoint[];
  approved: FieldPoint[];
  selectedVertex: number | null;
  onSelectVertex: (index: number | null) => void;
  onChange: (points: FieldPoint[]) => void;
  onReadyChange: (ready: boolean) => void;
}

function flatten(points: FieldPoint[]): number[] {
  return points.flatMap(([x, y]) => [x, y]);
}

export default function FieldPolygonEditor({
  displaySize,
  sourceResolution,
  suggestion,
  approved,
  selectedVertex,
  onSelectVertex,
  onChange,
  onReadyChange,
}: FieldPolygonEditorProps) {
  const transform = useMemo(
    () => computeContainTransform(sourceResolution, displaySize),
    [displaySize, sourceResolution],
  );
  const suggestionDisplay = suggestion.map((point) =>
    sourcePointToDisplay(point, transform),
  );
  const approvedDisplay = approved.map((point) =>
    sourcePointToDisplay(point, transform),
  );

  useEffect(() => {
    onReadyChange(displaySize.width > 0 && displaySize.height > 0);
    return () => onReadyChange(false);
  }, [displaySize.height, displaySize.width, onReadyChange]);

  function handleStageClick(event: Konva.KonvaEventObject<MouseEvent>) {
    const stage = event.target.getStage();
    if (!stage || event.target !== stage) return;
    const pointer = stage.getPointerPosition();
    if (!pointer) return;
    if (!isDisplayPointInsideContent([pointer.x, pointer.y], transform)) return;
    onSelectVertex(null);
    onChange([
      ...approved,
      clampDisplayPointToSource(
        [pointer.x, pointer.y],
        transform,
        sourceResolution,
      ),
    ]);
  }

  function handleDrag(index: number, event: Konva.KonvaEventObject<DragEvent>) {
    const next = approved.map((point) => [...point] as FieldPoint);
    next[index] = clampDisplayPointToSource(
      [event.target.x(), event.target.y()],
      transform,
      sourceResolution,
    );
    onChange(next);
  }

  return (
    <div
      className="absolute inset-0"
      data-testid="field-polygon-editor"
      aria-hidden="true"
    >
      <Stage
        width={displaySize.width}
        height={displaySize.height}
        onClick={handleStageClick}
        onTap={handleStageClick}
      >
        <Layer>
          {suggestionDisplay.length >= 2 && (
            <Line
              points={flatten(suggestionDisplay)}
              closed={suggestionDisplay.length >= 3}
              stroke="#facc15"
              strokeWidth={3}
              dash={[10, 7]}
              opacity={0.95}
              listening={false}
            />
          )}
          {approvedDisplay.length >= 2 && (
            <Line
              points={flatten(approvedDisplay)}
              closed={approvedDisplay.length >= 3}
              stroke="#22d3ee"
              strokeWidth={4}
              fill="rgba(34, 211, 238, 0.12)"
              listening={false}
            />
          )}
          {approvedDisplay.map(([x, y], index) => (
            <Circle
              key={`${index}-${approved[index][0]}-${approved[index][1]}`}
              name="approved-vertex"
              x={x}
              y={y}
              radius={selectedVertex === index ? 10 : 8}
              fill={selectedVertex === index ? "#fb7185" : "#0891b2"}
              stroke="#ffffff"
              strokeWidth={2}
              draggable
              hitStrokeWidth={12}
              onClick={(event) => {
                event.cancelBubble = true;
                onSelectVertex(index);
              }}
              onTap={(event) => {
                event.cancelBubble = true;
                onSelectVertex(index);
              }}
              onDragEnd={(event) => handleDrag(index, event)}
            />
          ))}
          {approvedDisplay.map(([x, y], index) => (
            <Text
              key={`label-${index}`}
              x={x + 11}
              y={y - 17}
              text={String(index + 1)}
              fontSize={14}
              fontStyle="bold"
              fill="#ffffff"
              stroke="#111827"
              strokeWidth={3}
              listening={false}
            />
          ))}
        </Layer>
      </Stage>
    </div>
  );
}
