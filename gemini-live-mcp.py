#!/usr/bin/env python3
"""
gemini-live-mcp: Generic MCP Server for Gemini Live Chrome Extensions

Exposes MCP tools for interfacing with and debugging live Gemini sessions
running inside any Gemini Live Chrome extension. All extension-specific
config is provided via environment variables (GLMCP_*).

Prerequisites:
  1. Chrome running with --remote-debugging-port=9222
  2. Target extension loaded (unpacked from its build output)
  3. For voice tools: GLMCP_VOICE_SCRIPT set
     (audio routing auto-configured on macOS with SwitchAudioSource + BlackHole)

Required env vars:
  GLMCP_SHADOW_HOST     - Shadow DOM host selector (e.g. #my-ext-root)
  GLMCP_FAB_SELECTOR    - CSS selector for the start/FAB button
  GLMCP_CLOSE_SELECTOR  - CSS selector for the close/stop button
  GLMCP_EXTENSION_NAME  - Extension name as shown in chrome://extensions

Optional env vars:
  GLMCP_CDP_URL         - Chrome DevTools Protocol URL (default: http://127.0.0.1:9222)
  GLMCP_TRANSCRIPT_PROP - Window property for transcript array (default: __tcTranscripts)
  GLMCP_SW_URL_SUFFIX   - Service worker URL suffix (default: /background.js)
  GLMCP_SW_URL_EXCLUDE  - Substring to exclude from SW URL matching (default: /build/)
  GLMCP_OFFSCREEN_URL   - Substring to match offscreen doc URL (default: offscreen.html)
  GLMCP_DEFAULT_URL     - Default test URL (default: Wikipedia Tyrannosaurus)
  GLMCP_VOICE_SCRIPT    - Path to TTS script for speak tool (optional)

Add to .mcp.json:
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
CDP_URL = os.environ.get('GLMCP_CDP_URL', 'http://127.0.0.1:9222')
SHADOW_HOST = os.environ['GLMCP_SHADOW_HOST']
FAB_SEL = os.environ['GLMCP_FAB_SELECTOR']
CLOSE_BTN_SEL = os.environ['GLMCP_CLOSE_SELECTOR']
EXTENSION_NAME = os.environ['GLMCP_EXTENSION_NAME']
TRANSCRIPT_PROP = os.environ.get('GLMCP_TRANSCRIPT_PROP', '__tcTranscripts')
SW_URL_SUFFIX = os.environ.get('GLMCP_SW_URL_SUFFIX', '/background.js')
SW_URL_EXCLUDE = os.environ.get('GLMCP_SW_URL_EXCLUDE', '/build/')
OFFSCREEN_URL = os.environ.get('GLMCP_OFFSCREEN_URL', 'offscreen.html')
DEFAULT_TEST_URL = os.environ.get('GLMCP_DEFAULT_URL', 'https://en.wikipedia.org/wiki/Tyrannosaurus')
VOICE_SCRIPT = os.environ.get('GLMCP_VOICE_SCRIPT', '')

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
    """Restore original audio devices. Called by stop_session and atexit."""
    global _audio_routed
    if _original_input:
        _run_switch(['-s', _original_input, '-t', 'input'])
    if _original_output:
        _run_switch(['-s', _original_output, '-t', 'output'])
    _audio_routed = False


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


# ---------------------------------------------------------------------------
# MCP tools
# ---------------------------------------------------------------------------

@mcp.tool()
async def get_session_state() -> str:
    """
    Returns the current state of the Gemini Live session:
    extension ID, page URL, whether the session is active (close button
    visible vs FAB visible), offscreen doc status, and transcript count.
    """
    try:
        ext_id, _ = _detect_ext_id()
    except RuntimeError as e:
        return f'ERROR: {e}'

    page = _get_page_target()
    page_url = page.get('url', 'unknown')

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
    session by clicking the FAB button. Always reloads to guarantee a fresh
    content script and hub port connection.

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

    # Wait for content script (up to 15s)
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
    End the current Gemini Live session by navigating to about:blank.
    Uses navigation rather than clicking the close button — clicking the
    close button breaks the hub port and prevents future sessions from
    starting on the same page.
    """
    page = _get_page_target()
    async with websockets.connect(page['webSocketDebuggerUrl'], open_timeout=5) as ws:
        await _cdp_send(ws, 'Page.navigate', {'url': 'about:blank'})
    _restore_audio()
    return 'Navigated to about:blank — session ended. Audio restored.'


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
    Polls the offscreen doc every second until a new pair appears.

    Args:
        timeout: Max seconds to wait (default 60).
        baseline: Start looking for entries after this index.
                  Use -1 (default) to automatically use the current count
                  as baseline (i.e. only capture entries added from now on).

    Returns:
        The captured user and model transcript texts, plus latency.
    """
    t = _get_offscreen_target()
    if not t:
        return 'ERROR: No offscreen target — start a session first.'

    ws_url = t['webSocketDebuggerUrl']

    # Auto-baseline: use current length
    if baseline < 0:
        async with websockets.connect(ws_url, open_timeout=5) as ws:
            result = await _cdp_send(ws, 'Runtime.evaluate', {
                'expression': f'(window.{TRANSCRIPT_PROP} || []).length',
                'returnByValue': True,
            })
            baseline = result.get('result', {}).get('value', 0)

    t_start = time.time()
    deadline = t_start + timeout
    entries = []

    async with websockets.connect(ws_url, open_timeout=5) as ws:
        while time.time() < deadline:
            result = await _cdp_send(ws, 'Runtime.evaluate', {
                'expression': f'JSON.stringify(window.{TRANSCRIPT_PROP} || [])',
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
    t = _get_offscreen_target()
    if not t:
        return 'No offscreen target — no active session.'

    async with websockets.connect(t['webSocketDebuggerUrl'], open_timeout=5) as ws:
        result = await _cdp_send(ws, 'Runtime.evaluate', {
            'expression': f'JSON.stringify(window.{TRANSCRIPT_PROP} || [])',
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
async def get_offscreen_logs(last_n: int = 30) -> str:
    """
    Return recent console logs from the offscreen hub document.
    These are debug logs from the offscreen doc — session lifecycle,
    Gemini connection status, tool calls, errors, etc.

    Args:
        last_n: Number of most-recent log lines to return (default 30).
    """
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
    Evaluate a JavaScript expression in the offscreen hub document context.
    Useful for inspecting session state, audio context, Gemini connection, etc.

    Args:
        expression: JS expression to evaluate. Return value must be JSON-serializable.
    """
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
    """
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

    # Navigate / reload for fresh content script
    page = _get_page_target()
    page_ws = page['webSocketDebuggerUrl']
    current_url = page.get('url', '')

    async with websockets.connect(page_ws, open_timeout=5) as ws:
        if current_url == url:
            await _cdp_send(ws, 'Page.reload', {})
        else:
            await _cdp_send(ws, 'Page.navigate', {'url': url})
    await asyncio.sleep(3)

    # Wait for content script
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
    offscreen_ws_url = None
    for _ in range(30):
        t = _get_offscreen_target()
        if t:
            offscreen_ws_url = t['webSocketDebuggerUrl']
            break
        await asyncio.sleep(0.5)
    if not offscreen_ws_url:
        return 'ERROR: Offscreen target did not appear'

    # Settle + baseline
    await asyncio.sleep(3)
    async with websockets.connect(offscreen_ws_url, open_timeout=5) as ws:
        r = await _cdp_send(ws, 'Runtime.evaluate', {
            'expression': f'(window.{TRANSCRIPT_PROP} || []).length',
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
    async with websockets.connect(offscreen_ws_url, open_timeout=5) as ws:
        while time.time() < deadline:
            r = await _cdp_send(ws, 'Runtime.evaluate', {
                'expression': f'JSON.stringify(window.{TRANSCRIPT_PROP} || [])',
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
