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
//   server -> hub:
//     {"type":"register","session_key":"..."}        (after the MCP handshake)
//
// There is deliberately no reply tool. Responses are read off the session
// transcript by the bridge when the turn ends, so the platform and the local
// session show the same conversation. Making the response a tool call meant
// it only arrived when the model chose to call it, which for plain chat it
// frequently did not, and the two surfaces drifted apart.
//
// REGISTRATION TIMING IS LOAD-BEARING. The hub takes `register` as its cue
// that the session can receive channel notifications, so we must not send it
// until the client is actually listening. Registering when the process boots
// (which is what we used to do) opens a ~1.5s window where the bridge pushes
// an event, the notification hits stdio with no handler on the other side,
// and the message is discarded with no error anywhere. We therefore register
// from `oninitialized` plus a short settle delay; the bridge's delivery-ack
// retry covers whatever residue is left.
//
// If the hub socket is absent or unset, the server still loads as a valid
// (inert) channel so the session starts cleanly — it just never receives or
// delivers events. This keeps a session launched without a bridge usable.

import net from 'node:net'
import { Server } from '@modelcontextprotocol/sdk/server/index.js'
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js'
import { ListToolsRequestSchema } from '@modelcontextprotocol/sdk/types.js'

const SOCK = process.env.SOCIETY_AI_CHANNEL_SOCK || ''
const SESSION_KEY = process.env.SOCIETY_AI_SESSION_KEY || ''
const RECONNECT_MS = 2000
// The client wires up its channel-notification handler shortly *after* it
// sends `notifications/initialized` (observed at 110-250ms). Waiting this out
// before we register keeps the bridge from pushing into that gap.
const SETTLE_MS = Number(process.env.SOCIETY_AI_CHANNEL_SETTLE_MS || 750)

// Diagnostics go to stderr, which Claude Code captures per session under
// ~/Library/Caches/claude-cli-nodejs/<project>/mcp-logs-society-ai-channel/.
// A silently dropped notification is the one failure this server can have,
// so it must never be swallowed.
function log(...args) {
  try {
    process.stderr.write(`[society-ai-channel] ${args.join(' ')}\n`)
  } catch {
    /* stderr gone; nothing useful left to do */
  }
}

// Named distinctly from the `society-ai` tool MCP server (mcp_server.py),
// which is registered separately at user scope. No collision: that one
// carries the mcp__society-ai__* platform actions; this one carries none.
const mcp = new Server(
  { name: 'society-ai-channel', version: '0.9.0' },
  {
    capabilities: {
      experimental: { 'claude/channel': {} },
      tools: {},
    },
    instructions:
      'Messages from Society AI (your platform) arrive as ' +
      '<channel source="society-ai-channel" event_id="..." kind="...">. Each is a ' +
      'task assignment, chat message, review request, or supervisor note. ' +
      'Act on it and answer in this session as you normally would. Your ' +
      'answer is sent back to the platform automatically when your turn ' +
      'ends, so whoever is watching from the web app reads exactly what you ' +
      'write here. There is no reply tool to call and nothing to send by ' +
      'hand; just make sure the answer is in what you say, not only in the ' +
      'tools you ran.',
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
    try {
      await mcp.notification({
        method: 'notifications/claude/channel',
        params: { content: msg.content, meta },
      })
    } catch (err) {
      // The bridge cannot see this, so say it where the session's MCP log
      // will keep it. Delivery is confirmed bridge-side by watching the
      // transcript, and a failure here means that check will retry.
      log(`notification failed for event_id=${meta.event_id}:`, err?.message || err)
    }
  }
}

// This server exposes no tools. It is inbound-only: events come in, and the
// session's answer goes back via its transcript, not via a call the model has
// to remember to make. The capability stays declared so the client's tool
// listing succeeds.
mcp.setRequestHandler(ListToolsRequestSchema, async () => ({ tools: [] }))

// Register with the hub only once the client has completed the MCP handshake
// and had a moment to attach its channel handler. Registering earlier tells
// the bridge we can receive events while we still cannot, and the event it
// pushes in that window is lost silently. See the header note.
let hubStarted = false
function startHub(why) {
  if (hubStarted) return
  hubStarted = true
  log(`registering with hub (${why})`)
  connectHub()
}

mcp.oninitialized = () => setTimeout(() => startHub('client initialized'), SETTLE_MS)

// Safety net: a client that never sends notifications/initialized would
// otherwise leave this session permanently undeliverable. Fall back to the
// old boot-time behaviour rather than hanging, and say so in the log.
setTimeout(() => startHub('handshake timeout; registering unconfirmed'), 15000)

await mcp.connect(new StdioServerTransport())
