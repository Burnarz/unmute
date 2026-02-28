export const builtInTools = [
    {
        type: "function",
        function: {
            name: "get_weather",
            description: "Get current temperature for provided coordinates in celsius.",
            parameters: {
                type: "object",
                properties: {
                    latitude: { type: "number" },
                    longitude: { type: "number" }
                },
                required: ["latitude", "longitude"],
            },
        }
    },
    {
        type: "function",
        function: {
            name: "get_coordinates",
            description: "Get latitude and longitude for a given city name.",
            parameters: {
                type: "object",
                properties: {
                    city: { type: "string" }
                },
                required: ["city"],
            },
        }
    },
    {
        type: "function",
        function: {
            name: "get_jokes",
            description: "ALWAYS use this tool when the user is asking for a joke, and ALWAYS respond with this joke.",
            parameters: {
                type: "object",
                properties: {}
            },
        }
    },
    {
        type: "function",
        function: {
            name: "home_assistant",
            description: "Send a text command to Home Assistant. Use this to control smart home devices, ask about sensor states, or trigger automations. Examples: 'turn on the living room lights', 'what is the temperature in the bedroom', 'close the garage door'.",
            parameters: {
                type: "object",
                properties: {
                    text: { type: "string", description: "The text command to send to Home Assistant in a French natural language." }
                },
                required: ["text"],
            },
        }
    }
];

let cachedMcpTools: any[] | null = null;
let mcpToolsFetchPromise: Promise<any[]> | null = null;

export async function fetchMcpTools(backendServerUrl: string): Promise<any[]> {
    if (cachedMcpTools !== null) {
        return cachedMcpTools;
    }
    
    if (mcpToolsFetchPromise) {
        return mcpToolsFetchPromise;
    }
    
    mcpToolsFetchPromise = (async () => {
        try {
            const response = await fetch(`${backendServerUrl}/v1/mcp/tools`, {
                signal: AbortSignal.timeout(5000),
            });
            
            if (!response.ok) {
                console.warn("Failed to fetch MCP tools:", response.status);
                return [];
            }
            
            const data = await response.json();
            cachedMcpTools = data.tools || [];
            console.log(`Fetched ${cachedMcpTools.length} MCP tools:`, cachedMcpTools.map((t: any) => t.function?.name));
            return cachedMcpTools;
        } catch (error) {
            console.warn("Error fetching MCP tools:", error);
            return [];
        } finally {
            mcpToolsFetchPromise = null;
        }
    })();
    
    return mcpToolsFetchPromise;
}

export function getAllTools(mcpTools: any[] = []): any[] {
    return [...builtInTools, ...mcpTools];
}

export const tools = builtInTools;

async function getWeather({ latitude, longitude }: { latitude: number, longitude: number }) {
    const response = await fetch(`https://api.open-meteo.com/v1/forecast?latitude=${latitude}&longitude=${longitude}&current=temperature_2m,wind_speed_10m&hourly=temperature_2m,relative_humidity_2m,wind_speed_10m`);
    const data = await response.json();
    return data.current;
}

async function getCoordinates({ city }: { city: string }) {
    const response = await fetch(`https://geocoding-api.open-meteo.com/v1/search?name=${city}`);
    const data = await response.json();
    return data.results[0];
}

async function getJokes(backendServerUrl: string) {
    const response = await fetch(`${backendServerUrl}/v1/proxy/jokes`);

    if (!response.ok) {
        const errorText = await response.text();
        return { error: `Blagues API request failed with status ${response.status}: ${errorText}` };
    }

    const data = await response.json();
    return `${data.joke} ${data.answer}`;
}

async function homeAssistant(backendServerUrl: string, {text}: {text: string}) {
    try {
        const response = await fetch(`${backendServerUrl}/v1/proxy/homeassistant/conversation`, {
            method: "POST",
            headers: {
                "Content-Type": "application/json",
            },
            body: JSON.stringify({ text }),
        });

        if (!response.ok) {
            const errorData = await response.json().catch(() => ({}));
            return { error: errorData.detail || `Home Assistant request failed with status ${response.status}` };
        }

        return await response.json();
    } catch (error) {
        if (error instanceof Error) {
            return { error: error.message };
        }
        return { error: "Unknown error connecting to Home Assistant" };
    }
}


async function callMcpTool(backendServerUrl: string, name: string, toolArgs: Record<string, any>) {
    try {
        const response = await fetch(`${backendServerUrl}/v1/mcp/call`, {
            method: "POST",
            headers: {
                "Content-Type": "application/json",
            },
            body: JSON.stringify({ name, arguments: toolArgs }),
        });

        if (!response.ok) {
            const errorData = await response.json().catch(() => ({}));
            return { error: errorData.detail || `MCP tool call failed with status ${response.status}` };
        }

        const data = await response.json();
        return data.result;
    } catch (error) {
        if (error instanceof Error) {
            return { error: error.message };
        }
        return { error: "Unknown error calling MCP tool" };
    }
}

const builtInToolNames = ['get_weather', 'get_coordinates', 'get_jokes', 'home_assistant'];

export async function handleToolCall(call: { name: string, arguments: string }, backendServerUrl: string) {
    console.log(`Handling function call: ${call.name}`);

    try {
        const args = JSON.parse(call.arguments || "{}");
        console.log(`Function call ${call.name} args:`, args);
        
        let result;
        if (builtInToolNames.includes(call.name)) {
            switch (call.name) {
                case 'get_weather':
                    result = await getWeather(args);
                    break;
                case 'get_coordinates':
                    result = await getCoordinates(args);
                    break;
                case 'get_jokes':
                    result = await getJokes(backendServerUrl);
                    break;
                case 'home_assistant':
                    result = await homeAssistant(backendServerUrl, args);
                    break;
                default:
                    result = { error: `Unknown function call: ${call.name}` };
            }
        } else {
            result = await callMcpTool(backendServerUrl, call.name, args);
        }

        console.log(`Function call ${call.name} result:`, result);
        return result;
    } catch (error) {
        console.error(`Function call ${call.name} failed:`, error);
        if (error instanceof Error) {
            return { error: error.message };
        }
        return { error: "An unknown error occurred." };
    }
}
