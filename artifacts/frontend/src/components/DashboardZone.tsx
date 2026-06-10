import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { ChevronDown, ChevronUp, Zap, Radio, Search, PenLine, Newspaper } from "lucide-react";
import type { SessionInit } from "@workspace/api-client-react";
import { useToast } from "@/hooks/use-toast";

interface DashboardZoneProps {
  sessionData: SessionInit | null;
  onSessionInit: (data: SessionInit) => void;
  isProducing: boolean;
}

export function DashboardZone({ sessionData, onSessionInit, isProducing }: DashboardZoneProps) {
  const { toast } = useToast();
  const [isSegmentsOpen, setIsSegmentsOpen] = useState(false);
  const [isPending, setIsPending]           = useState(false);
  const [mode, setMode]                     = useState<"auto" | "custom">("auto");
  const [customTopic, setCustomTopic]       = useState("");

  const launchInit = async (topic?: string) => {
    setIsPending(true);
    try {
      const res = await fetch("/api/init", {
        method:  "POST",
        headers: { "Content-Type": "application/json" },
        body:    JSON.stringify(topic ? { topic } : {}),
      });
      if (!res.ok) {
        const err = await res.json();
        throw new Error(err.error || "Erreur serveur");
      }
      const data: SessionInit = await res.json();
      onSessionInit(data);
      setIsSegmentsOpen(true);
    } catch (err: any) {
      toast({
        title:       "Erreur",
        description: err?.message || "Impossible d'initialiser la session.",
        variant:     "destructive",
      });
    } finally {
      setIsPending(false);
    }
  };

  const handleAutoLaunch  = () => launchInit();
  const handleCustomLaunch = () => {
    if (!customTopic.trim()) {
      toast({ title: "Sujet vide", description: "Entrez un sujet avant de continuer.", variant: "destructive" });
      return;
    }
    launchInit(customTopic.trim());
  };

  return (
    <div className={`transition-all duration-500 ${isProducing ? "opacity-50 pointer-events-none" : "opacity-100"}`}>
      <div className="flex items-center justify-between mb-6">
        <h2 className="text-xl font-bold flex items-center gap-2">
          <span className="text-primary"><Radio className="w-5 h-5" /></span>
          STEP 1: SCRIPT ENGINE
        </h2>
      </div>

      {/* ── Initial state ── */}
      {!sessionData && !isPending && (
        <Card className="border-border/50 bg-card/50 p-6 border-dashed space-y-6">
          {/* Mode selector */}
          <div className="flex gap-2">
            <button
              onClick={() => setMode("auto")}
              className={`flex-1 flex items-center justify-center gap-2 py-3 px-4 rounded border text-sm font-mono uppercase tracking-wider transition-all ${
                mode === "auto"
                  ? "border-primary bg-primary/10 text-primary"
                  : "border-border text-muted-foreground hover:border-primary/40"
              }`}
            >
              <Newspaper className="w-4 h-4" />
              Actualité du jour
            </button>
            <button
              onClick={() => setMode("custom")}
              className={`flex-1 flex items-center justify-center gap-2 py-3 px-4 rounded border text-sm font-mono uppercase tracking-wider transition-all ${
                mode === "custom"
                  ? "border-primary bg-primary/10 text-primary"
                  : "border-border text-muted-foreground hover:border-primary/40"
              }`}
            >
              <PenLine className="w-4 h-4" />
              Mon sujet
            </button>
          </div>

          {/* Auto mode */}
          {mode === "auto" && (
            <div className="flex flex-col items-center text-center pt-2">
              <div className="w-14 h-14 rounded-full bg-primary/10 flex items-center justify-center mb-3">
                <Search className="w-7 h-7 text-primary" />
              </div>
              <p className="text-muted-foreground mb-5 text-sm max-w-sm">
                L'IA récupère l'actualité la plus marquante du jour et génère un récit narratif complet en 10 segments.
              </p>
              <Button
                onClick={handleAutoLaunch}
                size="lg"
                className="w-full sm:w-auto font-mono uppercase tracking-wider font-bold shadow-[0_0_20px_rgba(0,255,255,0.4)] hover:shadow-[0_0_30px_rgba(0,255,255,0.6)] transition-all"
                data-testid="button-launch-search"
              >
                <Zap className="mr-2 w-4 h-4" /> Lancer la recherche
              </Button>
            </div>
          )}

          {/* Custom topic mode */}
          {mode === "custom" && (
            <div className="space-y-4 pt-2">
              <div>
                <label className="text-xs font-mono uppercase tracking-widest text-muted-foreground mb-2 block">
                  Ton sujet ou histoire
                </label>
                <textarea
                  value={customTopic}
                  onChange={(e) => setCustomTopic(e.target.value)}
                  placeholder="Ex : La renaissance du Bénin à travers les yeux de la diaspora haïtienne…"
                  rows={4}
                  className="w-full bg-background border border-border rounded p-3 text-sm text-foreground placeholder:text-muted-foreground/50 focus:outline-none focus:ring-1 focus:ring-primary resize-none font-sans"
                />
                <p className="text-xs text-muted-foreground mt-1">
                  L'IA s'occupera d'écrire le récit narratif, les prompts image et les métadonnées.
                </p>
              </div>
              <Button
                onClick={handleCustomLaunch}
                disabled={!customTopic.trim()}
                size="lg"
                className="w-full font-mono uppercase tracking-wider font-bold shadow-[0_0_20px_rgba(0,255,255,0.3)] transition-all"
                data-testid="button-custom-topic"
              >
                <PenLine className="mr-2 w-4 h-4" /> Générer le récit
              </Button>
            </div>
          )}
        </Card>
      )}

      {/* ── Loading skeleton ── */}
      {isPending && (
        <Card className="border-border/50 bg-card/50 p-6 space-y-4">
          <div className="flex items-center gap-4">
            <Skeleton className="w-12 h-12 rounded-full" />
            <div className="space-y-2">
              <Skeleton className="h-5 w-48" />
              <Skeleton className="h-4 w-32" />
            </div>
          </div>
          <Skeleton className="h-20 w-full" />
          <p className="text-xs font-mono text-primary/60 uppercase tracking-widest animate-pulse text-center">
            Génération du récit en cours…
          </p>
        </Card>
      )}

      {/* ── Session ready ── */}
      {sessionData && (
        <Card className="border-primary/30 bg-card/80 p-6 shadow-[0_0_15px_rgba(0,255,255,0.05)]">
          <div className="mb-5">
            <h3 className="text-xs text-primary font-mono uppercase tracking-widest mb-1">Récit ciblé</h3>
            <div className="text-xl font-bold mb-1 leading-tight">{sessionData.title}</div>
            <p className="text-muted-foreground text-sm">{sessionData.description}</p>
          </div>

          <div className="flex flex-wrap gap-2 mb-5">
            {sessionData.hashtags.map((tag: string, i: number) => (
              <span
                key={i}
                className="px-2 py-1 text-xs font-mono bg-secondary text-secondary-foreground rounded border border-border"
              >
                {tag}
              </span>
            ))}
          </div>

          <Collapsible open={isSegmentsOpen} onOpenChange={setIsSegmentsOpen}>
            <CollapsibleTrigger asChild>
              <Button
                variant="outline"
                className="w-full flex justify-between font-mono text-xs uppercase"
                data-testid="button-toggle-segments"
              >
                <span>Voir les {sessionData.segments.length} segments du récit</span>
                {isSegmentsOpen ? <ChevronUp className="w-4 h-4" /> : <ChevronDown className="w-4 h-4" />}
              </Button>
            </CollapsibleTrigger>
            <CollapsibleContent className="mt-4 space-y-3">
              {sessionData.segments.map((seg: SessionInit["segments"][number]) => (
                <div key={seg.index} className="p-4 bg-background border border-border/50 rounded-sm">
                  <div className="flex items-start gap-4">
                    <div className="w-8 h-8 shrink-0 bg-secondary flex items-center justify-center rounded text-xs font-mono font-bold text-muted-foreground">
                      {String(seg.index + 1).padStart(2, "0")}
                    </div>
                    <div className="flex-1 min-w-0">
                      <p className="text-sm leading-relaxed mb-3 text-white/90 italic">
                        "{seg.text}"
                      </p>
                      <div className="space-y-1">
                        {seg.image_prompts.map((prompt: string, pIdx: number) => (
                          <div
                            key={pIdx}
                            className="text-xs font-mono text-primary/60 bg-primary/5 px-2 py-1 rounded truncate"
                          >
                            ▸ {prompt}
                          </div>
                        ))}
                      </div>
                    </div>
                  </div>
                </div>
              ))}
            </CollapsibleContent>
          </Collapsible>
        </Card>
      )}
    </div>
  );
}
