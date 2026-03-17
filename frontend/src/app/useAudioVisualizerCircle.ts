import { useRef, useEffect, useState } from "react";
import { getCSSVariable } from "./cssUtil";
import { ChatMessage } from "./chatHistory";

import { VisualizerStyle } from "./UnmuteConfigurator";

const WIDTH_INACTIVE = 3;
const WIDTH_ACTIVE = 5;

// Size scale factors for connected/disconnected states
const SCALE_CONNECTED = 1;
const SCALE_DISCONNECTED = 0.8;
const ANIMATION_DURATION = 500; // milliseconds

const INTERRUPTION_CHAR = "—"; // em-dash

const sampleToNormalizedRadius = (x: number) => {
  return 0.8 + 0.2 * Math.tanh(x * 2);
};

interface Positioning {
  centerX: number;
  centerY: number;
  radius: number;
}

const drawCircleVisualization = (
  canvas: HTMLCanvasElement,
  canvasCtx: CanvasRenderingContext2D,
  data: Float32Array,
  colorName: string,
  lineWidth: number,
  animationProgress: number,
  positioning: Positioning,
  pulseScale: number = 1.0,
  isUser: boolean = false,
  style: VisualizerStyle = "classic"
) => {
  // Calculate scale factor from animation progress (0 = disconnected, 1 = connected)
  const scaleFactor =
    (SCALE_DISCONNECTED +
    (SCALE_CONNECTED - SCALE_DISCONNECTED) * animationProgress) * pulseScale;

  const color = getCSSVariable(colorName);
  canvasCtx.strokeStyle = color;

  if (isUser) {
    // Perfectly round circle for user, only pulsing in diameter
    const radius = positioning.radius * scaleFactor * 0.8; 
    canvasCtx.beginPath();
    canvasCtx.arc(positioning.centerX, positioning.centerY, radius, 0, Math.PI * 2);
    canvasCtx.lineWidth = lineWidth;
    canvasCtx.stroke();
  } else {
    // JARVIS STYLE for Assistant
    const time = performance.now() / 1000;
    const baseRadius = positioning.radius * scaleFactor;

    // 1. Rotating HUD Ring (Dashed) - Kept for all styles
    canvasCtx.save();
    canvasCtx.beginPath();
    canvasCtx.setLineDash([2, 10]);
    canvasCtx.lineWidth = 1;
    canvasCtx.globalAlpha = 0.3 * animationProgress;
    canvasCtx.arc(positioning.centerX, positioning.centerY, baseRadius * 0.75, time * 0.5, time * 0.5 + Math.PI * 2);
    canvasCtx.stroke();
    canvasCtx.restore();

    // 2. Main Visualization Style
    if (style === "classic") {
      // Original Double Energy Waves
      for (const offset of [0, 1]) {
        canvasCtx.beginPath();
        canvasCtx.lineWidth = offset === 0 ? lineWidth : lineWidth / 2;
        canvasCtx.globalAlpha = offset === 0 ? 1.0 : 0.5;
        if (offset === 0) { canvasCtx.shadowBlur = 15; canvasCtx.shadowColor = color; }
        else { canvasCtx.shadowBlur = 0; }

        for (let i = 0; i < data.length; i++) {
          const multiplier = offset === 0 ? 1.0 : 0.98;
          const radius = baseRadius * sampleToNormalizedRadius(data[i]) * multiplier;
          const angle = (i / data.length) * Math.PI * 2;
          const x = positioning.centerX + radius * Math.cos(angle);
          const y = positioning.centerY + radius * Math.sin(angle);
          if (i === 0) canvasCtx.moveTo(x, y);
          else canvasCtx.lineTo(x, y);
        }
        canvasCtx.closePath();
        canvasCtx.stroke();
      }
    } else {
      // POLAR STYLE (Default and only other option)
      canvasCtx.shadowBlur = 10;
      canvasCtx.shadowColor = color;
      const barCount = 64;
      const step = Math.floor(data.length / barCount);
      for (let i = 0; i < barCount; i++) {
        const audioValue = Math.abs(data[i * step]);
        
        // IDLE ANIMATION: Add a base movement even if silence
        // A combination of sines at different frequencies for an "organic" feel
        const idleNoise = (
          Math.sin(time * 2 + i * 0.2) * 0.05 + 
          Math.sin(time * 5 - i * 0.1) * 0.02
        ) * animationProgress; // Only if connected
        
        const finalValue = Math.max(0.02 * animationProgress, audioValue + idleNoise);
        const barHeight = finalValue * baseRadius * 0.4;
        const angle = (i / barCount) * Math.PI * 2 + time * 0.1;
        
        const innerRadius = baseRadius * 0.85;
        const x1 = positioning.centerX + innerRadius * Math.cos(angle);
        const y1 = positioning.centerY + innerRadius * Math.sin(angle);
        const x2 = positioning.centerX + (innerRadius + barHeight) * Math.cos(angle);
        const y2 = positioning.centerY + (innerRadius + barHeight) * Math.sin(angle);
        
        canvasCtx.beginPath();
        canvasCtx.lineWidth = 2;
        canvasCtx.moveTo(x1, y1);
        canvasCtx.lineTo(x2, y2);
        canvasCtx.stroke();
      }
    }
    
    // Clean up
    canvasCtx.shadowBlur = 0;
    canvasCtx.globalAlpha = 1.0;
  }
};

