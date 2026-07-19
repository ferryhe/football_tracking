import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Progress } from "@/components/ui/progress";
import { useLanguage } from "@/contexts/LanguageContext";
import { BALL_ANNOTATION_OPERATOR_GOVERNANCE } from "@/lib/ballAnnotationGovernance";

export type FeasibilityStatus =
  | "not_applicable"
  | "insufficient_evidence"
  | "feasibility_failed"
  | "feasibility_passed";

export interface FeasibilityDashboardView {
  status: FeasibilityStatus;
  totalFrames: number;
  annotatedFrames: number;
  confirmedLocalizablePositiveFrames: number;
  confirmedAbsentFrames: number;
  unknownFrames: number;
  excludedFrames: number;
  unconfirmedSuggestions: number;
  applicableScaleStrata: string[];
  applicableLightingStrata: string[];
  scalePositiveCounts: Record<string, number>;
  lightingPositiveCounts: Record<string, number>;
  motionPositiveCounts: Record<string, number>;
  missingStrata: string[];
  reasonCodes: string[];
  contradictions: Array<{ frameIndex: number; diagnosticCodes: string[] }>;
  requiresNewAttempt: boolean;
  datasetExpansionEligibility: {
    eligible: boolean;
    reasons: string[];
    localizablePositiveSeedCount: number;
    pendingSuggestionDecisionCount: number;
  };
  authorityGates: {
    developmentPackageBound: boolean;
    checkProbeBound: boolean;
    sealedEvidenceBound: boolean;
  };
  rawCounts: {
    top1Matches: number;
    top5Matches: number;
    evaluablePositives: number;
    falseCandidates: number;
    evaluableFrames: number;
    rawCandidates: number;
    candidateBudget: number;
  };
  intervals: {
    method: string;
    top1RecallLower: number;
    top5RecallLower: number;
    falseCandidatesPerFrameUpper: number;
  };
  lockedAttempt: {
    sessionId: string;
    profileId: string;
    profileSha256: string;
    metricProfileId: string;
    dataRole: "development" | "check";
    revealed: boolean;
  };
}

