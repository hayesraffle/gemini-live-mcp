#!/usr/bin/env python3
"""
gemini-live-mcp: Generic MCP Server for Gemini Live Chrome Extensions & Web Apps

Exposes MCP tools for interfacing with and debugging live Gemini sessions
running inside Chrome extensions or web apps. All target-specific config
is provided via environment variables (GLMCP_*).

Two modes:
  - extension (default): For Chrome extensions with Shadow DOM + offscreen docs.
  - webapp: For web apps that expose a `window.__mcp` bridge object.

Prerequisites:
  1. Chrome running with --remote-debugging-port=9222
  2. Extension mode: Target extension loaded (unpacked from its build output)
     Webapp mode: Target web app running (e.g. via Vite dev server)
  3. For voice tools: GLMCP_VOICE_SCRIPT set
     (audio routing auto-configured on macOS with SwitchAudioSource + BlackHole)

Required env vars (extension mode only):
  GLMCP_SHADOW_HOST     - Shadow DOM host selector (e.g. #my-ext-root)
  GLMCP_FAB_SELECTOR    - CSS selector for the start/FAB button
  GLMCP_CLOSE_SELECTOR  - CSS selector for the close/stop button
  GLMCP_EXTENSION_NAME  - Extension name as shown in chrome://extensions

Optional env vars:
  GLMCP_MODE            - 'extension' (default) or 'webapp'
  GLMCP_CDP_URL         - Chrome DevTools Protocol URL (default: http://127.0.0.1:9222)
  GLMCP_TRANSCRIPT_PROP - Window property for transcript array (default: __tcTranscripts)
  GLMCP_SW_URL_SUFFIX   - Service worker URL suffix (default: /background.js)
  GLMCP_SW_URL_EXCLUDE  - Substring to exclude from SW URL matching (default: /build/)
  GLMCP_OFFSCREEN_URL   - Substring to match offscreen doc URL (default: offscreen.html)
  GLMCP_DEFAULT_URL     - Default test URL (default: Wikipedia Tyrannosaurus)
  GLMCP_VOICE_SCRIPT    - Path to TTS script for speak tool (optional)

Add to .mcp.json (extension mode):
  "my-ext": {
    "command": "/path/to/.venv/bin/python",
    "args": ["/path/to/gemini-live-mcp.py"],
    "env": {
      "GLMCP_SHADOW_HOST": "#my-ext-root",
      "GLMCP_FAB_SELECTOR": ".start-btn",
      "GLMCP_CLOSE_SELECTOR": ".stop-btn",
      "GLMCP_EXTENSION_NAME": "My Extension"
    }
  }

Add to .mcp.json (webapp mode):
  "my-app": {
    "command": "/path/to/.venv/bin/python",
    "args": ["/path/to/gemini-live-mcp.py"],
    "env": {
      "GLMCP_MODE": "webapp",
      "GLMCP_DEFAULT_URL": "http://localhost:5173"
    }
  }
"""

import sys
import os
import atexit
import asyncio
import json
import shutil
import subprocess
import time
import urllib.request

import websockets
from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Configuration from environment
# ---------------------------------------------------------------------------
MODE = os.environ.get('GLMCP_MODE', 'extension')
CDP_URL = os.environ.get('GLMCP_CDP_URL', 'http://127.0.0.1:9222')
DEFAULT_TEST_URL = os.environ.get('GLMCP_DEFAULT_URL', 'https://en.wikipedia.org/wiki/Tyrannosaurus')
VOICE_SCRIPT = os.environ.get('GLMCP_VOICE_SCRIPT', '')

# Extension-mode-only config (not required for webapp mode)
if MODE == 'extension':
    SHADOW_HOST = os.environ['GLMCP_SHADOW_HOST']
    FAB_SEL = os.environ['GLMCP_FAB_SELECTOR']
    CLOSE_BTN_SEL = os.environ['GLMCP_CLOSE_SELECTOR']
    EXTENSION_NAME = os.environ['GLMCP_EXTENSION_NAME']
else:
    SHADOW_HOST = FAB_SEL = CLOSE_BTN_SEL = EXTENSION_NAME = ''

TRANSCRIPT_PROP = os.environ.get('GLMCP_TRANSCRIPT_PROP', '__tcTranscripts')
SW_URL_SUFFIX = os.environ.get('GLMCP_SW_URL_SUFFIX', '/background.js')
SW_URL_EXCLUDE = os.environ.get('GLMCP_SW_URL_EXCLUDE', '/build/')
OFFSCREEN_URL = os.environ.get('GLMCP_OFFSCREEN_URL', 'offscreen.html')

