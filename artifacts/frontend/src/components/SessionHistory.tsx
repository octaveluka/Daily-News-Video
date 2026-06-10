import { useEffect, useState } from "react";
import { Card } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Progress } from "@/components/ui/progress";
import { Download, History, RefreshCw, Play, AlertCircle, Loader2, CheckCircle } from "lucide-react";

interface SessionSummary {
  session_id: string;
  topic: string;
  title: string;
  status: string;
  progress: number;
  current_step: string;
  error: string | null;
  video_url: string | null;
  created_at: string;
}

interface SessionHistoryProps {
  /** called when user wants to resume viewing a done session */
  onResumeSession: (sessionId: string) => void;
  /** the currently active session id, so we can skip it in the list */
  activeSessionId: string | null;
}

const STATUS_COLORS: Record<string, string> = {
  done:       "bg-emerald-500/20 text-emerald-400 border-emerald-500/30",
  error:      "bg-red-500/20 text-red-400 border-red-500/30",
  generating: "bg-cyan-500/20 text-cyan-400 border-cyan-500/30",
  assembling: "bg-yellow-500/20 text-yellow-400 border-yellow-500/30",
  pending:    "bg-zinc-500/20 text-zinc-400 border-zinc-500/30",
};

const STATUS_LABELS: Record<string, string> = {
  done:       "Terminé",
  error:      "Erreur",
  generating: "En cours",
  assembling: "Montage",
  pending:    "En attente",
};

function StatusIcon({ status }: { status: string }) {
  if (status === "done")       return <CheckCircle className="w-3.5 h-3.5" />;
  if (status === "error")      return <AlertCircle className="w-3.5 h-3.5" />;
  if (status === "pending")    return <Loader2 className="w-3.5 h-3.5 animate-spin" />;
  return <Loader2 className="w-3.5 h-3.5 animate-spin" />;
}

export function SessionHistory({ onResumeSession, activeSessionId }: SessionHistoryProps) {
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [loading, setLoading]   = useState(true);

  const fetchSessions = async () => {
    try {
      const res  = await fetch("/api/sessions");
      const data = await res.json();
      setSessions(Array.isArray(data) ? data : []);
    } catch {
      // silently fail
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchSessions();
    // Refresh every 5s so in-progress sessions update
    const interval = setInterval(fetchSessions, 5000);
    return () => clearInterval(interval);
  }, []);

  // Filter out the currently active session from history to avoid duplication
  const displaySessions = sessions.filter(s => s.session_id !== activeSessionId);

  if (loading) return null;
  if (displaySessions.length === 0) return null;

  return (
    <div className="mt-16">
      {/* Header */}
      <div className="flex items-center justify-between mb-6">
        <h2 className="text-xl font-bold flex items-center gap-2 text-white/70">
          <History className="w-5 h-5 text-primary/70" />
          <span className="uppercase tracking-widest font-mono text-sm">Historique des productions</span>
        </h2>
        <button
          onClick={fetchSessions}
          className="text-muted-foreground hover:text-primary transition-colors"
          title="Actualiser"
        >
          <RefreshCw className="w-4 h-4" />
        </button>
      </div>

      <div className="space-y-3">
        {displaySessions.map((session) => (
          <Card
            key={session.session_id}
            className="border-border bg-card/50 hover:bg-card/80 transition-colors p-4"
          >
            <div className="flex items-start justify-between gap-4">
              {/* Left: topic + step */}
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2 mb-1">
                  <span
                    className={`inline-flex items-center gap-1 text-xs font-mono px-2 py-0.5 rounded border ${
                      STATUS_COLORS[session.status] ?? STATUS_COLORS.pending
                    }`}
                  >
                    <StatusIcon status={session.status} />
                    {STATUS_LABELS[session.status] ?? session.status}
                  </span>
                  <span className="text-xs text-muted-foreground font-mono truncate">
                    {session.session_id.slice(0, 8)}
                  </span>
                </div>

                <p className="font-medium text-white/90 text-sm truncate mb-0.5">
                  {session.title || session.topic}
                </p>
                <p className="text-xs text-muted-foreground truncate">
                  {session.current_step}
                </p>

                {/* Progress bar for in-progress sessions */}
                {session.status !== "done" && session.status !== "error" && (
                  <div className="mt-2">
                    <Progress value={session.progress} className="h-0.5" />
                  </div>
                )}

                {session.status === "error" && session.error && (
                  <p className="text-xs text-red-400 mt-1 truncate">{session.error}</p>
                )}
              </div>

              {/* Right: actions */}
              <div className="flex items-center gap-2 shrink-0">
                {session.status === "done" && session.video_url && (
                  <>
                    <Button
                      size="sm"
                      variant="outline"
                      className="h-8 text-xs font-mono border-primary/30 text-primary hover:bg-primary/10"
                      onClick={() => onResumeSession(session.session_id)}
                    >
                      <Play className="w-3 h-3 mr-1" />
                      Voir
                    </Button>
                    <a
                      href={`/api/download/${session.session_id}`}
                      download={`video-${session.session_id.slice(0, 8)}.mp4`}
                    >
                      <Button
                        size="sm"
                        variant="ghost"
                        className="h-8 text-xs text-muted-foreground hover:text-primary"
                        title="Télécharger"
                      >
                        <Download className="w-3.5 h-3.5" />
                      </Button>
                    </a>
                  </>
                )}

                {(session.status === "generating" || session.status === "assembling" || session.status === "pending") && (
                  <Button
                    size="sm"
                    variant="outline"
                    className="h-8 text-xs font-mono border-cyan-500/30 text-cyan-400 hover:bg-cyan-500/10"
                    onClick={() => onResumeSession(session.session_id)}
                  >
                    <Loader2 className="w-3 h-3 mr-1 animate-spin" />
                    Suivre
                  </Button>
                )}
              </div>
            </div>
          </Card>
        ))}
      </div>
    </div>
  );
}
