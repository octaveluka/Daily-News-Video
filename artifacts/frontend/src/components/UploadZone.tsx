import { useState, useRef } from "react";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { ImagePlus, UserCircle, Play, Monitor, Smartphone } from "lucide-react";
import { useToast } from "@/hooks/use-toast";
import type { VideoFormat } from "@/pages/Home";

export type ImageStyle =
  | "cinematic"
  | "documentary"
  | "anime"
  | "watercolor"
  | "noir"
  | "photorealistic";

const STYLES: { id: ImageStyle; label: string; emoji: string; desc: string }[] = [
  { id: "cinematic",      label: "Cinématique",   emoji: "🎬", desc: "Grain de film, lumière dramatique" },
  { id: "documentary",    label: "Documentaire",  emoji: "📷", desc: "Lumière naturelle, reportage" },
  { id: "anime",          label: "Anime",         emoji: "🎌", desc: "Cel-shaded, couleurs vives" },
  { id: "watercolor",     label: "Aquarelle",     emoji: "🎨", desc: "Peinture douce, palette pastel" },
  { id: "noir",           label: "Néo-Noir",      emoji: "🕵️", desc: "Contraste fort, ambiance sombre" },
  { id: "photorealistic", label: "Photoréaliste", emoji: "📸", desc: "DSLR professionnel, 8k" },
];

interface UploadZoneProps {
  isActive:       boolean;
  isDisabled:     boolean;
  characterImage: File | null;
  onImageChange:  (file: File | null) => void;
  onProduce:      () => void;
  sessionId?:     string;
  videoFormat:    VideoFormat;
  onFormatChange: (fmt: VideoFormat) => void;
  imageStyle:     ImageStyle;
  onStyleChange:  (style: ImageStyle) => void;
}

