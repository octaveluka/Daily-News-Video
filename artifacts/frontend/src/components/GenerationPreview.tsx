/**
 * GenerationPreview — real-time grid showing images and audio
 * as they're produced during the generating phase.
 * Polls /api/preview/<sessionId>/manifest every 2s.
 */
import { useEffect, useRef, useState } from "react";
import { Card } from "@/components/ui/card";
import { Images, Music } from "lucide-react";

interface Manifest {
  images: number[];
  audio:  number[];
}

interface GenerationPreviewProps {
  sessionId: string;
  active:    boolean;
}

export function GenerationPreview({ sessionId, active }: GenerationPreviewProps) {
  const [manifest, setManifest]   = useState<Manifest>({ images: [], audio: [] });
  const [playingIdx, setPlayingIdx] = useState<number | null>(null);
  const audioRefs                 = useRef<Record<number, HTMLAudioElement | null>>({});

  useEffect(() => {
    if (!active || !sessionId) return;

    const poll = async () => {
      try {
        const res  = await fetch(`/api/preview/${sessionId}/manifest`);
        if (res.ok) setManifest(await res.json());
      } catch { /* silent */ }
    };

    poll();
    const interval = setInterval(poll, 2000);
    return () => clearInterval(interval);
  }, [sessionId, active]);

  if (!active) return null;
  if (manifest.images.length === 0 && manifest.audio.length === 0) return null;

  const toggleAudio = (idx: number) => {
    const el = audioRefs.current[idx];
    if (!el) return;
    if (playingIdx === idx) {
      el.pause();
      setPlayingIdx(null);
    } else {
      // pause any currently playing
      if (playingIdx !== null && audioRefs.current[playingIdx]) {
        audioRefs.current[playingIdx]!.pause();
      }
      el.play();
      setPlayingIdx(idx);
    }
  };

  return (
    <div className="space-y-6 mt-6">
      {/* Images grid */}
      {manifest.images.length > 0 && (
        <div>
          <div className="flex items-center gap-2 mb-3">
            <Images className="w-4 h-4 text-primary/70" />
            <span className="text-xs font-mono uppercase tracking-widest text-muted-foreground">
              Images générées — {manifest.images.length}/20
            </span>
          </div>
          <div className="grid grid-cols-4 sm:grid-cols-5 gap-2">
            {Array.from({ length: 20 }, (_, i) => {
              const ready = manifest.images.includes(i);
              return (
                <div
                  key={i}
                  className={`aspect-video rounded overflow-hidden border transition-all duration-300 ${
                    ready
                      ? "border-primary/30 shadow-[0_0_8px_rgba(0,255,255,0.15)]"
                      : "border-border/20 bg-card/30"
                  }`}
                >
                  {ready ? (
                    <img
                      src={`/api/preview/${sessionId}/image/${i}`}
                      alt={`Image ${i + 1}`}
                      className="w-full h-full object-cover"
                      loading="lazy"
                    />
                  ) : (
                    <div className="w-full h-full flex items-center justify-center">
                      <span className="text-xs font-mono text-muted-foreground/30">
                        {String(i + 1).padStart(2, "0")}
                      </span>
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        </div>
      )}

      {/* Audio list */}
      {manifest.audio.length > 0 && (
        <div>
          <div className="flex items-center gap-2 mb-3">
            <Music className="w-4 h-4 text-primary/70" />
            <span className="text-xs font-mono uppercase tracking-widest text-muted-foreground">
              Audio synthétisé — {manifest.audio.length}/10
            </span>
          </div>
          <div className="grid grid-cols-2 sm:grid-cols-5 gap-2">
            {Array.from({ length: 10 }, (_, i) => {
              const ready = manifest.audio.includes(i);
              return (
                <div
                  key={i}
                  className={`rounded border px-3 py-2.5 flex items-center gap-2 transition-all ${
                    ready
                      ? "border-primary/30 bg-primary/5 cursor-pointer hover:bg-primary/10"
                      : "border-border/20 bg-card/30 opacity-30"
                  }`}
                  onClick={() => ready && toggleAudio(i)}
                  title={ready ? `Écouter segment ${i + 1}` : undefined}
                >
                  {ready && (
                    <audio
                      ref={(el) => { audioRefs.current[i] = el; }}
                      src={`/api/preview/${sessionId}/audio/${i}`}
                      onEnded={() => setPlayingIdx(null)}
                      preload="none"
                    />
                  )}
                  <div
                    className={`w-6 h-6 rounded-full flex items-center justify-center shrink-0 text-xs ${
                      ready
                        ? playingIdx === i
                          ? "bg-primary text-black"
                          : "bg-primary/20 text-primary"
                        : "bg-border/20 text-muted-foreground/30"
                    }`}
                  >
                    {playingIdx === i ? "■" : "▶"}
                  </div>
                  <span className="text-xs font-mono text-muted-foreground">
                    S{String(i + 1).padStart(2, "0")}
                  </span>
                </div>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}