mcp = FastMCP('gemini-live-mcp')

# ---------------------------------------------------------------------------
# Audio routing (macOS + SwitchAudioSource + BlackHole)
# ---------------------------------------------------------------------------

_audio_routed = False
_original_input = None
_original_output = None


def _run_switch(args):
    """Run SwitchAudioSource with given args, return stdout."""
    return subprocess.run(
        ['SwitchAudioSource'] + args,
        capture_output=True, text=True, timeout=5,
    ).stdout.strip()


def _find_blackhole():
    """Return the name of the first BlackHole device, or None."""
    sas = shutil.which('SwitchAudioSource')
    if not sas:
        return None
    out = _run_switch(['-a', '-t', 'input'])
    for line in out.splitlines():
        if 'BlackHole' in line:
            return line.strip()
    return None


def _get_current_audio():
    """Return (current_input, current_output) device names."""
    inp = _run_switch(['-c', '-t', 'input'])
    out = _run_switch(['-c', '-t', 'output'])
    return inp, out


def _ensure_audio_routed():
    """Lazy-init: switch input+output to BlackHole on first voice call."""
    global _audio_routed, _original_input, _original_output
    if _audio_routed:
        return
    if not shutil.which('SwitchAudioSource'):
        return
    bh = _find_blackhole()
    if not bh:
        return
    _original_input, _original_output = _get_current_audio()
    _run_switch(['-s', bh, '-t', 'input'])
    _run_switch(['-s', bh, '-t', 'output'])
    atexit.register(_restore_audio)
    _audio_routed = True


def _restore_audio():
    """Restore original OS audio devices. Called by stop_session and atexit."""
    global _audio_routed
    if _original_input:
        _run_switch(['-s', _original_input, '-t', 'input'])
    if _original_output:
        _run_switch(['-s', _original_output, '-t', 'output'])
    _audio_routed = False


async def _reset_chrome_mic():
    """Reset Chrome's internal mic preference to the system default device.

    Chrome has its own mic dropdown (chrome://settings/content/microphone)
    that persists across restarts and overrides the OS default. When tests
    route audio through BlackHole, Chrome latches onto it. This opens a
    temporary settings tab, selects the system default device, and closes it.
    """
    try:
        # Open settings tab via CDP (requires PUT method)
        req = urllib.request.Request(
            f'{CDP_URL}/json/new?chrome://settings/content/microphone', method='PUT')
        with urllib.request.urlopen(req, timeout=5) as r:
            resp = json.loads(r.read())
        ws_url = resp['webSocketDebuggerUrl']
        tab_id = resp['id']
        await asyncio.sleep(1.5)  # Settings page needs time to render shadow DOM

        async with websockets.connect(ws_url, open_timeout=5) as ws:
            # Chrome settings uses deep shadow DOM — recursive search finds the native <select>
            result = await _cdp_eval(ws, """
                (() => {
                    function findSelects(root) {
                        const results = [];
                        for (const el of root.querySelectorAll('*')) {
                            if (el.tagName === 'SELECT') results.push(el);
                            if (el.shadowRoot) results.push(...findSelects(el.shadowRoot));
                        }
                        return results;
                    }
                    const sel = findSelects(document)[0];
                    if (!sel) return 'no select found';
                    for (const opt of sel.options) {
                        if (opt.text.includes('System default') || opt.text.includes('Built-in')) {
                            if (sel.value === opt.value) return 'already set to: ' + opt.text;
                            sel.value = opt.value;
                            sel.dispatchEvent(new Event('change', {bubbles: true}));
                            return 'set to: ' + opt.text;
                        }
                    }
                    return 'no system default option found';
                })()
            """)

        # Close the settings tab
        _http_get(f'{CDP_URL}/json/close/{tab_id}')
        return result
    except Exception as e:
        return f'failed: {e}'


# ---------------------------------------------------------------------------
# CDP helpers
# ---------------------------------------------------------------------------

def _http_get(url):
    with urllib.request.urlopen(url, timeout=5) as r:
        return json.loads(r.read())


def _get_targets():
    return _http_get(f'{CDP_URL}/json')