export function UploadZone({
  isActive, isDisabled, characterImage, onImageChange,
  onProduce, sessionId, videoFormat, onFormatChange,
  imageStyle, onStyleChange,
}: UploadZoneProps) {
  const [isDragging, setIsDragging]   = useState(false);
  const [previewUrl, setPreviewUrl]   = useState<string | null>(null);
  const [isStarting, setIsStarting]   = useState(false);
  const fileInputRef                  = useRef<HTMLInputElement>(null);
  const { toast }                     = useToast();

  const handleDragOver  = (e: React.DragEvent) => { e.preventDefault(); if (!isDisabled) setIsDragging(true); };
  const handleDragLeave = () => setIsDragging(false);

  const processFile = (file: File) => {
    if (!file.type.startsWith("image/")) {
      toast({ title: "Fichier invalide", description: "Veuillez uploader une image.", variant: "destructive" });
      return;
    }
    onImageChange(file);
    setPreviewUrl(URL.createObjectURL(file));
  };

  const handleDrop = (e: React.DragEvent) => {
    e.preventDefault(); setIsDragging(false);
    if (isDisabled) return;
    if (e.dataTransfer.files?.length) processFile(e.dataTransfer.files[0]);
  };

  const handleFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    if (e.target.files?.length) processFile(e.target.files[0]);
  };

  const handleStart = async () => {
    if (!sessionId || !characterImage) return;
    setIsStarting(true);
    try {
      const formData = new FormData();
      formData.append("character_image", characterImage);
      formData.append("video_format",    videoFormat);
      formData.append("image_style",     imageStyle);

      const res = await fetch(`/api/produce/${sessionId}`, { method: "POST", body: formData });
      if (!res.ok) {
        const error = await res.json();
        throw new Error(error.error || "Erreur serveur");
      }
      onProduce();
    } catch (err: any) {
      toast({ title: "Erreur", description: err.message || "Erreur inattendue.", variant: "destructive" });
    } finally {
      setIsStarting(false);
    }
  };

  return (
    <div className={`transition-all duration-500 ${isDisabled ? "opacity-30 pointer-events-none" : "opacity-100"}`}>
      <div className="flex items-center justify-between mb-6">
        <h2 className="text-xl font-bold flex items-center gap-2">
          <span className="text-primary"><UserCircle className="w-5 h-5" /></span>
          STEP 2: CHARACTER INPUT
        </h2>
      </div>

      {/* ── Format selector ── */}
      <div className="flex gap-3 mb-4">
        <button
          onClick={() => onFormatChange("16:9")}
          className={`flex-1 flex items-center justify-center gap-2 py-3 px-4 rounded border text-sm font-mono uppercase tracking-wider transition-all ${
            videoFormat === "16:9"
              ? "border-primary bg-primary/10 text-primary"
              : "border-border text-muted-foreground hover:border-primary/40"
          }`}
        >
          <Monitor className="w-4 h-4" />
          16:9 — YouTube
        </button>
        <button
          onClick={() => onFormatChange("9:16")}
          className={`flex-1 flex items-center justify-center gap-2 py-3 px-4 rounded border text-sm font-mono uppercase tracking-wider transition-all ${
            videoFormat === "9:16"
              ? "border-primary bg-primary/10 text-primary"
              : "border-border text-muted-foreground hover:border-primary/40"
          }`}
        >
          <Smartphone className="w-4 h-4" />
          9:16 — TikTok / Reels
        </button>
      </div>

      {/* ── Style selector ── */}
      <div className="mb-4">
        <p className="text-xs font-mono uppercase tracking-widest text-muted-foreground mb-2">
          Style visuel
        </p>
        <div className="grid grid-cols-3 gap-2">
          {STYLES.map((s) => (
            <button
              key={s.id}
              onClick={() => onStyleChange(s.id)}
              title={s.desc}
              className={`flex flex-col items-center justify-center gap-1 py-2 px-2 rounded border text-xs font-mono transition-all ${
                imageStyle === s.id
                  ? "border-primary bg-primary/10 text-primary shadow-[0_0_10px_rgba(0,255,255,0.15)]"
                  : "border-border text-muted-foreground hover:border-primary/40 hover:text-foreground"
              }`}
            >
              <span className="text-base leading-none">{s.emoji}</span>
              <span className="uppercase tracking-wide leading-none">{s.label}</span>
            </button>
          ))}
        </div>
      </div>

      {/* ── Image dropzone ── */}
      <Card
        className={`border-dashed border-2 bg-card/50 p-8 transition-colors ${
          isDragging ? "border-primary bg-primary/5" : "border-border"
        } ${characterImage ? "border-primary/50 border-solid bg-card" : ""}`}
        onDragOver={handleDragOver}
        onDragLeave={handleDragLeave}
        onDrop={handleDrop}
        onClick={() => !characterImage && !isDisabled && fileInputRef.current?.click()}
      >
        <input
          type="file" accept="image/*" className="hidden"
          ref={fileInputRef} onChange={handleFileChange}
          data-testid="input-character-image"
        />

        {previewUrl ? (
          <div className="flex flex-col md:flex-row items-center gap-8">
            <div
              className="shrink-0 rounded overflow-hidden border border-primary/30 shadow-[0_0_20px_rgba(0,255,255,0.1)] cursor-default"
              style={{
                width:  videoFormat === "9:16" ? "80px"  : "160px",
                height: videoFormat === "9:16" ? "142px" : "90px",
              }}
            >
              <img src={previewUrl} alt="Character Preview" className="w-full h-full object-cover" />
            </div>
            <div className="flex-1 text-center md:text-left">
              <h3 className="text-lg font-bold mb-1 font-mono uppercase text-primary">Identity Registered</h3>
              <p className="text-xs text-muted-foreground font-mono mb-1">
                Format : <span className="text-primary font-bold">{videoFormat}</span>
                {videoFormat === "9:16" ? " — 720×1280" : " — 1280×720"}
              </p>
              <p className="text-xs text-muted-foreground font-mono mb-4">
                Style : <span className="text-primary font-bold">
                  {STYLES.find(s => s.id === imageStyle)?.emoji}{" "}
                  {STYLES.find(s => s.id === imageStyle)?.label}
                </span>
              </p>
              <div className="flex flex-col sm:flex-row gap-3">
                <Button
                  onClick={handleStart}
                  disabled={isStarting}
                  className="flex-1 font-mono uppercase tracking-widest font-bold shadow-[0_0_20px_rgba(0,255,255,0.3)]"
                  size="lg"
                  data-testid="button-produce"
                >
                  {isStarting ? "Initialisation…" : <><Play className="w-4 h-4 mr-2" /> Produire la vidéo</>}
                </Button>
                <Button
                  variant="outline"
                  onClick={(e) => { e.stopPropagation(); onImageChange(null); setPreviewUrl(null); }}
                  disabled={isStarting}
                >
                  Reset
                </Button>
              </div>
            </div>
          </div>
        ) : (
          <div className="flex flex-col items-center justify-center text-center cursor-pointer">
            <div className="w-16 h-16 rounded-full bg-secondary flex items-center justify-center mb-4">
              <ImagePlus className="w-8 h-8 text-muted-foreground" />
            </div>
            <h3 className="font-bold mb-2">Upload Character Image</h3>
            <p className="text-sm text-muted-foreground max-w-sm mb-2">
              Glissez-déposez ou cliquez pour choisir. Le personnage apparaîtra dans tous les segments.
            </p>
            <p className="text-xs font-mono text-primary/60">
              Format : <span className="text-primary">{videoFormat}</span>
              {" · "}Style : <span className="text-primary">
                {STYLES.find(s => s.id === imageStyle)?.emoji} {STYLES.find(s => s.id === imageStyle)?.label}
              </span>
            </p>
          </div>
        )}
      </Card>
    </div>
  );
}
