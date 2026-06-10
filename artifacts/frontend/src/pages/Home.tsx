import { useState, useEffect } from "react";
import { DashboardZone } from "@/components/DashboardZone";
import { UploadZone } from "@/components/UploadZone";
import { OutputZone } from "@/components/OutputZone";
import { SessionInit } from "@workspace/api-client-react/src/generated/api.schemas";

export default function Home() {
  const [sessionData, setSessionData] = useState<SessionInit | null>(null);
  const [characterImage, setCharacterImage] = useState<File | null>(null);
  const [isProducing, setIsProducing] = useState(false);
  const [productionSessionId, setProductionSessionId] = useState<string | null>(null);

  // When sessionData is cleared, reset subsequent steps
  useEffect(() => {
    if (!sessionData) {
      setCharacterImage(null);
      setIsProducing(false);
      setProductionSessionId(null);
    }
  }, [sessionData]);

  const handleStartProduction = async () => {
    if (!sessionData || !characterImage) return;
    setIsProducing(true);
    setProductionSessionId(sessionData.session_id);
  };

  const handleRestart = () => {
    setSessionData(null);
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

        <UploadZone 
          isActive={!!sessionData && !isProducing}
          isDisabled={!sessionData || isProducing}
          characterImage={characterImage}
          onImageChange={setCharacterImage}
          onProduce={handleStartProduction}
          sessionId={sessionData?.session_id}
        />

        <OutputZone 
          isActive={isProducing || !!productionSessionId}
          sessionId={productionSessionId}
          onRestart={handleRestart}
        />
      </div>
    </main>
  );
}