async def _cdp_send(ws, method, params=None):
    """Send CDP command, wait for matching response (ignore events)."""
    cmd_id = int(time.time() * 1000) % 100000 + 1
    await ws.send(json.dumps({'id': cmd_id, 'method': method, 'params': params or {}}))
    while True:
        msg = json.loads(await ws.recv())
        if msg.get('id') == cmd_id:
            if 'error' in msg:
                raise RuntimeError(f'CDP {method} error: {msg["error"]}')
            return msg.get('result', {})


async def _cdp_eval(ws, expression):
    """Evaluate JS in a CDP target, return the value."""
    result = await _cdp_send(ws, 'Runtime.evaluate', {
        'expression': expression,
        'returnByValue': True,
        'awaitPromise': True,
    })
    exc = result.get('exceptionDetails')
    if exc:
        raise RuntimeError(f'JS: {exc.get("text")} — {exc.get("exception", {}).get("description")}')
    return result.get('result', {}).get('value')


def _detect_ext_id():
    """Find extension ID from CDP targets using configured URL patterns."""
    targets = _get_targets()
    for t in targets:
        url = t.get('url', '')
        if t.get('type') == 'service_worker' and url.endswith(SW_URL_SUFFIX) and SW_URL_EXCLUDE not in url:
            return url.split('/')[2], t
    for t in targets:
        url = t.get('url', '')
        if OFFSCREEN_URL in url and url.startswith('chrome-extension://'):
            ext_id = url.split('/')[2]
            return ext_id, {'webSocketDebuggerUrl': None, 'url': url}
    raise RuntimeError(f'{EXTENSION_NAME} extension not found. Is Chrome running with the extension loaded?')


def _get_page_target():
    """Return the first non-chrome page target."""
    for t in _get_targets():
        url = t.get('url', '')
        if t.get('type') == 'page' and not url.startswith('chrome'):
            return t
    raise RuntimeError('No suitable page target found')


def _get_offscreen_target():
    """Return the offscreen doc target, or None."""
    for t in _get_targets():
        if OFFSCREEN_URL in t.get('url', ''):
            return t
    return None


async def _page_eval(expression):
    """Evaluate JS in the main page context."""
    page = _get_page_target()
    async with websockets.connect(page['webSocketDebuggerUrl'], open_timeout=5) as ws:
        return await _cdp_eval(ws, expression)


async def _offscreen_eval(expression):
    """Evaluate JS in the offscreen doc context."""
    t = _get_offscreen_target()
    if not t:
        raise RuntimeError('No offscreen target — is a session active?')
    async with websockets.connect(t['webSocketDebuggerUrl'], open_timeout=5) as ws:
        return await _cdp_eval(ws, expression)


def _get_mcp_target():
    """Return the CDP target where window.__mcp lives.

    Extension mode: the offscreen document.
    Webapp mode: the main page tab.
    """
    if MODE == 'extension':
        t = _get_offscreen_target()
        if not t:
            raise RuntimeError('No offscreen target — start a session first.')
        return t
    return _get_page_target()


async def _mcp_eval(expression):
    """Evaluate JS against the MCP bridge target."""
    t = _get_mcp_target()
    async with websockets.connect(t['webSocketDebuggerUrl'], open_timeout=5) as ws:
        return await _cdp_eval(ws, expression)


# ---------------------------------------------------------------------------
# MCP tools
# ---------------------------------------------------------------------------

