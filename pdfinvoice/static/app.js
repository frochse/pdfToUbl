"use strict";

const drop = document.getElementById("drop");
const picker = document.getElementById("picker");
const statusBox = document.getElementById("status");
const toolbar = document.getElementById("toolbar");
const summary = document.getElementById("summary");
const results = document.getElementById("results");

let state = { results: [], csv: "" };
const mailAvailable = document.body.dataset.mailAvailable === "yes";

document.getElementById("browse").addEventListener("click", () => picker.click());
picker.addEventListener("change", () => upload(picker.files));

["dragenter", "dragover"].forEach((event) =>
  drop.addEventListener(event, (e) => {
    e.preventDefault();
    drop.classList.add("over");
  })
);
["dragleave", "drop"].forEach((event) =>
  drop.addEventListener(event, (e) => {
    e.preventDefault();
    drop.classList.remove("over");
  })
);
drop.addEventListener("drop", (e) => upload(e.dataTransfer.files));

document.getElementById("clear").addEventListener("click", () => {
  state = { results: [], csv: "" };
  results.replaceChildren();
  toolbar.hidden = true;
  setStatus(null);
  picker.value = "";
});

document.querySelectorAll("[data-download]").forEach((button) =>
  button.addEventListener("click", () => download(button.dataset.download))
);

async function upload(fileList) {
  const files = [...fileList].filter((f) => /\.pdf$/i.test(f.name));
  if (!files.length) {
    setStatus("Dat zijn geen PDF-bestanden.", true);
    return;
  }

  const body = new FormData();
  files.forEach((file) => body.append("files", file));
  body.append("date_order", document.getElementById("date-order").value);
  body.append("ocr", document.getElementById("ocr").value);
  body.append("currency", document.getElementById("currency").value || "EUR");

  const meervoud = files.length > 1 ? "en" : "";
  setStatus(`Bezig met verwerken van ${files.length} bestand${meervoud}…`);
  try {
    const response = await fetch("/api/parse", { method: "POST", body });
    // A crash or a proxy can answer with an HTML page; say so in words rather
    // than letting JSON.parse complain about "<".
    const text = await response.text();
    let payload;
    try {
      payload = JSON.parse(text);
    } catch {
      throw new Error(`Server gaf ${response.status} ${response.statusText} terug.`);
    }
    if (!response.ok) throw new Error(payload.error || response.statusText);
    state = payload;
    render(payload);
  } catch (error) {
    setStatus(error.message || "Uploaden mislukt.", true);
  }
}

function setStatus(text, isError = false) {
  statusBox.hidden = text === null;
  statusBox.textContent = text || "";
  statusBox.classList.toggle("error", Boolean(isError));
}

function render(payload) {
  results.replaceChildren();
  payload.results.forEach((entry) => results.append(card(entry)));

  const ok = payload.results.filter((r) => r.ok);
  const flagged = ok.filter((r) => r.invoice.warnings.length).length;
  summary.textContent =
    `${ok.length} van ${payload.results.length} verwerkt` +
    (flagged ? ` · ${flagged} ter controle` : "");
  toolbar.hidden = false;
  setStatus(null);
}

function card(entry) {
  const root = el("article", "card");

  if (!entry.ok) {
    root.append(
      head(entry.file, null, null, badge("err", "mislukt")),
      el("div", "error-body", entry.error)
    );
    return root;
  }

  const inv = entry.invoice;
  const warned = inv.warnings.length > 0;
  const aantal = inv.warnings.length;
  root.append(
    head(
      entry.file,
      inv.invoice_number,
      money(inv.total_gross, inv.currency),
      badge(warned ? "warn" : "ok",
            warned ? `${aantal} waarschuwing${aantal > 1 ? "en" : ""}` : "in orde")
    )
  );

  if (warned) {
    root.append(warningBox("Controleer dit:", inv.warnings));
  }
  // NLCIUS rules Exact will judge the document by, which is a different
  // question from whether the parse looks right.
  const ublWarnings = entry.ubl_warnings || [];
  if (ublWarnings.length) {
    root.append(warningBox("Exact kan dit weigeren:", ublWarnings));
  }

  const panels = [
    ["Overzicht", summaryPanel(inv, entry)],
    ["Factuurregels", linesPanel(inv)],
    ["UBL", codePanel(entry.ubl, `${stem(inv, entry.file)}.xml`, "application/xml")],
    ["JSON", codePanel(JSON.stringify(inv, null, 2), `${stem(inv, entry.file)}.json`, "application/json")],
    ["Brontekst", codePanel(entry.raw_text, `${stem(inv, entry.file)}.txt`, "text/plain")],
  ];

  if (mailAvailable) {
    root.append(mailBar(entry));
  }

  const tabs = el("div", "tabs");
  panels.forEach(([label, panel], index) => {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = label;
    button.setAttribute("aria-selected", String(index === 0));
    button.addEventListener("click", () => {
      tabs.querySelectorAll("button").forEach((b) =>
        b.setAttribute("aria-selected", String(b === button))
      );
      panels.forEach(([, p], i) => (p.hidden = i !== index));
    });
    tabs.append(button);
    panel.hidden = index !== 0;
  });

  root.append(tabs, ...panels.map(([, panel]) => panel));
  return root;
}

