
/*
SHORO Bitget Trade Manager
Discord Secure Interactions - Version 3
Manual Trade Tracking and Exit Journal
*/

function hexToBytes(hex) {
if (!hex || hex.length % 2 !== 0 ||
!/^[0-9a-f]+$/i.test(hex)) {
throw new Error("Invalid hex");
}
return Uint8Array.from(
hex.match(/.{2}/g),
b => parseInt(b, 16)
);
}

async function verifyDiscord(req, keyHex) {
try {
const sig = req.headers.get("X-Signature-Ed25519");
const ts = req.headers.get("X-Signature-Timestamp");
if (!sig || !ts || !keyHex) return false;
const n = Number(ts);
if (!Number.isFinite(n) ||
Math.abs(Date.now() / 1000 - n) > 300) {
return false;
}
const key = await crypto.subtle.importKey(
"raw", hexToBytes(keyHex),
{ name: "Ed25519" }, false, ["verify"]
);
const body = await req.clone().text();
return crypto.subtle.verify(
"Ed25519", key, hexToBytes(sig),
new TextEncoder().encode(ts + body)
);
} catch {
return false;
}
}

function respond(data, status = 200) {
return new Response(JSON.stringify(data), {
status,
headers: { "Content-Type": "application/json" }
});
}

function privateReply(message) {
return {
type: 4,
data: { content: message, flags: 64 }
};
}

function opt(options, name) {
return options?.find(x => x.name === name)?.value;
}

function userId(i) {
return i.member?.user?.id || i.user?.id || "";
}

function buttons(id, disabled = false) {
return [{
type: 1,
components: [
{
type: 2, style: 3,
label: "Trade Taken",
custom_id: `trade_taken:${id}`,
disabled
},
{
type: 2, style: 2,
label: "Skip Trade",
custom_id: `skip_trade:${id}`,
disabled
}
]
}];
}

function closeButton(id) {
return [{
type: 1,
components: [{
type: 2, style: 1,
label: "Close Trade",
custom_id: `close_trade:${id}`
}]
}];
}

function price(v) {
return v == null ? "—" : String(v);
}

function validPrices(side, entry, sl, t1, t2, t3) {
if (![entry, sl, t1, t2].every(
v => typeof v === "number" &&
Number.isFinite(v) && v > 0
)) return false;

if (t3 != null &&
(typeof t3 !== "number" ||
!Number.isFinite(t3) || t3 <= 0)) {
return false;
}

if (side === "BUY") {
return sl < entry && entry < t1 &&
t1 < t2 && (t3 == null || t2 < t3);
}
if (side === "SELL") {
return sl > entry && entry > t1 &&
t1 > t2 && (t3 == null || t2 > t3);
}
return false;
}

async function newTrade(i, env) {
const o = i.data.options || [];
const pair = String(opt(o, "pair") || "")
.trim().toUpperCase();
const side = String(opt(o, "side") || "");
const entry = opt(o, "entry");
const sl = opt(o, "stop_loss");
const t1 = opt(o, "tp1");
const t2 = opt(o, "tp2");
const t3 = opt(o, "tp3") ?? null;

if (!/^[A-Z0-9_-]{2,30}$/.test(pair) ||
!validPrices(side, entry, sl, t1, t2, t3)) {
return privateReply(
"Invalid trade details. Check pair, side, Entry, SL and TP order."
);
}

const id = "SH-" +
Date.now().toString(36).toUpperCase() +
"-" + crypto.randomUUID().slice(0, 6).toUpperCase();

const now = new Date().toISOString();

await env.SHORO_DB.prepare(`
INSERT INTO shoro_trades
(trade_id, pair, side, entry_price,
stop_loss, tp1, tp2, tp3, status,
signal_time)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'SIGNAL', ?)
`).bind(
id, pair, side, entry, sl, t1, t2, t3, now
).run();

return {
type: 4,
data: {
content: `SHORO Signal: **${side} ${pair}**`,
embeds: [{
title: `${side} — ${pair}`,
color: side === "BUY" ? 3066993 : 15158332,
fields: [
{ name: "Trade ID", value: id },
{ name: "Entry", value: price(entry), inline: true },
{ name: "Stop Loss", value: price(sl), inline: true },
{ name: "TP1", value: price(t1), inline: true },
{ name: "TP2", value: price(t2), inline: true },
{ name: "TP3", value: price(t3), inline: true },
{ name: "Status", value: "SIGNAL" }
],
footer: { text: "SHORO Bitget Trade Manager V3" },
timestamp: now
}],
components: buttons(id)
}
};
}

