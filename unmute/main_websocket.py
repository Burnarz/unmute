import os
import asyncio
import base64
import json
import logging
from functools import cache, partial
from typing import Annotated, Any

import numpy as np
import requests
import sphn
from fastapi import (
    FastAPI,
    File,
    Form,
    HTTPException,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.websockets import WebSocketState
from fastrtc import AdditionalOutputs, CloseStream, audio_to_float32
from prometheus_fastapi_instrumentator import Instrumentator
from pydantic import BaseModel, Field, TypeAdapter, ValidationError, computed_field
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

import unmute.openai_realtime_api_events as ora
from unmute import metrics as mt
from unmute.exceptions import (
    MissingServiceAtCapacity,
    MissingServiceTimeout,
    WebSocketClosedError,
    make_ora_error,
)
from unmute.kyutai_constants import (
    KYUTAI_LLM_API_KEY,
    LLM_SERVER,
    MAX_VOICE_FILE_SIZE_MB,
    SAMPLE_RATE,
    STT_SERVER,
    TTS_SERVER,
    VOICE_CLONING_SERVER,
)
from unmute.service_discovery import async_ttl_cached
from unmute.timer import Stopwatch
from unmute.tts.voice_cloning import clone_voice
from unmute.tts.voice_donation import (
    VoiceDonationSubmission,
    generate_verification,
    submit_voice_donation,
)
from unmute.tts.voices import VoiceList
from unmute.unmute_handler import UnmuteHandler

app = FastAPI()

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)


@app.on_event("startup")
async def startup_event():
    """Pre-initialize MCP manager at server startup."""
    try:
        from unmute.mcp_manager import get_mcp_manager

        logger.info("Pre-initializing MCP manager...")
        manager = await get_mcp_manager()
        tool_count = len(manager.get_all_tools())
        logger.info("MCP manager initialized with %d tools", tool_count)
        await manager.load_memory()
        logger.info("Memory pre-loaded at startup")
    except Exception as e:
        logger.warning("Failed to pre-initialize MCP manager: %s", e)


# We prefer to scale this by running more instances of the server than having a single
# server handle more. This is to avoid the GIL.
MAX_CLIENTS = 4
SEMAPHORE = asyncio.Semaphore(MAX_CLIENTS)

Instrumentator().instrument(app).expose(app)
PROFILE_ACTIVE = False
_last_profile = None
_current_profile = None

ClientEventAdapter = TypeAdapter(
    Annotated[ora.ClientEvent, Field(discriminator="type")]
)

# Allow CORS for local development
CORS_ALLOW_ORIGINS = ["http://localhost", "http://localhost:3000"]
if "HTTP_HOST" in os.environ:
    CORS_ALLOW_ORIGINS.append(f"https://{os.environ['HTTP_HOST']}:3000")

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ALLOW_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
def root():
    return {"message": "You've reached the Unmute backend server."}


if PROFILE_ACTIVE:

    @app.get("/profile")
    def profile():
        if _last_profile is None:
            return HTMLResponse("<body>No last profiler saved</body>")
        else:
            return HTMLResponse(_last_profile.output_html())  # type: ignore


def _ws_to_http(ws_url: str) -> str:
    """Convert a WebSocket URL to an HTTP URL."""
    return ws_url.replace("ws://", "http://").replace("wss://", "https://")


def _check_server_status(server_url: str, headers: dict | None = None) -> bool:
    """Check if the server is up by sending a GET request."""
    try:
        response = requests.get(
            server_url,
            timeout=2,
            headers=headers or {},
        )
        logger.info(f"Response from {server_url}: {response}")
        return response.status_code == 200
    except requests.exceptions.RequestException as e:
        logger.info(f"Couldn't connect to {server_url}: {e}")
        return False


async def debug_running_tasks():
    while True:
        logger.debug(f"Running tasks: {len(asyncio.all_tasks())}")
        for task in asyncio.all_tasks():
            logger.debug(f"  Task: {task.get_name()} - {task.get_coro()}")
        await asyncio.sleep(5)


