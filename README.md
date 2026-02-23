# gemini-live-mcp

A generic [MCP](https://modelcontextprotocol.io/) server for interacting with **Gemini Live** Chrome extensions and web apps from any MCP-compatible AI agent — [Claude Code](https://claude.ai/code), [Cursor](https://cursor.sh), or your own.

It connects to Chrome via the [Chrome DevTools Protocol](https://chromedevtools.github.io/devtools-protocol/) and exposes tools to start/stop voice sessions, speak utterances, capture transcripts, inspect state, and run end-to-end voice tests — all programmatically.

## How It Works

```
┌─────────────────────────────────────────────────────────────────┐
│  MCP Client (Claude Code, Cursor, custom agent, etc.)           │
│                                                                 │
│  Calls MCP tools: start_session, speak, listen, run_voice_test  │
└──────────────────────────┬──────────────────────────────────────┘
                           │ MCP protocol (stdio)
                           ▼
┌─────────────────────────────────────────────────────────────────┐
│  gemini-live-mcp.py                                             │
│                                                                 │
│  ┌─────────────┐  ┌──────────────┐  ┌────────────────────────┐ │
│  │ Session mgmt │  │ Voice tools  │  │ Transcript capture     │ │
│  │ (CDP clicks, │  │ (TTS → Black │  │ (poll window prop via  │ │
│  │  navigation) │  │  Hole → mic) │  │  CDP Runtime.evaluate) │ │
│  └──────┬───────┘  └──────┬───────┘  └───────────┬────────────┘ │
└─────────┼─────────────────┼──────────────────────┼──────────────┘
          │ CDP              │ audio                │ CDP
          ▼                  ▼                      ▼
┌─────────────────────────────────────────────────────────────────┐
│  Chrome (--remote-debugging-port=9222)                          │
│                                                                 │
│  ┌─────────────────┐    ┌────────────────────────────────────┐  │
│  │  Page            │◄──►│  Extension / Web App               │  │
│  │  (content script │    │  ┌──────────────────────────────┐  │  │
│  │   or __mcp       │    │  │  Offscreen doc / session hub  │  │  │
│  │   bridge)        │    │  │  - Gemini Live WebSocket      │  │  │
│  │                  │    │  │  - Mic capture (BlackHole)     │  │  │
│  │                  │    │  │  - Audio playback              │  │  │
│  │                  │    │  │  - Transcript accumulation     │  │  │
│  └─────────────────┘    │  └──────────────────────────────┘  │  │
│                          └────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
                                    │
                                    │ WebSocket
                                    ▼
                          ┌───────────────────┐
                          │  Gemini Live API   │
                          │  (Google AI or     │
                          │   Vertex AI)       │
                          └───────────────────┘
```

### Voice test flow

```
Agent calls run_voice_test("what is this page about")
    │
    ├─► Navigate to URL
    ├─► Click FAB button (starts Gemini Live session)
    ├─► Wait for offscreen doc / session to connect
    ├─► Route OS audio: input + output → BlackHole
    ├─► TTS "what is this page about" → BlackHole → Chrome mic
    ├─► Poll offscreen doc for new transcript entries
    ├─► Capture user + model transcripts
    ├─► Assert model response contains expected text
    └─► Return results + latency metrics
```

## Two Modes

- **Extension mode** (default): For Chrome extensions with Shadow DOM overlays + offscreen documents. All extension-specific details (selectors, extension name, transcript property) are configured via environment variables.
- **Webapp mode**: For web apps (React, etc.) that expose a `window.__mcp` bridge object. No extension-specific config needed.

## Requirements

- Python 3.10+
- Chrome running with `--remote-debugging-port=9222`
- Python packages: `websockets`, `mcp`
- **For voice tools** (macOS):
  - [BlackHole](https://existential.audio/blackhole/) — virtual audio device for routing TTS into Chrome's mic
  - [SwitchAudioSource](https://github.com/deweller/switchaudio-osx) — CLI tool for switching audio devices
  - A TTS script (pointed to by `GLMCP_VOICE_SCRIPT`)

```bash
# Python deps
pip install websockets mcp

# macOS audio deps (for voice tools)
brew install blackhole-16ch switchaudio-osx
```

**Non-macOS:** Audio routing is skipped silently if `SwitchAudioSource` is not on PATH. Configure your virtual audio routing manually.

## Configuration

### Extension Mode (default)

Add to your `.mcp.json` (e.g. in your project root for Claude Code):

```json
{
  "mcpServers": {
    "gemini-live": {
      "command": "/path/to/.venv/bin/python",
      "args": ["/path/to/gemini-live-mcp.py"],
      "env": {
        "GLMCP_SHADOW_HOST": "#my-ext-root",
        "GLMCP_FAB_SELECTOR": ".start-btn",
        "GLMCP_CLOSE_SELECTOR": ".stop-btn",
        "GLMCP_EXTENSION_NAME": "My Extension"
      }
    }
  }
}
```

### Webapp Mode

```json
{
  "mcpServers": {
    "gemini-live": {
      "command": "/path/to/.venv/bin/python",
      "args": ["/path/to/gemini-live-mcp.py"],
      "env": {
        "GLMCP_MODE": "webapp",
        "GLMCP_DEFAULT_URL": "http://localhost:5173"
      }
    }
  }
}
```

### Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `GLMCP_MODE` | No | `extension` | `extension` or `webapp` |
| `GLMCP_SHADOW_HOST` | Extension only | -- | Shadow DOM host selector (e.g. `#my-ext-root`) |
| `GLMCP_FAB_SELECTOR` | Extension only | -- | CSS selector for the start/FAB button inside the shadow root |
| `GLMCP_CLOSE_SELECTOR` | Extension only | -- | CSS selector for the close/stop button inside the shadow root |
| `GLMCP_EXTENSION_NAME` | Extension only | -- | Extension name as shown in `chrome://extensions` |
| `GLMCP_CDP_URL` | No | `http://127.0.0.1:9222` | Chrome DevTools Protocol URL |
| `GLMCP_TRANSCRIPT_PROP` | No | `__tcTranscripts` | `window` property name for transcript array (extension mode) |
| `GLMCP_SW_URL_SUFFIX` | No | `/background.js` | Service worker URL suffix for extension detection |
| `GLMCP_SW_URL_EXCLUDE` | No | `/build/` | Substring to exclude from SW URL matching |
| `GLMCP_OFFSCREEN_URL` | No | `offscreen.html` | Substring to match the offscreen document URL |
| `GLMCP_DEFAULT_URL` | No | Wikipedia Tyrannosaurus page | Default URL for `start_session` and `run_voice_test` |
| `GLMCP_VOICE_SCRIPT` | No | -- | Path to a TTS script for the `speak` and `run_voice_test` tools |

## Tools

| Tool | Description |
|------|-------------|
| `start_session` | Navigate to a URL and start a Gemini Live session |
| `stop_session` | End the current session cleanly |
| `speak` | Generate TTS audio and play it into a virtual mic (requires `GLMCP_VOICE_SCRIPT`) |
| `listen` | Poll for new user+model transcript pair with timeout |
| `run_voice_test` | Full E2E test: navigate → start → speak → capture → assert (with latency) |
| `get_session_state` | Page URL, session status, transcript count, audio routing |
| `get_transcripts` | Return the last N transcript entries |
| `get_logs` | Return recent console logs from offscreen doc or page |
| `eval_page` | Evaluate JavaScript in the main page context |
| `eval_offscreen` | Evaluate JavaScript in the offscreen/hub document context |
| `navigate` | Navigate the main page to a URL |
| `reload_extension` | Reload the extension via `chrome://extensions` (extension mode only) |

## Audio Routing (macOS)

The `speak` and `run_voice_test` tools route audio through BlackHole so TTS output feeds into Chrome's mic input.

- **Auto-routes** on the first `speak` or `run_voice_test` call — switches OS input + output to BlackHole
- **Auto-restores** your original audio devices on shutdown (via `atexit` + SIGTERM/SIGINT signal handlers)
- **Lazy** — no audio changes until you actually use a voice tool

Check the current state with `get_session_state` — it reports the active input device and whether routing is active.

**Important:** Chrome persists its own mic preference separately from the OS default (`chrome://settings/content/microphone`). The `stop_session` tool resets this back to "System default" automatically.

## Chrome Setup

Quit Chrome completely first (Cmd+Q on macOS), then launch with the debug port:

```bash
/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome \
  --remote-debugging-port=9222 \
  --user-data-dir=/tmp/gemini-live-mcp-test \
  --no-first-run \
  --no-default-browser-check \
  "about:blank"
```

- **Extension mode**: Load your extension as unpacked at `chrome://extensions` (Developer mode).
- **Webapp mode**: Open your web app URL (e.g. `http://localhost:5173`).

## Voice Testing

The `speak` and `run_voice_test` tools require `GLMCP_VOICE_SCRIPT` set to a script that generates TTS audio and plays it through BlackHole.

The script is called as:
```bash
python <VOICE_SCRIPT> [--say] "<text>"
```

- `--say` uses macOS `say` (offline, fast)
- Without `--say`, use an alternative like `edge-tts` (online, higher quality)

## Transcript Contract

**Extension mode:** Reads `window.<GLMCP_TRANSCRIPT_PROP>` on the offscreen document — an array of `{ role: 'user' | 'model', text: string }`. Defaults to `__tcTranscripts`.

**Webapp mode:** Reads `window.__mcp.transcripts` on the page — same format.

The `listen` and `run_voice_test` tools handle transcript array resets after navigation (when the offscreen doc restarts, the array resets to empty — the tools detect this and adjust their baseline automatically).

## `window.__mcp` Bridge Contract (Webapp Mode)

Web apps expose state to the MCP server by setting `window.__mcp`:

```typescript
interface MCPBridge {
  transcripts: Array<{ role: 'user' | 'model'; text: string }>;
  isConnected: boolean;
  startSession?: () => void;
  stopSession?: () => void;
  sessionState?: string;
  logs?: Array<{ source: string; text: string }>;
}
```

### Adding the bridge to a React app

```tsx
useEffect(() => {
  (window as any).__mcp = {
    transcripts: transcriptHistory,
    isConnected,
    startSession: () => startSession(),
    stopSession: () => stopSession(),
  };
  return () => { delete (window as any).__mcp; };
}, [transcriptHistory, isConnected, startSession, stopSession]);
```

## License

MIT
