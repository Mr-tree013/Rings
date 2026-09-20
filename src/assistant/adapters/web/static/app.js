"use strict";

// The whole client. No framework, no CDN, no build step — and one rule that matters:
// user-controlled text is written with textContent (or as an attribute value), never as HTML.
// A task title, a mail subject or an action payload is data here, not markup.

const CSRF_COOKIE = "ga_mobile_csrf";
const CSRF_HEADER = "X-CSRF-Token";

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
  return { status: response.status, ok: response.ok, payload };
}

function line(label, value) {
  const item = document.createElement("li");
  const strong = document.createElement("strong");
  strong.textContent = label + ": ";
  const text = document.createElement("span");
  text.textContent = value === null || value === undefined ? "—" : String(value);
  item.appendChild(strong);
  item.appendChild(text);
  return item;
}

function fill(targetId, entries) {
  const list = document.getElementById(targetId);
  if (!list) {
    return;
  }
  list.replaceChildren(...entries);
}

function text(value) {
  return value === null || value === undefined ? "—" : String(value);
}

// ------------------------------------------------------------------ dashboard

async function loadDashboard() {
  const me = await api("GET", "/api/me");
  const signedOut = document.getElementById("signed-out");
  const signedIn = document.getElementById("signed-in");
  const connection = document.getElementById("connection");
  if (!me.ok) {
    signedOut.hidden = false;
    signedIn.hidden = true;
    connection.textContent = "not paired";
    return;
  }
  signedOut.hidden = true;
  signedIn.hidden = false;
  connection.textContent = "paired; session expires " + text(me.payload.expires_at);

  const dashboard = await api("GET", "/api/dashboard");
  if (dashboard.ok) {
    const data = dashboard.payload;
    fill("summary", [
      line("Open tasks", data.open_task_count),
      line("Open cases", data.open_cases.length),
      line("Unread notifications", data.unread_notification_count),
      line("Approved actions", data.approved_action_count),
      line("Unresolved actions", data.unresolved_action_count),
    ]);
    fill(
      "tasks",
      data.open_tasks.map((task) =>
        line(task.title, [task.priority, task.estimated_minutes ? task.estimated_minutes + "m" : "unestimated", task.deadline ? "due " + task.deadline : "no deadline"].join(" · "))
      )
    );
    fill(
      "notifications",
      data.notifications.map((item) => line(item.title, item.body))
    );
    fill(
      "cases",
      data.open_cases.map((item) => line(item.title, item.status))
    );
    fill(
      "drafts",
      data.recent_drafts.map((item) => line(item.subject, "version " + item.version))
    );
  }
  await loadActions();
}

async function loadActions() {
  const actions = await api("GET", "/api/actions");
  if (!actions.ok) {
    return;
  }
  const entries = actions.payload.actions.map((action) => {
    const item = document.createElement("li");
    const toggle = document.createElement("button");
    toggle.type = "button";
    toggle.textContent = action.type + " · " + action.status + " · " + action.approval;
    const detail = document.createElement("div");
    detail.hidden = true;
    toggle.addEventListener("click", async () => {
      if (!detail.hidden) {
        detail.hidden = true;
        return;
      }
      await showAction(action.id, detail);
      detail.hidden = false;
    });
    item.appendChild(toggle);
    item.appendChild(detail);
    return item;
  });
  fill("actions", entries);
}

async function showAction(id, container) {
  const detail = await api("GET", "/api/actions/" + encodeURIComponent(id));
  container.replaceChildren();
  if (!detail.ok) {
    container.textContent = text(detail.payload && detail.payload.error);
    return;
  }
  const action = detail.payload;
  const summary = document.createElement("ul");
  summary.appendChild(line("Action", action.id));
  summary.appendChild(line("Case", action.case_id));
  summary.appendChild(line("Type", action.type));
  summary.appendChild(line("Status", action.status));
  summary.appendChild(line("Fingerprint", action.fingerprint));
  summary.appendChild(line("Approval", action.approval));
  summary.appendChild(line("Execution", action.execution));
  container.appendChild(summary);

  const payload = document.createElement("pre");
  payload.textContent = action.payload_json;
  container.appendChild(payload);

  const status = document.createElement("p");
  status.className = "muted";
  container.appendChild(status);

  const approveButton = document.createElement("button");
  approveButton.type = "button";
  approveButton.textContent = "Approve this exact action";
  approveButton.addEventListener("click", async () => {
    // The challenge token stays in this closure: it is never stored, never in a URL, never logged.
    const challenge = await api("POST", "/api/actions/" + encodeURIComponent(id) + "/challenge");
    if (!challenge.ok) {
      status.textContent = text(challenge.payload && challenge.payload.error);
      return;
    }
    status.textContent =
      "Review the fingerprint above. Approving covers exactly this content; the host still has to execute it.";
    const approved = await api("POST", "/api/actions/" + encodeURIComponent(id) + "/approve", {
      token: challenge.payload.token,
    });
    if (!approved.ok) {
      status.textContent = text(approved.payload && approved.payload.error);
      return;
    }
    status.textContent = "Approved. Nothing was executed: run pw action execute on the host.";
    approveButton.disabled = true;
    await loadActions();
  });
  container.appendChild(approveButton);
}