class HealthStatus(BaseModel):
    tts_up: bool
    stt_up: bool
    llm_up: bool
    voice_cloning_up: bool

    @computed_field
    @property
    def ok(self) -> bool:
        # Note that voice cloning is not required for the server to be healthy.
        return self.tts_up and self.stt_up and self.llm_up


@partial(async_ttl_cached, ttl_sec=0.5)
async def _get_health(
    _none: None,
):  # dummy param _none because caching function expects a single param as cache key.
    async with asyncio.TaskGroup() as tg:
        tts_up = tg.create_task(
            asyncio.to_thread(
                _check_server_status, _ws_to_http(TTS_SERVER) + "/api/build_info"
            )
        )
        stt_up = tg.create_task(
            asyncio.to_thread(
                _check_server_status, _ws_to_http(STT_SERVER) + "/api/build_info"
            )
        )
        llm_up = tg.create_task(
            asyncio.to_thread(
                _check_server_status,
                _ws_to_http(LLM_SERVER) + "/v1/models",
                # The default vLLM server doesn't use auth, but this is needed if you
                # use OpenAI or another LLM server.
                headers={"Authorization": f"Bearer {KYUTAI_LLM_API_KEY}"},
            )
        )
        voice_cloning_up = tg.create_task(
            asyncio.to_thread(
                _check_server_status,
                _ws_to_http(VOICE_CLONING_SERVER) + "/api/build_info",
            )
        )
        tts_up_res = await tts_up
        stt_up_res = await stt_up
        llm_up_res = await llm_up
        voice_cloning_up_res = await voice_cloning_up

    return HealthStatus(
        tts_up=tts_up_res,
        stt_up=stt_up_res,
        llm_up=llm_up_res,
        voice_cloning_up=voice_cloning_up_res,
    )


@app.get("/v1/health")
async def get_health():
    health = await _get_health(None)
    mt.HEALTH_OK.observe(health.ok)
    return health


@app.get("/v1/voices")
@cache
def voices():
    voice_list = VoiceList()
    # Note that `voice.good` is bool | None, here we really take only True values.
    good_voices = [
        voice.model_dump(exclude={"comment"})
        for voice in voice_list.voices
        if voice.good
    ]
    return good_voices


class LimitUploadSizeForPath(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp, max_upload_size: int, path: str) -> None:
        super().__init__(app)
        self.max_upload_size = max_upload_size
        self.path = path

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        if request.method == "POST" and request.url.path == self.path:
            if "content-length" not in request.headers:
                return Response(status_code=status.HTTP_411_LENGTH_REQUIRED)

            content_length = int(request.headers["content-length"])
            if content_length > self.max_upload_size:
                return Response(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE)

        return await call_next(request)


app.add_middleware(
    LimitUploadSizeForPath,
    max_upload_size=MAX_VOICE_FILE_SIZE_MB * 1024 * 1024,
    path="/v1/voices",
)


@app.post("/v1/voices")
async def post_voices(file: UploadFile):
    """Upload a voice list file.

    Make sure the maximum file size is configured in uvicorn.
    """
    name = clone_voice(file.file.read())
    return {"name": name}


@app.get("/v1/voice-donation")
async def get_voice_donation():
    """Initiate a voice donation by asking for a verification text."""
    verification = generate_verification()
    return verification


app.add_middleware(
    LimitUploadSizeForPath,
    max_upload_size=MAX_VOICE_FILE_SIZE_MB * 1024 * 1024,
    path="/v1/voice-donation",
)


@app.post("/v1/voice-donation")
async def post_voice_donation(
    file: UploadFile = File(...),  # noqa: B008
    metadata: str = Form(...),
):
    """Finish a voice donation."""
    file_bytes = file.file.read()

    try:
        metadata_parsed = VoiceDonationSubmission(**json.loads(metadata))
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {e}") from e
    except ValidationError as e:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid submission: {e.errors()}",
        ) from e

    try:
        submit_voice_donation(metadata_parsed, file_bytes)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    return {}


MEMORIES_DIR = "memories"
os.makedirs(MEMORIES_DIR, exist_ok=True)


@app.get("/v1/memories/{voice_name}")
async def get_memories(voice_name: str):
    voice_memories_dir = os.path.join(MEMORIES_DIR, voice_name)
    if not os.path.exists(voice_memories_dir):
        return []
    return os.listdir(voice_memories_dir)