@mcp.tool()
async def get_session_state() -> str:
    """
    Returns the current state of the Gemini Live session:
    page URL, whether the session is active, transcript count, and audio status.
    In extension mode, also includes extension ID and offscreen doc status.
    """
    page = _get_page_target()
    page_url = page.get('url', 'unknown')

    if MODE == 'webapp':
        # Webapp mode: read state from window.__mcp
        try:
            state = await _page_eval("""
                (() => {
                    const m = window.__mcp;
                    if (!m) return { bridge: false };
                    return {
                        bridge: true,
                        isConnected: !!m.isConnected,
                        transcripts: (m.transcripts || []).length,
                        sessionState: m.sessionState || null,
                    };
                })()
            """)
        except Exception as e:
            state = {'bridge': False, 'error': str(e)}

        if not state.get('bridge'):
            session_state = 'no bridge (window.__mcp not found)'
            transcript_count = 0
        else:
            session_state = 'active' if state.get('isConnected') else 'inactive'
            if state.get('sessionState'):
                session_state += f' ({state["sessionState"]})'
            transcript_count = state.get('transcripts', 0)

        # Audio routing status
        if not shutil.which('SwitchAudioSource'):
            audio_status = 'unknown (SwitchAudioSource not available)'
        else:
            current_input, _ = _get_current_audio()
            routed = 'routed' if _audio_routed else 'not routed'
            audio_status = f'{current_input} ({routed})'

        return (
            f'Mode         : webapp\n'
            f'Page URL     : {page_url}\n'
            f'Session state: {session_state}\n'
            f'Transcripts  : {transcript_count} entries\n'
            f'Audio        : {audio_status}'
        )

    # Extension mode
    try:
        ext_id, _ = _detect_ext_id()
    except RuntimeError as e:
        return f'ERROR: {e}'

    try:
        session_state = await _page_eval(f"""
            (() => {{
                const root = document.querySelector('{SHADOW_HOST}')?.shadowRoot;
                if (!root) return 'no-shadow-root';
                if (root.querySelector('{CLOSE_BTN_SEL}')) return 'active';
                if (root.querySelector('{FAB_SEL}')) return 'inactive';
                return 'unknown';
            }})()
        """)
    except Exception as e:
        session_state = f'error: {e}'

    offscreen = _get_offscreen_target()
    offscreen_status = offscreen['url'] if offscreen else 'not running'

    transcript_count = 0
    if offscreen:
        try:
            async with websockets.connect(offscreen['webSocketDebuggerUrl'], open_timeout=5) as ws:
                result = await _cdp_send(ws, 'Runtime.evaluate', {
                    'expression': f'(window.{TRANSCRIPT_PROP} || []).length',
                    'returnByValue': True,
                })
                transcript_count = result.get('result', {}).get('value', 0)
        except Exception:
            pass

    # Audio routing status
    if not shutil.which('SwitchAudioSource'):
        audio_status = 'unknown (SwitchAudioSource not available)'
    else:
        current_input, _ = _get_current_audio()
        routed = 'routed' if _audio_routed else 'not routed'
        audio_status = f'{current_input} ({routed})'

    return (
        f'Mode         : extension\n'
        f'Extension ID : {ext_id}\n'
        f'Page URL     : {page_url}\n'
        f'Session state: {session_state}\n'
        f'Offscreen    : {offscreen_status}\n'
        f'Transcripts  : {transcript_count} entries\n'
        f'Audio        : {audio_status}'
    )


@mcp.tool()
async def start_session(url: str = DEFAULT_TEST_URL) -> str:
    """
    Navigate to a URL (or reload if already there) and start a Gemini Live
    session. In extension mode, clicks the FAB button. In webapp mode,
    calls window.__mcp.startSession() and waits for isConnected.

    Args:
        url: Page URL to open. Defaults to the configured default URL.
    """
    page = _get_page_target()
    page_ws = page['webSocketDebuggerUrl']
    current_url = page.get('url', '')

    async with websockets.connect(page_ws, open_timeout=5) as ws:
        if current_url == url:
            await _cdp_send(ws, 'Page.reload', {})
        else:
            await _cdp_send(ws, 'Page.navigate', {'url': url})

    await asyncio.sleep(3)

    if MODE == 'webapp':
        # Wait for window.__mcp to appear (up to 15s)
        deadline = time.time() + 15
        async with websockets.connect(page_ws, open_timeout=5) as ws:
            while time.time() < deadline:
                val = await _cdp_eval(ws, '!!window.__mcp')
                if val:
                    break
                await asyncio.sleep(0.5)
            else:
                return 'ERROR: window.__mcp did not appear within 15s'

            # Check if already connected
            connected = await _cdp_eval(ws, '!!window.__mcp.isConnected')
            if connected:
                return f'start_session: already-active (url={url})'

            # Call startSession if available
            has_start = await _cdp_eval(ws, 'typeof window.__mcp.startSession === "function"')
            if has_start:
                await _cdp_eval(ws, 'window.__mcp.startSession()')

                # Wait for isConnected (up to 15s)
                deadline2 = time.time() + 15
                while time.time() < deadline2:
                    connected = await _cdp_eval(ws, '!!window.__mcp.isConnected')
                    if connected:
                        return f'start_session: connected (url={url})'
                    await asyncio.sleep(0.5)
                return f'start_session: startSession() called but not connected within 15s (url={url})'
            else:
                return f'start_session: bridge found but no startSession function — app may auto-connect (url={url})'
    else:
        # Extension mode: wait for content script shadow DOM
        deadline = time.time() + 15
        async with websockets.connect(page_ws, open_timeout=5) as ws:
            while time.time() < deadline:
                val = await _cdp_eval(ws, f"!!document.querySelector('{SHADOW_HOST}')")
                if val:
                    break
                await asyncio.sleep(0.5)
            else:
                return 'ERROR: Content script did not appear within 15s'

            result = await _cdp_eval(ws, f"""
                (() => {{
                    const root = document.querySelector('{SHADOW_HOST}')?.shadowRoot;
                    if (!root) return 'no-shadow-root';
                    if (root.querySelector('{CLOSE_BTN_SEL}')) return 'already-active';
                    const fab = root.querySelector('{FAB_SEL}');
                    if (fab) {{ fab.click(); return 'clicked'; }}
                    return 'no-fab';
                }})()
            """)

        return f'start_session: {result} (url={url})'


