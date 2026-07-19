import type Konva from "konva";
import {
  Minus,
  MousePointer2,
  Move,
  Plus,
  Redo2,
  Square,
  Trash2,
} from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import {
  Circle,
  Image as KonvaImage,
  Layer,
  Rect,
  Stage,
  Text,
} from "react-konva";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useLanguage } from "@/contexts/LanguageContext";
import {
  annotationMaxZoom,
  clampSourceBox,
  clampSourcePoint,
  computeAnnotationTransform,
  displayBoxToSource,
  displayPointInsideSource,
  displayPointToSource,
  normalizeSourceBox,
  panForZoomAtDisplayPoint,
  sourceBoxToDisplay,
  sourcePointToDisplay,
  validateSourceBox,
  validateSourcePoint,
  type AnnotationBox,
  type AnnotationPoint,
  type AnnotationSize,
  type DisplayBox,
} from "@/lib/ballAnnotationGeometry";

type CanvasMode = "point" | "box" | "pan";
export type BallAnnotationImageDecodeState = "loading" | "ready" | "failed";

interface BallAnnotationCanvasProps {
  sourceSize: AnnotationSize;
  displaySize: AnnotationSize;
  imageUrl: string;
  point: AnnotationPoint | null;
  box: AnnotationBox | null;
  suggestion: AnnotationBox | null;
  zoom: number;
  pan: { x: number; y: number };
  disabled: boolean;
  canUndo: boolean;
  onGeometryChange: (geometry: {
    point: AnnotationPoint | null;
    box: AnnotationBox | null;
  }) => void;
  onClearGeometry: () => void;
  onUndo: () => void;
  onViewportChange: (viewport: {
    zoom: number;
    pan: { x: number; y: number };
  }) => void;
  onImageDecodeStateChange: (state: BallAnnotationImageDecodeState) => void;
}

function labels(language: "en" | "zh") {
  return language === "zh"
    ? {
        workspace: "足球标注画布",
        pointMode: "标记球心",
        boxMode: "绘制确认框",
        panMode: "平移图片",
        zoomIn: "放大",
        zoomOut: "缩小",
        resetView: "重置视图",
        clearGeometry: "清除草稿坐标",
        undo: "撤销",
        pointX: "球心 X（原片像素）",
        pointY: "球心 Y（原片像素）",
        left: "框左边（原片像素）",
        top: "框上边（原片像素）",
        right: "框右边（原片像素）",
        bottom: "框下边（原片像素）",
        suggestion: "未确认建议",
        suggestionWarning: "建议永远不会自动保存为已确认真值。",
        invalidCoordinate: "坐标必须位于原片范围内。",
        addPoint: "在画面中心添加球心",
        addBox: "在球心周围添加可编辑框",
      }
    : {
        workspace: "Ball annotation canvas",
        pointMode: "Place ball center",
        boxMode: "Draw confirmed box",
        panMode: "Pan image",
        zoomIn: "Zoom in",
        zoomOut: "Zoom out",
        resetView: "Reset view",
        clearGeometry: "Clear draft geometry",
        undo: "Undo",
        pointX: "Ball center X (source pixels)",
        pointY: "Ball center Y (source pixels)",
        left: "Box left (source pixels)",
        top: "Box top (source pixels)",
        right: "Box right (source pixels)",
        bottom: "Box bottom (source pixels)",
        suggestion: "Unconfirmed suggestion",
        suggestionWarning: "A suggestion is never saved as confirmed truth.",
        invalidCoordinate: "Coordinates must stay inside the source frame.",
        addPoint: "Add point at frame center",
        addBox: "Add editable box around point",
      };
}