@app.get("/v1/memories/{voice_name}/{filename}")
async def get_memory(voice_name: str, filename: str):
    file_path = os.path.join(MEMORIES_DIR, voice_name, filename)
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="Memory not found")
    with open(file_path, "r") as f:
        return json.load(f)


@app.post("/v1/memories/{voice_name}")
async def post_memory(voice_name: str, file: UploadFile = File(...)):
    voice_memories_dir = os.path.join(MEMORIES_DIR, voice_name)
    os.makedirs(voice_memories_dir, exist_ok=True)

    if not file.filename:
        raise HTTPException(status_code=400, detail="Filename not provided")

    filename = os.path.basename(file.filename)
    if not filename.endswith(".json"):
        raise HTTPException(
            status_code=400, detail="Invalid file type, only .json is accepted"
        )

    file_path = os.path.join(voice_memories_dir, filename)

    try:
        content = await file.read()
        # verify it's valid json
        json.loads(content)
        with open(file_path, "wb") as f:
            f.write(content)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON file")

    return {"filename": filename}


@app.get("/v1/proxy/jokes")
async def proxy_jokes(request: Request):
    jokes_api_key = os.environ.get("BLAGUES_API_KEY")
    if not jokes_api_key:
        raise HTTPException(status_code=500, detail="BLAGUES_API_KEY is not set")

    params = request.query_params
    response = requests.get(
        "https://www.blagues-api.fr/api/random",
        params=params,
        headers={"Authorization": f"Bearer {jokes_api_key}"},
    )
    return JSONResponse(content=response.json())


class HomeAssistantRequest(BaseModel):
    text: str


@app.post("/v1/proxy/homeassistant/conversation")
async def proxy_homeassistant_conversation(request: HomeAssistantRequest):
    ha_url = os.environ.get("HOME_ASSISTANT_URL")
    ha_token = os.environ.get("HOME_ASSISTANT_TOKEN")

    if not ha_url:
        raise HTTPException(
            status_code=500, detail="HOME_ASSISTANT_URL environment variable is not set"
        )
    if not ha_token:
        raise HTTPException(
            status_code=500,
            detail="HOME_ASSISTANT_TOKEN environment variable is not set",
        )

    try:
        response = requests.post(
            f"{ha_url}/api/conversation/process",
            headers={
                "Authorization": f"Bearer {ha_token}",
                "Content-Type": "application/json",
            },
            json={
                "text": request.text,
                "language": "fr",
                "agent_id": "conversation.home_assistant",
            },
            timeout=10,
        )

        if response.status_code != 200:
            return JSONResponse(
                content={
                    "error": f"Home Assistant returned status {response.status_code}: {response.text}"
                },
                status_code=response.status_code,
            )

        data = response.json()
        return {
            "response": data.get("response", {})
            .get("speech", {})
            .get("plain", {})
            .get("speech", "Command sent"),
            "success": True,
        }
    except requests.exceptions.ConnectionError:
        raise HTTPException(status_code=503, detail="Cannot connect to Home Assistant")
    except requests.exceptions.Timeout:
        raise HTTPException(status_code=504, detail="Home Assistant request timed out")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Home Assistant error: {str(e)}")


class MCPToolCallRequest(BaseModel):
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


@app.get("/v1/mcp/tools")
async def get_mcp_tools():
    """Get all available MCP tools in OpenAI function calling format."""
    from unmute.mcp_manager import get_mcp_manager

    try:
        manager = await get_mcp_manager()
        tools = manager.get_tools_for_llm()
        return {"tools": tools, "count": len(tools)}
    except Exception as e:
        logger.error("Failed to get MCP tools: %s", e)
        return {"tools": [], "count": 0, "error": str(e)}


@app.post("/v1/mcp/call")
async def call_mcp_tool(request: MCPToolCallRequest):
    """Call an MCP tool by name."""
    from unmute.mcp_manager import get_mcp_manager

    try:
        manager = await get_mcp_manager()

        if not manager.is_tool_from_mcp(request.name):
            raise HTTPException(
                status_code=400, detail=f"Tool '{request.name}' is not an MCP tool"
            )

        result = await manager.call_tool(request.name, request.arguments)
        return {"success": True, "result": result}

    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.error("MCP tool call failed: %s", e)
        raise HTTPException(status_code=500, detail=f"MCP tool call failed: {str(e)}")