function dashboardLabels(language: "en" | "zh") {
  return language === "zh"
    ? {
        title: "20–50 帧可行性",
        description:
          "一次性数据隔离检查；显示原始计数、覆盖层次和预先锁定的区间。",
        progress: (annotated: number, total: number) =>
          `已标注 ${annotated} / ${total} 帧`,
        positives: "可定位正样本",
        backgrounds: "确认缺席背景",
        excluded: "排除/不可判定",
        unknown: "未知",
        suggestions: (count: number) => `${count} 个未确认建议`,
        missing: "缺少的层次",
        reasons: "状态原因",
        motion: "运动/遮挡层次",
        eligibility: "开发集扩充资格",
        eligible: "可扩充到约 100–300 个确认框",
        ineligible: "当前不可扩充训练集",
        authority: "权威门禁",
        complete: "适用层次覆盖完整",
        contradictionTitle: "预声明层次与确认标注冲突",
        contradictionDetail: (dimension: string, stratum: string) =>
          `${dimension === "scale" ? "尺度" : "光照"}“${stratum}”在揭示前声明为不适用，但确认标注使用了该层次。`,
        top1: "Top-1 命中 / 正样本",
        top5: "Top-5 命中 / 正样本",
        falseCandidates: "假候选 / 可评估帧",
        rawCandidates: "原始候选",
        top1Lower: "Top-1 单侧 95% 下界",
        top5Lower: "Top-5 单侧 95% 下界",
        falseUpper: "假候选/帧单侧 95% 上界",
        attempt: "已锁定尝试",
        checkOnly: "数据隔离检查 · 仅用于评估，绝不能用于训练",
        development: "开发集 · 仅为探索性证据",
        revealed: "数据隔离的未见帧检查结果已揭示；该组永久退休",
        exploratory: "小样本探索性结果",
        status: {
          not_applicable: "仅开发证据",
          insufficient_evidence: "证据不足",
          feasibility_failed: "可行性未通过",
          feasibility_passed: "可行性通过",
        },
        passedBoundary:
          "通过只授权扩充到 100–300 个确认框；不批准试跑、机位、全量任务或生产使用。",
        failedBoundary: "当前结果不能进入训练扩充或试跑接受。",
        nextRecommendationTitle: "建议的下一步",
        nextRecommendation:
          "更换模型或配置，或补充更困难的开发帧。任何下一次检查都必须使用新冻结的未见时间组。",
      }
    : {
        title: "20–50-frame feasibility",
        description:
          "One-time data-isolated check with raw counts, strata coverage, and predeclared intervals.",
        progress: (annotated: number, total: number) =>
          `${annotated} / ${total} frames annotated`,
        positives: "Localizable positives",
        backgrounds: "Confirmed absent backgrounds",
        excluded: "Excluded / unresolvable",
        unknown: "Unknown",
        suggestions: (count: number) => `${count} unconfirmed suggestions`,
        missing: "Missing strata",
        reasons: "Status reasons",
        motion: "Motion / occlusion strata",
        eligibility: "Development dataset-expansion eligibility",
        eligible: "Eligible to expand toward 100–300 confirmed boxes",
        ineligible: "Not eligible for dataset expansion",
        authority: "Authority gates",
        complete: "All applicable strata covered",
        contradictionTitle:
          "Declared strata conflict with confirmed annotations",
        contradictionDetail: (dimension: string, stratum: string) =>
          `${dimension === "scale" ? "Scale" : "Lighting"} ‘${stratum}’ was declared not applicable before reveal, but confirmed annotations use it.`,
        top1: "Top-1 matches / positives",
        top5: "Top-5 matches / positives",
        falseCandidates: "False candidates / evaluable frames",
        rawCandidates: "Raw candidates",
        top1Lower: "Top-1 one-sided 95% lower bound",
        top5Lower: "Top-5 one-sided 95% lower bound",
        falseUpper: "False candidates/frame one-sided 95% upper bound",
        attempt: "Locked attempt",
        checkOnly: "Data-isolated check · evaluation only, never training data",
        development: "Development · exploratory evidence only",
        revealed:
          "Data-isolated unseen-frame check result revealed; this group is permanently retired",
        exploratory: "Exploratory small-sample result",
        status: {
          not_applicable: "Development evidence only",
          insufficient_evidence: "Insufficient evidence",
          feasibility_failed: "Feasibility failed",
          feasibility_passed: "Feasibility passed",
        },
        passedBoundary:
          "Passed only authorizes expanding to 100–300 confirmed boxes. It does not approve a trial, camera, full run, or production use.",
        failedBoundary:
          "This result cannot authorize dataset expansion or trial acceptance.",
        nextRecommendationTitle: "Recommended next step",
        nextRecommendation:
          "Try another model/profile or collect harder development frames. Any next check must use a newly frozen unseen temporal group.",
      };
}

function percent(value: number) {
  return Number.isFinite(value) ? `${(value * 100).toFixed(1)}%` : "—";
}

