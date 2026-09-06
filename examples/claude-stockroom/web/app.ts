// Stockroom frontend (plain TypeScript, compiled with bun)

interface Warehouse { id: number; name: string; }
interface Summary {
  product_count: number;
  available_units: number;
  inventory_value_cents: number;
  low_stock_count: number;
}
interface InventoryItem {
  product_id: number;
  sku: string;
  name: string;
  warehouse_id: number;
  warehouse_name: string;
  on_hand: number;
  reserved: number;
  available: number;
  price_cents: number;
  reorder_point: number;
}
interface InventoryResp { total: number; items: InventoryItem[]; }

const $ = <T extends HTMLElement>(sel: string): T => {
  const el = document.querySelector(sel);
  if (!el) throw new Error("missing element " + sel);
  return el as T;
};

const state = {
  limit: 20,
  offset: 0,
  q: "",
  warehouseId: "",
  total: 0,
  warehouses: [] as Warehouse[],
};

function money(cents: number): string {
  return (cents / 100).toLocaleString(undefined, { style: "currency", currency: "USD" });
}

async function api<T>(url: string, opts?: RequestInit): Promise<T> {
  const res = await fetch(url, opts);
  const data = await res.json().catch(() => ({ error: "invalid response" }));
  if (!res.ok) {
    throw new Error((data && (data as any).error) || ("request failed " + res.status));
  }
  return data as T;
}

function loadSummary() {
  api<Summary>("/api/summary").then((s) => {
    $("#card-products .card-value").textContent = String(s.product_count);
    $("#card-units .card-value").textContent = String(s.available_units);
    $("#card-value .card-value").textContent = money(s.inventory_value_cents);
    $("#card-low .card-value").textContent = String(s.low_stock_count);
  }).catch((e) => console.error(e));
}

function loadWarehouses() {
  api<Warehouse[]>("/api/warehouses").then((ws) => {
    state.warehouses = ws;
    const filter = $<HTMLSelectElement>("#warehouse-filter");
    ws.forEach((w) => {
      const opt = document.createElement("option");
      opt.value = String(w.id);
      opt.textContent = w.name;
      filter.appendChild(opt);
    });
    const from = $<HTMLSelectElement>("#tf-from");
    const to = $<HTMLSelectElement>("#tf-to");
    [from, to].forEach((sel) => {
      const keep = sel.value;
      sel.innerHTML = "";
      ws.forEach((w) => {
        const o = document.createElement("option");
        o.value = String(w.id);
        o.textContent = w.name;
        sel.appendChild(o);
      });
      if (keep) sel.value = keep;
    });
  }).catch((e) => console.error(e));
}

function loadInventory() {
  const body = $<HTMLTableSectionElement>("#inventory-body");
  body.innerHTML = '<tr><td colspan="8" class="empty">Loading&hellip;</td></tr>';
  const params = new URLSearchParams();
  params.set("limit", String(state.limit));
  params.set("offset", String(state.offset));
  if (state.q) params.set("q", state.q);
  if (state.warehouseId) params.set("warehouse_id", state.warehouseId);
  api<InventoryResp>("/api/inventory?" + params.toString()).then((r) => {
    state.total = r.total;
    renderTable(r.items);
    updatePager(r.total);
  }).catch((e) => {
    body.innerHTML = '<tr><td colspan="8" class="empty">Error: ' + escapeHtml(String(e.message)) + '</td></tr>';
    const prev = $<HTMLButtonElement>("#prev-page");
    const next = $<HTMLButtonElement>("#next-page");
    prev.disabled = true; next.disabled = true;
    $("#page-info").textContent = "";
  });
}

function renderTable(items: InventoryItem[]) {
  const body = $<HTMLTableSectionElement>("#inventory-body");
  if (items.length === 0) {
    body.innerHTML = '<tr><td colspan="8" class="empty">No inventory matches.</td></tr>';
    return;
  }
  body.innerHTML = "";
  for (const it of items) {
    const tr = document.createElement("tr");
    const cells = [
      it.sku, it.name, it.warehouse_name, String(it.on_hand),
      String(it.reserved), String(it.available), money(it.price_cents),
      String(it.reorder_point),
    ];
    for (const c of cells) {
      const td = document.createElement("td");
      td.textContent = c;
      tr.appendChild(td);
    }
    body.appendChild(tr);
  }
}