@mcp.tool()
async def stop_session() -> str:
    """
    End the current Gemini Live session. In webapp mode, calls
    window.__mcp.stopSession() if available, then navigates to about:blank.
    In extension mode, releases the mic on the offscreen doc first.
    """
    if MODE == 'webapp':
        # Try calling stopSession on the bridge
        try:
            await _page_eval("""
                (() => {
                    if (window.__mcp && typeof window.__mcp.stopSession === 'function') {
                        window.__mcp.stopSession();
                    }
                })()
            """)
        except Exception:
            pass
    else:
        # Extension mode: release mic stream before navigating
        try:
            await _offscreen_eval('window.__tcReleaseMic ? window.__tcReleaseMic() : "no-op"')
        except Exception:
            pass

    page = _get_page_target()
    async with websockets.connect(page['webSocketDebuggerUrl'], open_timeout=5) as ws:
        await _cdp_send(ws, 'Page.navigate', {'url': 'about:blank'})

    # Restore OS audio devices
    was_routed = _audio_routed
    _restore_audio()

    # Reset Chrome's internal mic preference (persists across restarts,
    # overrides OS default — must be reset via the settings page)
    mic_result = 'skipped'
    if was_routed:
        mic_result = await _reset_chrome_mic()

    return f'Session ended. Audio restored. Chrome mic: {mic_result}'


@mcp.tool()
async def speak(text: str, use_say: bool = True) -> str:
    """
    Generate TTS and play it into the BlackHole virtual audio device so
    the extension's mic capture picks it up and sends it to Gemini.

    Requires: GLMCP_VOICE_SCRIPT env var set, audio routing configured.
    On macOS with SwitchAudioSource + BlackHole installed, audio routing
    is handled automatically on first call.

    Args:
        text: The utterance to speak.
        use_say: If True, use macOS `say` (offline, fast). If False, use
                 edge-tts (online, higher quality).
    """
    _ensure_audio_routed()

    if not VOICE_SCRIPT:
        return 'ERROR: GLMCP_VOICE_SCRIPT not set — speak tool unavailable'

    cmd = [sys.executable, VOICE_SCRIPT]
    if use_say:
        cmd.append('--say')
    cmd.append(text)

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    stdout, _ = await proc.communicate()
    output = stdout.decode().strip() if stdout else ''
    if proc.returncode != 0:
        return f'ERROR: voice script exited {proc.returncode}\n{output}'
    return f'Spoke: "{text}"\n{output}'