@app.get("/v1/mcp/status")
async def get_mcp_status():
    """Get the status of all MCP server connections."""
    from unmute.mcp_manager import get_mcp_manager

    try:
        manager = await get_mcp_manager()
        servers = []
        for name, connection in manager.servers.items():
            servers.append(
                {
                    "name": name,
                    "connected": connection.session is not None,
                    "tool_count": len(connection.tools),
                    "tools": [t.name for t in connection.tools],
                }
            )
        return {
            "initialized": manager._initialized,
            "server_count": len(manager.servers),
            "servers": servers,
        }
    except Exception as e:
        logger.error("Failed to get MCP status: %s", e)
        return {"error": str(e), "initialized": False, "server_count": 0, "servers": []}


@app.websocket("/v1/realtime")
async def websocket_route(websocket: WebSocket):
    global _last_profile, _current_profile
    mt.SESSIONS.inc()
    mt.ACTIVE_SESSIONS.inc()
    session_watch = Stopwatch()
    if PROFILE_ACTIVE and _current_profile is None:
        from pyinstrument import Profiler

        logger.info("Profiler started.")
        _current_profile = Profiler(interval=0.0001, async_mode="disabled")
        import inspect

        frame = inspect.currentframe()
        while frame is not None and frame.f_back:
            frame = frame.f_back
        _current_profile.start(caller_frame=frame)

    async with SEMAPHORE:
        try:
            # The `subprotocol` argument is important because the client specifies what
            # protocol(s) it supports and OpenAI uses "realtime" as the value. If we
            # don't set this, the client will think this is not the right endpoint and
            # will not connect.
            await websocket.accept(subprotocol="realtime")

            handler = UnmuteHandler()
            async with handler:
                await handler.start_up()
                await _run_route(websocket, handler)

        except Exception as exc:
            await _report_websocket_exception(websocket, exc)
        finally:
            if _current_profile is not None:
                _current_profile.stop()
                logger.info("Profiler saved.")
                _last_profile = _current_profile
                _current_profile = None

            mt.ACTIVE_SESSIONS.dec()
            mt.SESSION_DURATION.observe(session_watch.time())


async def _report_websocket_exception(websocket: WebSocket, exc: Exception):
    if isinstance(exc, ExceptionGroup):
        exceptions = exc.exceptions
    else:
        exceptions = [exc]

    error_message = None

    for exc in exceptions:
        if isinstance(exc, (MissingServiceAtCapacity)):
            mt.FATAL_SERVICE_MISSES.inc()
            error_message = (
                f"Too many people are connected to service '{exc.service}'. "
                "Please try again later."
            )
        elif isinstance(exc, MissingServiceTimeout):
            mt.FATAL_SERVICE_MISSES.inc()
            error_message = (
                f"Service '{exc.service}' timed out. Please try again later."
            )
        elif isinstance(exc, WebSocketClosedError):
            logger.debug("Websocket was closed.")
        else:
            logger.exception("Unexpected error: %r", exc)
            mt.HARD_ERRORS.inc()
            error_message = "Internal server error :( Complain to Kyutai"

    if error_message is not None:
        mt.FORCE_DISCONNECTS.inc()

        try:
            await websocket.send_text(
                make_ora_error(type="fatal", message=error_message).model_dump_json()
            )
        except WebSocketDisconnect:
            logger.warning("Failed to send error message due to disconnect.")

        try:
            await websocket.close(
                code=status.WS_1011_INTERNAL_ERROR,
                reason=error_message,
            )
        except RuntimeError:
            logger.warning("Socket already closed.")


