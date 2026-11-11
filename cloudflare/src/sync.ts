// Pull balances, transactions and investments from Plaid into D1.
// Port of ../sync.py. Runs on the cron trigger and from the sync_accounts tool.

const HOSTS: Record<string, string> = {
	sandbox: "https://sandbox.plaid.com",
	production: "https://production.plaid.com",
};
const INVESTMENT_HISTORY_DAYS = 730;
const SOFT_ERRORS = new Set([
	"PRODUCT_NOT_READY", "PRODUCTS_NOT_SUPPORTED", "NO_INVESTMENT_ACCOUNTS",
	"NO_INVESTMENT_AUTH_ACCOUNTS", "ADDITIONAL_CONSENT_REQUIRED", "NO_ACCOUNTS",
]);

export class PlaidError extends Error {
	constructor(public code: string, message: string) {
		super(`${code}: ${message}`);
	}
}

type Item = {
	item_id: string;
	access_token: string;
	institution_name: string | null;
	products: string | null;
	txn_cursor: string | null;
};

const now = () => new Date().toISOString().slice(0, 19);
const today = () => new Date().toISOString().slice(0, 10);

async function plaid(env: Env, path: string, body: Record<string, unknown>): Promise<any> {
	const host = HOSTS[env.PLAID_ENV] ?? HOSTS.production;
	const r = await fetch(host + path, {
		method: "POST",
		headers: { "Content-Type": "application/json" },
		body: JSON.stringify({ client_id: env.PLAID_CLIENT_ID, secret: env.PLAID_SECRET, ...body }),
	});
	const data: any = await r.json();
	if (!r.ok) throw new PlaidError(data.error_code ?? "UNKNOWN", data.error_message ?? JSON.stringify(data));
	return data;
}

// D1 caps statements per batch; chunk to be safe.
async function runBatch(env: Env, stmts: D1PreparedStatement[]) {
	for (let i = 0; i < stmts.length; i += 50) await env.DB.batch(stmts.slice(i, i + 50));
}

async function syncAccounts(env: Env, item: Item) {
	const data = await plaid(env, "/accounts/get", { access_token: item.access_token });
	const stmts: D1PreparedStatement[] = [];
	for (const a of data.accounts) {
		const b = a.balances ?? {};
		stmts.push(env.DB.prepare(
			`INSERT INTO accounts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
			 ON CONFLICT(account_id) DO UPDATE SET
			   name=excluded.name, official_name=excluded.official_name, mask=excluded.mask,
			   type=excluded.type, subtype=excluded.subtype, current_balance=excluded.current_balance,
			   available_balance=excluded.available_balance, credit_limit=excluded.credit_limit,
			   currency=excluded.currency, updated_at=excluded.updated_at,
			   institution_name=excluded.institution_name`,
		).bind(a.account_id, item.item_id, item.institution_name, a.name ?? null, a.official_name ?? null,
			a.mask ?? null, a.type ?? null, a.subtype ?? null, b.current ?? null, b.available ?? null,
			b.limit ?? null, b.iso_currency_code ?? b.unofficial_currency_code ?? null, now()));
		stmts.push(env.DB.prepare("INSERT OR REPLACE INTO balance_history VALUES (?,?,?)")
			.bind(today(), a.account_id, b.current ?? null));
	}
	await runBatch(env, stmts);
	return data.accounts.length;
}

async function syncTransactions(env: Env, item: Item) {
	let added: any[] = [], modified: any[] = [], removed: any[] = [];
	let cursor = item.txn_cursor;
	for (let attempt = 0; ; attempt++) {
		cursor = item.txn_cursor;
		added = []; modified = []; removed = [];
		try {
			while (true) {
				const body: Record<string, unknown> = { access_token: item.access_token, count: 500 };
				if (cursor) body.cursor = cursor;
				const page = await plaid(env, "/transactions/sync", body);
				added.push(...page.added); modified.push(...page.modified); removed.push(...page.removed);
				cursor = page.next_cursor;
				if (!page.has_more) break;
			}
			break;
		} catch (e) {
			if (!(e instanceof PlaidError) || e.code !== "TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION" || attempt >= 2) throw e;
		}
	}
	const stmts: D1PreparedStatement[] = [];
	for (const t of [...added, ...modified]) {
		const pfc = t.personal_finance_category ?? {};
		stmts.push(env.DB.prepare("INSERT OR REPLACE INTO transactions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)")
			.bind(t.transaction_id, t.account_id, t.date, t.authorized_date ?? null, t.name ?? null,
				t.merchant_name ?? null, t.amount, t.iso_currency_code ?? t.unofficial_currency_code ?? null,
				pfc.primary ?? null, pfc.detailed ?? null, t.payment_channel ?? null, t.pending ? 1 : 0));
	}
	for (const r of removed) stmts.push(env.DB.prepare("DELETE FROM transactions WHERE transaction_id=?").bind(r.transaction_id));
	stmts.push(env.DB.prepare("UPDATE items SET txn_cursor=? WHERE item_id=?").bind(cursor, item.item_id));
	await runBatch(env, stmts);
	return { added: added.length, modified: modified.length, removed: removed.length };
}