export function BallAnnotationCanvas({
  sourceSize,
  displaySize,
  imageUrl,
  point,
  box,
  suggestion,
  zoom,
  pan,
  disabled,
  canUndo,
  onGeometryChange,
  onClearGeometry,
  onUndo,
  onViewportChange,
  onImageDecodeStateChange,
}: BallAnnotationCanvasProps) {
  const { language } = useLanguage();
  const text = labels(language);
  const [mode, setMode] = useState<CanvasMode>("point");
  const [image, setImage] = useState<HTMLImageElement | null>(null);
  const [drawStart, setDrawStart] = useState<AnnotationPoint | null>(null);
  const [draftBox, setDraftBox] = useState<AnnotationBox | null>(null);
  const [inputError, setInputError] = useState<string | null>(null);
  const [coordinateInputs, setCoordinateInputs] = useState({
    pointX: "",
    pointY: "",
    left: "",
    top: "",
    right: "",
    bottom: "",
  });
  const maxZoom = useMemo(
    () => annotationMaxZoom(sourceSize, displaySize),
    [displaySize, sourceSize],
  );
  const transform = useMemo(
    () =>
      computeAnnotationTransform({
        source: sourceSize,
        display: displaySize,
        zoom,
        pan,
        devicePixelRatio: window.devicePixelRatio || 1,
      }),
    [displaySize, pan, sourceSize, zoom],
  );

  useEffect(() => {
    setImage(null);
    setDrawStart(null);
    setDraftBox(null);
    onImageDecodeStateChange("loading");
    const nextImage = new window.Image();
    nextImage.decoding = "async";
    nextImage.onload = () => {
      setImage(nextImage);
      onImageDecodeStateChange("ready");
    };
    nextImage.onerror = () => {
      setImage(null);
      onImageDecodeStateChange("failed");
    };
    nextImage.src = imageUrl;
    return () => {
      nextImage.onload = null;
      nextImage.onerror = null;
    };
  }, [imageUrl, onImageDecodeStateChange]);

  useEffect(() => {
    setCoordinateInputs({
      pointX: point ? String(point[0]) : "",
      pointY: point ? String(point[1]) : "",
      left: box ? String(box.left) : "",
      top: box ? String(box.top) : "",
      right: box ? String(box.right) : "",
      bottom: box ? String(box.bottom) : "",
    });
  }, [box, point]);

  const visibleBox = draftBox ?? box;
  const displayBox = visibleBox
    ? sourceBoxToDisplay(visibleBox, transform)
    : null;
  const displaySuggestion = suggestion
    ? sourceBoxToDisplay(suggestion, transform)
    : null;
  const displayPoint = point ? sourcePointToDisplay(point, transform) : null;

  function centerOfBox(sourceBox: AnnotationBox): AnnotationPoint {
    return [
      (sourceBox.left + sourceBox.right) / 2,
      (sourceBox.top + sourceBox.bottom) / 2,
    ];
  }

  function emitBox(sourceBox: AnnotationBox) {
    if (!validateSourceBox(sourceBox, sourceSize).valid) return;
    onGeometryChange({ point: centerOfBox(sourceBox), box: sourceBox });
  }

  function boxAtCenter(
    sourceBox: AnnotationBox,
    requestedCenter: AnnotationPoint,
  ): AnnotationBox {
    const width = sourceBox.right - sourceBox.left;
    const height = sourceBox.bottom - sourceBox.top;
    const left = Math.max(
      0,
      Math.min(sourceSize.width - width, requestedCenter[0] - width / 2),
    );
    const top = Math.max(
      0,
      Math.min(sourceSize.height - height, requestedCenter[1] - height / 2),
    );
    return { left, top, right: left + width, bottom: top + height };
  }

  function emitPoint(sourcePoint: AnnotationPoint) {
    const clamped = clampSourcePoint(sourcePoint, sourceSize);
    if (box) {
      emitBox(boxAtCenter(box, clamped));
    } else {
      onGeometryChange({ point: clamped, box: null });
    }
  }

  function addPointAtFrameCenter() {
    emitPoint([sourceSize.width / 2, sourceSize.height / 2]);
  }

  function addEditableBoxAroundPoint() {
    if (!point) return;
    emitBox(
      boxAtCenter(
        {
          left: 0,
          top: 0,
          right: Math.min(12, sourceSize.width),
          bottom: Math.min(12, sourceSize.height),
        },
        point,
      ),
    );
  }

  function pointer(event: Konva.KonvaEventObject<Event>) {
    const stage = event.target.getStage();
    const position = stage?.getPointerPosition();
    return position ? ([position.x, position.y] as AnnotationPoint) : null;
  }

  function handlePointerDown(event: Konva.KonvaEventObject<Event>) {
    if (disabled || mode === "pan" || event.target !== event.target.getStage())
      return;
    const display = pointer(event);
    if (!display || !displayPointInsideSource(display, transform)) return;
    const source = clampSourcePoint(
      displayPointToSource(display, transform),
      sourceSize,
    );
    if (mode === "point") {
      emitPoint(source);
    } else {
      setDrawStart(source);
      setDraftBox(normalizeSourceBox(source, source));
    }
  }

  function handlePointerMove(event: Konva.KonvaEventObject<Event>) {
    if (disabled || mode !== "box" || !drawStart) return;
    const display = pointer(event);
    if (!display) return;
    const source = clampSourcePoint(
      displayPointToSource(display, transform),
      sourceSize,
    );
    setDraftBox(normalizeSourceBox(drawStart, source));
  }

  function handlePointerUp() {
    if (
      !disabled &&
      draftBox &&
      validateSourceBox(draftBox, sourceSize).valid
    ) {
      emitBox(draftBox);
    }
    setDrawStart(null);
    setDraftBox(null);
  }

  function handlePointerCancel() {
    setDrawStart(null);
    setDraftBox(null);
  }

  function zoomAtCenter(nextZoom: number) {
    const boundedZoom = Math.max(1, Math.min(maxZoom, nextZoom));
    const center: AnnotationPoint = [
      displaySize.width / 2,
      displaySize.height / 2,
    ];
    onViewportChange({
      zoom: boundedZoom,
      pan: panForZoomAtDisplayPoint(transform, boundedZoom, center),
    });
  }

  function updatePointCoordinate(axis: 0 | 1, raw: string) {
    if (!point) return;
    const value = Number(raw);
    const next: AnnotationPoint = [...point];
    next[axis] = value;
    if (validateSourcePoint(next, sourceSize).valid) {
      setInputError(null);
      emitPoint(next);
    } else {
      setInputError(text.invalidCoordinate);
    }
  }

  function updateBoxEdge(edge: keyof AnnotationBox, raw: string) {
    if (!box) return;
    const next = { ...box, [edge]: Number(raw) };
    if (validateSourceBox(next, sourceSize).valid) {
      setInputError(null);
      emitBox(next);
    } else {
      setInputError(text.invalidCoordinate);
    }
  }

  function moveAnnotation(dx: number, dy: number) {
    if (box) {
      const width = box.right - box.left;
      const height = box.bottom - box.top;
      const left = Math.max(
        0,
        Math.min(sourceSize.width - width, box.left + dx),
      );
      const top = Math.max(
        0,
        Math.min(sourceSize.height - height, box.top + dy),
      );
      emitBox({ left, top, right: left + width, bottom: top + height });
    } else if (point) {
      emitPoint([point[0] + dx, point[1] + dy]);
    }
  }

  function handleKeyDown(event: React.KeyboardEvent<HTMLDivElement>) {
    if (disabled) return;
    if (
      event.target instanceof HTMLInputElement ||
      event.target instanceof HTMLSelectElement ||
      event.target instanceof HTMLButtonElement
    ) {
      return;
    }
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "z") {
      event.preventDefault();
      if (canUndo) onUndo();
      return;
    }
    if (event.key === "Delete" || event.key === "Backspace") {
      event.preventDefault();
      onClearGeometry();
      return;
    }
    const step = event.shiftKey ? 10 : 1;
    const movement: Record<string, AnnotationPoint> = {
      ArrowLeft: [-step, 0],
      ArrowRight: [step, 0],
      ArrowUp: [0, -step],
      ArrowDown: [0, step],
    };
    const delta = movement[event.key];
    if (delta) {
      event.preventDefault();
      moveAnnotation(delta[0], delta[1]);
    }
  }

  function moveDisplayBox(next: DisplayBox) {
    const source = clampSourceBox(
      displayBoxToSource(next, transform),
      sourceSize,
    );
    emitBox(source);
  }

  const controlClass = "grid min-w-0 grid-cols-2 gap-2 sm:grid-cols-3";

  return (
    <section
      className="min-w-0 space-y-3"
      data-testid="ball-annotation-workspace"
      aria-label={text.workspace}
      tabIndex={0}
      onKeyDown={handleKeyDown}
    >
      <div className="flex min-w-0 flex-wrap gap-2" role="toolbar">
        <Button
          type="button"
          size="sm"
          variant={mode === "point" ? "default" : "outline"}
          aria-label={text.pointMode}
          aria-pressed={mode === "point"}
          disabled={disabled}
          onClick={() => setMode("point")}
        >
          <MousePointer2 aria-hidden="true" />
        </Button>
        <Button
          type="button"
          size="sm"
          variant={mode === "box" ? "default" : "outline"}
          aria-label={text.boxMode}
          aria-pressed={mode === "box"}
          disabled={disabled}
          onClick={() => setMode("box")}
        >
          <Square aria-hidden="true" />
        </Button>
        <Button
          type="button"
          size="sm"
          variant={mode === "pan" ? "default" : "outline"}
          aria-label={text.panMode}
          aria-pressed={mode === "pan"}
          disabled={disabled}
          onClick={() => setMode("pan")}
        >
          <Move aria-hidden="true" />
        </Button>
        <Button
          type="button"
          size="sm"
          variant="outline"
          aria-label={text.zoomOut}
          disabled={disabled || zoom <= 1}
          onClick={() => zoomAtCenter(zoom / 1.25)}
        >
          <Minus aria-hidden="true" />
        </Button>
        <Button
          type="button"
          size="sm"
          variant="outline"
          aria-label={text.zoomIn}
          disabled={disabled || zoom >= maxZoom}
          onClick={() => zoomAtCenter(zoom * 1.25)}
        >
          <Plus aria-hidden="true" />
        </Button>
        <Button
          type="button"
          size="sm"
          variant="outline"
          onClick={() => onViewportChange({ zoom: 1, pan: { x: 0, y: 0 } })}
          disabled={disabled || (zoom === 1 && pan.x === 0 && pan.y === 0)}
        >
          {text.resetView}
        </Button>
      </div>

      <div
        className="max-w-full overflow-hidden rounded-md border bg-black"
        data-testid="ball-annotation-touch-surface"
        data-zoom={zoom}
        data-max-zoom={maxZoom}
        data-pan-x={pan.x}
        data-pan-y={pan.y}
        data-source-pixel-scale={transform.scale}
        style={{ touchAction: "none" }}
      >
        <Stage
          width={displaySize.width}
          height={displaySize.height}
          pixelRatio={transform.devicePixelRatio}
          draggable={!disabled && mode === "pan"}
          onMouseDown={handlePointerDown}
          onTouchStart={handlePointerDown}
          onMouseMove={handlePointerMove}
          onTouchMove={handlePointerMove}
          onMouseUp={handlePointerUp}
          onTouchEnd={handlePointerUp}
          onTouchCancel={handlePointerCancel}
          onDragEnd={(event) => {
            if (disabled || mode !== "pan") return;
            onViewportChange({
              zoom,
              pan: { x: pan.x + event.target.x(), y: pan.y + event.target.y() },
            });
            event.target.position({ x: 0, y: 0 });
          }}
          onWheel={(event) => {
            event.evt.preventDefault();
            if (disabled) return;
            const display = pointer(event);
            if (!display) return;
            const nextZoom = Math.max(
              1,
              Math.min(maxZoom, event.evt.deltaY > 0 ? zoom / 1.1 : zoom * 1.1),
            );
            onViewportChange({
              zoom: nextZoom,
              pan: panForZoomAtDisplayPoint(transform, nextZoom, display),
            });
          }}
        >
          <Layer>
            {image && (
              <KonvaImage
                image={image}
                x={transform.offsetX}
                y={transform.offsetY}
                width={transform.contentWidth}
                height={transform.contentHeight}
                listening={false}
              />
            )}
            {displaySuggestion && (
              <>
                <Rect
                  name="suggested-ball-box"
                  {...displaySuggestion}
                  stroke="#facc15"
                  strokeWidth={2}
                  dash={[8, 6]}
                  listening={false}
                />
                <Text
                  x={displaySuggestion.x}
                  y={Math.max(0, displaySuggestion.y - 18)}
                  text={text.suggestion}
                  fill="#facc15"
                  fontSize={14}
                  listening={false}
                />
              </>
            )}
            {displayBox && (
              <>
                <Rect
                  name="confirmed-ball-box"
                  {...displayBox}
                  stroke="#22d3ee"
                  strokeWidth={3}
                  hitStrokeWidth={44}
                  draggable={!disabled && mode === "box"}
                  onDragEnd={(event) => {
                    if (disabled || mode !== "box") return;
                    moveDisplayBox({
                      ...displayBox,
                      x: event.target.x(),
                      y: event.target.y(),
                    });
                  }}
                />
                {(
                  [
                    ["top-left", displayBox.x, displayBox.y, "left", "top"],
                    [
                      "top-right",
                      displayBox.x + displayBox.width,
                      displayBox.y,
                      "right",
                      "top",
                    ],
                    [
                      "bottom-left",
                      displayBox.x,
                      displayBox.y + displayBox.height,
                      "left",
                      "bottom",
                    ],
                    [
                      "bottom-right",
                      displayBox.x + displayBox.width,
                      displayBox.y + displayBox.height,
                      "right",
                      "bottom",
                    ],
                  ] as const
                ).map(([name, x, y, horizontalEdge, verticalEdge]) => (
                  <Circle
                    key={name}
                    name={`confirmed-ball-box-${name}`}
                    x={x}
                    y={y}
                    radius={6}
                    fill="#22d3ee"
                    stroke="#fff"
                    strokeWidth={2}
                    hitStrokeWidth={32}
                    draggable={!disabled && mode === "box"}
                    onDragEnd={(event) => {
                      if (disabled || mode !== "box" || !box) return;
                      const [sourceX, sourceY] = displayPointToSource(
                        [event.target.x(), event.target.y()],
                        transform,
                      );
                      const next = {
                        ...box,
                        [horizontalEdge]: Math.max(
                          0,
                          Math.min(sourceSize.width, sourceX),
                        ),
                        [verticalEdge]: Math.max(
                          0,
                          Math.min(sourceSize.height, sourceY),
                        ),
                      };
                      if (validateSourceBox(next, sourceSize).valid) {
                        emitBox(next);
                      }
                    }}
                  />
                ))}
              </>
            )}
            {displayPoint && (
              <Circle
                name="confirmed-ball-center"
                x={displayPoint[0]}
                y={displayPoint[1]}
                radius={6}
                fill="#fb7185"
                stroke="#fff"
                strokeWidth={2}
                hitStrokeWidth={32}
                draggable={!disabled && mode === "point"}
                onDragEnd={(event) => {
                  if (disabled || mode !== "point") return;
                  emitPoint(
                    displayPointToSource(
                      [event.target.x(), event.target.y()],
                      transform,
                    ),
                  );
                }}
              />
            )}
          </Layer>
        </Stage>
      </div>

      {suggestion && (
        <p className="text-sm text-amber-700 dark:text-amber-300">
          <strong>{text.suggestion}.</strong> {text.suggestionWarning}
        </p>
      )}

      <div className={controlClass}>
        <div className="min-w-0 space-y-1">
          <Label htmlFor="ball-point-x">{text.pointX}</Label>
          <Input
            id="ball-point-x"
            type="number"
            step="any"
            min={0}
            max={sourceSize.width}
            value={coordinateInputs.pointX}
            disabled={disabled || !point}
            onChange={(event) =>
              setCoordinateInputs((current) => ({
                ...current,
                pointX: event.target.value,
              }))
            }
            onBlur={() => updatePointCoordinate(0, coordinateInputs.pointX)}
            onKeyDown={(event) => {
              if (event.key === "Enter") event.currentTarget.blur();
            }}
          />
        </div>
        <div className="min-w-0 space-y-1">
          <Label htmlFor="ball-point-y">{text.pointY}</Label>
          <Input
            id="ball-point-y"
            type="number"
            step="any"
            min={0}
            max={sourceSize.height}
            value={coordinateInputs.pointY}
            disabled={disabled || !point}
            onChange={(event) =>
              setCoordinateInputs((current) => ({
                ...current,
                pointY: event.target.value,
              }))
            }
            onBlur={() => updatePointCoordinate(1, coordinateInputs.pointY)}
            onKeyDown={(event) => {
              if (event.key === "Enter") event.currentTarget.blur();
            }}
          />
        </div>
        {(["left", "top", "right", "bottom"] as const).map((edge) => (
          <div key={edge} className="min-w-0 space-y-1">
            <Label htmlFor={`ball-box-${edge}`}>{text[edge]}</Label>
            <Input
              id={`ball-box-${edge}`}
              type="number"
              step="any"
              min={0}
              max={
                edge === "left" || edge === "right"
                  ? sourceSize.width
                  : sourceSize.height
              }
              value={coordinateInputs[edge]}
              disabled={disabled || !box}
              onChange={(event) =>
                setCoordinateInputs((current) => ({
                  ...current,
                  [edge]: event.target.value,
                }))
              }
              onBlur={() => updateBoxEdge(edge, coordinateInputs[edge])}
              onKeyDown={(event) => {
                if (event.key === "Enter") event.currentTarget.blur();
              }}
            />
          </div>
        ))}
      </div>
      {inputError && (
        <p role="alert" className="text-sm text-destructive">
          {inputError}
        </p>
      )}
      <div className="flex flex-wrap gap-2">
        <Button
          type="button"
          variant="outline"
          onClick={addPointAtFrameCenter}
          disabled={disabled || point !== null}
        >
          {text.addPoint}
        </Button>
        <Button
          type="button"
          variant="outline"
          onClick={addEditableBoxAroundPoint}
          disabled={disabled || point === null || box !== null}
        >
          {text.addBox}
        </Button>
        <Button
          type="button"
          variant="outline"
          onClick={onUndo}
          disabled={disabled || !canUndo}
        >
          <Redo2 className="mr-2 h-4 w-4 scale-x-[-1]" aria-hidden="true" />
          {text.undo}
        </Button>
        <Button
          type="button"
          variant="destructive"
          onClick={onClearGeometry}
          disabled={disabled || (!point && !box)}
        >
          <Trash2 className="mr-2 h-4 w-4" aria-hidden="true" />
          {text.clearGeometry}
        </Button>
      </div>
    </section>
  );
}
