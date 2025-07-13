"use client";
import useWebSocket, { ReadyState } from "react-use-websocket";
import { useCallback, useEffect, useState } from "react";
import { useMicrophoneAccess } from "./useMicrophoneAccess";
import { base64DecodeOpus, base64EncodeOpus } from "./audioUtil";
import SlantedButton from "@/app/SlantedButton";
import { useAudioProcessor as useAudioProcessor } from "./useAudioProcessor";
import useKeyboardShortcuts from "./useKeyboardShortcuts";
import { prettyPrintJson } from "pretty-print-json";
import PositionedAudioVisualizer from "./PositionedAudioVisualizer";
import UnmuteConfigurator, {
  DEFAULT_UNMUTE_CONFIG,
  UnmuteConfig,
} from "./UnmuteConfigurator";
import CouldNotConnect, { HealthStatus } from "./CouldNotConnect";
import UnmuteHeader from "./UnmuteHeader";
import Subtitles from "./Subtitles";
import {
  ChatMessage,
  compressChatHistory,
  loadChatHistory,
  saveChatHistory,
  saveMemory,
  getMemoryList,
  loadMemory,
  clearChatHistory,
} from "./chatHistory";
import useWakeLock from "./useWakeLock";
import ErrorMessages, { ErrorItem, makeErrorItem } from "./ErrorMessages";
import { useRecordingCanvas } from "./useRecordingCanvas";
import { useGoogleAnalytics } from "./useGoogleAnalytics";
import clsx from "clsx";
import { useBackendServerUrl } from "./useBackendServerUrl";
import { RECORDING_CONSENT_STORAGE_KEY } from "./ConsentModal";
import Modal from "./Modal";

