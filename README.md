# gemini-live-mcp

A generic [MCP](https://modelcontextprotocol.io/) server for interacting with **Gemini Live** Chrome extensions from AI coding agents like [Claude Code](https://claude.ai/code).

It connects to Chrome via the [Chrome DevTools Protocol](https://chromedevtools.github.io/devtools-protocol/) and exposes tools to start/stop voice sessions, speak utterances, capture transcripts, inspect state, and run end-to-end voice tests -- all from your MCP client.

Works with any Gemini Live Chrome extension that uses an offscreen document for audio and a Shadow DOM overlay for its UI. All extension-specific details (selectors, extension name, transcript property) are configured via environment variables.

## Requirements

- Python 3.10+
- Chrome running with `--remote-debugging-port=9222`
- Your Gemini Live extension loaded in Chrome
- Python packages: `websockets`, `mcp`

## Installation

```bash
pip install websockets mcp
```

Or with a virtual environment:

```bash
python -m venv .venv
source .venv/bin/activate
pip install websockets mcp
```

## Configuration

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

### Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `GLMCP_SHADOW_HOST` | Yes | -- | Shadow DOM host selector (e.g. `#my-ext-root`) |
| `GLMCP_FAB_SELECTOR` | Yes | -- | CSS selector for the start/FAB button inside the shadow root |
| `GLMCP_CLOSE_SELECTOR` | Yes | -- | CSS selector for the close/stop button inside the shadow root |
| `GLMCP_EXTENSION_NAME` | Yes | -- | Extension name as shown in `chrome://extensions` |
| `GLMCP_CDP_URL` | No | `http://127.0.0.1:9222` | Chrome DevTools Protocol URL |
| `GLMCP_TRANSCRIPT_PROP` | No | `__tcTranscripts` | `window` property name for the transcript array |
| `GLMCP_SW_URL_SUFFIX` | No | `/background.js` | Service worker URL suffix for extension detection |
| `GLMCP_SW_URL_EXCLUDE` | No | `/build/` | Substring to exclude from SW URL matching |
| `GLMCP_OFFSCREEN_URL` | No | `offscreen.html` | Substring to match the offscreen document URL |
| `GLMCP_DEFAULT_URL` | No | Wikipedia Tyrannosaurus page | Default URL for `start_session` and `run_voice_test` |
| `GLMCP_VOICE_SCRIPT` | No | -- | Path to a TTS script for the `speak` and `run_voice_test` tools |

## Tools

| Tool | Description |
|------|-------------|
| `get_session_state` | Check extension ID, page URL, session status, offscreen doc, transcript count |
| `start_session` | Navigate to a URL and click the FAB to start a Gemini Live session |
| `stop_session` | End the session by navigating to `about:blank` |
| `speak` | Generate TTS audio and play it into a virtual mic (requires `GLMCP_VOICE_SCRIPT`) |
| `listen` | Poll for new user+model transcript pair with timeout |
| `get_transcripts` | Return the last N transcript entries |
| `get_offscreen_logs` | Return recent console logs from the offscreen hub document |
| `eval_page` | Evaluate JavaScript in the main page context |
| `eval_offscreen` | Evaluate JavaScript in the offscreen document context |
| `navigate` | Navigate the main page to a URL |
| `reload_extension` | Reload the extension via `chrome://extensions` |
| `run_voice_test` | Full end-to-end test: navigate, start session, speak, capture transcript, assert |

## Chrome Setup

Quit Chrome completely first, then launch with the debug port:

```bash
/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome \
  --remote-debugging-port=9222 \
  --user-data-dir=/tmp/gemini-live-mcp-test \
  --no-first-run \
  --no-default-browser-check \
  "about:blank"
```

Load your extension as unpacked at `chrome://extensions` (Developer mode).

## Audio Routing (macOS)

The `speak` and `run_voice_test` tools need audio routed through a virtual device (BlackHole) so TTS output feeds into Chrome's mic input. On macOS, the server handles this automatically:

- **Auto-routes** on the first `speak` or `run_voice_test` call — switches both input and output to BlackHole
- **Auto-restores** your original audio devices when the MCP server shuts down (via `atexit`)
- **Lazy** — no audio changes until you actually use a voice tool

**Prerequisites:**

```bash
brew install blackhole-16ch switchaudio-osx
```

**Non-macOS:** Audio routing is skipped silently if `SwitchAudioSource` is not on PATH. Configure your virtual audio routing manually.

You can check the current audio state with `get_session_state` — it shows the current input device and whether routing is active.

## Voice Testing

The `speak` and `run_voice_test` tools require:

1. **`GLMCP_VOICE_SCRIPT`** set to a script that generates TTS audio and plays it through a virtual audio device (like BlackHole on macOS) so the extension's mic capture picks it up.

2. The script is called as: `python <VOICE_SCRIPT> [--say] "<text>"` where `--say` uses macOS `say` (offline, fast) and omitting it uses an alternative like edge-tts.

3. Audio routing — handled automatically on macOS (see above), or configure manually on other platforms.

## Transcript Contract

The server expects the extension's offscreen document to expose transcripts on `window` as an array of objects with `{ role: 'user' | 'model', text: string }`. The property name defaults to `__tcTranscripts` but is configurable via `GLMCP_TRANSCRIPT_PROP`.

## License

MIT
