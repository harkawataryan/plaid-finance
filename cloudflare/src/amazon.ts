// Amazon orders: read Amazon's order / shipping emails from Gmail, store them in D1,
// and match each Amazon card charge to the order (or shipment) it paid for.
// Product names are not in Amazon's emails; they come from add_amazon_items or amazon_export_import.py.

const AMAZON_SENDERS = [
	"auto-confirm@amazon.com",       // "Ordered ..."  -> order grand total
	"shipment-tracking@amazon.com",  // "Shipped ..."  -> per-shipment total (what the card is charged)
	"order-update@amazon.com",       // "Delivered ...", grocery updates
	"digital-no-reply@amazon.com",   // Kindle / digital orders
];
// Every sync lists this whole window and skips mail it has already read, so a backlog bigger than
// one run (MAX_MESSAGES_PER_RUN) keeps draining on later runs instead of being cut off.
const SYNC_WINDOW_DAYS = 180;
const MAX_MESSAGES_PER_RUN = 40;  // stays under the Workers subrequest limit; the rest is read next run
export const LOCAL_TZ = "America/Chicago";

export type AmazonEnv = Env & {
	GOOGLE_CLIENT_ID?: string;
	GOOGLE_CLIENT_SECRET?: string;
	GOOGLE_REFRESH_TOKEN?: string;
};

export type ParsedOrder = {
	order_id: string;
	summary: string | null;
	total: number | null;
	total_is_estimate: boolean;
};
export type ParsedEmail = { kind: "ordered" | "shipped" | "delivered" | "other"; orders: ParsedOrder[] };

// ---------------------------------------------------------------- parsing (pure, unit-testable)

const ENTITIES: Record<string, string> = { amp: "&", lt: "<", gt: ">", quot: '"', apos: "'", nbsp: " " };

