import { useState, useEffect } from "react";
import { DashboardZone } from "@/components/DashboardZone";
import { UploadZone }    from "@/components/UploadZone";
import { OutputZone }    from "@/components/OutputZone";
import { SessionHistory } from "@/components/SessionHistory";
import type { SessionInit } from "@workspace/api-client-react";
import type { ImageStyle } from "@/components/UploadZone";

export type VideoFormat = "16:9" | "9:16";

export default function Home() {
  const [sessionData, setSessionData]           = useState<SessionInit | null>(null);
  const [characterImage, setCharacterImage]     = useState<File | null>(null);
  const [videoFormat, setVideoFormat]           = useState<VideoFormat>("16:9");
  const [imageStyle, setImageStyle]             = useState<ImageStyle>("cinematic");
  const [isProducing, setIsProducing]           = useState(false);
  const [productionSessionId, setProductionSessionId] = useState<string | null>(null);

  useEffect(() => {
    if (!sessionData) {
      setCharacterImage(null);
      setIsProducing(false);
      setProductionSessionId(null);
      setVideoFormat("16:9");
      setImageStyle("cinematic");
    }
  }, [sessionData]);

  const handleStartProduction = () => {
    if (!sessionData || !characterImage) return;
    setIsProducing(true);
    setProductionSessionId(sessionData.session_id);
  };

  const handleRestart = () => setSessionData(null);

  /** Called when user clicks "Suivre" on a session already in production */
  const handleResumeSession = (sessionId: string) => {
    setIsProducing(true);
    setProductionSessionId(sessionId);
    setTimeout(() => {
      document.getElementById("output-section")?.scrollIntoView({ behavior: "smooth" });
    }, 100);
  };

  /**
   * Called when user clicks "Continuer" on a pending session that has a script
   * but no character image yet. Fetches the full session data and restores Step 2.
   */
  const handleContinueSession = async (sessionId: string) => {
    try {
      const res  = await fetch(`/api/session-data/${sessionId}`);
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "Session introuvable");
      setSessionData(data as SessionInit);
      setIsProducing(false);
      setProductionSessionId(null);
      setTimeout(() => {
        document.getElementById("upload-section")?.scrollIntoView({ behavior: "smooth" });
      }, 100);
    } catch (err: any) {
      console.error("[Home] continueSession:", err.message);
    }
  };

  return (
    <main className="min-h-[100dvh] bg-background text-foreground selection:bg-primary selection:text-primary-foreground dark">
      <div className="max-w-4xl mx-auto px-6 py-16 space-y-12">
        <header className="mb-12">
          <h1 className="text-4xl font-bold tracking-tight text-white mb-2 uppercase">V-CTRL</h1>
          <p className="text-muted-foreground font-mono text-sm uppercase tracking-widest">
            Automated Video Production Platform // System Online
          </p>
        </header>

        <DashboardZone
          sessionData={sessionData}
          onSessionInit={setSessionData}
          isProducing={isProducing}
        />

        <div id="upload-section">
          <UploadZone
            isActive={!!sessionData && !isProducing}
            isDisabled={!sessionData || isProducing}
            characterImage={characterImage}
            onImageChange={setCharacterImage}
            onProduce={handleStartProduction}
            sessionId={sessionData?.session_id}
            videoFormat={videoFormat}
            onFormatChange={setVideoFormat}
            imageStyle={imageStyle}
            onStyleChange={setImageStyle}
          />
        </div>

        <div id="output-section">
          <OutputZone
            isActive={isProducing || !!productionSessionId}
            sessionId={productionSessionId}
            onRestart={handleRestart}
          />
        </div>

        <SessionHistory
          onResumeSession={handleResumeSession}
          onContinueSession={handleContinueSession}
          activeSessionId={productionSessionId}
        />
      </div>
    </main>
  );
}
