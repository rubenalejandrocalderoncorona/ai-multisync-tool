'use strict';
/**
 * Minimal MCP client over SSE (what the cAImanDesk FastMCP server speaks). One short-lived connection
 * per batch of calls: connect, call tools, close. No state is kept between runs.
 */

/** Tool results arrive as text content (JSON) and/or structuredContent; normalise to plain JS. */
function unwrap(result) {
  if (result.isError) {
    const msg = (result.content || []).map((c) => c.text).filter(Boolean).join(' ') || 'tool error';
    throw new Error(`MCP tool error: ${msg.slice(0, 300)}`);
  }
  const sc = result.structuredContent;
  if (sc !== undefined && sc !== null) return sc.result !== undefined && Object.keys(sc).length === 1 ? sc.result : sc;
  const parts = (result.content || []).filter((c) => c.type === 'text').map((c) => c.text);
  const parsed = parts.map((t) => { try { return JSON.parse(t); } catch { return t; } });
  if (parsed.length === 0) return null;
  return parsed.length === 1 ? parsed[0] : parsed; // a list result may arrive as one item per element
}

/**
 * @param {string} url      SSE endpoint, e.g. http://vikunja-mcp.caimanlabs-operations.svc.cluster.local:8000/sse
 * @param {(call:(name:string,args?:object)=>Promise<any>)=>Promise<T>} fn
 * @param {{headers?:object, timeoutMs?:number}} [opts]
 */
async function withMcp(url, fn, { headers = {}, timeoutMs = 30000 } = {}) {
  const { Client } = require('@modelcontextprotocol/sdk/client/index.js');
  const { SSEClientTransport } = require('@modelcontextprotocol/sdk/client/sse.js');
  const hasHeaders = Object.keys(headers).length > 0;
  const transport = new SSEClientTransport(new URL(url), hasHeaders ? { requestInit: { headers } } : undefined);
  const client = new Client({ name: 'ai-multisync-tool', version: '0.3.0' });
  // The SSE client reconnects in the background after a failed connect, which would keep the process alive
  // forever. Bound the connect and always close the transport on failure.
  let timer;
  try {
    await Promise.race([
      client.connect(transport),
      new Promise((_, reject) => { timer = setTimeout(() => reject(new Error(`MCP connect timed out after ${timeoutMs}ms: ${url}`)), timeoutMs); }),
    ]);
  } catch (err) {
    await transport.close().catch(() => {});
    throw err;
  } finally {
    clearTimeout(timer);
  }
  try {
    return await fn(async (name, args = {}) => unwrap(await client.callTool({ name, arguments: args }, undefined, { timeout: timeoutMs })));
  } finally {
    await client.close().catch(() => {});
  }
}

module.exports = { withMcp, unwrap };
