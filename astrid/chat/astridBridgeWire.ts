/** Public, non-secret parts of the Astrid/Astrid local bridge handshake. */
export const ASTRID_BRIDGE_PROTOCOL_VERSION = 'v1';
export const ASTRID_BRIDGE_PROTOCOL_HEADER = 'X-Astrid-Bridge-Version';
/** Shared end-to-end deadline for browser requests and both Vite proxy sockets.
 *
 * Task detail reads include the immutable render snapshot and can legitimately
 * take several seconds while the local bridge serializes it. A ten-second
 * deadline made an otherwise healthy render look failed to the editor.
 */
export const ASTRID_BRIDGE_REQUEST_TIMEOUT_MS = 30_000;
/** ACP control RPC deadline. Prompts wait for agent completion without a wall-clock deadline. */
export const ASTRID_ACP_REQUEST_TIMEOUT_MS = 5 * 60_000;
