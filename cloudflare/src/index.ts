// Always-on, read-only finance MCP server (port of ../server.py) on Cloudflare Workers + D1.
// Sign-in: GitHub OAuth, restricted to ALLOWED_GITHUB_LOGIN.
import { env as globalEnv } from "cloudflare:workers";
import OAuthProvider from "@cloudflare/workers-oauth-provider";
import { McpServer } from "@modelcontextprotocol/server";
import { createMcpHandler, getMcpAuthContext } from "agents/mcp/server";
import { z } from "zod";
import { GitHubHandler } from "./github-handler";
import { syncAll } from "./sync";
import type { Props } from "./utils";

const LIABILITY = "('credit','loan')";
// Money moving between your own accounts, not real spending/income.
const TRANSFERS = "('TRANSFER_IN','TRANSFER_OUT','LOAN_PAYMENTS')";

const env = globalEnv as unknown as Env;

async function rows(sql: string, ...params: unknown[]): Promise<any[]> {
	const { results } = await env.DB.prepare(sql).bind(...params).all();
	return results;
}

const json = (obj: unknown) => ({ content: [{ type: "text" as const, text: JSON.stringify(obj, null, 2) }] });

function isoDaysAgo(fromIso: string, days: number) {
	return new Date(new Date(fromIso + "T00:00:00Z").getTime() - days * 86400000).toISOString().slice(0, 10);
}
function range(start?: string, end?: string, days = 30): [string, string] {
	const e = end ?? new Date().toISOString().slice(0, 10);
	return [start ?? isoDaysAgo(e, days), e];
}
const round = (n: number) => Math.round(n * 100) / 100;

