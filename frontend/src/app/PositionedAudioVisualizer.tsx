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
}: {
  chatHistory: ChatMessage[];
  role: "user" | "assistant";
  analyserNode: AnalyserNode | null;
  isConnected: boolean;
  onCircleClick?: () => void;
}) => {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const isAssistant = role === "assistant";

  useAudioVisualizerCircle(canvasRef, {
    chatHistory,
    role,
    analyserNode,
    isConnected,
    showPlayButton: !!onCircleClick,
    clearCanvas: true,
  });

  // Resize the canvas to fit its parent element
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;

    const parent = canvas.parentElement;
    if (!parent) return;

    const size = Math.min(parent.clientWidth, parent.clientHeight);

    // If we don't do this `if` check, the recording ends up with flickering
    if (canvas.width !== size || canvas.height !== size) {
      canvas.width = size;
      canvas.height = size;
    }
  });

  return (
    <div className="flex items-center justify-center">
      <div
        className={clsx(
          isAssistant
            ? "w-44 md:w-56 2xl:w-64"
            : "w-[calc(11rem+40px)] md:w-[calc(14rem+40px)] 2xl:w-[calc(16rem+40px)]"
        )}
      >
        <canvas
          ref={canvasRef}
          className={`w-full h-full rounded-full ${
            onCircleClick ? "cursor-pointer" : ""
          }`}
          onClick={onCircleClick}
        />
      </div>
    </div>
  );
};

export default PositionedAudioVisualizer;
