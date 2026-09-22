/* Rings settings. Self-contained: no framework, no CDN, no build step (ADR-0043 §27).
 *
 * Two rules, the same two the chat surface follows:
 *
 * 1. Durable state is authoritative: every action refetches the safe account list.
 * 2. Everything a user or a provider wrote is *text*: it reaches the DOM through textContent.
 *
 * This file never sees a password. The only credential-shaped thing it knows is a variable *name*,
 * which the server sends so the page can tell the user where to put a secret — and which it never
 * tries to send back.
 */

"use strict";

const CSRF_COOKIE = "ga_mobile_csrf";
const CSRF_HEADER = "X-CSRF-Token";

const OUTCOME_WORDS = {
  ok: "连接与登录都正常",
  reachable: "能连上，但还没有可用凭据，未验证登录",
  credential_missing: "缺少凭据",
  not_configured: "还没有配置",
  connect_failed: "连接不上服务器",
  tls_failed: "TLS 握手失败",
  authentication_failed: "服务器拒绝了这个凭据",
  mailbox_unavailable: "连上了，但打不开这个收件箱",
  server_error: "服务器返回了错误"
};

function cookie(name) {
  const parts = document.cookie ? document.cookie.split(";") : [];
  for (const part of parts) {
    const [key, ...rest] = part.trim().split("=");
    if (key === name) {
      return decodeURIComponent(rest.join("="));
    }
  }
  return null;
}