function updatePager(total: number) {
  const prev = $<HTMLButtonElement>("#prev-page");
  const next = $<HTMLButtonElement>("#next-page");
  const maxPage = Math.max(1, Math.ceil(total / state.limit));
  const page = Math.floor(state.offset / state.limit) + 1;
  prev.disabled = state.offset === 0;
  next.disabled = state.offset + state.limit >= total;
  $("#page-info").textContent = "Page " + page + " of " + maxPage + " (" + total + " rows)";
}

function escapeHtml(s: string): string {
  return s.replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c] as string));
}

function setup() {
  loadSummary();
  loadWarehouses();
  loadInventory();

  const search = $<HTMLInputElement>("#search");
  let debounce = 0;
  search.addEventListener("input", () => {
    clearTimeout(debounce);
    debounce = window.setTimeout(() => {
      state.q = search.value;
      state.offset = 0;
      loadInventory();
    }, 200);
  });

  const filter = $<HTMLSelectElement>("#warehouse-filter");
  filter.addEventListener("change", () => {
    state.warehouseId = filter.value;
    state.offset = 0;
    loadInventory();
  });

  $<HTMLButtonElement>("#prev-page").addEventListener("click", () => {
    state.offset = Math.max(0, state.offset - state.limit);
    loadInventory();
  });
  $<HTMLButtonElement>("#next-page").addEventListener("click", () => {
    state.offset = state.offset + state.limit;
    loadInventory();
  });

  const form = $<HTMLFormElement>("#transfer-form");
  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const msg = $("#transfer-msg");
    msg.className = "form-msg";
    // Gather and validate client-side
    const product = $<HTMLInputElement>("#tf-product").value.trim();
    const from = $<HTMLSelectElement>("#tf-from").value;
    const to = $<HTMLSelectElement>("#tf-to").value;
    const qty = $<HTMLInputElement>("#tf-qty").value.trim();
    const key = $<HTMLInputElement>("#tf-key").value.trim();

    const productNum = Number(product);
    const qtyNum = Number(qty);
    if (!Number.isInteger(productNum) || productNum <= 0) {
      msg.textContent = "Product ID must be a positive integer."; msg.className = "form-msg err"; return;
    }
    if (!from || !to) {
      msg.textContent = "Select both warehouses."; msg.className = "form-msg err"; return;
    }
    if (from === to) {
      msg.textContent = "From and to warehouses must differ."; msg.className = "form-msg err"; return;
    }
    if (!Number.isInteger(qtyNum) || qtyNum <= 0) {
      msg.textContent = "Quantity must be a positive integer."; msg.className = "form-msg err"; return;
    }
    if (!key) {
      msg.textContent = "Idempotency key is required."; msg.className = "form-msg err"; return;
    }
    if (key.length > 128) {
      msg.textContent = "Idempotency key max 128 chars."; msg.className = "form-msg err"; return;
    }

    const btn = $<HTMLButtonElement>("#tf-submit");
    btn.disabled = true;
    msg.textContent = "Submitting&hellip;";
    msg.className = "form-msg";
    try {
      const r = await api<{ ok: boolean }>("/api/transfers", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          product_id: productNum,
          from_warehouse_id: Number(from),
          to_warehouse_id: Number(to),
          quantity: qtyNum,
          idempotency_key: key,
        }),
      });
      msg.textContent = "Transfer complete.";
      msg.className = "form-msg ok";
      loadSummary();
      loadInventory();
      // reset key for next transfer
      $<HTMLInputElement>("#tf-key").value = "";
    } catch (e) {
      msg.textContent = String((e as Error).message);
      msg.className = "form-msg err";
    } finally {
      btn.disabled = false;
    }
  });
}

document.addEventListener("DOMContentLoaded", setup);