@mcp.tool()
async def listen(timeout: int = 60, baseline: int = -1) -> str:
    """
    Wait for a new user+model transcript pair in the transcript array.
    Polls every second until a new pair appears.

    Args:
        timeout: Max seconds to wait (default 60).
        baseline: Start looking for entries after this index.
                  Use -1 (default) to automatically use the current count
                  as baseline (i.e. only capture entries added from now on).

    Returns:
        The captured user and model transcript texts, plus latency.
    """
    try:
        t = _get_mcp_target()
    except RuntimeError as e:
        return f'ERROR: {e}'

    ws_url = t['webSocketDebuggerUrl']
    transcript_expr = 'window.__mcp.transcripts' if MODE == 'webapp' else f'window.{TRANSCRIPT_PROP}'

    # Auto-baseline: use current length
    if baseline < 0:
        async with websockets.connect(ws_url, open_timeout=5) as ws:
            result = await _cdp_send(ws, 'Runtime.evaluate', {
                'expression': f'({transcript_expr} || []).length',
                'returnByValue': True,
            })
            baseline = result.get('result', {}).get('value', 0)

    t_start = time.time()
    deadline = t_start + timeout
    entries = []

    async with websockets.connect(ws_url, open_timeout=5) as ws:
        while time.time() < deadline:
            result = await _cdp_send(ws, 'Runtime.evaluate', {
                'expression': f'JSON.stringify({transcript_expr} || [])',
                'returnByValue': True,
            })
            raw = result.get('result', {}).get('value')
            if raw:
                entries = json.loads(raw)
                new = entries[baseline:]
                user = next((e['text'] for e in new if e['role'] == 'user'), None)
                model = next((e['text'] for e in new if e['role'] == 'model'), None)
                if user and model:
                    elapsed = time.time() - t_start
                    return (
                        f'User  : {user}\n'
                        f'Model : {model}\n'
                        f'Elapsed: {elapsed:.1f}s'
                    )
            await asyncio.sleep(1)

    return (
        f'ERROR: No transcript within {timeout}s '
        f'(baseline={baseline}, current={len(entries)})'
    )


@mcp.tool()
async def get_transcripts(last_n: int = 10) -> str:
    """
    Return the last N transcript entries from the current session.

    Args:
        last_n: Number of entries to return (default 10, 0 = all).
    """
    try:
        t = _get_mcp_target()
    except RuntimeError as e:
        return f'No active session: {e}'

    transcript_expr = 'window.__mcp.transcripts' if MODE == 'webapp' else f'window.{TRANSCRIPT_PROP}'

    async with websockets.connect(t['webSocketDebuggerUrl'], open_timeout=5) as ws:
        result = await _cdp_send(ws, 'Runtime.evaluate', {
            'expression': f'JSON.stringify({transcript_expr} || [])',
            'returnByValue': True,
        })
    raw = result.get('result', {}).get('value', '[]')
    entries = json.loads(raw)
    if last_n > 0:
        entries = entries[-last_n:]
    if not entries:
        return 'No transcripts yet.'
    lines = []
    for e in entries:
        prefix = 'User ' if e['role'] == 'user' else 'Model'
        lines.append(f'{prefix}: {e["text"]}')
    return '\n'.join(lines)


@mcp.tool()
async def get_logs(last_n: int = 30) -> str:
    """
    Return recent logs. In extension mode, captures console logs from the
    offscreen hub document. In webapp mode, reads window.__mcp.logs if
    available, otherwise captures console logs from the page.

    Args:
        last_n: Number of most-recent log lines to return (default 30).
    """
    if MODE == 'webapp':
        # Try reading structured logs from the bridge first
        try:
            raw = await _page_eval('JSON.stringify(window.__mcp && window.__mcp.logs || null)')
            if raw and raw != 'null':
                log_entries = json.loads(raw)
                if log_entries:
                    lines = []
                    for entry in log_entries[-last_n:] if last_n > 0 else log_entries:
                        src = entry.get('source', '')
                        text = entry.get('text', '')
                        lines.append(f'[{src}] {text}' if src else text)
                    return '\n'.join(lines) if lines else 'No logs.'
        except Exception:
            pass

        # Fall back to console API on the page
        t = _get_page_target()
    else:
        t = _get_offscreen_target()
        if not t:
            return 'No offscreen target.'

    logs = []
    async with websockets.connect(t['webSocketDebuggerUrl'], open_timeout=5) as ws:
        await ws.send(json.dumps({'id': 1, 'method': 'Runtime.enable', 'params': {}}))
        deadline = time.time() + 3
        while time.time() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=0.5)
                msg = json.loads(raw)
                if msg.get('method') == 'Runtime.consoleAPICalled':
                    args = msg['params'].get('args', [])
                    text = ' '.join(a.get('value', '') for a in args if a.get('type') == 'string')
                    logs.append(text)
            except asyncio.TimeoutError:
                continue

    if not logs:
        return 'No logs.'
    if last_n > 0:
        logs = logs[-last_n:]
    return '\n'.join(logs)