function createServer() {
	const server = new McpServer({ name: "finance", version: "1.0.0" }, {
		instructions:
			"Read-only access to the user's synced bank (UFCU) and brokerage (Robinhood) data via Plaid. " +
			"Transaction amount > 0 means money spent / left the account; < 0 means money received. " +
			"Data syncs automatically every 4 hours.",
	});
	const props = getMcpAuthContext()?.props as Props | undefined;
	// Defense in depth: the GitHub callback already refuses other accounts.
	if (!props || props.login !== env.ALLOWED_GITHUB_LOGIN) return server;

	server.registerTool("sync_accounts", {
		description: "Pull the latest balances, transactions and investments from all linked institutions now.",
	}, async () => json(await syncAll(env)));

	server.registerTool("list_connections", {
		description: "Linked institutions, which data each provides, when each last synced (UTC), and any error.",
	}, async () => json(await rows("SELECT institution_name, products, env, created_at, last_synced, last_error FROM items")));

	server.registerTool("list_accounts", {
		description: "All accounts with current/available balances, type (depository, credit, loan, investment) and institution.",
	}, async () => json(await rows(`SELECT institution_name, name, mask, type, subtype, current_balance, available_balance,
		credit_limit, currency, updated_at FROM accounts ORDER BY institution_name, type, name`)));

	server.registerTool("net_worth", {
		description: "Net worth = cash + investments - credit card and loan balances, with a breakdown and recent history.",
	}, async () => {
		const byType = await rows("SELECT type, SUM(current_balance) AS total FROM accounts GROUP BY type");
		const liab = ["credit", "loan"];
		const assets = byType.filter((r) => !liab.includes(r.type)).reduce((s, r) => s + (r.total ?? 0), 0);
		const liabilities = byType.filter((r) => liab.includes(r.type)).reduce((s, r) => s + (r.total ?? 0), 0);
		const history = await rows(`SELECT h.date,
			ROUND(SUM(CASE WHEN a.type IN ${LIABILITY} THEN -h.current_balance ELSE h.current_balance END), 2) AS net_worth
			FROM balance_history h JOIN accounts a USING(account_id) GROUP BY h.date ORDER BY h.date DESC LIMIT 90`);
		return json({ net_worth: round(assets - liabilities), assets: round(assets), liabilities: round(liabilities),
			by_account_type: byType, history_newest_first: history });
	});

	server.registerTool("search_transactions", {
		description: `Bank/card transactions, newest first. Dates are YYYY-MM-DD (default: last 30 days).
amount > 0 = money spent / left the account; amount < 0 = money received (paycheck, refund).
text matches merchant or description. category is a Plaid primary category such as FOOD_AND_DRINK,
GENERAL_MERCHANDISE, TRANSPORTATION, RENT_AND_UTILITIES, INCOME, TRANSFER_OUT, ENTERTAINMENT, TRAVEL,
LOAN_PAYMENTS, PERSONAL_CARE, MEDICAL, GENERAL_SERVICES. account matches account name, mask or institution.`,
		inputSchema: z.object({
			start_date: z.string().optional(), end_date: z.string().optional(), text: z.string().optional(),
			category: z.string().optional(), account: z.string().optional(),
			min_amount: z.number().optional(), max_amount: z.number().optional(),
			limit: z.number().int().default(200),
		}),
	}, async (a) => {
		const [start, end] = range(a.start_date, a.end_date);
		let sql = `SELECT t.date, COALESCE(t.merchant_name, t.name) AS merchant, t.name AS description, t.amount,
			t.category, t.category_detailed, t.pending, a.institution_name, a.name AS account, a.mask
			FROM transactions t LEFT JOIN accounts a USING(account_id) WHERE t.date BETWEEN ? AND ?`;
		const p: unknown[] = [start, end];
		if (a.text) { sql += " AND (t.merchant_name LIKE ? OR t.name LIKE ?)"; p.push(`%${a.text}%`, `%${a.text}%`); }
		if (a.category) { sql += " AND (t.category = ? OR t.category_detailed LIKE ?)"; p.push(a.category.toUpperCase(), `%${a.category.toUpperCase()}%`); }
		if (a.account) { sql += " AND (a.name LIKE ? OR a.mask = ? OR a.institution_name LIKE ?)"; p.push(`%${a.account}%`, a.account, `%${a.account}%`); }
		if (a.min_amount !== undefined) { sql += " AND t.amount >= ?"; p.push(a.min_amount); }
		if (a.max_amount !== undefined) { sql += " AND t.amount <= ?"; p.push(a.max_amount); }
		sql += " ORDER BY t.date DESC LIMIT ?"; p.push(Math.min(a.limit, 2000));
		const r = await rows(sql, ...p);
		return json({ start_date: start, end_date: end, count: r.length, transactions: r });
	});

	const GROUPS: Record<string, string> = {
		category: "t.category",
		category_detailed: "t.category_detailed",
		merchant: "COALESCE(t.merchant_name, t.name)",
		month: "substr(t.date, 1, 7)",
		week: "strftime('%Y-W%W', t.date)",
		account: "a.institution_name || ' ' || a.name",
	};
	server.registerTool("spending_summary", {
		description: `Totals of spending and income over a date range (default last 30 days).
group_by: category | category_detailed | merchant | month | week | account.
Transfers between your own accounts and credit-card/loan payments are excluded unless include_transfers.
spent = money out, received = money in.`,
		inputSchema: z.object({
			start_date: z.string().optional(), end_date: z.string().optional(),
			group_by: z.enum(["category", "category_detailed", "merchant", "month", "week", "account"]).default("category"),
			include_transfers: z.boolean().default(false),
		}),
	}, async (a) => {
		const [start, end] = range(a.start_date, a.end_date);
		let where = "t.date BETWEEN ? AND ?";
		if (!a.include_transfers) where += ` AND COALESCE(t.category, '') NOT IN ${TRANSFERS}`;
		const r = await rows(`SELECT ${GROUPS[a.group_by]} AS grp,
			ROUND(SUM(CASE WHEN t.amount > 0 THEN t.amount ELSE 0 END), 2) AS spent,
			ROUND(SUM(CASE WHEN t.amount < 0 THEN -t.amount ELSE 0 END), 2) AS received,
			COUNT(*) AS n
			FROM transactions t LEFT JOIN accounts a USING(account_id)
			WHERE ${where} GROUP BY grp ORDER BY spent DESC`, start, end);
		return json({ start_date: start, end_date: end, group_by: a.group_by,
			total_spent: round(r.reduce((s, x) => s + x.spent, 0)),
			total_received: round(r.reduce((s, x) => s + x.received, 0)), groups: r });
	});

	server.registerTool("recurring_charges", {
		description: "Likely subscriptions / recurring bills: merchants charged in at least 3 different months recently.",
		inputSchema: z.object({ months: z.number().int().default(4) }),
	}, async ({ months }) => {
		const since = isoDaysAgo(new Date().toISOString().slice(0, 10), 31 * months);
		return json(await rows(`SELECT COALESCE(merchant_name, name) AS merchant, COUNT(*) AS charges,
			COUNT(DISTINCT substr(date,1,7)) AS months_seen, ROUND(AVG(amount), 2) AS avg_amount,
			MAX(date) AS last_charged, category
			FROM transactions WHERE date >= ? AND amount > 0 AND COALESCE(category,'') NOT IN ${TRANSFERS}
			GROUP BY merchant HAVING months_seen >= 3 ORDER BY avg_amount DESC`, since));
	});

	server.registerTool("get_holdings", {
		description: "Current investment positions (e.g. Robinhood): ticker, quantity, price, value, cost basis, gain/loss.",
	}, async () => {
		const r = await rows(`SELECT a.institution_name, a.name AS account, s.ticker, s.name, s.type, h.quantity,
			h.price, h.value, h.cost_basis, ROUND(h.value - h.cost_basis, 2) AS unrealized_gain,
			CASE WHEN h.cost_basis > 0 THEN ROUND(100.0*(h.value - h.cost_basis)/h.cost_basis, 2) END AS unrealized_gain_pct,
			h.price_as_of
			FROM holdings h JOIN securities s USING(security_id) LEFT JOIN accounts a USING(account_id)
			ORDER BY h.value DESC`);
		const total = r.reduce((s, x) => s + (x.value ?? 0), 0);
		for (const x of r) x.pct_of_portfolio = total ? round((100 * (x.value ?? 0)) / total) : null;
		return json({ total_value: round(total), positions: r });
	});

	server.registerTool("investment_activity", {
		description: `Buys, sells, dividends, deposits and fees in brokerage accounts (default last 365 days).
amount > 0 = cash left the brokerage account (e.g. a buy); amount < 0 = cash came in (sell, dividend).`,
		inputSchema: z.object({
			start_date: z.string().optional(), end_date: z.string().optional(),
			ticker: z.string().optional(), limit: z.number().int().default(300),
		}),
	}, async (a) => {
		const [start, end] = range(a.start_date, a.end_date, 365);
		let sql = `SELECT t.date, s.ticker, s.name AS security, t.type, t.subtype, t.quantity, t.price, t.amount,
			t.fees, t.name AS description, a.institution_name
			FROM investment_transactions t LEFT JOIN securities s USING(security_id)
			LEFT JOIN accounts a USING(account_id) WHERE t.date BETWEEN ? AND ?`;
		const p: unknown[] = [start, end];
		if (a.ticker) { sql += " AND s.ticker = ?"; p.push(a.ticker.toUpperCase()); }
		sql += " ORDER BY t.date DESC LIMIT ?"; p.push(Math.min(a.limit, 2000));
		const r = await rows(sql, ...p);
		return json({ start_date: start, end_date: end, count: r.length, transactions: r });
	});

	server.registerTool("query_finance_db", {
		description: `Run a read-only SQL SELECT on the finance database for questions the other tools don't cover.
Tables: accounts(account_id, institution_name, name, mask, type, subtype, current_balance,
  available_balance, credit_limit, currency, updated_at)
balance_history(date, account_id, current_balance)
transactions(transaction_id, account_id, date, authorized_date, name, merchant_name, amount,
  currency, category, category_detailed, payment_channel, pending)  -- amount>0 = money out
securities(security_id, name, ticker, type, close_price, close_price_as_of)
holdings(account_id, security_id, quantity, price, value, cost_basis, price_as_of)
investment_transactions(investment_transaction_id, account_id, security_id, date, name, type,
  subtype, quantity, price, amount, fees)
Dates are 'YYYY-MM-DD' text. Results are capped at 1000 rows.`,
		inputSchema: z.object({ sql: z.string() }),
	}, async ({ sql }) => {
		const s = sql.trim().replace(/;\s*$/, "");
		if (!/^(select|with)\b/i.test(s) || s.includes(";")) throw new Error("Only a single SELECT / WITH query is allowed");
		if (/\b(insert|update|delete|replace|drop|alter|create|attach|detach|pragma|vacuum|reindex)\b/i.test(s))
			throw new Error("Only read-only queries are allowed");
		if (/\b(items|access_token|sqlite_\w*|_cf_\w*)\b/i.test(s)) throw new Error("That table is not queryable");
		const r = await rows(s);
		return json(r.slice(0, 1000));
	});

	return server;
}

const mcpHandler = createMcpHandler(createServer);

const provider = new OAuthProvider<Env>({
	apiHandler: { fetch: (req: Request, e: Env, ctx: ExecutionContext) => mcpHandler(req, e, ctx) },
	apiRoute: "/mcp",
	authorizeEndpoint: "/authorize",
	clientRegistrationEndpoint: "/register",
	defaultHandler: GitHubHandler as any,
	tokenEndpoint: "/token",
	resourceMetadata: { resource: env.MCP_RESOURCE },
});

export default {
	fetch: (req: Request, e: Env, ctx: ExecutionContext) => provider.fetch(req, e, ctx),
	async scheduled(_c: ScheduledController, e: Env, ctx: ExecutionContext) {
		ctx.waitUntil(syncAll(e).then((r) => console.log("sync", JSON.stringify(r))));
	},
} satisfies ExportedHandler<Env>;