function head(name, number, total, badgeEl) {
  const bar = el("header", "card-head");
  bar.append(el("span", "name", name));
  if (number) bar.append(el("span", "num", number));
  bar.append(badgeEl);
  if (total) bar.append(el("span", "total", total));
  return bar;
}

function badge(kind, text) {
  return el("span", `badge ${kind}`, text);
}

function mailBar(entry) {
  const bar = el("div", "mail-bar");
  const note = el("span", "mail-note", "");

  const button = document.createElement("button");
  button.type = "button";
  // Only the UBL: the PDF is embedded in it, and a second attachment makes
  // the purchase inbox open a second document for the same invoice.
  button.textContent = "Mail UBL";
  button.addEventListener("click", async () => {
    const recipient = document.getElementById("mail-to").value.trim();

    const body = new FormData();
    body.append("ubl", entry.ubl);
    body.append("recipient", recipient);
    body.append("stem", stem(entry.invoice, entry.file));
    body.append(
      "subject",
      `Factuur ${entry.invoice.invoice_number || entry.file}` +
        (entry.invoice.supplier.name ? ` — ${entry.invoice.supplier.name}` : "")
    );

    button.disabled = true;
    note.textContent = "Concept wordt aangemaakt in Mail…";
    try {
      const response = await fetch("/api/email", { method: "POST", body });
      const text = await response.text();
      let payload;
      try {
        payload = JSON.parse(text);
      } catch {
        throw new Error(`Server gaf ${response.status} terug.`);
      }
      if (!response.ok) throw new Error(payload.error || response.statusText);
      note.textContent =
        `Concept staat klaar in Mail voor ${payload.recipient} ` +
        `(${payload.attachments.join(", ")}) — nog zelf versturen.`;
    } catch (error) {
      note.textContent = error.message;
      note.classList.add("error");
    } finally {
      button.disabled = false;
    }
  });

  bar.append(button, note);
  return bar;
}

function warningBox(title, messages) {
  const box = el("div", "warnings", title);
  const list = document.createElement("ul");
  messages.forEach((message) => list.append(el("li", null, message)));
  box.append(list);
  return box;
}

function summaryPanel(inv, entry) {
  const panel = el("div", "panel");
  const addresses = entry.addresses || {};
  const rows = [
    ["Factuurnummer", inv.invoice_number],
    ["Factuurdatum", inv.invoice_date],
    ["Vervaldatum", inv.due_date],
    ["Ordernummer", inv.order_number],
    ["Debiteurennummer", inv.customer_number],
    ["Betalingskenmerk", inv.payment_reference],
    ["Leverancier", inv.supplier.name],
    ["Btw-nummer leverancier", inv.supplier.vat_number],
    ["KvK-nummer leverancier", inv.supplier.coc_number],
    ["IBAN leverancier", inv.supplier.iban],
    ["Klant", inv.customer.name],
    ["T.a.v.", inv.customer.contact_name],
    ["Btw-nummer klant", inv.customer.vat_number],
    ["Bedrag excl. btw", money(inv.total_net, inv.currency)],
    ["Btw-bedrag", money(inv.total_tax, inv.currency)],
    ["Totaal incl. btw", money(inv.total_gross, inv.currency)],
    ["Btw-tarief", inv.tax_rate === null ? null : `${inv.tax_rate}%`],
  ];
  panel.append(fieldGrid(rows));

  // The split address, not the printed lines: these are the fields the UBL
  // carries, and the ones BR-NL-3 / BR-NL-4 are judged on.
  [["supplier", "leverancier"], ["customer", "klant"]].forEach(([role, naam]) => {
    const address = addresses[role];
    if (!address || !hasAddress(address)) return;
    panel.append(
      el("h4", "sub-head", `Adres ${naam}`),
      fieldGrid([
        ["Straat en huisnummer", address.street],
        ["Extra adresregel", address.additional_street],
        ["Postcode", address.postcode],
        ["Plaats", address.city],
        ["Land", address.country],
      ])
    );
    if (address.lines.length) {
      panel.append(
        el("p", "as-printed", `Zoals op de factuur: ${address.lines.join(" · ")}`)
      );
    }
  });
  return panel;
}