// New function to draw a play triangle
const drawPlayButton = (
  canvas: HTMLCanvasElement,
  canvasCtx: CanvasRenderingContext2D,
  colorName: string,
  animationProgress: number,
  positioning: Positioning
) => {
  // Calculate opacity based on animation progress (0 = fully visible, 1 = invisible)
  const opacity = 1 - animationProgress;

  if (opacity <= 0) return; // Don't draw if fully transparent

  const centerX = positioning.centerX;
  const centerY = positioning.centerY;
  const size = positioning.radius * 0.2; // Play button size relative to circle size

  // Create triangle path
  canvasCtx.beginPath();
  canvasCtx.moveTo(centerX + size / 2, centerY);
  canvasCtx.lineTo(centerX - size / 4, centerY - size / 2);
  canvasCtx.lineTo(centerX - size / 4, centerY + size / 2);
  canvasCtx.closePath();

  // Fill with color and opacity
  const color = getCSSVariable(colorName);
  // Parse the CSS variable color to get RGB values
  const tempCanvas = document.createElement("canvas");
  const tempCtx = tempCanvas.getContext("2d");
  if (!tempCtx) return;

  tempCtx.fillStyle = color;
  tempCtx.fillRect(0, 0, 1, 1);
  const rgba = tempCtx.getImageData(0, 0, 1, 1).data;

  // Apply opacity to the color
  canvasCtx.fillStyle = `rgba(${rgba[0]}, ${rgba[1]}, ${rgba[2]}, ${opacity})`;
  canvasCtx.fill();
};

const getAnalyzerData = (
  analyserNode: AnalyserNode | null
): [Float32Array, Float32Array] => {
  const fftSize = 2048;
  const frequencyData = new Float32Array(fftSize / 2);
  const timeDomainData = new Float32Array(fftSize / 8);

  if (!analyserNode) {
    // return arrays corresponding to silence
    frequencyData.fill(-100); // -100 dBFS
    timeDomainData.fill(0); // silence

    return [frequencyData, timeDomainData];
  } else {
    // Configure analyzer node
    analyserNode.fftSize = fftSize;
    analyserNode.smoothingTimeConstant = 0.85;

    analyserNode.getFloatTimeDomainData(timeDomainData);
    analyserNode.getFloatFrequencyData(frequencyData);

    return [frequencyData, timeDomainData];
  }
};