async function decision(i, env, action, id) {
const uid = userId(i);
if (!uid) return privateReply("User not found.");

const trade = await env.SHORO_DB.prepare(`
SELECT * FROM shoro_trades WHERE trade_id = ?
`).bind(id).first();

if (!trade) return privateReply("Trade not found.");
if (trade.status !== "SIGNAL") {
return privateReply(
`Trade already handled: ${trade.status}`
);
}

const status = action === "trade_taken"
? "ACTIVE" : "SKIPPED";
const now = new Date().toISOString();

const result = await env.SHORO_DB.prepare(`
UPDATE shoro_trades
SET status = ?, discord_user_id = ?,
taken_time = CASE
WHEN ? = 'ACTIVE' THEN ?
ELSE taken_time
END
WHERE trade_id = ? AND status = 'SIGNAL'
`).bind(status, uid, status, now, id).run();

if (result.meta?.changes !== 1) {
return privateReply("Trade already handled.");
}

return {
type: 7,
data: {
content: `Trade **${id}** marked **${status}** by <@${uid}>.`,
embeds: i.message?.embeds || [],
components: status === "ACTIVE"
? closeButton(id) : buttons(id, true)
}
};
}

function exitModal(id) {
return {
type: 9,
data: {
custom_id: `exit_modal:${id}`,
title: "SHORO Close Trade",
components: [
{
type: 1,
components: [{
type: 4,
custom_id: "exit_reason",
style: 1,
label: "Reason: TP, SL, EARLY or MANUAL",
placeholder: "TP",
required: true,
min_length: 2,
max_length: 10
}]
},
{
type: 1,
components: [{
type: 4,
custom_id: "exit_price",
style: 1,
label: "Actual Exit Price",
placeholder: "Enter actual closing price",
required: true,
max_length: 32
}]
}
]
}
};
}

function modalValue(i, name) {
for (const row of i.data.components || []) {
for (const c of row.components || []) {
if (c.custom_id === name) return c.value;
}
}
return null;
}

async function closeTrade(env, id, uid, reason, exitPrice) {
if (!["TP", "SL", "EARLY", "MANUAL"].includes(reason)) {
return { error: "Reason must be TP, SL, EARLY or MANUAL." };
}

if (!Number.isFinite(exitPrice) || exitPrice <= 0) {
return { error: "Exit price must be a positive number." };
}

const trade = await env.SHORO_DB.prepare(`
SELECT * FROM shoro_trades WHERE trade_id = ?
`).bind(id).first();

if (!trade) return { error: "Trade not found." };

if (trade.discord_user_id !== uid) {
return { error: "Only the user who took this trade can close it." };
}

if (trade.status !== "ACTIVE") {
return { error: `Trade is ${trade.status}, not ACTIVE.` };
}

const now = new Date().toISOString();

const result = await env.SHORO_DB.prepare(`
UPDATE shoro_trades
SET status = 'CLOSED',
exit_reason = ?,
exit_price = ?,
exit_time = ?
WHERE trade_id = ?
AND discord_user_id = ?
AND status = 'ACTIVE'
`).bind(reason, exitPrice, now, id, uid).run();

if (result.meta?.changes !== 1) {
return { error: "Trade was already updated." };
}

return { trade, reason, exitPrice };
}

function closeSummary(result, id) {
const t = result.trade;
const movement = t.side === "BUY"
? (result.exitPrice - t.entry_price) / t.entry_price
: (t.entry_price - result.exitPrice) / t.entry_price;

const percent = Number.isFinite(movement)
? `${(movement * 100).toFixed(3)}%`
: "N/A";

return [
"**SHORO TRADE CLOSED**",
`Trade ID: ${id}`,
`Pair: ${t.pair} | Side: ${t.side}`,
`Entry: ${price(t.entry_price)}`,
`Exit: ${price(result.exitPrice)}`,
`Reason: ${result.reason}`,
`Price movement: ${percent}`,
"Status: CLOSED",
"_Price movement excludes leverage, fees and funding._"
].join("\n");
}

