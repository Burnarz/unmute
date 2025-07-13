export type ChatRole = "user" | "assistant" | "system";

export type ChatMessage = {
  role: ChatRole;
  content: string;
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
      compressed[compressed.length - 1].role === message.role
    ) {
      compressed[compressed.length - 1].content += `${separator}${message.content}`;
    } else {
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
