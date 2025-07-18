export type ChatRole = "user" | "assistant" | "system" | "tool";

export type ToolCall = {
  id: string;
  type: "function";
  function: {
    name: string;
    arguments: string;
  };
};

export type ChatMessage = {
  role: ChatRole;
  content: string | null;
  tool_calls?: ToolCall[];
  tool_call_id?: string;
};

/** If there are multiple messages from the same role in a row, combine them into one message */
export const compressChatHistory = (
  chatHistory: ChatMessage[],
  separator: string = "\n"
): ChatMessage[] => {
  const compressed: ChatMessage[] = [];
  for (const message of chatHistory) {
    if (
      compressed.length > 0 &&
      compressed[compressed.length - 1].role === message.role &&
      message.content && // Only merge if there is content
      compressed[compressed.length - 1].content
    ) {
      compressed[compressed.length - 1].content += `${separator}${message.content}`;
    } else {
      if (typeof message.content === "string") {
        message.content = message.content.trimStart();
      }
      compressed.push({ ...message });
    }
  }
  return compressed;
};

const getStorageKey = (voiceName: string) => `chatHistory_${voiceName}`;

export const saveChatHistory = (
  chatHistory: ChatMessage[],
  voiceName: string
) => {
  localStorage.setItem(
    getStorageKey(voiceName),
    JSON.stringify(chatHistory)
  );
};

export const loadChatHistory = (voiceName: string): ChatMessage[] => {
  const savedHistory = localStorage.getItem(getStorageKey(voiceName));
  if (!savedHistory) {
    return [];
  }
  try {
    const parsed = JSON.parse(savedHistory);
    if (Array.isArray(parsed)) {
      return parsed;
    }
    return [];
  } catch (e) {
    console.error("Could not parse chat history", e);
    return [];
  }
};

export const saveMemory = (voiceName: string) => {
  const timestamp = new Date().toISOString();
  const memoryKey = `chatHistory_${voiceName}_memory_${timestamp}`;
  const currentHistory = localStorage.getItem(getStorageKey(voiceName));
  if (currentHistory) {
    localStorage.setItem(memoryKey, currentHistory);
  }
};

export const getMemoryList = (voiceName: string): string[] => {
  const memoryTimestamps: string[] = [];
  for (let i = 0; i < localStorage.length; i++) {
    const key = localStorage.key(i);
    if (key && key.startsWith(`chatHistory_${voiceName}_memory_`)) {
      memoryTimestamps.push(key.replace(`chatHistory_${voiceName}_memory_`, ""));
    }
  }
  return memoryTimestamps.sort().reverse();
};

export const loadMemory = (
  voiceName: string,
  memoryTimestamp: string
) => {
  const memoryKey = `chatHistory_${voiceName}_memory_${memoryTimestamp}`;
  const memoryHistory = localStorage.getItem(memoryKey);
  if (memoryHistory) {
    localStorage.setItem(getStorageKey(voiceName), memoryHistory);
  }
};

export const clearChatHistory = (voiceName: string) => {
  localStorage.removeItem(getStorageKey(voiceName));
};