function securityStmts(env: Env, securities: any[]) {
	return securities.map((s) => env.DB.prepare("INSERT OR REPLACE INTO securities VALUES (?,?,?,?,?,?)")
		.bind(s.security_id, s.name ?? null, s.ticker_symbol ?? null, s.type ?? null,
			s.close_price ?? null, s.close_price_as_of ?? null));
}

async function syncInvestments(env: Env, item: Item) {
	const h = await plaid(env, "/investments/holdings/get", { access_token: item.access_token });
	const stmts: D1PreparedStatement[] = securityStmts(env, h.securities);
	for (const a of h.accounts) stmts.push(env.DB.prepare("DELETE FROM holdings WHERE account_id=?").bind(a.account_id));
	for (const x of h.holdings) {
		stmts.push(env.DB.prepare("INSERT OR REPLACE INTO holdings VALUES (?,?,?,?,?,?,?)")
			.bind(x.account_id, x.security_id, x.quantity ?? null, x.institution_price ?? null,
				x.institution_value ?? null, x.cost_basis ?? null, x.institution_price_as_of ?? null));
	}
	await runBatch(env, stmts);

	const end = new Date();
	const start = new Date(end.getTime() - INVESTMENT_HISTORY_DAYS * 86400000);
	let offset = 0, total = Infinity, n = 0;
	while (offset < total) {
		const page = await plaid(env, "/investments/transactions/get", {
			access_token: item.access_token,
			start_date: start.toISOString().slice(0, 10),
			end_date: end.toISOString().slice(0, 10),
			options: { count: 500, offset },
		});
		total = page.total_investment_transactions;
		const s2 = securityStmts(env, page.securities);
		for (const t of page.investment_transactions) {
			s2.push(env.DB.prepare("INSERT OR REPLACE INTO investment_transactions VALUES (?,?,?,?,?,?,?,?,?,?,?)")
				.bind(t.investment_transaction_id, t.account_id, t.security_id ?? null, t.date, t.name ?? null,
					t.type ?? null, t.subtype ?? null, t.quantity ?? null, t.price ?? null, t.amount ?? null, t.fees ?? null));
		}
		await runBatch(env, s2);
		const got = page.investment_transactions.length;
		offset += got; n += got;
		if (got === 0) break;
	}
	return { holdings: h.holdings.length, investment_transactions: n };
}

export async function syncAll(env: Env) {
	const { results: items } = await env.DB.prepare(
		"SELECT item_id, access_token, institution_name, products, txn_cursor FROM items WHERE env=?",
	).bind(env.PLAID_ENV).all<Item>();
	const out: any[] = [];
	for (const item of items) {
		const res: any = { institution: item.institution_name, notes: [] as string[] };
		const products = new Set((item.products ?? "").split(","));
		const steps: [string, (env: Env, item: Item) => Promise<unknown>][] = [["accounts", syncAccounts]];
		if (products.has("transactions")) steps.push(["transactions", syncTransactions]);
		if (products.has("investments")) steps.push(["investments", syncInvestments]);
		let hardError: string | null = null;
		for (const [name, fn] of steps) {
			try {
				res[name] = await fn(env, item);
			} catch (e) {
				if (e instanceof PlaidError && SOFT_ERRORS.has(e.code)) {
					res.notes.push(`${name}: ${e.message}`);
				} else {
					hardError = `${name}: ${(e as Error).message}`;
					if (e instanceof PlaidError && e.code === "ITEM_LOGIN_REQUIRED") hardError += " -> re-link this institution";
					res.error = hardError;
					break;
				}
			}
		}
		await env.DB.prepare("UPDATE items SET last_synced=?, last_error=? WHERE item_id=?")
			.bind(now(), hardError, item.item_id).run();
		out.push(res);
	}
	return out;
}