async def _run_route(websocket: WebSocket, handler: UnmuteHandler):
    health = await get_health()
    if not health.ok:
        logger.info("Health check failed, closing WebSocket connection.")
        await websocket.close(
            code=status.WS_1011_INTERNAL_ERROR,
            reason=f"Server is not healthy: {health}",
        )
        return

    emit_queue: asyncio.Queue[ora.ServerEvent] = asyncio.Queue()
    try:
        async with asyncio.TaskGroup() as tg:
            tg.create_task(
                receive_loop(websocket, handler, emit_queue), name="receive_loop()"
            )
            tg.create_task(
                emit_loop(websocket, handler, emit_queue), name="emit_loop()"
            )
            tg.create_task(handler.quest_manager.wait(), name="quest_manager.wait()")
            tg.create_task(debug_running_tasks(), name="debug_running_tasks()")
    finally:
        await handler.cleanup()
        logger.info("websocket_route() finished")


async def receive_loop(
    websocket: WebSocket,
    handler: UnmuteHandler,
    emit_queue: asyncio.Queue[ora.ServerEvent],
):
    """Receive messages from the WebSocket.

    Can decide to send messages via `emit_queue`.
    """
    opus_reader = sphn.OpusStreamReader(SAMPLE_RATE)
    wait_for_first_opus = True
    while True:
        try:
            message_raw = await websocket.receive_text()
        except WebSocketDisconnect as e:
            logger.info(
                "receive_loop() stopped because WebSocket disconnected: "
                f"{e.code=} {e.reason=}"
            )
            raise WebSocketClosedError() from e
        except RuntimeError as e:
            # This is expected when the client disconnects
            if "WebSocket is not connected" not in str(e):
                raise  # re-raise unexpected errors

            logger.info("receive_loop() stopped because WebSocket disconnected.")
            raise WebSocketClosedError() from e

        try:
            message: ora.ClientEvent = ClientEventAdapter.validate_json(message_raw)
        except json.JSONDecodeError as e:
            await emit_queue.put(
                ora.Error(
                    error=ora.ErrorDetails(
                        type="invalid_request_error",
                        message=f"Invalid JSON: {e}",
                    )
                )
            )
            continue
        except ValidationError as e:
            await emit_queue.put(
                ora.Error(
                    error=ora.ErrorDetails(
                        type="invalid_request_error",
                        message="Invalid message",
                        details=json.loads(e.json()),
                    )
                )
            )
            continue

        message_to_record = message

        if isinstance(message, ora.InputAudioBufferAppend):
            opus_bytes = base64.b64decode(message.audio)
            if wait_for_first_opus:
                # Somehow the UI is sending us potentially old messages from a previous
                # connection on reconnect, so that we might get some old OGG packets,
                # waiting for the bit set for first packet to feed to the decoder.
                if opus_bytes[5] & 2:
                    wait_for_first_opus = False
                else:
                    continue
            pcm = await asyncio.to_thread(opus_reader.append_bytes, opus_bytes)

            message_to_record = ora.UnmuteInputAudioBufferAppendAnonymized(
                number_of_samples=pcm.size,
            )

            if pcm.size:
                await handler.receive((SAMPLE_RATE, pcm[np.newaxis, :]))
        elif isinstance(message, ora.SessionUpdate):
            await handler.update_session(message.session)
            await emit_queue.put(ora.SessionUpdated(session=message.session))
        elif isinstance(message, ora.ResponseCreate):
            await handler._generate_response()
        elif isinstance(message, ora.ConversationItemCreate):
            if isinstance(message.item, ora.MessageItem):
                new_message = {
                    "role": message.item.role,
                    "content": message.item.content[0].text
                    if message.item.content
                    else None,
                }
                if message.item.tool_calls:
                    new_message["tool_calls"] = [
                        tc.model_dump() for tc in message.item.tool_calls
                    ]
            elif isinstance(message.item, ora.FunctionCallOutputItem):
                new_message = {
                    "role": "tool",
                    "tool_call_id": message.item.call_id,
                    "content": message.item.output,
                }
            else:
                raise ValueError(f"Unknown item type: {message.item.type}")
            handler.chatbot.chat_history.append(new_message)

        elif isinstance(message, ora.UnmuteAdditionalOutputs):
            # Don't record this: it's a debugging message and can be verbose. Anything
            # important to store should be in the other event types.
            message_to_record = None

        else:
            logger.info("Ignoring message:", str(message)[:100])

        if message_to_record is not None and handler.recorder is not None:
            await handler.recorder.add_event("client", message_to_record)