async function api(method, path, body) {
  const headers = { Accept: "application/json" };
  if (body !== undefined) {
    headers["Content-Type"] = "application/json";
  }
  if (method !== "GET") {
    const csrf = cookie(CSRF_COOKIE);
    if (csrf) {
      headers[CSRF_HEADER] = csrf;
    }
  }
  const response = await fetch(path, {
    method,
    headers,
    credentials: "same-origin",
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  let payload = null;
  try {
    payload = await response.json();
  } catch (error) {
    payload = null;
  }
  if (!response.ok) {
    const failure = new Error((payload && payload.error) || "请求没有成功");
    failure.status = response.status;
    throw failure;
  }
  return payload;
}

function announce(text) {
  const live = document.getElementById("live-status");
  if (live) {
    live.textContent = text;
  }
}

function text(value) {
  return value === null || value === undefined || value === "" ? "—" : String(value);
}

function el(tag, className, content) {
  const node = document.createElement(tag);
  if (className) {
    node.className = className;
  }
  if (content !== undefined) {
    node.textContent = content;
  }
  return node;
}

function definition(label, value) {
  return [el("dt", null, label), el("dd", null, text(value))];
}

// ------------------------------------------------------------------- accounts

async function loadAccounts() {
  const list = document.getElementById("account-list");
  const payload = await api("GET", "/api/settings/mail/accounts");
  list.replaceChildren();
  if (!payload.accounts || payload.accounts.length === 0) {
    list.appendChild(
      el("li", "muted small", "还没有配置邮箱账号。添加一个之后，收邮件和发邮件才会可用。")
    );
    return payload;
  }
  payload.accounts.forEach((account) => list.appendChild(renderAccount(account)));
  return payload;
}

function renderAccount(account) {
  const item = el("li", "account");
  const head = el("div", "account-head");
  head.appendChild(el("span", "account-id", account.id));
  head.appendChild(
    el("span", `tag ${account.enabled ? "ok" : "off"}`, account.enabled ? "已启用" : "已停用")
  );
  head.appendChild(
    el(
      "span",
      `tag ${account.receive_ready ? "ok" : "off"}`,
      account.receive_ready ? "可以收信" : "收信未就绪"
    )
  );
  head.appendChild(
    el(
      "span",
      `tag ${account.send_ready ? "ok" : "off"}`,
      account.send_ready ? "可以发信" : "发信未就绪"
    )
  );
  item.appendChild(head);

  const facts = el("dl");
  const imap = account.imap;
  const smtp = account.smtp;
  definition("收件", `${text(imap.host)}:${text(imap.port)}（${text(imap.security)}）`).forEach((n) =>
    facts.appendChild(n)
  );
  definition(
    "发件",
    smtp.configured ? `${text(smtp.host)}:${text(smtp.port)}（${text(smtp.security)}）` : "未配置"
  ).forEach((n) => facts.appendChild(n));
  definition("用户名", account.username).forEach((n) => facts.appendChild(n));
  definition("收件箱", account.mailbox).forEach((n) => facts.appendChild(n));
  definition("发件地址", account.from_address).forEach((n) => facts.appendChild(n));
  const credential = account.credential;
  definition(
    "凭据",
    credential.configured
      ? `已配置（来自环境变量 ${credential.reference}）`
      : `未配置（需要环境变量 ${credential.reference}）`
  ).forEach((n) => facts.appendChild(n));
  item.appendChild(facts);

  const row = el("div", "row");
  row.appendChild(probeButton(account, "测试收信", "test-imap", item));
  row.appendChild(probeButton(account, "测试发信连接", "test-smtp", item));
  row.appendChild(editButton(account));
  row.appendChild(toggleButton(account));
  item.appendChild(row);
  return item;
}

function probeButton(account, label, verb, item) {
  const button = el("button", null, label);
  button.type = "button";
  button.addEventListener("click", async () => {
    button.disabled = true;
    const existing = item.querySelector(".probe");
    if (existing) {
      existing.remove();
    }
    try {
      const report = await api(
        "POST",
        `/api/settings/mail/accounts/${encodeURIComponent(account.id)}/${verb}`
      );
      const line = el(
        "p",
        `probe ${report.reachable ? "ok" : "bad"}`,
        `${OUTCOME_WORDS[report.outcome] || report.outcome}（${report.detail}）`
      );
      item.appendChild(line);
      announce(line.textContent);
    } catch (error) {
      const line = el("p", "probe bad", error.message);
      item.appendChild(line);
      announce(error.message);
    } finally {
      button.disabled = false;
    }
  });
  return button;
}

function editButton(account) {
  const button = el("button", null, "编辑");
  button.type = "button";
  button.addEventListener("click", () => openForm(account));
  return button;
}

function toggleButton(account) {
  const button = el("button", null, account.enabled ? "停用" : "启用");
  button.type = "button";
  button.addEventListener("click", async () => {
    button.disabled = true;
    try {
      const draft = { ...account };
      draft.enabled = !account.enabled;
      const result = await api(
        "PATCH",
        `/api/settings/mail/accounts/${encodeURIComponent(account.id)}`,
        { account: draft }
      );
      announce(result.detail);
      await refresh();
    } catch (error) {
      announce(error.message);
    } finally {
      button.disabled = false;
    }
  });
  return button;
}

// ---------------------------------------------------------------------- form

function openForm(account) {
  const form = document.getElementById("account-form");
  form.hidden = false;
  form.dataset.editing = account ? account.id : "";
  document.getElementById("form-title").textContent = account ? `编辑 ${account.id}` : "添加账号";
  form.reset();
  if (account) {
    form.elements.id.value = account.id;
    form.elements.id.readOnly = true;
    form.elements.host.value = account.imap.host || "";
    form.elements.port.value = account.imap.port || 993;
    form.elements.username.value = account.username || "";
    form.elements.mailbox.value = account.mailbox || "INBOX";
    form.elements.smtp_host.value = account.smtp.host || "";
    form.elements.smtp_port.value = account.smtp.port || "";
    form.elements.smtp_security.value = account.smtp.security || "";
    form.elements.from_address.value = account.from_address || "";
  } else {
    form.elements.id.readOnly = false;
    form.elements.mailbox.value = "INBOX";
    form.elements.port.value = 993;
  }
  refreshSecretReference();
}

function refreshSecretReference() {
  const form = document.getElementById("account-form");
  const id = (form.elements.id.value || "<ACCOUNT_ID>").toUpperCase().replace(/-/g, "_");
  document.getElementById("secret-reference").textContent =
    `GROWING_ASSISTANT_MAIL_${id}_PASSWORD`;
}

async function submitForm(event) {
  event.preventDefault();
  const form = event.target;
  const editing = form.dataset.editing;
  const draft = {
    id: form.elements.id.value.trim(),
    host: form.elements.host.value.trim(),
    port: Number(form.elements.port.value) || 993,
    username: form.elements.username.value.trim(),
    mailbox: form.elements.mailbox.value.trim() || "INBOX",
    enabled: true,
    smtp_host: form.elements.smtp_host.value.trim() || null,
    smtp_port: form.elements.smtp_port.value ? Number(form.elements.smtp_port.value) : null,
    smtp_security: form.elements.smtp_security.value || null,
    smtp_username: form.elements.smtp_username.value.trim() || null,
    from_address: form.elements.from_address.value.trim() || null,
    sent_mailbox: form.elements.sent_mailbox.value.trim() || null,
  };
  try {
    const result = editing
      ? await api("PATCH", `/api/settings/mail/accounts/${encodeURIComponent(editing)}`, {
          account: draft,
        })
      : await api("POST", "/api/settings/mail/accounts", { account: draft });
    form.hidden = true;
    announce(result.detail);
    await refresh();
  } catch (error) {
    announce(error.message);
  }
}

// -------------------------------------------------------------------- system

async function loadCapabilities() {
  const facts = document.getElementById("system-facts");
  try {
    const payload = await api("GET", "/api/chat/bootstrap");
    facts.replaceChildren();
    const home = payload.home || {};
    definition("对话", "可用").forEach((n) => facts.appendChild(n));
    definition(
      "需要处理",
      home.attention_total === undefined ? "—" : String(home.attention_total)
    ).forEach((n) => facts.appendChild(n));
  } catch (error) {
    facts.replaceChildren();
    definition("状态", error.status === 401 ? "还没有配对这个浏览器" : "暂时读不到").forEach((n) =>
      facts.appendChild(n)
    );
  }
}

async function loadPlanning() {
  const facts = document.getElementById("planning-facts");
  facts.replaceChildren();
  let payload = null;
  try {
    payload = await api("GET", "/api/settings/planning");
  } catch (error) {
    payload = null;
  }
  if (!payload || !payload.available) {
    definition("计划", "这台主机还没有配置计划时区").forEach((n) => facts.appendChild(n));
    return;
  }
  const prefs = payload.preferences || {};
  definition("计划时区", `${text(payload.timezone)}（来自 ${payload.timezone_authority}，只读）`).forEach(
    (n) => facts.appendChild(n)
  );
  definition("计划时段", `${text(prefs.day_start_local)}–${text(prefs.day_end_local)}`).forEach((n) =>
    facts.appendChild(n)
  );
  definition("每天上限", `${text(prefs.max_daily_hours)} 小时`).forEach((n) =>
    facts.appendChild(n)
  );
  definition(
    "单次时长",
    `通常 ${text(prefs.preferred_block_minutes)} 分钟，最长 ${text(prefs.max_block_minutes)} 分钟`
  ).forEach((n) => facts.appendChild(n));
  definition("计划提案", "需要你确认之后才会生效").forEach((n) => facts.appendChild(n));
}

async function refresh() {
  try {
    await loadAccounts();
  } catch (error) {
    const status = document.getElementById("mail-status");
    status.hidden = false;
    status.textContent =
      error.status === 401 ? "这个浏览器还没有与 Rings 配对。" : "暂时读不到邮箱设置。";
  }
}

document.addEventListener("DOMContentLoaded", () => {
  document.getElementById("account-form").addEventListener("submit", submitForm);
  document.getElementById("add-account").addEventListener("click", () => openForm(null));
  document
    .getElementById("cancel-form")
    .addEventListener("click", () => (document.getElementById("account-form").hidden = true));
  document.getElementById("account-form").elements.id.addEventListener("input", refreshSecretReference);
  loadPlanning();
  refresh();
  loadCapabilities();
});
