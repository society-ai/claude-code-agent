#!/usr/bin/env node
// Society AI channel server (production).
//
// One instance per Claude Code session — spawned by Claude Code as the
// channel MCP subprocess (stdio). It is a DUMB PIPE between the session and
// the bridge daemon: it carries whatever the bridge sends in, and whatever
// the session replies back out. No policy, no platform logic, no decisions
// live here (the IP boundary: transport only).
//
// Transport to the bridge: a persistent line-delimited JSON connection over
// the bridge's per-machine "channel hub" Unix socket. The session this
// server belongs to is identified by SOCIETY_AI_SESSION_KEY, set by the
// bridge's SessionManager when it launches the session.
//
//   env in:
//     SOCIETY_AI_CHANNEL_SOCK  hub socket path (required to be useful)
//     SOCIETY_AI_SESSION_KEY   opaque session/work-item key (required)
//
//   hub -> server (pushed into the session):
//     {"type":"event","content":"...","meta":{...}}
//
//   server -> hub (from the session's reply tool):
//     {"type":"reply","session_key":"...","event_id":"...","text":"..."}
//     {"type":"register","session_key":"..."}        (on connect)
//
// If the hub socket is absent or unset, the server still loads as a valid
// (inert) channel so the session starts cleanly — it just never receives or
// delivers events. This keeps a session launched without a bridge usable.

import net from 'node:net'
import { Server } from '@modelcontextprotocol/sdk/server/index.js'
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js'
import {
  ListToolsRequestSchema,
  CallToolRequestSchema,
} from '@modelcontextprotocol/sdk/types.js'

const SOCK = process.env.SOCIETY_AI_CHANNEL_SOCK || ''
const SESSION_KEY = process.env.SOCIETY_AI_SESSION_KEY || ''
const RECONNECT_MS = 2000

// Named distinctly from the `society-ai` tool MCP server (mcp_server.py),
// which is registered separately at user scope. No collision: the tools are
// mcp__society-ai__* (platform actions) vs mcp__society-ai-channel__reply.
const mcp = new Server(
  { name: 'society-ai-channel', version: '0.7.0' },
  {
    capabilities: {
      experimental: { 'claude/channel': {} },
      tools: {},
    },
    instructions:
      'Messages from Society AI (your platform) arrive as ' +
      '<channel source="society-ai-channel" event_id="..." kind="...">. Each is a ' +
      'task assignment, chat message, review request, or supervisor note. ' +
      'Act on it, then call the society-ai reply tool with the event_id from ' +
      'the tag and a short text summarizing your response or result. Replies ' +
      'route back to the platform and to whoever is watching.',
  },
)

// --- bridge hub connection (persistent, auto-reconnecting) ------------------

let hub = null // net.Socket | null
let hubBuf = ''

function connectHub() {
  if (!SOCK || !SESSION_KEY) return // inert mode
  const sock = net.createConnection(SOCK)
  sock.setEncoding('utf8')

  sock.on('connect', () => {
    hub = sock
    send({ type: 'register', session_key: SESSION_KEY })
  })

  sock.on('data', (chunk) => {
    hubBuf += chunk
    let nl
    while ((nl = hubBuf.indexOf('\n')) >= 0) {
      const line = hubBuf.slice(0, nl)
      hubBuf = hubBuf.slice(nl + 1)
      if (line.trim()) onHubLine(line)
    }
  })

  const drop = () => {
    if (hub === sock) hub = null
    hubBuf = ''
    setTimeout(connectHub, RECONNECT_MS)
  }
  sock.on('error', drop)
  sock.on('close', drop)
}

function send(obj) {
  if (hub && !hub.destroyed) {
    try {
      hub.write(JSON.stringify(obj) + '\n')
    } catch {
      /* dropped; reconnect loop will re-register */
    }
  }
}

async function onHubLine(line) {
  let msg
  try {
    msg = JSON.parse(line)
  } catch {
    return
  }
  if (msg && msg.type === 'event' && typeof msg.content === 'string') {
    const meta = { event_id: '', ...(msg.meta || {}) }
    // Only identifier-safe meta keys survive as channel tag attributes;
    // the bridge is responsible for sending clean keys.
    await mcp
      .notification({
        method: 'notifications/claude/channel',
        params: { content: msg.content, meta },
      })
      .catch(() => {})
  }
}

// --- reply tool: the session's voice back to the platform -------------------

mcp.setRequestHandler(ListToolsRequestSchema, async () => ({
  tools: [
    {
      name: 'reply',
      description:
        'Send a response back to Society AI for a channel event. Pass the ' +
        'event_id from the <channel> tag you are responding to.',
      inputSchema: {
        type: 'object',
        properties: {
          event_id: {
            type: 'string',
            description: 'The event_id attribute from the channel tag',
          },
          text: { type: 'string', description: 'Your response or result summary' },
        },
        required: ['event_id', 'text'],
      },
    },
  ],
}))

mcp.setRequestHandler(CallToolRequestSchema, async (req) => {
  if (req.params.name === 'reply') {
    const { event_id, text } = req.params.arguments || {}
    send({
      type: 'reply',
      session_key: SESSION_KEY,
      event_id: String(event_id || ''),
      text: String(text || ''),
    })
    return { content: [{ type: 'text', text: 'sent' }] }
  }
  throw new Error(`unknown tool: ${req.params.name}`)
})

await mcp.connect(new StdioServerTransport())
connectHub()
