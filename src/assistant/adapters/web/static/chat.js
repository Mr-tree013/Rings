/* Tree local chat. Self-contained: no framework, no CDN, no build step (ADR-0041 §36, §40-§44).
 *
 * Two rules hold everywhere in this file:
 *
 * 1. Durable state is authoritative. The snapshot is the truth; an SSE frame only says "something
 *    changed", and a reconnect or a reload always refetches.
 * 2. Everything a user, a model, a contact or an email wrote is *text*. It reaches the DOM through
 *    textContent or a text node, never by assigning markup.
 */

(function () {
  "use strict";

  var SESSION_COOKIE = "ga_mobile_session";
  var CSRF_COOKIE = "ga_mobile_csrf";
  var RESYNC_EVENT = "resync.required";
  var ATTENTION_EVENT = "attention.updated";

  var STAGE_LABELS = {
    queued: "已排队，等待 Tree 开始处理…",
    understanding: "Tree 正在理解你的请求…",
    reading_local_state: "Tree 正在查看本地信息…",
    planning: "Tree 正在安排时间…",
    querying_knowledge: "Tree 正在查找资料…",
    preparing_mail: "Tree 正在准备邮件…",
    updating_local_state: "Tree 正在保存变更…",
    waiting_confirmation: "等待你的确认…",
    external_execution: "Tree 正在执行已确认的操作…",
    finalizing: "Tree 正在整理回复…"
  };

  var REQUEST_LABELS = {
    queued: "排队中",
    processing: "处理中",
    completed: "已完成",
    failed: "没有处理成功",
    cancelled: "已取消",
    interrupted: "已中断"
  };

  var state = {
    threadId: null,
    snapshot: null,
    stream: null,
    inFlight: false,
    sending: false,
    nearBottom: true,
    unreadWhileScrolled: false,
    composing: false
  };

  var elements = {};

  document.addEventListener("DOMContentLoaded", function () {
    elements.live = document.getElementById("live-status");
    elements.sidebar = document.getElementById("sidebar");
    elements.threadList = document.getElementById("thread-list");
    elements.newThread = document.getElementById("new-thread");
    elements.closeSidebar = document.getElementById("close-sidebar");
    elements.openSidebar = document.getElementById("open-sidebar");
    elements.title = document.getElementById("thread-title");
    elements.connection = document.getElementById("connection");
    elements.scroller = document.getElementById("scroller");
    elements.welcome = document.getElementById("welcome");
    elements.greeting = document.getElementById("greeting");
    elements.quickActions = document.getElementById("quick-actions");
    elements.brief = document.getElementById("brief");
    elements.messages = document.getElementById("messages");
    elements.queue = document.getElementById("queue");
    elements.cards = document.getElementById("cards");
    elements.activity = document.getElementById("activity");
    elements.composer = document.getElementById("composer");
    elements.input = document.getElementById("input");
    elements.send = document.getElementById("send");
    elements.stop = document.getElementById("stop");
    elements.newMessages = document.getElementById("new-messages");
    elements.attentionButton = document.getElementById("attention-button");
    elements.attentionCount = document.getElementById("attention-count");
    elements.attentionDrawer = document.getElementById("attention-drawer");
    elements.attentionList = document.getElementById("attention-list");
    elements.attentionEmpty = document.getElementById("attention-empty");
    elements.closeAttention = document.getElementById("close-attention");
    elements.pairing = document.getElementById("pairing");
    elements.pairingText = document.getElementById("pairing-text");
    elements.pairingLink = document.getElementById("pairing-link");

    wireComposer();
    wireSidebar();
    wireAttention();
    start();
  });

  // ------------------------------------------------------------------ startup

  function start() {
    loadBootstrap().catch(reportStartupFailure);
  }

  /* Why a start failed is three different things, and v1.2 said the same sentence for all of
   * them. Saying "无法连接主机" when the host is answering perfectly well and merely does not
   * recognise this browser is simply false, and it sends the user to look at the wrong problem. */
  function reportStartupFailure(error) {
    if (error && error.status === 401) {
      if (hasSessionCookie()) {
        showPairing("配对状态已失效，请重新配对。", "重新配对");
      } else {
        showPairing("这个浏览器还没有与 Rings 配对。", "去配对");
      }
      return;
    }
    setConnection("无法连接主机，正在重试…", "offline");
  }

  function showPairing(text, label) {
    setConnection(text, "offline");
    if (!elements.pairing) {
      return;
    }
    elements.pairingText.textContent = text;
    elements.pairingLink.textContent = label;
    elements.pairing.hidden = false;
  }

  function hidePairing() {
    if (elements.pairing) {
      elements.pairing.hidden = true;
    }
  }

  function hasSessionCookie() {
    return new RegExp("(?:^|; )" + SESSION_COOKIE + "=").test(document.cookie);
  }

  function loadBootstrap() {
    return api("GET", "/api/chat/bootstrap").then(function (payload) {
      hidePairing();
      renderThreadList(payload.threads || []);
      renderHome(payload.home);
      loadAttention().catch(function () {});
      var current = payload.current_thread_id;
      if (!current) {
        return createThread();
      }
      return selectThread(current);
    });
  }

  function createThread() {
    return api("POST", "/api/chat/threads").then(function (thread) {
      return loadThreads().then(function () {
        return selectThread(thread.id);
      });
    });
  }

  function loadThreads() {
    return api("GET", "/api/chat/threads").then(function (payload) {
      renderThreadList(payload.threads || []);
    });
  }

  function selectThread(threadId) {
    state.threadId = threadId;
    closeStream();
    return api("GET", "/api/chat/threads/" + threadId + "/snapshot").then(function (snapshot) {
      applySnapshot(snapshot);
      openStream(threadId);
      highlightThread();
      closeSidebar();
    });
  }

  function refreshSnapshot() {
    if (!state.threadId) {
      return Promise.resolve();
    }
    return api("GET", "/api/chat/threads/" + state.threadId + "/snapshot").then(function (
      snapshot
    ) {
      if (snapshot.thread.id === state.threadId) {
        applySnapshot(snapshot);
      }
    });
  }

  // ------------------------------------------------------------------- events

  function openStream(threadId) {
    if (typeof EventSource !== "function") {
      setConnection("这个浏览器不支持实时更新，刷新页面即可。", "offline");
      return;
    }
    setConnection("正在连接…", "connecting");
    var stream = new EventSource("/api/chat/threads/" + threadId + "/events");
    state.stream = stream;
    stream.addEventListener("open", function () {
      setConnection("已连接", "online");
    });
    stream.addEventListener("error", function () {
      // A live stream dropping is a transport problem, and it is the only thing this message ever
      // means. An authentication problem never reaches here: the endpoint answers 401 with JSON,
      // and EventSource reports that as a plain error, so the startup path owns that wording.
      setConnection("实时连接中断，正在恢复…", "offline");
    });
    [
      "request.queued",
      "request.started",
      "request.stage",
      "request.cancel_requested",
      "request.cancelled",
      "request.completed",
      "request.failed",
      "request.interrupted",
      "assistant.message",
      "confirmation.required",
      "confirmation.updated",
      "thread.updated"
    ].forEach(function (name) {
      stream.addEventListener(name, function (event) {
        handleEvent(name, event);
      });
    });
    stream.addEventListener(ATTENTION_EVENT, function () {
      loadAttention().catch(function () {});
    });
    stream.addEventListener(RESYNC_EVENT, function () {
      setConnection("正在重新同步…", "connecting");
      refreshSnapshot().catch(function () {});
      closeStream();
      openStream(threadId);
    });
  }

  function closeStream() {
    if (state.stream) {
      state.stream.close();
      state.stream = null;
    }
  }

  function handleEvent(name, event) {
    var payload = parse(event.data);
    if (name === "assistant.message") {
      announce("Tree 回复了");
    } else if (name === "request.queued") {
      announce("消息已加入队列");
    } else if (name === "request.completed") {
      announce("Tree 处理完成");
    } else if (name === "request.failed") {
      announce("这条消息没有处理成功");
    } else if (name === "confirmation.required") {
      announce("有一项确认需要你处理");
    }
    if (!payload) {
      return;
    }
    // Every frame is a hint. The durable truth arrives in the refreshed snapshot.
    refreshSnapshot().catch(function () {});
  }

  function parse(raw) {
    try {
      return JSON.parse(raw);
    } catch (error) {
      return null;
    }
  }

  // -------------------------------------------------------------- rendering

  function applySnapshot(snapshot) {
    state.snapshot = snapshot;
    state.title = snapshot.thread.display_title;
    elements.title.textContent = snapshot.thread.display_title;
    renderMessages(snapshot.messages || []);
    renderQueue(snapshot.pending || []);
    renderCards(snapshot.cards || []);
    renderActivity(snapshot.active);
    renderHome(snapshot.home);
    renderStop(snapshot.active);
    maybeScroll();
  }

  function renderMessages(messages) {
    var list = elements.messages;
    clear(list);
    messages.forEach(function (message) {
      var item = el("li", "message " + (message.role === "user" ? "user" : "assistant"));
      var role = el("span", "role");
      role.textContent = message.role === "user" ? "你" : "Tree";
      var body = el("span", "body");
      body.textContent = message.text;
      item.appendChild(role);
      item.appendChild(body);
      list.appendChild(item);
    });
  }

  function renderQueue(pending) {
    clear(elements.queue);
    // A queued message is shown as what it is: the user's words, still waiting to be a turn.
    pending.forEach(function (request) {
      var item = el("li", "queued");
      var badge = el("span", "badge");
      badge.textContent = REQUEST_LABELS.queued;
      var text = el("span", "queued-text");
      text.textContent = request.text || "";
      item.appendChild(badge);
      item.appendChild(text);
      if (request.can_cancel) {
        item.appendChild(cancelQueuedButton(request));
      }
      elements.queue.appendChild(item);
    });
  }

  function cancelQueuedButton(request) {
    var button = el("button", "small-button");
    button.type = "button";
    button.textContent = "取消排队";
    button.addEventListener("click", function () {
      button.disabled = true;
      api("POST", "/api/chat/requests/" + request.id + "/cancel")
        .then(refreshSnapshot)
        .catch(function (error) {
          button.disabled = false;
          reportError(error);
        });
    });
    return button;
  }

  function renderActivity(active) {
    if (!active) {
      elements.activity.hidden = true;
      elements.activity.textContent = "";
      return;
    }
    var label = STAGE_LABELS[active.stage] || "Tree 正在处理…";
    elements.activity.hidden = false;
    elements.activity.textContent = label;
  }

  function renderStop(active) {
    state.inFlight = Boolean(active);
    if (active && active.can_cancel) {
      elements.stop.hidden = false;
      elements.stop.disabled = false;
      elements.stop.textContent = "停止";
      elements.stop.title = "";
      return;
    }
    if (active) {
      elements.stop.hidden = false;
      elements.stop.disabled = true;
      elements.stop.textContent = "不能安全停止";
      elements.stop.title = "当前操作已经进入执行阶段，不能安全停止。";
      return;
    }
    elements.stop.hidden = true;
    elements.stop.disabled = false;
  }

  function renderCards(cards) {
    clear(elements.cards);
    cards.forEach(function (card) {
      elements.cards.appendChild(renderCard(card));
    });
  }

  function renderCard(card) {
    var container = el("section", "card " + (card.severity === "high" ? "high" : ""));
    var heading = el("h3");
    heading.textContent = card.title;
    var summary = el("p", "muted small");
    summary.textContent = card.summary;
    container.appendChild(heading);
    container.appendChild(summary);

    if (card.fields && card.fields.length) {
      var list = el("dl");
      card.fields.forEach(function (field) {
        var term = el("dt");
        term.textContent = field.label;
        var value = el("dd");
        value.textContent = field.value;
        list.appendChild(term);
        list.appendChild(value);
      });
      container.appendChild(list);
    }
    (card.items || []).forEach(function (item) {
      if (typeof item.body === "string") {
        var body = el("pre");
        body.textContent = item.body;
        container.appendChild(body);
        return;
      }
      var line = el("li");
      line.textContent = describeItem(item);
      if (!container.querySelector("ul")) {
        container.appendChild(el("ul"));
      }
      container.querySelector("ul").appendChild(line);
    });

    var actions = el("div", "card-actions");
    actions.appendChild(cardButton(card, true));
    actions.appendChild(cardButton(card, false));
    container.appendChild(actions);
    var hint = el("p", "muted small");
    hint.textContent = card.hint;
    container.appendChild(hint);
    return container;
  }

  function describeItem(item) {
    if (item.title && item.start) {
      var window = item.weekday + " " + item.start + "–" + item.end;
      return item.title + "（" + window + "）";
    }
    if (item.title && item.starts_at) {
      return (
        item.title + "：" + localWindow(item.starts_at, item.ends_at) + "（" + item.minutes + " 分钟）"
      );
    }
    return Object.keys(item)
      .map(function (key) {
        return item[key];
      })
      .join(" ");
  }

  function localWindow(starts, ends) {
    return format(starts) + " → " + format(ends);
  }

  function format(value) {
    var parsed = new Date(value);
    if (isNaN(parsed.getTime())) {
      return value;
    }
    return parsed.toLocaleString();
  }

  function cardButton(card, confirm) {
    var button = el("button", confirm ? "primary" : "");
    button.type = "button";
    button.textContent = confirm ? card.confirm_label : card.cancel_label;
    button.addEventListener("click", function () {
      var actions = button.parentNode.querySelectorAll("button");
      Array.prototype.forEach.call(actions, function (item) {
        item.disabled = true;
      });
      var suffix = confirm ? "confirm" : "cancel";
      api(
        "POST",
        "/api/chat/threads/" +
          state.threadId +
          "/confirmations/" +
          encodeURIComponent(card.id) +
          "/" +
          suffix,
        { expected_revision: card.expected_revision }
      )
        .then(refreshSnapshot)
        .catch(function (error) {
          Array.prototype.forEach.call(actions, function (item) {
            item.disabled = false;
          });
          reportError(error);
          return refreshSnapshot();
        });
    });
    return button;
  }

  function renderThreadList(threads) {
    clear(elements.threadList);
    threads.forEach(function (thread) {
      var item = el("li");
      var button = el("button");
      button.type = "button";
      button.textContent = thread.display_title;
      button.dataset.threadId = thread.id;
      button.setAttribute("aria-current", String(thread.id === state.threadId));
      button.addEventListener("click", function () {
        selectThread(thread.id).catch(reportError);
      });
      item.appendChild(button);
      elements.threadList.appendChild(item);
    });
  }

  function highlightThread() {
    var buttons = elements.threadList.querySelectorAll("button");
    Array.prototype.forEach.call(buttons, function (button) {
      button.setAttribute("aria-current", String(button.dataset.threadId === state.threadId));
    });
  }

  function renderHome(home) {
    if (!home) {
      elements.welcome.hidden = true;
      return;
    }
    elements.welcome.hidden = false;
    elements.greeting.textContent = home.greeting || "";
    clear(elements.quickActions);
    (home.quick_actions || []).forEach(function (action) {
      var button = el("button");
      button.type = "button";
      button.textContent = action.label;
      button.addEventListener("click", function () {
        state.input.value = action.text;
        submitMessage();
      });
      elements.quickActions.appendChild(button);
    });
    clear(elements.brief);
    if (home.brief && home.brief.available) {
      elements.brief.hidden = false;
      var title = el("p", "small");
      title.textContent = "今天的概览（" + home.brief.local_date + "）";
      elements.brief.appendChild(title);
      if (home.brief.is_empty) {
        var empty = el("p", "muted small");
        empty.textContent = "今天没有排定的安排。";
        elements.brief.appendChild(empty);
      }
      var list = el("ul");
      (home.brief.lines || []).forEach(function (line) {
        var item = el("li");
        item.textContent = line.group + "：" + line.text;
        list.appendChild(item);
      });
      elements.brief.appendChild(list);
    } else {
      elements.brief.hidden = true;
    }
  }

  // --------------------------------------------------------------- composer

  function wireAttention() {
    if (!elements.attentionButton) {
      return;
    }
    elements.attentionButton.addEventListener("click", function () {
      elements.attentionDrawer.hidden = false;
      loadAttention().catch(function () {});
    });
    elements.closeAttention.addEventListener("click", function () {
      elements.attentionDrawer.hidden = true;
    });
  }

  /* The inbox is durable state, so the browser never derives it: every open, every SSE hint and
   * every settle refetches it. A frame that never arrives costs a refresh, never correctness. */
  function loadAttention() {
    return api("GET", "/api/chat/attention").then(function (payload) {
      renderAttention(payload);
      return payload;
    });
  }

  function renderAttention(payload) {
    if (!elements.attentionButton) {
      return;
    }
    var total = payload.total || 0;
    elements.attentionCount.textContent = String(total);
    elements.attentionButton.hidden = total === 0;
    elements.attentionButton.dataset.severity = highestSeverity(payload.items || []);
    clear(elements.attentionList);
    elements.attentionEmpty.hidden = total !== 0;
    (payload.items || []).forEach(function (item) {
      elements.attentionList.appendChild(renderAttentionItem(item));
    });
    if (payload.overflow) {
      var more = el("li", "muted small");
      more.textContent = "另有 " + payload.overflow + " 项";
      elements.attentionList.appendChild(more);
    }
  }

  function highestSeverity(items) {
    var worst = "info";
    items.forEach(function (item) {
      if (item.severity === "high") {
        worst = "high";
      } else if (item.severity === "normal" && worst !== "high") {
        worst = "normal";
      }
    });
    return worst;
  }

  function renderAttentionItem(item) {
    var row = el("li", "attention-item");
    row.dataset.severity = item.severity;
    var title = el("p", "attention-title");
    title.textContent = item.title;
    row.appendChild(title);
    if (item.summary) {
      var summary = el("p", "muted small");
      summary.textContent = item.summary;
      row.appendChild(summary);
    }
    var actions = el("div", "attention-actions");
    actions.appendChild(attentionAction(item, "知道了", "acknowledge"));
    actions.appendChild(attentionAction(item, "不再提醒", "dismiss"));
    row.appendChild(actions);
    return row;
  }

  function attentionAction(item, label, verb) {
    var button = el("button", verb === "dismiss" ? "" : "primary");
    button.type = "button";
    button.textContent = label;
    button.addEventListener("click", function () {
      button.disabled = true;
      api("POST", "/api/chat/attention/" + item.id + "/" + verb)
        .then(function () {
          announce(verb === "dismiss" ? "好，先不再提醒这一条。" : "好，这一条已经标记为看过。");
          return loadAttention();
        })
        .catch(function (error) {
          if (error && error.status === 404) {
            announce("这条提醒已经不在列表里了。");
            return loadAttention();
          }
          reportError(error);
        })
        .then(function () {
          button.disabled = false;
        });
    });
    return button;
  }

  function wireComposer() {
    elements.input.addEventListener("compositionstart", function () {
      state.composing = true;
    });
    elements.input.addEventListener("compositionend", function () {
      state.composing = false;
    });
    elements.input.addEventListener("input", autosize);
    elements.input.addEventListener("keydown", function (event) {
      if (event.key !== "Enter" || event.shiftKey) {
        return;
      }
      // A Chinese IME uses Enter to accept a candidate. Submitting here would turn "选词" into
      // "发送", which is the single most annoying bug a chat input can have (ADR-0041 §39).
      if (state.composing || event.isComposing || event.keyCode === 229) {
        return;
      }
      event.preventDefault();
      submitMessage();
    });
    elements.composer.addEventListener("submit", function (event) {
      event.preventDefault();
      submitMessage();
    });
    elements.stop.addEventListener("click", function () {
      stopActive();
    });
    elements.newMessages.addEventListener("click", function () {
      scrollToBottom();
    });
    elements.scroller.addEventListener("scroll", function () {
      var distance =
        elements.scroller.scrollHeight - elements.scroller.scrollTop - elements.scroller.clientHeight;
      state.nearBottom = distance < 80;
      if (state.nearBottom) {
        state.unreadWhileScrolled = false;
        elements.newMessages.hidden = true;
      }
    });
  }

  function autosize() {
    elements.input.style.height = "auto";
    elements.input.style.height = Math.min(elements.input.scrollHeight, 192) + "px";
  }

  function submitMessage() {
    var text = elements.input.value;
    if (!text.trim() || state.sending || !state.threadId) {
      return;
    }
    state.sending = true;
    elements.send.disabled = true;
    var clientRequestId = newClientRequestId();
    api("POST", "/api/chat/threads/" + state.threadId + "/messages", {
      client_request_id: clientRequestId,
      text: text
    })
      .then(function () {
        elements.input.value = "";
        autosize();
        return refreshSnapshot();
      })
      .catch(function (error) {
        reportError(error);
      })
      .then(function () {
        state.sending = false;
        elements.send.disabled = false;
        elements.input.focus();
      });
  }

  function stopActive() {
    var active = state.snapshot && state.snapshot.active;
    if (!active) {
      return;
    }
    elements.stop.disabled = true;
    elements.stop.textContent = "正在停止…";
    api("POST", "/api/chat/requests/" + active.id + "/cancel")
      .then(function () {
        announce("已请求停止，等待 Tree 确认…");
        return refreshSnapshot();
      })
      .catch(function (error) {
        reportError(error);
        return refreshSnapshot();
      });
  }

  function wireSidebar() {
    elements.newThread.addEventListener("click", function () {
      createThread().catch(reportError);
    });
    elements.openSidebar.addEventListener("click", function () {
      elements.sidebar.classList.add("open");
    });
    elements.closeSidebar.addEventListener("click", function () {
      closeSidebar();
    });
  }

  function closeSidebar() {
    elements.sidebar.classList.remove("open");
  }

  // ------------------------------------------------------------------ plumbing

  function newClientRequestId() {
    if (window.crypto && typeof window.crypto.randomUUID === "function") {
      return window.crypto.randomUUID();
    }
    return "req-" + Date.now() + "-" + Math.random().toString(16).slice(2) + "-000000";
  }

  function csrfToken() {
    var match = document.cookie.match(new RegExp("(?:^|; )" + CSRF_COOKIE + "=([^;]*)"));
    return match ? decodeURIComponent(match[1]) : "";
  }

  function api(method, url, body) {
    var headers = {};
    var options = { method: method, credentials: "same-origin", headers: headers };
    // Every mutation carries the CSRF header, body or not: creating a conversation is a mutation
    // even though it has nothing to say.
    if (method !== "GET") {
      headers["X-CSRF-Token"] = csrfToken();
    }
    if (body !== undefined) {
      headers["Content-Type"] = "application/json";
      options.body = JSON.stringify(body);
    }
    return fetch(url, options).then(function (response) {
      return response.text().then(function (raw) {
        var payload = parse(raw) || {};
        if (!response.ok) {
          var error = new Error(payload.error || "请求没有成功");
          error.code = payload.code;
          error.status = response.status;
          throw error;
        }
        return payload;
      });
    });
  }

  function reportError(error) {
    var code = error && error.code;
    if (code === "STALE_CONFIRMATION") {
      announce("这项确认已经过期，我已刷新最新状态。");
      return;
    }
    if (code === "CANNOT_CANCEL_SAFELY") {
      announce("当前操作已经进入执行阶段，不能安全停止。");
      return;
    }
    if (error && error.status === 401) {
      if (hasSessionCookie()) {
        showPairing("配对状态已失效，请重新配对。", "重新配对");
      } else {
        showPairing("这个浏览器还没有与 Rings 配对。", "去配对");
      }
      announce("这个浏览器需要先配对才能继续。");
      return;
    }
    announce("这条消息暂时没有处理成功，你可以重试。");
  }

  function announce(text) {
    elements.live.textContent = text;
  }

  function setConnection(text, state_) {
    elements.connection.textContent = text;
    elements.connection.dataset.state = state_;
  }

  function maybeScroll() {
    if (state.nearBottom) {
      scrollToBottom();
      return;
    }
    state.unreadWhileScrolled = true;
    elements.newMessages.hidden = false;
  }

  function scrollToBottom() {
    elements.scroller.scrollTop = elements.scroller.scrollHeight;
    elements.newMessages.hidden = true;
    state.nearBottom = true;
  }

  function el(tag, className) {
    var node = document.createElement(tag);
    if (className) {
      node.className = className;
    }
    return node;
  }

  function clear(node) {
    while (node.firstChild) {
      node.removeChild(node.firstChild);
    }
  }
})();
