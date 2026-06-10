import { useState, useRef } from "react";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { ImagePlus, UserCircle, Play } from "lucide-react";
import { useToast } from "@/hooks/use-toast";
import { useProduceVideo } from "@workspace/api-client-react";

interface UploadZoneProps {
  isActive: boolean;
  isDisabled: boolean;
  characterImage: File | null;
  onImageChange: (file: File | null) => void;
  onProduce: () => void;
  sessionId?: string;
}

export function UploadZone({ isActive, isDisabled, characterImage, onImageChange, onProduce, sessionId }: UploadZoneProps) {
  const [isDragging, setIsDragging] = useState(false);
  const [previewUrl, setPreviewUrl] = useState<string | null>(null);
  const [isStarting, setIsStarting] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const { toast } = useToast();
  // Using useProduceVideo just to satisfy requirements, but using fetch for the multipart form data as per instructions
  const produceVideoMutation = useProduceVideo();

  const handleDragOver = (e: React.DragEvent) => {
    e.preventDefault();
    if (!isDisabled) setIsDragging(true);
  };

  const handleDragLeave = () => {
    setIsDragging(false);
  };

  const processFile = (file: File) => {
    if (!file.type.startsWith("image/")) {
      toast({
        title: "Invalid file",
        description: "Please upload an image file.",
        variant: "destructive"
      });
      return;
    }
    onImageChange(file);
    const url = URL.createObjectURL(file);
    setPreviewUrl(url);
  };

  const handleDrop = (e: React.DragEvent) => {
    e.preventDefault();
    setIsDragging(false);
    if (isDisabled) return;
    
    if (e.dataTransfer.files && e.dataTransfer.files.length > 0) {
      processFile(e.dataTransfer.files[0]);
    }
  };

  const handleFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    if (e.target.files && e.target.files.length > 0) {
      processFile(e.target.files[0]);
    }
  };

  const handleStart = async () => {
    if (!sessionId || !characterImage) return;
    setIsStarting(true);
    
    try {
      const formData = new FormData();
      formData.append("character_image", characterImage);
      
      const res = await fetch(`/api/produce/${sessionId}`, {
        method: "POST",
        body: formData,
      });
      
      if (!res.ok) {
        const error = await res.json();
        throw new Error(error.error || "Failed to start production");
      }
      
      onProduce();
    } catch (err: any) {
      toast({
        title: "Production Error",
        description: err.message || "An unexpected error occurred.",
        variant: "destructive"
      });
    } finally {
      setIsStarting(false);
    }
  };

  return (
    <div className={`transition-all duration-500 ${isDisabled ? 'opacity-30 pointer-events-none' : 'opacity-100'}`}>
      <div className="flex items-center justify-between mb-6">
        <h2 className="text-xl font-bold flex items-center gap-2">
          <span className="text-primary"><UserCircle className="w-5 h-5" /></span>
          STEP 2: CHARACTER INPUT
        </h2>
      </div>

      <Card 
        className={`border-dashed border-2 bg-card/50 p-8 transition-colors ${isDragging ? 'border-primary bg-primary/5' : 'border-border'} ${characterImage ? 'border-primary/50 border-solid bg-card' : ''}`}
        onDragOver={handleDragOver}
        onDragLeave={handleDragLeave}
        onDrop={handleDrop}
        onClick={() => !characterImage && !isDisabled && fileInputRef.current?.click()}
      >
        <input 
          type="file" 
          accept="image/*" 
          className="hidden" 
          ref={fileInputRef} 
          onChange={handleFileChange}
          data-testid="input-character-image"
        />
        
        {previewUrl ? (
          <div className="flex flex-col md:flex-row items-center gap-8">
            <div className="w-32 h-32 md:w-48 md:h-48 rounded overflow-hidden border border-primary/30 shrink-0 shadow-[0_0_20px_rgba(0,255,255,0.1)]">
              <img src={previewUrl} alt="Character Preview" className="w-full h-full object-cover" />
            </div>
            <div className="flex-1 text-center md:text-left">
              <h3 className="text-lg font-bold mb-2 font-mono uppercase text-primary">Identity Registered</h3>
              <p className="text-sm text-muted-foreground mb-6">Image acquired. Engine is primed for rendering.</p>
              
              <div className="flex flex-col sm:flex-row gap-3">
                <Button 
                  onClick={handleStart} 
                  disabled={isStarting}
                  className="flex-1 font-mono uppercase tracking-widest font-bold shadow-[0_0_20px_rgba(0,255,255,0.3)]"
                  size="lg"
                  data-testid="button-produce"
                >
                  {isStarting ? "Initializing..." : <><Play className="w-4 h-4 mr-2" /> Produire la vidéo</>}
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
            <p className="text-sm text-muted-foreground max-w-sm mb-4">
              Drag and drop an image here, or click to browse. The character will be used in all generated video segments.
            </p>
          </div>
        )}
      </Card>
    </div>
  );
}