@mcp.tool()
async def eval_page(expression: str) -> str:
    """
    Evaluate a JavaScript expression in the main page context.
    Useful for inspecting DOM state, checking overlay visibility, etc.

    Args:
        expression: JS expression to evaluate. Return value must be JSON-serializable.
    """
    try:
        result = await _page_eval(expression)
        return json.dumps(result, indent=2) if result is not None else 'undefined'
    except Exception as e:
        return f'ERROR: {e}'


@mcp.tool()
async def eval_offscreen(expression: str) -> str:
    """
    Evaluate a JavaScript expression in the offscreen hub document context
    (extension mode) or the main page context (webapp mode, same as eval_page).

    Args:
        expression: JS expression to evaluate. Return value must be JSON-serializable.
    """
    if MODE == 'webapp':
        # In webapp mode there's no offscreen doc — redirect to page eval
        try:
            result = await _page_eval(expression)
            return json.dumps(result, indent=2) if result is not None else 'undefined'
        except Exception as e:
            return f'ERROR: {e}'
    try:
        result = await _offscreen_eval(expression)
        return json.dumps(result, indent=2) if result is not None else 'undefined'
    except Exception as e:
        return f'ERROR: {e}'


@mcp.tool()
async def navigate(url: str) -> str:
    """
    Navigate the main page to a URL.

    Args:
        url: Full URL to navigate to.
    """
    page = _get_page_target()
    async with websockets.connect(page['webSocketDebuggerUrl'], open_timeout=5) as ws:
        await _cdp_send(ws, 'Page.navigate', {'url': url})
    await asyncio.sleep(2)
    return f'Navigated to {url}'


@mcp.tool()
async def reload_extension() -> str:
    """
    Reload the extension via chrome://extensions. Use after a build to
    pick up changes. Requires chrome://extensions to be open as a tab.
    Extension mode only.
    """
    if MODE == 'webapp':
        return 'Not available in webapp mode. Use navigate() to reload the page instead.'

    targets = _get_targets()
    ext_page = next(
        (t for t in targets if t.get('url', '').startswith('chrome://extensions')),
        None,
    )
    if not ext_page:
        return 'ERROR: chrome://extensions page not open. Navigate there first, then try again.'

    reload_js = f"""
        (() => {{
            const mgr = document.querySelector('extensions-manager');
            const itemList = mgr?.shadowRoot?.querySelector('extensions-item-list');
            const items = itemList?.shadowRoot?.querySelectorAll('extensions-item') ?? [];
            for (const item of items) {{
                const sr = item.shadowRoot;
                if (sr?.querySelector('#name')?.textContent?.trim() === '{EXTENSION_NAME}') {{
                    sr.querySelector('#dev-reload-button')?.click();
                    return 'reloaded';
                }}
            }}
            return 'not-found';
        }})()
    """
    async with websockets.connect(ext_page['webSocketDebuggerUrl'], open_timeout=5) as ws:
        result = await _cdp_eval(ws, reload_js)

    if result == 'reloaded':
        await asyncio.sleep(2)
        return 'Extension reloaded.'
    return f'ERROR: {result}'


