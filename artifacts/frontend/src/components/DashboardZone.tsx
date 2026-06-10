import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { ChevronDown, ChevronUp, Zap, Radio, Search } from "lucide-react";
import { useInitSession } from "@workspace/api-client-react";
import { SessionInit } from "@workspace/api-client-react/src/generated/api.schemas";
import { useToast } from "@/hooks/use-toast";

interface DashboardZoneProps {
  sessionData: SessionInit | null;
  onSessionInit: (data: SessionInit) => void;
  isProducing: boolean;
}

export function DashboardZone({ sessionData, onSessionInit, isProducing }: DashboardZoneProps) {
  const initSession = useInitSession();
  const { toast } = useToast();
  const [isSegmentsOpen, setIsSegmentsOpen] = useState(false);

  const handleLaunch = () => {
    initSession.mutate(undefined, {
      onSuccess: (data) => {
        onSessionInit(data);
        setIsSegmentsOpen(true);
      },
      onError: (err) => {
        toast({
          title: "System Error",
          description: err?.error || "Failed to initialize search.",
          variant: "destructive",
        });
      }
    });
  };

  return (
    <div className={`transition-all duration-500 ${isProducing ? 'opacity-50 pointer-events-none' : 'opacity-100'}`}>
      <div className="flex items-center justify-between mb-6">
        <h2 className="text-xl font-bold flex items-center gap-2">
          <span className="text-primary"><Radio className="w-5 h-5" /></span>
          STEP 1: SCRIPT ENGINE
        </h2>
      </div>

      {!sessionData && !initSession.isPending && (
        <Card className="border-border/50 bg-card/50 p-8 flex flex-col items-center justify-center border-dashed">
          <div className="w-16 h-16 rounded-full bg-primary/10 flex items-center justify-center mb-4">
            <Search className="w-8 h-8 text-primary" />
          </div>
          <p className="text-muted-foreground mb-6 text-center max-w-sm">
            Initialize the engine to fetch the latest trending topics and generate a multi-segment script.
          </p>
          <Button 
            onClick={handleLaunch} 
            size="lg" 
            className="w-full sm:w-auto font-mono uppercase tracking-wider text-primary-foreground font-bold shadow-[0_0_20px_rgba(0,255,255,0.4)] hover:shadow-[0_0_30px_rgba(0,255,255,0.6)] transition-all"
            data-testid="button-launch-search"
          >
            <Zap className="mr-2 w-4 h-4" /> Lancer la recherche
          </Button>
        </Card>
      )}

      {initSession.isPending && (
        <Card className="border-border/50 bg-card/50 p-6 space-y-4">
          <div className="flex items-center gap-4">
            <Skeleton className="w-12 h-12 rounded-full" />
            <div className="space-y-2">
              <Skeleton className="h-5 w-48" />
              <Skeleton className="h-4 w-32" />
            </div>
          </div>
          <Skeleton className="h-20 w-full" />
        </Card>
      )}

      {sessionData && (
        <Card className="border-primary/30 bg-card/80 p-6 shadow-[0_0_15px_rgba(0,255,255,0.05)]">
          <div className="mb-6">
            <h3 className="text-xs text-primary font-mono uppercase tracking-widest mb-1">Target Acquired</h3>
            <div className="text-2xl font-bold mb-2">{sessionData.topic}</div>
            <p className="text-muted-foreground">{sessionData.description}</p>
          </div>

          <div className="flex flex-wrap gap-2 mb-6">
            {sessionData.hashtags.map((tag, i) => (
              <span key={i} className="px-2 py-1 text-xs font-mono bg-secondary text-secondary-foreground rounded border border-border">
                {tag}
              </span>
            ))}
          </div>

          <Collapsible open={isSegmentsOpen} onOpenChange={setIsSegmentsOpen}>
            <CollapsibleTrigger asChild>
              <Button variant="outline" className="w-full flex justify-between font-mono text-xs uppercase" data-testid="button-toggle-segments">
                <span>View {sessionData.segments.length} Segments</span>
                {isSegmentsOpen ? <ChevronUp className="w-4 h-4" /> : <ChevronDown className="w-4 h-4" />}
              </Button>
            </CollapsibleTrigger>
            <CollapsibleContent className="mt-4 space-y-3">
              {sessionData.segments.map((seg) => (
                <div key={seg.index} className="p-4 bg-background border border-border/50 rounded-sm">
                  <div className="flex items-start gap-4">
                    <div className="w-8 h-8 shrink-0 bg-secondary flex items-center justify-center rounded text-xs font-mono font-bold text-muted-foreground">
                      {String(seg.index).padStart(2, '0')}
                    </div>
                    <div>
                      <p className="text-sm leading-relaxed mb-3">{seg.text}</p>
                      <div className="space-y-1">
                        {seg.image_prompts.map((prompt, pIdx) => (
                          <div key={pIdx} className="text-xs font-mono text-primary/70 bg-primary/5 px-2 py-1 rounded">
                            &gt; {prompt}
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
