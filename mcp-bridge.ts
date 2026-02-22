/**
 * MCP Bridge — exposes React state to window.__mcp for CDP-based MCP tools.
 *
 * Usage in a React component or hook:
 *
 *   useEffect(() => {
 *     installMCPBridge({
 *       transcripts: transcriptHistory,
 *       isConnected,
 *       startSession: () => startSession(),
 *       stopSession: () => stopSession(),
 *     });
 *     return () => removeMCPBridge();
 *   }, [transcriptHistory, isConnected]);
 */

export interface MCPBridgeState {
  /** Conversation transcript entries with role and text. */
  transcripts: Array<{ role: 'user' | 'model'; text: string }>;
  /** Whether the Gemini Live session is currently connected. */
  isConnected: boolean;
  /** Optional: call to start a session programmatically. */
  startSession?: () => void;
  /** Optional: call to stop the session programmatically. */
  stopSession?: () => void;
  /** Optional: current session state string (e.g. 'CONNECTED', 'CONNECTING'). */
  sessionState?: string;
  /** Optional: structured log entries for get_logs tool. */
  logs?: Array<{ source: string; text: string }>;
}

/** Install or update the MCP bridge on window. */
export function installMCPBridge(state: MCPBridgeState): void {
  (window as any).__mcp = state;
}

/** Remove the MCP bridge from window (e.g. on unmount). */
export function removeMCPBridge(): void {
  delete (window as any).__mcp;
}
