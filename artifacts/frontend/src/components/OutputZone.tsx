import { useEffect, useState } from "react";
import { Card } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Progress } from "@/components/ui/progress";
import { Download, RefreshCw, Copy, Check, Terminal } from "lucide-react";
import {
  useGetStatus,
  useGetResult,
  getGetStatusQueryKey,
  getGetResultQueryKey,
  ProductionStatusStatus,
} from "@workspace/api-client-react";
import { useToast } from "@/hooks/use-toast";
import { GenerationPreview } from "@/components/GenerationPreview";

interface OutputZoneProps {
  isActive:  boolean;
  sessionId: string | null;
  onRestart: () => void;
}

export function OutputZone({ isActive, sessionId, onRestart }: OutputZoneProps) {
  const { toast }                 = useToast();
  const [copiedTag, setCopiedTag] = useState<string | null>(null);

  const { data: statusData, error: statusError } = useGetStatus(sessionId ?? "", {
    query: {
      queryKey: getGetStatusQueryKey(sessionId ?? ""),
      enabled: !!sessionId,
      refetchInterval: (query) => {
        const s = query.state.data?.status;
        return s === ProductionStatusStatus.done || s === ProductionStatusStatus.error
          ? false
          : 2000;
      },
    },
  });

  const { data: resultData } = useGetResult(sessionId ?? "", {
    query: {
      queryKey: getGetResultQueryKey(sessionId ?? ""),
      enabled: !!sessionId && statusData?.status === ProductionStatusStatus.done,
    },
  });

  useEffect(() => {
    if (statusError) {
      toast({ title: "Polling Error", description: "Failed to get production status.", variant: "destructive" });
    }
  }, [statusError, toast]);

  useEffect(() => {
    if (statusData?.status === ProductionStatusStatus.error) {
      toast({ title: "Production Error", description: statusData.error ?? "An error occurred.", variant: "destructive" });
    }
  }, [statusData?.status, statusData?.error, toast]);

  const copyToClipboard = (text: string) => {
    navigator.clipboard.writeText(text);
    setCopiedTag(text);
    setTimeout(() => setCopiedTag(null), 2000);
  };

  const handleDownload = () => {
    if (!sessionId) return;
    const a = document.createElement("a");
    a.href     = `/api/download/${sessionId}`;
    a.download = `video-${sessionId}.mp4`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
  };

  // ── Inactive placeholder ──
  if (!isActive) {
    return (
      <div className="opacity-30 pointer-events-none transition-opacity duration-500">
        <div className="flex items-center justify-between mb-6">
          <h2 className="text-xl font-bold flex items-center gap-2">
            <span className="text-primary"><Terminal className="w-5 h-5" /></span>
            STEP 3: OUTPUT TERMINAL
          </h2>
        </div>
        <Card className="border-border bg-card/30 p-8 flex items-center justify-center min-h-[200px]">
          <p className="text-muted-foreground font-mono text-sm uppercase tracking-widest">
            Awaiting production start...
          </p>
        </Card>
      </div>
    );
  }

  const isDone      = statusData?.status === ProductionStatusStatus.done;
  const hasError    = statusData?.status === ProductionStatusStatus.error;
  const isAssembling = statusData?.status === ProductionStatusStatus.assembling;
  // Any active non-assembling, non-done, non-error state = generating phase
  const isGenerating = !isDone && !hasError && !isAssembling;

  return (
    <div className="transition-all duration-500 opacity-100">
      <div className="flex items-center justify-between mb-6">
        <h2 className="text-xl font-bold flex items-center gap-2">
          <span className="text-primary"><Terminal className="w-5 h-5" /></span>
          STEP 3: OUTPUT TERMINAL
        </h2>
      </div>

      <Card className="border-border bg-card p-6 shadow-2xl relative overflow-hidden">
        {/* Ambient glow while processing */}
        {!isDone && !hasError && (
          <div className="absolute inset-0 bg-primary/5 animate-pulse pointer-events-none" />
        )}

        {/* ── Generating / assembling ── */}
        {!isDone && !hasError && (
          <div className="relative z-10">
            <div className="py-8 flex flex-col items-center justify-center text-center">
              <div className="w-24 h-24 relative mb-6">
                <div className="absolute inset-0 border-4 border-primary/20 rounded-full" />
                <div className="absolute inset-0 border-4 border-primary rounded-full border-t-transparent animate-spin" />
                <div className="absolute inset-0 flex items-center justify-center text-xs font-mono font-bold text-primary">
                  {statusData?.progress ?? 0}%
                </div>
              </div>
              <h3 className="text-lg font-mono uppercase mb-2 tracking-widest text-primary">
                {isAssembling ? "Assemblage" : "Génération"}
              </h3>
              <p className="text-muted-foreground font-mono text-sm max-w-md">
                {statusData?.current_step ?? "Initialisation..."}
              </p>
              <div className="w-full max-w-md mt-6">
                <Progress value={statusData?.progress ?? 0} className="h-1" />
              </div>
            </div>

            {/* Real-time image + audio preview during generating phase */}
            {isGenerating && sessionId && (
              <GenerationPreview sessionId={sessionId} active={isGenerating} />
            )}
          </div>
        )}

        {/* ── Error ── */}
        {hasError && (
          <div className="py-12 flex flex-col items-center justify-center text-center">
            <div className="w-16 h-16 rounded-full bg-destructive/20 flex items-center justify-center mb-6">
              <span className="text-destructive font-bold text-2xl">!</span>
            </div>
            <h3 className="text-xl font-bold text-destructive mb-2">Production Failed</h3>
            <p className="text-muted-foreground mb-8 max-w-md font-mono text-xs">
              {statusData?.error}
            </p>
            <Button variant="outline" onClick={onRestart}>
              <RefreshCw className="w-4 h-4 mr-2" /> Nouvelle session
            </Button>
          </div>
        )}

        {/* ── Done ── */}
        {isDone && resultData && (
          <div className="grid md:grid-cols-2 gap-8 relative z-10">
            <div className="space-y-4">
              <div className="aspect-video bg-black rounded overflow-hidden border border-primary/30 shadow-[0_0_30px_rgba(0,255,255,0.15)]">
                <video
                  src={`/api/download/${sessionId}`}
                  controls
                  className="w-full h-full object-contain"
                  data-testid="video-player"
                />
              </div>
              <div className="flex gap-3">
                <Button
                  onClick={handleDownload}
                  className="flex-1 font-mono uppercase"
                  data-testid="button-download"
                >
                  <Download className="w-4 h-4 mr-2" /> Télécharger MP4
                </Button>
                <Button variant="outline" onClick={onRestart}>
                  <RefreshCw className="w-4 h-4 mr-2" /> Nouveau
                </Button>
              </div>
            </div>

            <div className="space-y-5 flex flex-col justify-center">
              <div>
                <h3 className="text-2xl font-bold mb-1 text-white leading-tight">
                  {resultData.title}
                </h3>
                <p className="text-xs font-mono text-primary/70 mb-3">
                  Durée : {Math.round(resultData.duration_seconds)}s
                </p>
                <p className="text-muted-foreground leading-relaxed text-sm">
                  {resultData.description}
                </p>
              </div>

              <div>
                <h4 className="text-xs font-mono uppercase tracking-widest text-muted-foreground mb-3">
                  Hashtags
                </h4>
                <div className="flex flex-wrap gap-2">
                  {resultData.hashtags.map((tag) => (
                    <button
                      key={tag}
                      onClick={() => copyToClipboard(tag)}
                      className="px-3 py-1.5 text-xs font-mono bg-secondary hover:bg-primary hover:text-primary-foreground text-secondary-foreground rounded border border-border flex items-center gap-1.5 transition-colors"
                      title="Copier"
                    >
                      {copiedTag === tag ? <Check className="w-3 h-3" /> : <Copy className="w-3 h-3" />}
                      {tag}
                    </button>
                  ))}
                </div>
              </div>
            </div>
          </div>
        )}
      </Card>
    </div>
  );
}