/** Visible text of an HTML email as a list of non-empty text chunks, in reading order. */
export function htmlToTokens(html: string): string[] {
	const text = html
		.replace(/<(style|script|head|title)\b[\s\S]*?<\/\1>/gi, " ")
		.replace(/<!--[\s\S]*?-->/g, " ")
		.replace(/<[^>]+>/g, "\u0001")
		.replace(/&#x([0-9a-f]+);/gi, (_, h) => String.fromCodePoint(parseInt(h, 16)))
		.replace(/&#(\d+);/g, (_, d) => String.fromCodePoint(+d))
		.replace(/&([a-z]+);/gi, (m, n) => ENTITIES[n.toLowerCase()] ?? " ");
	return text
		.split("\u0001")
		// drop invisible padding characters Amazon puts in preheaders and around order numbers
		.map((t) => t.replace(/[­͏​-‏‪-‮ ⁠﻿]/g, "").replace(/\s+/g, " ").trim())
		.filter(Boolean);
}

const ORDER_ID = /\b([0-9D]\d{2}-\d{7}-\d{7})\b/;
const MONEY = /^\$?\s*([\d,]+\.\d{2})(\s*USD)?$/;
const HEADING = /^\d+ items?: .+/;

function money(tok: string | undefined): number | null {
	const m = tok?.match(MONEY);
	return m ? parseFloat(m[1].replace(/,/g, "")) : null;
}

export function emailKind(sender: string, subject: string): ParsedEmail["kind"] {
	const s = sender.toLowerCase(), subj = subject.toLowerCase();
	if (s.includes("shipment-tracking") || subj.startsWith("shipped")) return "shipped";
	if (subj.startsWith("delivered")) return "delivered";
	if (s.includes("auto-confirm") || s.includes("digital-no-reply") || subj.startsWith("ordered") || subj.includes("your amazon.com order"))
		return "ordered";
	return "other";
}

/** Orders (id, heading, total) mentioned in one Amazon email. One email can cover several orders. */
export function parseAmazonEmail(sender: string, subject: string, html: string): ParsedEmail {
	const kind = emailKind(sender, subject);
	const toks = htmlToTokens(html);
	const orders: ParsedOrder[] = [];
	let heading: string | null = null;
	for (let i = 0; i < toks.length; i++) {
		const t = toks[i];
		if (HEADING.test(t) && t !== subject.trim()) heading = t;
		if (!/^Order\s*#/i.test(t)) continue;
		const idMatch = (t + " " + (toks[i + 1] ?? "")).match(ORDER_ID);
		if (!idMatch) continue;
		const order: ParsedOrder = { order_id: idMatch[1], summary: heading, total: null, total_is_estimate: false };
		// The total follows the order number, before the next order block starts.
		for (let j = i + 1; j < toks.length && !/^Order\s*#/i.test(toks[j]); j++) {
			const label = toks[j].toLowerCase().replace(/:$/, "");
			if (["grand total", "estimated grand total", "total", "order total", "estimated total"].includes(label)) {
				const v = money(toks[j + 1]);
				if (v !== null) {
					order.total = v;
					order.total_is_estimate = label.startsWith("estimated");
					break;
				}
			}
		}
		if (!orders.some((o) => o.order_id === order.order_id)) orders.push(order);
		heading = null;
	}
	return { kind, orders };
}

// ---------------------------------------------------------------- Gmail

function b64urlDecode(data: string): string {
	const bin = atob(data.replace(/-/g, "+").replace(/_/g, "/"));
	const bytes = Uint8Array.from(bin, (c) => c.charCodeAt(0));
	return new TextDecoder().decode(bytes);
}

function findPart(part: any, mime: string): string | null {
	if (part?.mimeType === mime && part.body?.data) return b64urlDecode(part.body.data);
	for (const p of part?.parts ?? []) {
		const found = findPart(p, mime);
		if (found) return found;
	}
	return null;
}

async function googleAccessToken(env: AmazonEnv): Promise<string> {
	const r = await fetch("https://oauth2.googleapis.com/token", {
		method: "POST",
		headers: { "Content-Type": "application/x-www-form-urlencoded" },
		body: new URLSearchParams({
			client_id: env.GOOGLE_CLIENT_ID!, client_secret: env.GOOGLE_CLIENT_SECRET!,
			refresh_token: env.GOOGLE_REFRESH_TOKEN!, grant_type: "refresh_token",
		}),
	});
	const d: any = await r.json();
	if (!r.ok) throw new Error(`Google token refresh failed: ${d.error ?? r.status} ${d.error_description ?? ""}` +
		(d.error === "invalid_grant" ? " -> run google_auth.py again" : ""));
	return d.access_token;
}

async function gmail(token: string, path: string): Promise<any> {
	const r = await fetch("https://gmail.googleapis.com/gmail/v1/users/me/" + path, {
		headers: { Authorization: `Bearer ${token}` },
	});
	const d: any = await r.json();
	if (!r.ok) throw new Error(`Gmail ${r.status}: ${d.error?.message ?? JSON.stringify(d)}`);
	return d;
}

export function localDate(ms: number): string {
	// en-CA formats as YYYY-MM-DD
	return new Intl.DateTimeFormat("en-CA", { timeZone: LOCAL_TZ, year: "numeric", month: "2-digit", day: "2-digit" })
		.format(new Date(ms));
}

const nowIso = () => new Date().toISOString().slice(0, 19);

export async function syncAmazon(env: AmazonEnv) {
	if (!env.GOOGLE_CLIENT_ID || !env.GOOGLE_CLIENT_SECRET || !env.GOOGLE_REFRESH_TOKEN)
		return { error: "Gmail is not connected: set GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET / GOOGLE_REFRESH_TOKEN (run google_auth.py)" };
	const token = await googleAccessToken(env);

	const q = `from:(${AMAZON_SENDERS.join(" OR ")}) newer_than:${SYNC_WINDOW_DAYS}d`;

	// List ids (newest first). Each page is one subrequest, so cap the pages.
	const ids: string[] = [];
	let pageToken: string | undefined;
	for (let page = 0; page < 5; page++) {
		const d = await gmail(token, `messages?maxResults=500&q=${encodeURIComponent(q)}` + (pageToken ? `&pageToken=${pageToken}` : ""));
		ids.push(...(d.messages ?? []).map((m: any) => m.id));
		pageToken = d.nextPageToken;
		if (!pageToken) break;
	}

	// Skip already-processed messages; read the oldest new ones first.
	const seen = new Set<string>();
	for (let i = 0; i < ids.length; i += 90) {
		const chunk = ids.slice(i, i + 90);
		const { results } = await env.DB.prepare(
			`SELECT email_id FROM amazon_emails WHERE email_id IN (${chunk.map(() => "?").join(",")})`,
		).bind(...chunk).all<{ email_id: string }>();
		for (const r of results) seen.add(r.email_id);
	}
	const todo = ids.filter((id) => !seen.has(id)).reverse();
	const batch = todo.slice(0, MAX_MESSAGES_PER_RUN);

	let ordersTouched = 0, shipments = 0;
	for (const id of batch) {
		const m = await gmail(token, `messages/${id}?format=full`);
		const headers: Record<string, string> = {};
		for (const h of m.payload?.headers ?? []) headers[h.name.toLowerCase()] = h.value;
		const html = findPart(m.payload, "text/html") ?? (findPart(m.payload, "text/plain") ?? "").replace(/\n/g, "<br>");
		const parsed = parseAmazonEmail(headers["from"] ?? "", headers["subject"] ?? "", html);
		const date = localDate(Number(m.internalDate));
		const stmts: D1PreparedStatement[] = [];
		for (const o of parsed.orders) {
			const isOrderTotal = parsed.kind === "ordered" ? o.total : null;
			stmts.push(env.DB.prepare(
				`INSERT INTO amazon_orders (order_id, order_date, summary, order_total, total_is_estimate, status, updated_at)
				 VALUES (?,?,?,?,?,?,?)
				 ON CONFLICT(order_id) DO UPDATE SET
				   order_date = MIN(order_date, excluded.order_date),
				   summary = COALESCE(amazon_orders.summary, excluded.summary),
				   order_total = COALESCE(excluded.order_total, amazon_orders.order_total),
				   total_is_estimate = CASE WHEN excluded.order_total IS NOT NULL THEN excluded.total_is_estimate ELSE amazon_orders.total_is_estimate END,
				   status = CASE WHEN excluded.status = 'delivered' OR amazon_orders.status IS NULL
				                   OR (excluded.status = 'shipped' AND amazon_orders.status = 'ordered')
				                 THEN excluded.status ELSE amazon_orders.status END,
				   updated_at = excluded.updated_at`,
			).bind(o.order_id, date, o.summary, isOrderTotal, o.total_is_estimate ? 1 : 0,
				parsed.kind === "other" ? "ordered" : parsed.kind, nowIso()));
			ordersTouched++;
			if (parsed.kind === "shipped" && o.total !== null) {
				stmts.push(env.DB.prepare("INSERT OR REPLACE INTO amazon_shipments VALUES (?,?,?,?,?)")
					.bind(`${id}:${o.order_id}`, o.order_id, date, o.summary, o.total));
				shipments++;
			}
		}
		stmts.push(env.DB.prepare("INSERT OR REPLACE INTO amazon_emails VALUES (?,?,?,?)")
			.bind(id, parsed.kind, parsed.orders.map((o) => o.order_id).join(","), nowIso()));
		await env.DB.batch(stmts);
	}
	return {
		emails_read: batch.length, orders_updated: ordersTouched, shipments_recorded: shipments,
		emails_remaining: todo.length - batch.length,
		note: todo.length > batch.length ? "More emails are waiting; run sync_amazon again (cron also continues)." : undefined,
	};
}

// ---------------------------------------------------------------- matching charges to orders

type Txn = { transaction_id: string; date: string; amount: number; merchant: string; description: string; pending: number };
type Unit = { key: string; order_id: string; date: string; amount: number; estimate: boolean; kind: "shipment" | "order" };

const AMAZON_TXN_WHERE = `(t.merchant_name LIKE '%amazon%' OR t.name LIKE '%amazon%' OR t.name LIKE '%amzn%')`;
const daysBetween = (a: string, b: string) => (Date.parse(b) - Date.parse(a)) / 86400000;

function isPrimeMembership(t: Txn) {
	return /prime/i.test(t.merchant) && !/(reta|mktp|marketplace)/i.test(t.description) || /prime (membership|video)/i.test(t.description);
}

/** One-to-one matching: exact amounts first, then estimated grocery totals within 20%, closest dates first. */
export function matchCharges(txns: Txn[], units: Unit[]) {
	const pairs: { t: Txn; u: Unit; exact: boolean; gap: number }[] = [];
	for (const t of txns) for (const u of units) {
		const gap = daysBetween(u.date, t.date);
		if (gap < -2 || gap > 10) continue;               // charge lands from ~2 days before to 10 days after the email
		const diff = Math.abs(t.amount - u.amount);
		const exact = diff < 0.005;
		if (!exact && !(u.estimate && diff <= Math.max(1, 0.2 * u.amount))) continue;
		pairs.push({ t, u, exact, gap: Math.abs(gap) });
	}
	pairs.sort((a, b) => Number(b.exact) - Number(a.exact) || a.gap - b.gap);
	const usedT = new Set<string>(), usedU = new Set<string>();
	const out = new Map<string, { unit: Unit; exact: boolean }>();
	for (const p of pairs) {
		if (usedT.has(p.t.transaction_id) || usedU.has(p.u.key)) continue;
		usedT.add(p.t.transaction_id); usedU.add(p.u.key);
		out.set(p.t.transaction_id, { unit: p.u, exact: p.exact });
	}
	return { matches: out, unmatchedUnits: units.filter((u) => !usedU.has(u.key)) };
}

export async function amazonPurchases(env: Env, start: string, end: string) {
	const pad = (d: string, n: number) => new Date(Date.parse(d) + n * 86400000).toISOString().slice(0, 10);
	const { results: txns } = await env.DB.prepare(
		`SELECT t.transaction_id, t.date, t.amount, COALESCE(t.merchant_name, t.name) AS merchant, t.name AS description, t.pending
		 FROM transactions t WHERE t.date BETWEEN ? AND ? AND t.amount <> 0 AND ${AMAZON_TXN_WHERE} ORDER BY t.date DESC`,
	).bind(pad(start, -12), pad(end, 12)).all<Txn>();

	const { results: orders } = await env.DB.prepare(
		`SELECT * FROM amazon_orders WHERE order_date BETWEEN ? AND ?`,
	).bind(pad(start, -12), pad(end, 2)).all<any>();
	const orderById = new Map(orders.map((o) => [o.order_id, o]));
	const ids = orders.map((o) => o.order_id);
	const shipments: any[] = [], items: any[] = [];
	for (let i = 0; i < ids.length; i += 90) {
		const chunk = ids.slice(i, i + 90), ph = chunk.map(() => "?").join(",");
		shipments.push(...(await env.DB.prepare(`SELECT * FROM amazon_shipments WHERE order_id IN (${ph})`).bind(...chunk).all()).results);
		items.push(...(await env.DB.prepare(`SELECT * FROM amazon_items WHERE order_id IN (${ph})`).bind(...chunk).all()).results);
	}

	// What the card gets charged for: each shipment with a total; orders with no such shipment are charged as a whole.
	const units: Unit[] = shipments.map((s) => ({ key: s.email_id, order_id: s.order_id, date: s.date, amount: s.amount, estimate: false, kind: "shipment" as const }));
	const shippedOrders = new Set(shipments.map((s) => s.order_id));
	for (const o of orders) {
		if (o.order_total === null) continue;
		const sumShip = shipments.filter((s) => s.order_id === o.order_id).reduce((a, s) => a + s.amount, 0);
		if (!shippedOrders.has(o.order_id) || Math.abs(sumShip - o.order_total) > 0.005 && sumShip < o.order_total) {
			// No shipment totals (e.g. grocery), or shipments don't add up yet: the order total is also a candidate.
			units.push({ key: "order:" + o.order_id, order_id: o.order_id, date: o.order_date, amount: shippedOrders.has(o.order_id) ? o.order_total - sumShip : o.order_total,
				estimate: !!o.total_is_estimate, kind: "order" });
		}
	}

	const charges = txns.filter((t) => t.amount > 0 && !isPrimeMembership(t));
	const { matches, unmatchedUnits } = matchCharges(charges, units);
	const describe = (order_id: string) => {
		const o = orderById.get(order_id);
		const its = items.filter((i) => i.order_id === order_id).map((i) => ({ title: i.title, quantity: i.quantity, unit_price: i.unit_price }));
		return {
			order_id, order_date: o?.order_date, summary: o?.summary, order_total: o?.order_total, status: o?.status,
			items: its.length ? its : undefined,
			link: `https://www.amazon.com/your-orders/order-details?orderID=${order_id}`,
		};
	};

	const inRange = (d: string) => d >= start && d <= end;
	const rows = txns.filter((t) => inRange(t.date)).map((t) => {
		const base = { date: t.date, amount: t.amount, description: t.description, pending: !!t.pending };
		if (t.amount < 0) return { ...base, kind: "refund_or_credit" };
		if (isPrimeMembership(t)) return { ...base, kind: "prime_membership" };
		const m = matches.get(t.transaction_id);
		if (!m) return { ...base, kind: "unmatched", hint: "No Amazon email with this amount near this date (gift card balance, digital order, or emails not synced yet)." };
		return { ...base, kind: m.unit.kind === "shipment" ? "shipment" : "order", match: m.exact ? "exact_amount" : "estimated_total", order: describe(m.unit.order_id) };
	});
	const pending = unmatchedUnits.filter((u) => inRange(u.date)).map((u) => ({ ...describe(u.order_id), not_charged_yet_amount: u.amount }));
	const missingNames = new Set(rows.flatMap((r: any) => r.order && !r.order.items ? [r.order.order_id] : []));
	return {
		start_date: start, end_date: end,
		total_amazon_spend: Math.round(rows.reduce((s, r) => s + r.amount, 0) * 100) / 100,
		charges: rows,
		orders_not_charged_yet: pending,
		note: missingNames.size ? `Product names missing for ${missingNames.size} order(s): Amazon's emails only give a category. ` +
			"Open the order links (e.g. in the user's browser) and save names with add_amazon_items, or import Amazon's data export." : undefined,
	};
}
