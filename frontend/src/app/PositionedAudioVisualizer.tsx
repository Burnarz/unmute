import clsx from "clsx";
import { ChatMessage } from "./chatHistory";
import { useAudioVisualizerCircle } from "./useAudioVisualizerCircle";
import { useEffect, useRef } from "react";

const PositionedAudioVisualizer = ({
  chatHistory,
  role,
  analyserNode,
  isConnected,
  onCircleClick,
  className,
  gapFromGreen,
}: {
  chatHistory: ChatMessage[];
  role: "user" | "assistant";
  analyserNode: AnalyserNode | null;
  isConnected: boolean;
  onCircleClick?: () => void;
  className?: string;
  gapFromGreen?: number;
}) => {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const containerRef = useRef<HTMLDivElement | null>(null);

  useAudioVisualizerCircle(canvasRef, {
    chatHistory,
    role,
    analyserNode,
    isConnected,
    showPlayButton: !!onCircleClick,
    clearCanvas: true,
    gapFromGreen,
  });

  // Resize canvas to fit container using ResizeObserver
  useEffect(() => {
    const container = containerRef.current;
    const canvas = canvasRef.current;
    if (!container || !canvas) return;

    const updateSize = () => {
      const size = Math.min(container.clientWidth, container.clientHeight);
      if (size > 0 && (canvas.width !== size || canvas.height !== size)) {
        canvas.width = size;
        canvas.height = size;
      }
    };

    updateSize();
    const observer = new ResizeObserver(updateSize);
    observer.observe(container);
    return () => observer.disconnect();
  }, []);

  return (
    <div
      ref={containerRef}
      className={clsx("w-full h-full", className)}
      onClick={onCircleClick}
      style={{ cursor: onCircleClick ? "pointer" : undefined }}
    >
      <canvas ref={canvasRef} className="w-full h-full rounded-full" />
    </div>
  );
};

export default PositionedAudioVisualizer;