async function history(i, env) {
const uid = userId(i);
const status = opt(i.data.options, "status");

let query = `
SELECT trade_id, pair, side, status,
entry_price, exit_price, exit_reason
FROM shoro_trades
WHERE discord_user_id = ?
`;
const args = [uid];

if (status) {
query += " AND status = ?";
args.push(status);
}

query += " ORDER BY created_at DESC, rowid DESC LIMIT 10";

const data = await env.SHORO_DB.prepare(query)
.bind(...args).all();

if (!data.results?.length) {
return privateReply("No matching trades found.");
}

const lines = data.results.map(t => {
const ending = t.status === "CLOSED"
? ` | ${t.exit_reason} @ ${price(t.exit_price)}`
: "";
return `**${t.trade_id}**\n${t.pair} ${t.side} | ${t.status} | Entry ${price(t.entry_price)}${ending}`;
});

return privateReply(
"**SHORO Trade History (latest 10)**\n\n" +
lines.join("\n\n")
);
}

function commands() {
return [
{
name: "trade",
description: "Create a SHORO trade signal",
type: 1,
options: [
{
name: "pair", type: 3, required: true,
description: "Example BTCUSDT"
},
{
name: "side", type: 3, required: true,
description: "BUY or SELL",
choices: [
{ name: "BUY", value: "BUY" },
{ name: "SELL", value: "SELL" }
]
},
{
name: "entry", type: 10, required: true,
description: "Entry price"
},
{
name: "stop_loss", type: 10, required: true,
description: "Stop loss"
},
{
name: "tp1", type: 10, required: true,
description: "First target"
},
{
name: "tp2", type: 10, required: true,
description: "Second target"
},
{
name: "tp3", type: 10, required: false,
description: "Optional third target"
}
]
},
{
name: "close",
description: "Close your active SHORO trade",
type: 1,
options: [
{
name: "trade_id", type: 3, required: true,
description: "Trade ID from signal"
},
{
name: "reason", type: 3, required: true,
description: "Exit reason",
choices: [
{ name: "TP Hit", value: "TP" },
{ name: "SL Hit", value: "SL" },
{ name: "Early Exit", value: "EARLY" },
{ name: "Manual Close", value: "MANUAL" }
]
},
{
name: "exit_price", type: 10, required: true,
description: "Actual exit price"
}
]
},
{
name: "history",
description: "View your SHORO trade history",
type: 1,
options: [{
name: "status", type: 3, required: false,
description: "Filter by status",
choices: [
{ name: "ACTIVE", value: "ACTIVE" },
{ name: "CLOSED", value: "CLOSED" },
{ name: "SKIPPED", value: "SKIPPED" }
]
}]
}
];
}

async function register(env) {
if (!env.SHORO_DISCORD_APPLICATION_ID ||
!env.SHORO_DISCORD_BOT_TOKEN) {
throw new Error("Discord credentials missing");
}

const r = await fetch(
`https://discord.com/api/v10/applications/${env.SHORO_DISCORD_APPLICATION_ID}/commands`,
{
method: "PUT",
headers: {
Authorization: `Bot ${env.SHORO_DISCORD_BOT_TOKEN}`,
"Content-Type": "application/json"
},
body: JSON.stringify(commands())
}
);

if (!r.ok) {
throw new Error(`Discord API ${r.status}: ${await r.text()}`);
}
}