class EmitDebugLogger:
    def __init__(self):
        self.last_emitted_n = 0
        self.last_emitted_type = ""

    def on_emit(self, to_emit: ora.ServerEvent):
        if self.last_emitted_type == to_emit.type:
            self.last_emitted_n += 1
        else:
            self.last_emitted_n = 1
            self.last_emitted_type = to_emit.type

        if self.last_emitted_n == 1:
            logger.debug(f"Emitting: {to_emit.type}")
        else:
            logger.debug(f"Emitting ({self.last_emitted_n}): {self.last_emitted_type}")


async def emit_loop(
    websocket: WebSocket,
    handler: UnmuteHandler,
    emit_queue: asyncio.Queue[ora.ServerEvent],
):
    """Send messages to the WebSocket."""
    emit_debug_logger = EmitDebugLogger()

    opus_writer = sphn.OpusStreamWriter(SAMPLE_RATE)

    while True:
        if (
            websocket.application_state == WebSocketState.DISCONNECTED
            or websocket.client_state == WebSocketState.DISCONNECTED
        ):
            logger.info("emit_loop() stopped because WebSocket disconnected")
            raise WebSocketClosedError()

        try:
            to_emit = emit_queue.get_nowait()
        except asyncio.QueueEmpty:
            emitted_by_handler = await handler.emit()

            if emitted_by_handler is None:
                continue
            elif isinstance(emitted_by_handler, AdditionalOutputs):
                assert len(emitted_by_handler.args) == 1
                to_emit = ora.UnmuteAdditionalOutputs(
                    args=emitted_by_handler.args[0],
                )
            elif isinstance(emitted_by_handler, CloseStream):
                # Close here explicitly so that the receive loop stops too
                await websocket.close()
                break
            elif isinstance(emitted_by_handler, ora.ServerEvent):
                to_emit = emitted_by_handler
            else:
                _sr, audio = emitted_by_handler
                audio = audio_to_float32(audio)
                opus_bytes = await asyncio.to_thread(opus_writer.append_pcm, audio)
                # Due to buffering/chunking, Opus doesn't necessarily output something on every PCM added
                if opus_bytes:
                    to_emit = ora.ResponseAudioDelta(
                        delta=base64.b64encode(opus_bytes).decode("utf-8"),
                    )
                else:
                    continue

        emit_debug_logger.on_emit(to_emit)

        if handler.recorder is not None:
            await handler.recorder.add_event("server", to_emit)

        try:
            await websocket.send_text(to_emit.model_dump_json())
        except (WebSocketDisconnect, RuntimeError) as e:
            if isinstance(e, RuntimeError):
                if "Unexpected ASGI message 'websocket.send'" in str(e):
                    # This is expected when the client disconnects
                    message = f"emit_loop() stopped because WebSocket disconnected: {e}"
                else:
                    raise
            else:
                message = (
                    "emit_loop() stopped because WebSocket disconnected: "
                    f"{e.code=} {e.reason=}"
                )

            logger.info(message)
            raise WebSocketClosedError() from e


def _cors_headers_for_error(request: Request):
    origin = request.headers.get("origin")
    allow_origin = origin if origin in CORS_ALLOW_ORIGINS else None
    headers = {"Access-Control-Allow-Credentials": "true"}
    if allow_origin:
        headers["Access-Control-Allow-Origin"] = allow_origin

    return headers


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    # We need this so that CORS header are added even when the route raises an
    # exception. Otherwise you get a confusing CORS error even if the issue is totally
    # unrelated.
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail},
        headers=_cors_headers_for_error(request),
    )


@app.exception_handler(Exception)
async def general_exception_handler(request: Request, exc: Exception):
    # We need this so that CORS header are added even when the route raises an
    # exception. Otherwise you get a confusing CORS error even if the issue is totally
    # unrelated.
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error"},
        headers=_cors_headers_for_error(request),
    )


if __name__ == "__main__":
    import sys

    print(f"Run this via:\nfastapi dev {sys.argv[0]}")
    exit(1)
