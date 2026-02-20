export const tools = [
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
            name: "get_news",
            description: "Get the latest news headlines. You can filter by country, category, or sources. At least one of these parameters is required but source cannot be used with country or category.",
            parameters: {
                type: "object",
                properties: {
                    country: { type: "string", description: "e.g. 'us', 'de'" },
                    category: { type: "string", description: "e.g. 'business', 'technology'" },
                    sources: { type: "string", description: "A comma-seperated string of identifiers for the news sources or blogs you want headlines from (e.g. 'the-verge', 'bbc-news')." }
                },
                required: [],
            },
        }
    },
    {
        type: "function",
        function: {
            name: "get_news_sources",
            description: "Get the list of available news sources. You can filter by category.",
            parameters: {
                type: "object",
                properties: {
                    category: { type: "string", description: "The category to filter sources by (e.g. 'business', 'technology')." }
                },
                required: [],
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
                    text: { type: "string", description: "The text command to send to Home Assistant in natural language" }
                },
                required: ["text"],
            },
        }
    }
];

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

async function getNews(backendServerUrl: string, {country, category, sources}: {country?: string, category?: string, sources?: string}) {
    const params = new URLSearchParams();
    if (country) params.append("country", country);
    if (category) params.append("category", category);
    if (sources) params.append("sources", sources);

    const response = await fetch(`${backendServerUrl}/v1/proxy/news/top-headlines?${params.toString()}`);

    if (!response.ok) {
        const errorText = await response.text();
        return { error: `News API request failed with status ${response.status}: ${errorText}` };
    }
    const data = await response.json();
    
    if (data.status && data.status !== 'ok') {
        return { error: `News API returned an error: ${data.message}` };
    }

    // Return only the articles, and only the first 5, and select fields to match python implementation
    return data.articles?.slice(0, 5).map((article: any) => ({
        source: article.source,
        author: article.author,
        title: article.title,
        description: article.description,
        publishedAt: article.publishedAt,
        content: article.content,
    })) || [];
}

async function getNewsSources(backendServerUrl: string, {category}: {category?: string}) {
    const params = new URLSearchParams();
    if (category) params.append("category", category);

    const response = await fetch(`${backendServerUrl}/v1/proxy/news/sources?${params.toString()}`);

    if (!response.ok) {
        const errorText = await response.text();
        return { error: `News API request failed with status ${response.status}: ${errorText}` };
    }
    const data = await response.json();

    if (data.status && data.status !== 'ok') {
        return { error: `News API returned an error: ${data.message}` };
    }

    return data.sources;
}

async function homeAssistant({text}: {text: string}) {
    const haUrl = localStorage.getItem("homeAssistantUrl");
    const haToken = localStorage.getItem("homeAssistantToken");

    if (!haUrl || !haToken) {
        return { error: "Home Assistant not configured. Please set homeAssistantUrl and homeAssistantToken in localStorage." };
    }

    try {
        const response = await fetch(`${haUrl}/api/conversation/process`, {
            method: "POST",
            headers: {
                "Authorization": `Bearer ${haToken}`,
                "Content-Type": "application/json",
            },
            body: JSON.stringify({
                text: text,
                language: "fr",
                agent_id: "conversation.home_assistant",
            }),
        });

        if (!response.ok) {
            const errorText = await response.text();
            return { error: `Home Assistant request failed with status ${response.status}: ${errorText}` };
        }

        const data = await response.json();
        return {
            response: data.response?.speech?.plain?.speech || data.response?.speech?.plain?.response || "Command sent",
            success: true,
        };
    } catch (error) {
        if (error instanceof Error) {
            return { error: error.message };
        }
        return { error: "Unknown error connecting to Home Assistant" };
    }
}


export async function handleToolCall(call: { name: string, arguments: string }, backendServerUrl: string) {
    console.log(`Handling function call: ${call.name}`);

    try {
        const args = JSON.parse(call.arguments || "{}");
        console.log(`Function call ${call.name} args:`, args);
        
        let result;
        switch (call.name) {
            case 'get_weather':
                result = await getWeather(args);
                break;
            case 'get_coordinates':
                result = await getCoordinates(args);
                break;
            case 'get_news':
                result = await getNews(backendServerUrl, args);
                break;
            case 'get_news_sources':
                result = await getNewsSources(backendServerUrl, args);
                break;
            case 'home_assistant':
                result = await homeAssistant(args);
                break;
            default:
                result = { error: `Unknown function call: ${call.name}` };
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
