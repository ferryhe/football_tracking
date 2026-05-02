import { useState } from "react";
import { useQuery, useMutation } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { formatDateTime, runMoment, statusBadgeClass } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Badge } from "@/components/ui/badge";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Separator } from "@/components/ui/separator";
import { Textarea } from "@/components/ui/textarea";
import { Label } from "@/components/ui/label";
import { Sparkles, Brain, AlertCircle, Loader2, CheckCircle2, ChevronDown, ChevronUp } from "lucide-react";
import { cn } from "@/lib/utils";
import { useToast } from "@/hooks/use-toast";
import type { AISuggestion } from "@/lib/types";

export default function AIAnalysisPage() {
  const { toast } = useToast();
  const [selectedRunId, setSelectedRunId] = useState("");
  const [objective, setObjective] = useState("");
  const [showPatch, setShowPatch] = useState(false);
  const [suggestion, setSuggestion] = useState<AISuggestion | null>(null);

  const { data: runs, isLoading: runsLoading } = useQuery({
    queryKey: ["runs"],
    queryFn: api.listRuns,
    refetchInterval: 10_000,
  });

  const completedRuns = (runs ?? []).filter((r) => r.status === "completed");
  const selectedRun = completedRuns.find((r) => r.run_id === selectedRunId) ?? null;

  const recommend = useMutation({
    mutationFn: () =>
      api.aiRecommend({
        run_id: selectedRunId,
        objective: objective.trim() || undefined,
        language: "en",
      }),
    onSuccess: (data) => {
      setSuggestion(data);
      toast({ title: "AI recommendation ready" });
    },
    onError: (err: Error) => {
      toast({
        title: "AI recommendation failed",
        description: err.message,
        variant: "destructive",
      });
    },
  });

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold">AI Analysis</h1>
        <p className="text-muted-foreground mt-1">
          Select a completed run and ask the AI for tracking improvement recommendations.
        </p>
      </div>

      <div className="grid gap-4 md:grid-cols-2">
        {/* Run selector */}
        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="flex items-center gap-2 text-base">
              <Brain className="h-4 w-4 text-primary" />
              Select Run
            </CardTitle>
            <CardDescription>Only completed runs can be analysed</CardDescription>
          </CardHeader>
          <CardContent className="space-y-3">
            {runsLoading ? (
              <div className="flex items-center gap-2 text-muted-foreground text-sm">
                <Loader2 className="h-4 w-4 animate-spin" />
                Loading runs…
              </div>
            ) : !completedRuns.length ? (
              <Alert>
                <AlertCircle className="h-4 w-4" />
                <AlertDescription>No completed runs found yet.</AlertDescription>
              </Alert>
            ) : (
              <Select value={selectedRunId} onValueChange={setSelectedRunId} data-testid="select-ai-run">
                <SelectTrigger data-testid="trigger-ai-run">
                  <SelectValue placeholder="Select a completed run…" />
                </SelectTrigger>
                <SelectContent>
                  {completedRuns.map((r) => (
                    <SelectItem key={r.run_id} value={r.run_id} data-testid={`option-ai-run-${r.run_id}`}>
                      {r.run_id}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            )}

            {selectedRun && (
              <div className="rounded-md bg-muted/50 p-3 space-y-1.5">
                <div className="flex items-center gap-2">
                  <span className={cn("text-xs font-medium px-2 py-0.5 rounded-full", statusBadgeClass(selectedRun.status))}>
                    {selectedRun.status}
                  </span>
                  <span className="text-xs text-muted-foreground">
                    {formatDateTime(runMoment(selectedRun))}
                  </span>
                </div>
                <p className="text-xs font-mono text-muted-foreground truncate">{selectedRun.output_dir}</p>
                {selectedRun.input_video && (
                  <p className="text-xs text-muted-foreground truncate">
                    Video: {selectedRun.input_video}
                  </p>
                )}
              </div>
            )}
          </CardContent>
        </Card>

        {/* Objective */}
        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="flex items-center gap-2 text-base">
              <Sparkles className="h-4 w-4 text-primary" />
              Objective (optional)
            </CardTitle>
            <CardDescription>Describe what you want to improve</CardDescription>
          </CardHeader>
          <CardContent className="space-y-3">
            <Label htmlFor="input-objective" className="sr-only">Objective</Label>
            <Textarea
              id="input-objective"
              placeholder="e.g. Reduce lost frames in fast-moving sequences…"
              value={objective}
              onChange={(e) => setObjective(e.target.value)}
              rows={4}
              data-testid="input-objective"
              className="resize-none"
            />
            <Button
              onClick={() => recommend.mutate()}
              disabled={!selectedRunId || recommend.isPending}
              className="w-full"
              data-testid="button-ai-recommend"
            >
              {recommend.isPending ? (
                <>
                  <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                  Analysing…
                </>
              ) : (
                <>
                  <Sparkles className="h-4 w-4 mr-2" />
                  Get Recommendation
                </>
              )}
            </Button>
          </CardContent>
        </Card>
      </div>

      {/* Results */}
      {suggestion && (
        <Card data-testid="card-ai-suggestion">
          <CardHeader className="pb-3">
            <div className="flex items-start gap-3">
              <CheckCircle2 className="h-5 w-5 text-emerald-500 mt-0.5 shrink-0" />
              <div>
                <CardTitle className="text-base">{suggestion.title}</CardTitle>
                <CardDescription className="mt-1">{suggestion.diagnosis}</CardDescription>
              </div>
            </div>
          </CardHeader>
          <CardContent className="space-y-4">
            <div>
              <p className="text-sm font-medium mb-1">Recommendation</p>
              <p className="text-sm text-muted-foreground">{suggestion.recommendation}</p>
            </div>

            {suggestion.expected_tradeoff && (
              <>
                <Separator />
                <div>
                  <p className="text-sm font-medium mb-1">Expected tradeoff</p>
                  <p className="text-sm text-muted-foreground">{suggestion.expected_tradeoff}</p>
                </div>
              </>
            )}

            {suggestion.evidence.length > 0 && (
              <>
                <Separator />
                <div>
                  <p className="text-sm font-medium mb-2">Evidence</p>
                  <ul className="space-y-1">
                    {suggestion.evidence.map((e, i) => (
                      <li key={i} className="text-sm text-muted-foreground flex gap-2">
                        <span className="text-primary mt-0.5">•</span>
                        {e}
                      </li>
                    ))}
                  </ul>
                </div>
              </>
            )}

            {suggestion.patch_preview.length > 0 && (
              <>
                <Separator />
                <div>
                  <button
                    type="button"
                    onClick={() => setShowPatch((p) => !p)}
                    className="flex items-center gap-2 text-sm font-medium hover:text-primary transition-colors"
                    data-testid="button-toggle-patch"
                  >
                    Config patch preview
                    {showPatch ? <ChevronUp className="h-4 w-4" /> : <ChevronDown className="h-4 w-4" />}
                  </button>
                  {showPatch && (
                    <pre className="mt-2 rounded-md bg-muted p-3 text-xs font-mono overflow-x-auto">
                      {suggestion.patch_preview.join("\n")}
                    </pre>
                  )}
                </div>
              </>
            )}

            {suggestion.output_name_suggestion && (
              <div className="rounded-md bg-accent/50 p-3">
                <p className="text-xs text-muted-foreground">Suggested output name</p>
                <p className="font-mono text-sm font-medium">{suggestion.output_name_suggestion}</p>
              </div>
            )}

            <div className="flex flex-wrap gap-2 pt-1">
              {suggestion.output_name_suggestion && (
                <Badge variant="outline">
                  Suggested: {suggestion.output_name_suggestion}
                </Badge>
              )}
            </div>
          </CardContent>
        </Card>
      )}
    </div>
  );
}