const getIsActive = (
  chatHistory: ChatMessage[],
  role: "user" | "assistant"
) => {
  // Find the latest non-empty message from the specified role
  for (let i = chatHistory.length - 1; i >= 0; i--) {
    const message = chatHistory[i];

    // Empty messages, or ones where the LLM started generating but was interrupted
    // before it said anything
    if (message.content === "" || message.content === INTERRUPTION_CHAR)
      continue;

    if (message.content === "...") {
      // The user is silent, no more speech is coming
      return false;
    }

    if (message.role === role) {
      return true;
    } else {
      return false;
    }
  }
  // No non-empty messages found
  return false;
};

export interface UseAudioVisualizerCircleOptions {
  chatHistory: ChatMessage[];
  role: "user" | "assistant";
  analyserNode: AnalyserNode | null;
  isConnected?: boolean;
  showPlayButton?: boolean;
  positioning?: Positioning;
  clearCanvas: boolean;
  visualizerStyle?: VisualizerStyle;
}

export const useAudioVisualizerCircle = (
  canvasRef: React.RefObject<HTMLCanvasElement | null>,
  options: UseAudioVisualizerCircleOptions
) => {
  const {
    chatHistory,
    role,
    analyserNode,
    isConnected = false,
    showPlayButton = false,
    positioning,
    clearCanvas,
    visualizerStyle = "classic",
  } = options;

  const isActive = getIsActive(chatHistory, role);
  const isAssistant = role === "assistant";
  const colorName = isAssistant ? "color-green" : "color-white";

  const animationRef = useRef<number>(-1);
  const cicleBuffer = useRef<Float32Array>(new Float32Array(256));
  const circleIndex = useRef(0);
  const animationFrameRef = useRef<number | null>(null);
  const animationStartTime = useRef<number | null>(null);
  const animationPreviousProgress = useRef<number>(isConnected ? 1 : 0);

  const interruptionTimeRef = useRef(0);
  const [interruptionIndex, setInterruptionIndex] = useState(0);

  // Single state for animation progress: 0 = disconnected state, 1 = connected state
  const [animationProgress, setAnimationProgress] = useState<number>(
    isConnected ? 1 : 0
  );

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const stream = canvas.captureStream(30);
    stream.getTracks().forEach((track) => {
      track.stop();
    });
  }, [canvasRef]);

  useEffect(() => {
    if (chatHistory.length > interruptionIndex) {
      if (
        role === "user" &&
        chatHistory[chatHistory.length - 1].role === "assistant" &&
        // An interruption
        chatHistory[chatHistory.length - 1].content.endsWith(
          INTERRUPTION_CHAR
        ) &&
        // but not *only* an interruption char. That would mean the LLM got interrupted
        // before it said anything, and we don't want to count that as an interruption
        chatHistory[chatHistory.length - 1].content !== INTERRUPTION_CHAR
      ) {
        interruptionTimeRef.current = Date.now();
        setInterruptionIndex(chatHistory.length);
      }
    }
  }, [chatHistory, interruptionIndex, role]);

  // Handle connection state changes
  useEffect(() => {
    const animate = (timestamp: number) => {
      if (!animationStartTime.current) {
        animationStartTime.current = timestamp;
        animationPreviousProgress.current = animationProgress;
      }

      const elapsed = timestamp - animationStartTime.current;
      const duration = ANIMATION_DURATION;
      const progress = Math.min(elapsed / duration, 1);

      const targetProgress = isConnected ? 1 : 0;

      // Ease in-out function for smoother animation
      const easeInOutCubic = (t: number): number => {
        return t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2;
      };

      const newProgress =
        animationPreviousProgress.current +
        easeInOutCubic(progress) *
          (targetProgress - animationPreviousProgress.current);

      setAnimationProgress(newProgress);

      if (progress < 1) {
        animationFrameRef.current = requestAnimationFrame(animate);
      } else {
        // Animation complete
        animationFrameRef.current = null;
        animationStartTime.current = null;
        setAnimationProgress(targetProgress); // Ensure we end exactly at target value
      }
    };

    // Cancel any existing animation
    if (animationFrameRef.current) {
      cancelAnimationFrame(animationFrameRef.current);
      animationFrameRef.current = null;
    }

    // Start the animation
    animationFrameRef.current = requestAnimationFrame(animate);

    // Cleanup on unmount
    return () => {
      if (animationFrameRef.current) {
        cancelAnimationFrame(animationFrameRef.current);
      }
    };
  }, [isConnected, animationProgress]);

  // Track current opacity for smooth transitions
  const currentAlphaRef = useRef(1.0);

  // Main drawing effect
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;

    const canvasCtx = canvas.getContext("2d");
    if (!canvasCtx) return;

    const draw = () => {
      // Calculate positioning - use provided positioning or default to center
      const currentPositioning: Positioning = positioning || {
        centerX: canvas.width / 2,
        centerY: canvas.height / 2,
        radius: Math.min(canvas.width / 2, canvas.height / 2),
      };

      const [frequencyData, timeDomainData] = getAnalyzerData(analyserNode);
      
      // Calculate instantaneous volume from time domain (RMS-like) for better pulsing
      let sumSquares = 0;
      for (let i = 0; i < timeDomainData.length; i++) {
        sumSquares += timeDomainData[i] * timeDomainData[i];
      }
      const rms = Math.sqrt(sumSquares / timeDomainData.length);
      // Normalize rms: typically 0 to 0.5 for loud speech
      const volInst = Math.min(1.0, rms * 4.0); 

      // Dynamic Opacity & Pulse Logic
      let targetAlpha = 1.0;
      let pulseScale = 1.0;
      let currentLineWidth = isActive ? WIDTH_ACTIVE : WIDTH_INACTIVE;

      if (role === "user") {
        // User circle is now constant 20% opacity
        targetAlpha = 0.2;
        
        // Use a thinner line for user
        currentLineWidth = 3;
        
        // Pulse diameter using the instantaneous volume
        // Subtle multiplier of 0.3
        pulseScale = 1.0 + (volInst * 0.3);
      }
      
      // Fast interpolation
      currentAlphaRef.current += (targetAlpha - currentAlphaRef.current) * 0.3;
      
      const volumeNormalized = volInst; // Use this for the buffer loop below
      
      for (let i = 0; i < 1 + volumeNormalized * 10; i++) {
        cicleBuffer.current[circleIndex.current] = timeDomainData[i];
        circleIndex.current =
          (circleIndex.current + 1) % cicleBuffer.current.length;
      }

      // Schedule the next animation frame
      animationRef.current = requestAnimationFrame(draw);

      if (clearCanvas) {
        canvasCtx.clearRect(
          currentPositioning.centerX - currentPositioning.radius,
          currentPositioning.centerY - currentPositioning.radius,
          currentPositioning.radius * 2,
          currentPositioning.radius * 2
        );
      }

      const secSinceInterruption =
        (Date.now() - interruptionTimeRef.current) / 1000;
      const widthScale =
        1 + 2 * Math.exp(-Math.pow(secSinceInterruption * 3, 2));

      // Apply dynamic opacity
      canvasCtx.globalAlpha = currentAlphaRef.current;

      drawCircleVisualization(
        canvas,
        canvasCtx,
        cicleBuffer.current,
        colorName,
        currentLineWidth * (role === "assistant" ? widthScale : 1),
        animationProgress,
        currentPositioning,
        pulseScale,
        !isAssistant,
        visualizerStyle
      );
      
      // Reset alpha for other drawings (like play button)
      canvasCtx.globalAlpha = 1.0;

      // Draw play button if we have onClick and not fully connected
      if (showPlayButton && animationProgress < 1) {
        drawPlayButton(
          canvas,
          canvasCtx,
          colorName,
          animationProgress,
          currentPositioning
        );
      }
    };

    // Start the animation
    draw();

    // Clean up the animation on unmount
    return () => {
      if (animationRef.current) {
        cancelAnimationFrame(animationRef.current);
      }
    };
  }, [
    analyserNode,
    colorName,
    isActive,
    animationProgress,
    showPlayButton,
    canvasRef,
    positioning,
    clearCanvas,
  ]);

  return {};
};