const Unmute = () => {
  const { isDevMode, showSubtitles } = useKeyboardShortcuts();
  const [debugDict, setDebugDict] = useState<object | null>(null);
  const [unmuteConfig, setUnmuteConfig] = useState<UnmuteConfig>(
    DEFAULT_UNMUTE_CONFIG,
  );
  const [rawChatHistory, setRawChatHistory] = useState<ChatMessage[]>([]);
  const chatHistory = compressChatHistory(rawChatHistory);

  const { microphoneAccess, askMicrophoneAccess } = useMicrophoneAccess();

  const [shouldConnect, setShouldConnect] = useState(false);
  const backendServerUrl = useBackendServerUrl();
  const [webSocketUrl, setWebSocketUrl] = useState<string | null>(null);
  const [healthStatus, setHealthStatus] = useState<HealthStatus | null>(null);
  const [errors, setErrors] = useState<ErrorItem[]>([]);
  const [savedMemories, setSavedMemories] = useState<string[]>([]);
  const [closeModalSignal, setCloseModalSignal] = useState(0);

  useWakeLock(shouldConnect);
  const { analyticsOnDownloadRecording } = useGoogleAnalytics({
    shouldConnect,
    unmuteConfig,
  });

  useEffect(() => {
    // Load Eruda script
    const script = document.createElement("script");
    script.src = "https://cdn.jsdelivr.net/npm/eruda";
    script.async = true;

    script.onload = () => {
      // Initialize Eruda after script loads
      if (window.eruda) {
        window.eruda.init();
        // window.eruda.show();
      }
    };

    document.head.appendChild(script);

    // Cleanup function
    return () => {
      document.head.removeChild(script);
      // Optionally destroy Eruda
      if (window.eruda) {
        window.eruda.destroy();
      }
    };
  }, []);

  // Load chat history from local storage when the component mounts
  useEffect(() => {
    setRawChatHistory(loadChatHistory(unmuteConfig.voiceName));
    setSavedMemories(getMemoryList(unmuteConfig.voiceName));
  }, [unmuteConfig.voiceName]);

  // Save chat history to local storage when it changes
  useEffect(() => {
    saveChatHistory(rawChatHistory, unmuteConfig.voiceName);
  }, [rawChatHistory, unmuteConfig.voiceName]);

  // Check if the backend server is healthy. If we setHealthStatus to null,
  // a "server is down" screen will be shown.
  useEffect(() => {
    if (!backendServerUrl) return;

    setWebSocketUrl(backendServerUrl.toString() + "/v1/realtime");

    const checkHealth = async () => {
      try {
        const controller = new AbortController();
        const timeoutId = setTimeout(() => controller.abort(), 3000);

        const response = await fetch(`${backendServerUrl}/v1/health`, {
          signal: controller.signal,
        });

        clearTimeout(timeoutId);
        if (!response.ok) {
          setHealthStatus({
            connected: "yes_request_fail",
            ok: false,
          });
        }
        const data = await response.json();
        data["connected"] = "yes_request_ok";

        if (data.ok && !data.voice_cloning_up) {
          console.debug("Voice cloning not available, hiding upload button.");
        }
        setHealthStatus(data);
      } catch {
        setHealthStatus({
          connected: "no",
          ok: false,
        });
      }
    };

    checkHealth();
  }, [backendServerUrl]);

  const { sendMessage, lastMessage, readyState } = useWebSocket(
    webSocketUrl || null,
    {
      protocols: ["realtime"],
    },
    shouldConnect,
  );

  // Send microphone audio to the server (via useAudioProcessor below)
  const onOpusRecorded = useCallback(
    (opus: Uint8Array) => {
      sendMessage(
        JSON.stringify({
          type: "input_audio_buffer.append",
          audio: base64EncodeOpus(opus),
        }),
      );
    },
    [sendMessage],
  );

  const { setupAudio, shutdownAudio, audioProcessor } =
    useAudioProcessor(onOpusRecorded);
  const {
    canvasRef: recordingCanvasRef,
    downloadRecording,
    recordingAvailable,
  } = useRecordingCanvas({
    size: 1080,
    shouldRecord: shouldConnect,
    audioProcessor: audioProcessor.current,
    chatHistory: rawChatHistory,
  });

  const onConnectButtonPress = async () => {
    // If we're not connected yet
    if (!shouldConnect) {
      const mediaStream = await askMicrophoneAccess();
      // If we have access to the microphone:
      if (mediaStream) {
        await setupAudio(mediaStream);
        setShouldConnect(true);
      }
    } else {
      setShouldConnect(false);
      shutdownAudio();
    }
  };

  const onDownloadRecordingButtonPress = () => {
    try {
      downloadRecording(false);
      analyticsOnDownloadRecording();
    } catch (e) {
      if (e instanceof Error) {
        setErrors((prev) => [...prev, makeErrorItem(e.message)]);
      }
    }
  };

  // If the websocket connection is closed, shut down the audio processing
  useEffect(() => {
    if (readyState === ReadyState.CLOSING || readyState === ReadyState.CLOSED) {
      setShouldConnect(false);
      shutdownAudio();
    }
  }, [readyState, shutdownAudio]);

  // Handle incoming messages from the server
  useEffect(() => {
    if (lastMessage === null) return;

    const data = JSON.parse(lastMessage.data);
    if (data.type === "response.audio.delta") {
      const opus = base64DecodeOpus(data.delta);
      const ap = audioProcessor.current;
      if (!ap) return;

      ap.decoder.postMessage(
        {
          command: "decode",
          pages: opus,
        },
        [opus.buffer],
      );
    } else if (data.type === "unmute.additional_outputs") {
      setDebugDict(data.args.debug_dict);
    } else if (data.type === "error") {
      if (data.error.type === "warning") {
        console.warn(`Warning from server: ${data.error.message}`, data);
        // Warnings aren't explicitly shown in the UI
      } else {
        console.error(`Error from server: ${data.error.message}`, data);
        setErrors((prev) => [...prev, makeErrorItem(data.error.message)]);
      }
    } else if (
      data.type === "conversation.item.input_audio_transcription.delta"
    ) {
      // Transcription of the user speech
      setRawChatHistory((prev) => [
        ...prev,
        { role: "user", content: " " + data.delta },
      ]);
    } else if (data.type === "response.text.delta") {
      // Text-to-speech output
      setRawChatHistory((prev) => [
        ...prev,
        // The TTS doesn't include spaces in its messages, so add a leading space.
        // This way we'll get a leading space at the very beginning of the message,
        // but whatever.
        { role: "assistant", content: " " + data.delta },
      ]);
    } else {
      const ignoredTypes = [
        "session.updated",
        "response.created",
        "response.text.delta",
        "response.text.done",
        "response.audio.done",
        "conversation.item.input_audio_transcription.delta",
        "input_audio_buffer.speech_stopped",
        "input_audio_buffer.speech_started",
        "unmute.interrupted_by_vad",
        "unmute.response.text.delta.ready",
        "unmute.response.audio.delta.ready",
      ];
      if (!ignoredTypes.includes(data.type)) {
        console.warn("Received unknown message:", data);
      }
    }
  }, [audioProcessor, lastMessage]);

  // When we connect, we send the initial config (voice and instructions) to the server.
  // Also clear the chat history.
  useEffect(() => {
    if (readyState !== ReadyState.OPEN) return;

    const recordingConsent =
      localStorage.getItem(RECORDING_CONSENT_STORAGE_KEY) === "true";

    // setRawChatHistory([]);
    sendMessage(
      JSON.stringify({
        type: "session.update",
        session: {
          instructions: unmuteConfig.instructions,
          voice: unmuteConfig.voice,
          allow_recording: recordingConsent,
        },
      }),
    );
    if (rawChatHistory.length > 0) {
      const compressedHistory = compressChatHistory(rawChatHistory, "");
      for (const message of compressedHistory) {
        sendMessage(
          JSON.stringify({
            type: "conversation.item.create",
            item: {
              type: "message",
              role: message.role,
              content: [
                {
                  type: "input_text",
                  text: message.content.trimStart(),
                },
              ],
            },
          })
        );
      }
    }
  }, [unmuteConfig, readyState, sendMessage]);

  // Disconnect when the voice or instruction changes.
  // TODO: If it's a voice change, immediately reconnect with the new voice.
  useEffect(() => {
    setShouldConnect(false);
    shutdownAudio();
  }, [shutdownAudio, unmuteConfig.voice, unmuteConfig.instructions]);

  const handleSaveMemory = () => {
    saveMemory(unmuteConfig.voiceName);
    setSavedMemories(getMemoryList(unmuteConfig.voiceName));
    alert("Memory saved to local storage!");
  };

  const handleSelectMemory = (memory: string) => {
    loadMemory(unmuteConfig.voiceName, memory);
    setRawChatHistory(loadChatHistory(unmuteConfig.voiceName));
    setCloseModalSignal((s) => s + 1);
  };

  const handleClearMemory = () => {
    if (confirm("Are you sure you want to clear the memory?")) {
      clearChatHistory(unmuteConfig.voiceName);
      setRawChatHistory([]);
    }
  };

  const handleUploadMemories = async () => {
    if (
      !confirm(
        "This will upload all your local memories to the server. Are you sure?"
      )
    ) {
      return;
    }

    const memoryKeys = getMemoryList(unmuteConfig.voiceName);
    if (memoryKeys.length === 0) {
      alert("No local memories to upload.");
      return;
    }

    let successCount = 0;
    let errorCount = 0;

    for (const timestamp of memoryKeys) {
      const memoryKey = `chatHistory_${unmuteConfig.voiceName}_memory_${timestamp}`;
      const memoryHistory = localStorage.getItem(memoryKey);
      if (memoryHistory) {
        try {
          const filename = `${timestamp}.json`;
          const file = new File([memoryHistory], filename, {
            type: "application/json",
          });
          const formData = new FormData();
          formData.append("file", file);

          const response = await fetch(
            `${backendServerUrl}/v1/memories/${unmuteConfig.voiceName}`,
            {
              method: "POST",
              body: formData,
            }
          );

          if (response.ok) {
            successCount++;
          } else {
            console.error(
              `Failed to upload memory ${timestamp}`,
              await response.text()
            );
            errorCount++;
          }
        } catch (error) {
          console.error(`Failed to upload memory ${timestamp}`, error);
          errorCount++;
        }
      }
    }

    alert(
      `Upload complete. ${successCount} memories uploaded successfully, ${errorCount} failed.`
    );
  };

  const handleDownloadMemories = async () => {
    if (
      !confirm(
        "This will download all memories from the server and may overwrite existing local memories. Are you sure?"
      )
    ) {
      return;
    }

    try {
      const listResponse = await fetch(
        `${backendServerUrl}/v1/memories/${unmuteConfig.voiceName}`
      );
      if (!listResponse.ok) {
        alert("Failed to list memories from the server.");
        return;
      }
      const memoryFilenames = await listResponse.json();

      for (const filename of memoryFilenames) {
        if (!filename.endsWith(".json")) {
          continue;
        }
        try {
          const memoryResponse = await fetch(
            `${backendServerUrl}/v1/memories/${unmuteConfig.voiceName}/${filename}`
          );
          if (memoryResponse.ok) {
            const memoryContent = await memoryResponse.json();
            const timestamp = filename.slice(0, -5); // Remove .json
            const memoryKey = `chatHistory_${unmuteConfig.voiceName}_memory_${timestamp}`;
            localStorage.setItem(memoryKey, JSON.stringify(memoryContent));
          } else {
            console.warn(`Failed to download memory: ${filename}`);
          }
        } catch (e) {
          console.warn(`Error processing memory ${filename}:`, e);
        }
      }

      setSavedMemories(getMemoryList(unmuteConfig.voiceName));
      alert("Memories downloaded successfully!");
    } catch (error) {
      console.error("Failed to download memories", error);
      alert("Failed to download memories.");
    }
  };

  useEffect(() => {
    setSavedMemories(getMemoryList(unmuteConfig.voiceName));
  }, [unmuteConfig.voiceName]);

  const formatTimestamp = (timestamp: string) => {
    const date = new Date(timestamp);
    const day = String(date.getDate()).padStart(2, "0");
    const month = String(date.getMonth() + 1).padStart(2, "0"); // Month is 0-indexed
    const year = date.getFullYear();
    const hours = String(date.getHours()).padStart(2, "0");
    const minutes = String(date.getMinutes()).padStart(2, "0");
    const seconds = String(date.getSeconds()).padStart(2, "0");
    return `${day}/${month}/${year} ${hours}:${minutes}:${seconds}`;
  };

  if (!healthStatus || !backendServerUrl) {
    return (
      <div className="flex flex-col gap-4 items-center">
        <h1 className="text-xl mb-4">Loading...</h1>
      </div>
    );
  }

  if (healthStatus && !healthStatus.ok) {
    return <CouldNotConnect healthStatus={healthStatus} />;
  }

  return (
    <div className="w-full">
      <ErrorMessages errors={errors} setErrors={setErrors} />
      {/* The main full-height demo */}
      <div className="relative flex w-full min-h-screen flex-col text-white bg-background items-center">
        {/* z-index on the header to put it in front of the circles */}
        <header className="static md:absolute max-w-6xl px-3 md:px-8 right-0 flex justify-end z-10">
          <UnmuteHeader />
        </header>
        <div
          className={clsx(
            "w-full h-auto min-h-75",
            "flex flex-row-reverse md:flex-row items-center justify-center grow",
            "-mt-10 md:mt-0 mb-10 md:mb-0 md:-mr-4",
          )}
        >
          <PositionedAudioVisualizer
            chatHistory={chatHistory}
            role={"assistant"}
            analyserNode={audioProcessor.current?.outputAnalyser || null}
            onCircleClick={onConnectButtonPress}
            isConnected={shouldConnect}
          />
          <PositionedAudioVisualizer
            chatHistory={chatHistory}
            role={"user"}
            analyserNode={audioProcessor.current?.inputAnalyser || null}
            isConnected={shouldConnect}
          />
        </div>
        {showSubtitles && <Subtitles chatHistory={chatHistory} />}
        <UnmuteConfigurator
          backendServerUrl={backendServerUrl}
          config={unmuteConfig}
          setConfig={setUnmuteConfig}
          voiceCloningUp={healthStatus.voice_cloning_up || false}
        />
        <div className="w-full flex flex-col items-center justify-center px-3 gap-3 my-6">
          <div className="w-full flex flex-col md:flex-row items-center justify-center gap-3">
            <SlantedButton
              onClick={onDownloadRecordingButtonPress}
              kind={recordingAvailable ? "secondary" : "disabled"}
              extraClasses="w-full max-w-96"
            >
              {"download recording"}
            </SlantedButton>
            <SlantedButton
              onClick={onConnectButtonPress}
              kind={shouldConnect ? "secondary" : "primary"}
              extraClasses="w-full max-w-96"
            >
              {shouldConnect ? "disconnect" : "connect"}
            </SlantedButton>
          </div>
          <div className="w-full flex flex-col md:flex-row items-center justify-center gap-3">
            <SlantedButton
              onClick={handleSaveMemory}
              kind="secondary"
              extraClasses="w-full max-w-96"
            >
              {"save"}
            </SlantedButton>
            <Modal
              className="w-full max-w-96"
              trigger={
                <SlantedButton kind="secondary" extraClasses="w-full">
                  {"load"}
                </SlantedButton>
              }
              forceFullscreen={true}
              closeSignal={closeModalSignal}
            >
              <div className="p-4">
                <h2 className="text-lg font-bold mb-4">Load Memory</h2>
                {savedMemories.length === 0 ? (
                  <p>No saved memories found.</p>
                ) : (
                  <ul>
                    {savedMemories.map((memory) => (
                      <li key={memory} className="mb-2">
                        <button
                          onClick={() => handleSelectMemory(memory)}
                          className="w-full text-left p-2 bg-gray-700 hover:bg-gray-600 rounded"
                        >
                          {formatTimestamp(memory)}
                        </button>
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            </Modal>
            <SlantedButton
              onClick={handleClearMemory}
              kind="secondary"
              extraClasses="w-full max-w-96"
            >
              {"clear"}
            </SlantedButton>
            <SlantedButton
              onClick={handleUploadMemories}
              kind="secondary"
              extraClasses="w-full max-w-96"
            >
              {"upload"}
            </SlantedButton>
            <SlantedButton
              onClick={handleDownloadMemories}
              kind="secondary"
              extraClasses="w-full max-w-96"
            >
              {"download"}
            </SlantedButton>
          </div>
          {/* Maybe we don't need to explicitly show the status */}
          {/* {renderConnectionStatus(readyState, false)} */}
          {microphoneAccess === "refused" && (
            <div className="text-red">
              {"You'll need to allow microphone access to use the demo. " +
                "Please check your browser settings."}
            </div>
          )}
        </div>
      </div>
      {/* Debug stuff, not counted into the screen height */}
      {isDevMode && (
        <div>
          <div className="text-xs w-full overflow-auto">
            <pre
              className="whitespace-pre-wrap break-words"
              dangerouslySetInnerHTML={{
                __html: prettyPrintJson.toHtml(debugDict),
              }}
            ></pre>
          </div>
          <div>Subtitles: press S. Dev mode: press D.</div>
        </div>
      )}
      <canvas ref={recordingCanvasRef} className="hidden" />
    </div>
  );
};

export default Unmute;