// ----------------------------------------------------------------------- pair

async function setupPairing() {
  const form = document.getElementById("pair-form");
  if (!form) {
    return;
  }
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const status = document.getElementById("pair-status");
    const code = document.getElementById("pair-code").value;
    const result = await api("POST", "/api/pair", { token: code });
    if (result.ok) {
      status.textContent = "Paired. Redirecting…";
      window.location.replace("/");
      return;
    }
    status.textContent = text(result.payload && result.payload.error);
  });
}

// -------------------------------------------------------------- approval link

function readFragmentToken() {
  if (!window.location.hash) {
    return null;
  }
  const params = new URLSearchParams(window.location.hash.replace(/^#/, ""));
  return params.get("token");
}

async function setupApprovalLink() {
  const button = document.getElementById("approve-button");
  if (!button) {
    return;
  }
  const actionId = window.location.pathname.split("/").pop();
  const token = readFragmentToken();
  // Strip the fragment immediately: it never reaches the server, and it does not linger in the
  // address bar or in anything the user shares afterwards.
  window.history.replaceState(null, "", window.location.pathname);
  const status = document.getElementById("link-status");
  if (!actionId || !token) {
    status.textContent = "This approval link is incomplete.";
    return;
  }
  const preview = await api("POST", "/api/approval-link/preview", {
    action_id: actionId,
    token: token,
  });
  if (!preview.ok) {
    status.textContent = text(preview.payload && preview.payload.error);
    return;
  }
  document.getElementById("link-status").textContent =
    "Approving below covers exactly this content.";
  const action = preview.payload.action;
  fill("action-summary", [
    line("Action", action.id),
    line("Type", action.type),
    line("Status", action.status),
    line("Fingerprint", action.fingerprint),
    line("Approval", action.approval),
    line("Execution", action.execution),
  ]);
  document.getElementById("action-payload").textContent = action.payload_json;
  document.getElementById("link-detail").hidden = false;
  button.addEventListener("click", async () => {
    const result = await api("POST", "/api/approval-link/approve", {
      action_id: actionId,
      token: token,
    });
    const target = document.getElementById("approve-status");
    if (!result.ok) {
      target.textContent = text(result.payload && result.payload.error);
      return;
    }
    target.textContent = "Approved. Nothing was executed: run pw action execute on the host.";
    button.disabled = true;
  });
}

// ---------------------------------------------------------------------- tasks

async function setupTasks() {
  const form = document.getElementById("task-form");
  if (!form) {
    return;
  }
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const title = document.getElementById("task-title").value;
    const minutes = document.getElementById("task-minutes").value;
    const deadline = document.getElementById("task-deadline").value;
    const body = {
      title: title,
      priority: document.getElementById("task-priority").value,
      estimated_minutes: minutes ? Number(minutes) : null,
      deadline: deadline ? deadline : null,
    };
    const result = await api("POST", "/api/tasks", body);
    if (!result.ok) {
      document.getElementById("connection").textContent = text(result.payload && result.payload.error);
      return;
    }
    document.getElementById("task-title").value = "";
    await loadDashboard();
  });
}

async function setupLogout() {
  const button = document.getElementById("logout");
  if (!button) {
    return;
  }
  button.addEventListener("click", async () => {
    await api("POST", "/api/logout");
    window.location.reload();
  });
}

document.addEventListener("DOMContentLoaded", async () => {
  await setupPairing();
  await setupApprovalLink();
  await setupTasks();
  await setupLogout();
  if (document.getElementById("signed-in")) {
    await loadDashboard();
  }
});