@mcp.tool()
async def run_voice_test(
    utterance: str = 'what is this page about',
    url: str = DEFAULT_TEST_URL,
    expect: str = '',
    use_say: bool = True,
) -> str:
    """
    Run a full end-to-end voice test: navigate -> start session -> speak ->
    capture transcript -> assert. Measures response latency.

    Args:
        utterance: Text to speak to Gemini.
        url: Page URL to test on (default: configured default URL).
        expect: If set, assert that model response contains this text (case-insensitive).
        use_say: Use macOS say for TTS (offline, fast). Set False for edge-tts.
    """
    _ensure_audio_routed()

    if not VOICE_SCRIPT:
        return 'ERROR: GLMCP_VOICE_SCRIPT not set — voice test unavailable'

    # Navigate / reload
    page = _get_page_target()
    page_ws = page['webSocketDebuggerUrl']
    current_url = page.get('url', '')

    async with websockets.connect(page_ws, open_timeout=5) as ws:
        if current_url == url:
            await _cdp_send(ws, 'Page.reload', {})
        else:
            await _cdp_send(ws, 'Page.navigate', {'url': url})
    await asyncio.sleep(3)

    if MODE == 'webapp':
        # Wait for window.__mcp bridge (up to 15s)
        deadline = time.time() + 15
        async with websockets.connect(page_ws, open_timeout=5) as ws:
            while time.time() < deadline:
                val = await _cdp_eval(ws, '!!window.__mcp')
                if val:
                    break
                await asyncio.sleep(0.5)
            else:
                return 'ERROR: window.__mcp did not appear'

            # Start session if not already connected
            connected = await _cdp_eval(ws, '!!window.__mcp.isConnected')
            if not connected:
                has_start = await _cdp_eval(ws, 'typeof window.__mcp.startSession === "function"')
                if has_start:
                    await _cdp_eval(ws, 'window.__mcp.startSession()')
                # Wait for connection
                deadline2 = time.time() + 15
                while time.time() < deadline2:
                    connected = await _cdp_eval(ws, '!!window.__mcp.isConnected')
                    if connected:
                        break
                    await asyncio.sleep(0.5)
                if not connected:
                    return 'ERROR: Session did not connect within 15s'

        # Settle + baseline
        await asyncio.sleep(3)
        transcript_expr = 'window.__mcp.transcripts'
        mcp_ws_url = page_ws
    else:
        # Extension mode: wait for content script
        deadline = time.time() + 15
        async with websockets.connect(page_ws, open_timeout=5) as ws:
            while time.time() < deadline:
                val = await _cdp_eval(ws, f"!!document.querySelector('{SHADOW_HOST}')")
                if val:
                    break
                await asyncio.sleep(0.5)
            else:
                return 'ERROR: Content script did not appear'
            await _cdp_eval(ws, f"""
                (() => {{
                    const root = document.querySelector('{SHADOW_HOST}')?.shadowRoot;
                    root?.querySelector('{FAB_SEL}')?.click();
                }})()
            """)

        # Wait for offscreen (up to 15s)
        mcp_ws_url = None
        for _ in range(30):
            t = _get_offscreen_target()
            if t:
                mcp_ws_url = t['webSocketDebuggerUrl']
                break
            await asyncio.sleep(0.5)
        if not mcp_ws_url:
            return 'ERROR: Offscreen target did not appear'

        # Settle
        await asyncio.sleep(3)
        transcript_expr = f'window.{TRANSCRIPT_PROP}'

    # Get baseline
    async with websockets.connect(mcp_ws_url, open_timeout=5) as ws:
        r = await _cdp_send(ws, 'Runtime.evaluate', {
            'expression': f'({transcript_expr} || []).length',
            'returnByValue': True,
        })
        baseline = r.get('result', {}).get('value', 0)

    # Speak
    cmd = [sys.executable, VOICE_SCRIPT]
    if use_say:
        cmd.append('--say')
    cmd.append(utterance)
    t_speak_start = time.time()
    proc = await asyncio.create_subprocess_exec(*cmd,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    stdout, _ = await proc.communicate()
    t_speech_done = time.time()
    speech_duration = t_speech_done - t_speak_start

    if proc.returncode != 0:
        return f'ERROR: TTS failed\n{stdout.decode()}'

    # Poll for transcript
    deadline = time.time() + 90
    user_text = model_text = None
    async with websockets.connect(mcp_ws_url, open_timeout=5) as ws:
        while time.time() < deadline:
            r = await _cdp_send(ws, 'Runtime.evaluate', {
                'expression': f'JSON.stringify({transcript_expr} || [])',
                'returnByValue': True,
            })
            raw = r.get('result', {}).get('value')
            if raw:
                entries = json.loads(raw)
                new = entries[baseline:]
                user_text = next((e['text'] for e in new if e['role'] == 'user'), None)
                model_text = next((e['text'] for e in new if e['role'] == 'model'), None)
                if user_text and model_text:
                    break
            await asyncio.sleep(1)

    t_done = time.time()
    response_latency = t_done - t_speech_done

    if not (user_text and model_text):
        return f'ERROR: Transcript not captured within 90s (baseline={baseline})'

    lines = [
        f'Test: "{utterance}"',
        '-' * 60,
        f'  User  : {user_text}',
        f'  Model : {model_text}',
        '-' * 60,
        f'  Speech duration : {speech_duration:.1f}s',
        f'  Response latency: {response_latency:.1f}s',
        '-' * 60,
    ]

    if expect:
        if expect.lower() in model_text.lower():
            lines.append(f'PASS  (found "{expect}" in model response)')
        else:
            lines.append(f'FAIL  ("{expect}" not found in model response)')
    else:
        lines.append('PASS  (no assertion)')

    return '\n'.join(lines)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    mcp.run(transport='stdio')