export function ProductionFeasibilityDashboard({
  view,
}: {
  view: FeasibilityDashboardView;
}) {
  const { language } = useLanguage();
  const text = dashboardLabels(language);
  const progress =
    view.totalFrames > 0
      ? Math.min(
          100,
          Math.max(0, (view.annotatedFrames / view.totalFrames) * 100),
        )
      : 0;
  const legacyContradictions = view.missingStrata.flatMap((item) => {
    const match = /^applicability_contradiction:(scale|lighting):([^:]+)$/.exec(
      item,
    );
    return match ? [{ dimension: match[1], stratum: match[2] }] : [];
  });
  const contradictions = [
    ...legacyContradictions,
    ...view.contradictions.flatMap((item) =>
      item.diagnosticCodes.map((code) => ({
        dimension: code.startsWith("scale") ? "scale" : "lighting",
        stratum: `frame ${item.frameIndex}: ${code}`,
      })),
    ),
  ];

  return (
    <Card className="min-w-0" data-testid="feasibility-dashboard">
      <CardHeader>
        <div className="flex min-w-0 flex-wrap items-center justify-between gap-2">
          <CardTitle>{text.title}</CardTitle>
          <Badge variant="outline">{text.status[view.status]}</Badge>
        </div>
        <CardDescription>{text.description}</CardDescription>
      </CardHeader>
      <CardContent className="min-w-0 space-y-5">
        <div
          className="rounded-md border bg-muted/30 p-3 text-sm"
          data-testid="ball-annotation-dashboard-governance"
        >
          {BALL_ANNOTATION_OPERATOR_GOVERNANCE}
        </div>
        <div className="space-y-2">
          <p role="status" className="text-sm font-medium">
            {text.progress(view.annotatedFrames, view.totalFrames)}
          </p>
          <Progress value={progress} aria-label={text.title} />
          <dl className="grid gap-2 text-sm sm:grid-cols-5">
            <div className="rounded-md border p-2">
              <dt>{text.positives}</dt>
              <dd className="font-mono">
                {view.confirmedLocalizablePositiveFrames}
              </dd>
            </div>
            <div className="rounded-md border p-2">
              <dt>{text.backgrounds}</dt>
              <dd className="font-mono">{view.confirmedAbsentFrames}</dd>
            </div>
            <div className="rounded-md border p-2">
              <dt>{text.unknown}</dt>
              <dd className="font-mono">{view.unknownFrames}</dd>
            </div>
            <div className="rounded-md border p-2">
              <dt>{text.excluded}</dt>
              <dd className="font-mono">{view.excludedFrames}</dd>
            </div>
            <div className="rounded-md border p-2">
              <dt>{text.suggestions(view.unconfirmedSuggestions)}</dt>
              <dd className="font-mono">{view.unconfirmedSuggestions}</dd>
            </div>
          </dl>
        </div>

        <section
          aria-labelledby="feasibility-strata-title"
          className="space-y-2"
        >
          <h4 id="feasibility-strata-title" className="font-semibold">
            {view.missingStrata.length ? text.missing : text.complete}
          </h4>
          {view.missingStrata.length > 0 && (
            <ul className="flex flex-wrap gap-2">
              {view.missingStrata.map((stratum) => (
                <li key={stratum}>
                  <Badge variant="destructive">{stratum}</Badge>
                </li>
              ))}
            </ul>
          )}
          <div className="grid gap-2 text-xs sm:grid-cols-2">
            <p className="break-words font-mono">
              scale: {JSON.stringify(view.scalePositiveCounts)}
            </p>
            <p className="break-words font-mono">
              lighting: {JSON.stringify(view.lightingPositiveCounts)}
            </p>
            <p className="break-words font-mono sm:col-span-2">
              {text.motion}: {JSON.stringify(view.motionPositiveCounts)}
            </p>
          </div>
        </section>

        <section className="space-y-2" aria-label={text.reasons}>
          <h4 className="font-semibold">{text.reasons}</h4>
          <div className="flex flex-wrap gap-2">
            {(view.reasonCodes.length ? view.reasonCodes : ["none"]).map(
              (reason) => (
                <Badge key={reason} variant="outline">
                  {reason}
                </Badge>
              ),
            )}
          </div>
          {view.requiresNewAttempt && (
            <p className="text-sm text-destructive">requires_new_attempt</p>
          )}
        </section>

        {contradictions.length > 0 && (
          <Alert variant="destructive" aria-label={text.contradictionTitle}>
            <AlertTitle>{text.contradictionTitle}</AlertTitle>
            <AlertDescription>
              <ul className="list-disc space-y-1 pl-5">
                {contradictions.map(({ dimension, stratum }) => (
                  <li key={`${dimension}:${stratum}`}>
                    {text.contradictionDetail(dimension, stratum)}
                  </li>
                ))}
              </ul>
            </AlertDescription>
          </Alert>
        )}

        <div className="overflow-x-auto">
          <table className="w-full min-w-[32rem] border-collapse text-left text-sm">
            <tbody>
              <tr>
                <th className="border p-2">{text.top1}</th>
                <td className="border p-2 font-mono">
                  {view.rawCounts.top1Matches} /{" "}
                  {view.rawCounts.evaluablePositives}
                </td>
                <th className="border p-2">{text.top1Lower}</th>
                <td className="border p-2 font-mono">
                  {percent(view.intervals.top1RecallLower)}
                </td>
              </tr>
              <tr>
                <th className="border p-2">{text.top5}</th>
                <td className="border p-2 font-mono">
                  {view.rawCounts.top5Matches} /{" "}
                  {view.rawCounts.evaluablePositives}
                </td>
                <th className="border p-2">{text.top5Lower}</th>
                <td className="border p-2 font-mono">
                  {percent(view.intervals.top5RecallLower)}
                </td>
              </tr>
              <tr>
                <th className="border p-2">{text.falseCandidates}</th>
                <td className="border p-2 font-mono">
                  {view.rawCounts.falseCandidates} /{" "}
                  {view.rawCounts.evaluableFrames}
                </td>
                <th className="border p-2">{text.falseUpper}</th>
                <td className="border p-2 font-mono">
                  {view.intervals.falseCandidatesPerFrameUpper.toFixed(3)}
                </td>
              </tr>
              <tr>
                <th className="border p-2">{text.rawCandidates}</th>
                <td className="border p-2 font-mono">
                  {view.rawCounts.rawCandidates}
                </td>
                <th className="border p-2">top-k budget</th>
                <td className="border p-2 font-mono">
                  {view.rawCounts.candidateBudget}
                </td>
              </tr>
            </tbody>
          </table>
          <p className="mt-1 break-all font-mono text-xs text-muted-foreground">
            {view.intervals.method}
          </p>
        </div>

        <section className="space-y-2" aria-labelledby="locked-attempt-title">
          <h4 id="locked-attempt-title" className="font-semibold">
            {text.attempt}
          </h4>
          <p>{view.lockedAttempt.profileId}</p>
          <p className="break-all font-mono text-xs">
            {view.lockedAttempt.profileSha256}
          </p>
          <p className="break-all font-mono text-xs">
            {view.lockedAttempt.sessionId} ·{" "}
            {view.lockedAttempt.metricProfileId}
          </p>
          <p className="font-medium">
            {view.lockedAttempt.dataRole === "check"
              ? text.checkOnly
              : text.development}
          </p>
          {view.lockedAttempt.revealed && (
            <p className="text-sm text-muted-foreground">{text.revealed}</p>
          )}
        </section>

        <Alert
          variant={
            view.datasetExpansionEligibility.eligible
              ? "default"
              : "destructive"
          }
        >
          <AlertTitle>{text.eligibility}</AlertTitle>
          <AlertDescription>
            <p>
              {view.datasetExpansionEligibility.eligible
                ? text.eligible
                : text.ineligible}
            </p>
            <p className="mt-1 break-words font-mono text-xs">
              {view.datasetExpansionEligibility.reasons.join(", ") || "none"}
            </p>
          </AlertDescription>
        </Alert>

        <section className="space-y-1 text-sm" aria-label={text.authority}>
          <h4 className="font-semibold">{text.authority}</h4>
          <p className="font-mono">
            development_package_bound=
            {String(view.authorityGates.developmentPackageBound)}
          </p>
          <p className="font-mono">
            check_probe_bound={String(view.authorityGates.checkProbeBound)}
          </p>
          <p className="font-mono">
            sealed_evidence_bound=
            {String(view.authorityGates.sealedEvidenceBound)}
          </p>
        </section>

        <Alert
          variant={
            view.status === "feasibility_passed" ? "default" : "destructive"
          }
        >
          <AlertTitle>{text.exploratory}</AlertTitle>
          <AlertDescription>
            <p>
              {view.status === "feasibility_passed"
                ? text.passedBoundary
                : text.failedBoundary}
            </p>
            {view.requiresNewAttempt && (
              <p className="mt-2 font-medium">
                {text.nextRecommendationTitle}: {text.nextRecommendation}
              </p>
            )}
          </AlertDescription>
        </Alert>
      </CardContent>
    </Card>
  );
}