export default {
async fetch(request, env) {
const url = new URL(request.url);

if (request.method === "GET" &&
url.pathname === "/") {
return new Response(
"SHORO Bitget Trade Manager V3 is running"
);
}

if (request.method === "GET" &&
url.pathname === "/register") {
const supplied = url.searchParams.get("key");
if (!env.SHORO_SETUP_SECRET ||
!supplied ||
supplied !== env.SHORO_SETUP_SECRET) {
return new Response("Forbidden", { status: 403 });
}

try {
await register(env);
return new Response(
"SHORO V3 commands registered successfully. Return to Discord."
);
} catch (e) {
console.error("Registration error", e.message);
return new Response(
"Registration failed. Check Worker logs.",
{ status: 500 }
);
}
}
if (
  request.method === "POST" &&
  url.pathname === "/shoro-price-update"
) {
  const auth = request.headers.get("Authorization");

  if (
    !env.SHORO_BRIDGE_SECRET ||
    auth !== `Bearer ${env.SHORO_BRIDGE_SECRET}`
  ) {
    return new Response("Unauthorized", { status: 401 });
  }

  if (!env.SHORO_DB) {
    return new Response("Database binding missing", {
      status: 500
    });
  }

  let body;
  try {
    body = await request.json();
  } catch {
    return new Response("Invalid JSON", { status: 400 });
  }

  const allowedSymbols = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT",
    "XRPUSDT", "DOGEUSDT", "ZENUSDT", "ZECUSDT"
  ];

  if (
    !body.prices ||
    typeof body.prices !== "object" ||
    Array.isArray(body.prices)
  ) {
    return new Response("Invalid prices", { status: 400 });
  }

  const entries = Object.entries(body.prices);

  if (
    entries.length === 0 ||
    entries.some(([symbol, price]) =>
      !allowedSymbols.includes(symbol) ||
      typeof price !== "number" ||
      !Number.isFinite(price) ||
      price <= 0
    )
  ) {
    return new Response("Invalid price data", {
      status: 400
    });
  }

  try {
    await env.SHORO_DB.prepare(`
      CREATE TABLE IF NOT EXISTS shoro_prices (
        symbol TEXT PRIMARY KEY,
        price REAL NOT NULL,
        updated_at TEXT NOT NULL
      )
    `).run();

    const updatedAt = new Date().toISOString();

    for (const [symbol, price] of entries) {
      await env.SHORO_DB.prepare(`
        INSERT INTO shoro_prices (symbol, price, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(symbol) DO UPDATE SET
          price = excluded.price,
          updated_at = excluded.updated_at
      `).bind(symbol, price, updatedAt).run();
    }

    return Response.json({
      success: true,
      updated: entries.length
    });
  } catch (error) {
    console.error("Price update failed:", error.message);
    return new Response("Price database update failed", {
      status: 500
    });
  }
}
if (request.method !== "POST") {
return new Response("Method Not Allowed", {
status: 405
});
}

if (!await verifyDiscord(
request, env.SHORO_DISCORD_PUBLIC_KEY
)) {
return new Response("Invalid signature", {
status: 401
});
}

let i;
try {
i = await request.json();
} catch {
return new Response("Invalid JSON", { status: 400 });
}

if (i.type === 1) {
return respond({ type: 1 });
}

try {
if (!env.SHORO_DB) {
return respond(
privateReply("Database binding SHORO_DB missing.")
);
}

if (i.type === 2) {
const name = i.data?.name;

if (name === "trade") {
return respond(await newTrade(i, env));
}

if (name === "history") {
return respond(await history(i, env));
}

if (name === "close") {
const o = i.data.options || [];
const id = String(opt(o, "trade_id") || "").trim();
const reason = String(opt(o, "reason") || "");
const p = opt(o, "exit_price");

const result = await closeTrade(
env, id, userId(i), reason, p
);

return respond(privateReply(
result.error || closeSummary(result, id)
));
}
}

if (i.type === 3) {
const cid = i.data?.custom_id || "";
const colon = cid.indexOf(":");
const action = cid.slice(0, colon);
const id = cid.slice(colon + 1);

if (!id || colon < 0) {
return respond(privateReply("Invalid button."));
}

if (action === "trade_taken" ||
action === "skip_trade") {
return respond(
await decision(i, env, action, id)
);
}

if (action === "close_trade") {
const trade = await env.SHORO_DB.prepare(`
SELECT status, discord_user_id
FROM shoro_trades WHERE trade_id = ?
`).bind(id).first();

if (!trade || trade.status !== "ACTIVE") {
return respond(
privateReply("Trade is not ACTIVE.")
);
}

if (trade.discord_user_id !== userId(i)) {
return respond(privateReply(
"Only the trade owner can close it."
));
}

return respond(exitModal(id));
}
}

if (i.type === 5) {
const cid = i.data?.custom_id || "";
if (cid.startsWith("exit_modal:")) {
const id = cid.slice("exit_modal:".length);
const reason = String(
modalValue(i, "exit_reason") || ""
).trim().toUpperCase();
const pText = String(
modalValue(i, "exit_price") || ""
).trim();
const p = pText ? Number(pText) : NaN;

const result = await closeTrade(
env, id, userId(i), reason, p
);

if (result.error) {
return respond(privateReply(result.error));
}

// Modal submitted from a message button: update that original message,
// remove its Close Trade button, and show the final journal summary.
if (i.message) {
return respond({
type: 7,
data: {
content: closeSummary(result, id),
embeds: i.message.embeds || [],
components: []
}
});
}
return respond(privateReply(closeSummary(result, id)));
}
}

return respond(
privateReply("Unsupported SHORO interaction.")
);
} catch (error) {
console.error("SHORO Worker error", error);
return respond(privateReply(
"An error occurred. Check Cloudflare Worker logs."
));
}
}
};