function hasAddress(address) {
  return Boolean(
    address.street || address.city || address.postcode ||
    address.country || address.lines.length
  );
}

function fieldGrid(rows) {
  const grid = el("div", "fields");
  rows.filter(([, value]) => value).forEach(([key, value]) => {
    const field = el("div", "field");
    field.append(el("div", "k", key), el("div", "v", String(value)));
    grid.append(field);
  });
  return grid;
}

function linesPanel(inv) {
  const panel = el("div", "panel");
  if (!inv.lines.length) {
    panel.append(el("p", null, "Geen factuurregels gevonden."));
    return panel;
  }

  const table = document.createElement("table");
  table.innerHTML =
    "<thead><tr><th>Omschrijving</th><th class='num'>Aantal</th>" +
    "<th class='num'>Stuksprijs</th><th class='num'>Btw</th>" +
    "<th class='num'>Bedrag</th></tr></thead>";

  const body = document.createElement("tbody");
  inv.lines.forEach((line) => {
    const row = document.createElement("tr");
    row.append(
      el("td", null, line.description),
      el("td", "num", num(line.quantity)),
      el("td", "num", num(line.unit_price, 2)),
      el("td", "num", line.tax_rate === null ? "" : `${line.tax_rate}%`),
      el("td", "num", num(line.amount, 2))
    );
    body.append(row);
  });

  const foot = document.createElement("tfoot");
  const totals = document.createElement("tr");
  totals.append(
    el("td", null, "Bedrag excl. btw"),
    el("td", null, ""),
    el("td", null, ""),
    el("td", null, ""),
    el("td", "num", num(inv.total_net, 2))
  );
  foot.append(totals);

  table.append(body, foot);
  panel.append(table);
  return panel;
}

function codePanel(text, filename, mime) {
  const panel = el("div", "panel");
  const actions = el("div", "panel-actions");

  const save = document.createElement("button");
  save.type = "button";
  save.textContent = "Downloaden";
  save.addEventListener("click", () => saveBlob(text, filename, mime));

  const copy = document.createElement("button");
  copy.type = "button";
  copy.textContent = "Kopiëren";
  copy.addEventListener("click", async () => {
    await navigator.clipboard.writeText(text);
    copy.textContent = "Gekopieerd";
    setTimeout(() => (copy.textContent = "Kopiëren"), 1200);
  });

  actions.append(save, copy);
  const pre = document.createElement("pre");
  pre.textContent = text;
  panel.append(actions, pre);
  return panel;
}

function download(kind) {
  const parsed = state.results.filter((r) => r.ok);
  if (!parsed.length) return;

  if (kind === "csv") {
    saveBlob(state.csv, "invoices.csv", "text/csv");
  } else if (kind === "json") {
    const payload = parsed.map((r) => r.invoice);
    saveBlob(JSON.stringify(payload, null, 2), "invoices.json", "application/json");
  } else if (kind === "ubl") {
    // One document per invoice: UBL has no multi-invoice envelope.
    parsed.forEach((entry, index) =>
      setTimeout(
        () => saveBlob(entry.ubl, `${stem(entry.invoice, entry.file)}.xml`, "application/xml"),
        index * 150
      )
    );
  }
}

function saveBlob(text, filename, mime) {
  const url = URL.createObjectURL(new Blob([text], { type: `${mime};charset=utf-8` }));
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function stem(inv, fallback) {
  const base = inv.invoice_number || fallback.replace(/\.pdf$/i, "");
  return base.replace(/[^\w.-]+/g, "_");
}

function money(value, currency) {
  if (value === null || value === undefined) return "";
  return `${value.toFixed(2)} ${currency || ""}`.trim();
}

function num(value, decimals) {
  if (value === null || value === undefined) return "";
  return decimals === undefined ? String(value) : value.toFixed(decimals);
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = text;
  return node;
}
