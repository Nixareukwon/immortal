(() => {
  "use strict";

  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
  const csrf = $('meta[name="csrf-token"]').content;
  const state = { data: null, view: "overview", query: "", channel: null, chatDraft: "", chatChannel: "", botBusy: false };
  const labels = {
    music: ["Музыка для OBS", "ВИДЖЕТ НА ЭКРАН", "Spotify, Яндекс Музыка и YouTube Music. Без Twitch и обычных видео."],
    overview: ["Обзор", "ВАШ ЦЕНТР УПРАВЛЕНИЯ", "Ваш бот, чат и сообщество — в одном месте."],
    commands: ["Команды", "ПОЛЬЗОВАТЕЛЬСКИЕ КОМАНДЫ", "Создавайте ответы, алиасы и личные кулдауны."],
    timers: ["Таймеры", "АВТОМАТИЗАЦИЯ ЭФИРА", "Настраивайте автоматические сообщения в чате."],
    users: ["Зрители и баллы", "СООБЩЕСТВО КАНАЛА", "Управляйте балансом зрителей и смотрите таблицу лидеров."],
    moderation: ["Модерация", "БЕЗОПАСНОСТЬ ЧАТА", "Настройте антифлуд, таймауты и запрещённые слова."],
    quotes: ["Цитаты", "ЛУЧШИЕ МОМЕНТЫ", "Сохраняйте и редактируйте цитаты из эфиров."],
    categories: ["Категории", "АЛИАСЫ КАТЕГОРИЙ", "Связывайте короткие названия с категориями Twitch."],
    reference: ["Справочник команд", "ВОЗМОЖНОСТИ БОТА", "Встроенные команды и переменные для ваших ответов."]
  };
  const icons = {
    music: '<path d="M9 18V5l12-2v13M9 9l12-2"/><ellipse cx="6" cy="18" rx="3" ry="3"/><ellipse cx="18" cy="16" rx="3" ry="3"/>',
    dashboard: '<rect x="3" y="3" width="8" height="8" rx="1"/><rect x="13" y="3" width="8" height="5" rx="1"/><rect x="13" y="10" width="8" height="11" rx="1"/><rect x="3" y="13" width="8" height="8" rx="1"/>',
    terminal: '<path d="m4 17 6-6-6-6"/><path d="M12 19h8"/>',
    clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    users: '<path d="M16 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="10" cy="7" r="4"/><path d="M20 21v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75"/>',
    shield: '<path d="M12 22s8-4 8-11V5l-8-3-8 3v6c0 7 8 11 8 11Z"/><path d="m9 12 2 2 4-4"/>',
    quote: '<path d="M3 21c3 0 7-2 7-8V5H3v7h4c0 3-1 5-4 6v3Zm11 0c3 0 7-2 7-8V5h-7v7h4c0 3-1 5-4 6v3Z"/>',
    layers: '<path d="m12 2 9 5-9 5-9-5 9-5Z"/><path d="m3 12 9 5 9-5M3 17l9 5 9-5"/>',
    book: '<path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2Z"/>',
    twitch: '<path d="M4 3h17v12l-5 5h-4l-3 3v-3H4V3Z"/><path d="M10 8v5M16 8v5"/>',
    logout: '<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><path d="m16 17 5-5-5-5M21 12H9"/>',
    menu: '<path d="M4 6h16M4 12h16M4 18h16"/>',
    search: '<circle cx="11" cy="11" r="7"/><path d="m20 20-4-4"/>',
    plus: '<path d="M12 5v14M5 12h14"/>',
    close: '<path d="m18 6-12 12M6 6l12 12"/>',
    edit: '<path d="M12 20h9"/><path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L8 18l-4 1 1-4Z"/>',
    trash: '<path d="M3 6h18M8 6V4h8v2m3 0-1 15H6L5 6m4 4v7m6-7v7"/>',
    play: '<path d="m7 4 13 8-13 8V4Z"/>',
    stop: '<rect x="5" y="5" width="14" height="14" rx="2"/>',
    bot: '<rect x="4" y="7" width="16" height="13" rx="4"/><path d="M12 7V3m-2 0h4M8 12v2m8-2v2m-6 3h4M1 11v5m22-5v5"/>',
    refresh: '<path d="M20 7v5h-5M4 17v-5h5"/><path d="M6.1 6.1A8 8 0 0 1 20 12M4 12a8 8 0 0 0 13.9 5.9"/>',
    'chevron-right': '<path d="m9 5 7 7-7 7"/>',
    chevron: '<path d="m6 9 6 6 6-6"/>',
    arrow: '<path d="M4 12h16m-6-6 6 6-6 6"/>',
    spark: '<path d="m12 3 2.5 6.5L21 12l-6.5 2.5L12 21l-2.5-6.5L3 12l6.5-2.5L12 3Z"/>',
    send: '<path d="m21 3-7 18-4-7-7-4 18-7ZM10 14 21 3"/>',
    activity: '<path d="M3 12h4l3-8 4 16 3-8h4"/>',
    check: '<path d="m4 12.5 5 5L20 6.5"/>'
  };

  function icon(name) {
    return `<svg viewBox="0 0 24 24" aria-hidden="true">${icons[name] || ""}</svg>`;
  }

  function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>"']/g, (char) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
    })[char]);
  }

  function toast(message, type = "success") {
    const item = document.createElement("div");
    item.className = `toast ${type}`;
    item.textContent = message;
    $("#toasts").append(item);
    window.setTimeout(() => {
      item.classList.add("leaving");
      window.setTimeout(() => item.remove(), 320);
    }, 3900);
  }

  async function request(url, options = {}) {
    const method = options.method || "GET";
    const headers = { ...(options.headers || {}) };
    if (method !== "GET") {
      headers["Content-Type"] = "application/json";
      headers["X-CSRF-Token"] = csrf;
    }
    const response = await fetch(url, {
      ...options,
      signal: options.signal || AbortSignal.timeout(15000),
      method,
      headers,
      body: options.body === undefined ? undefined : JSON.stringify(options.body)
    });
    let body = {};
    try {
      body = await response.json();
    } catch {
      if (!response.ok) throw new Error(`Ошибка сервера (${response.status}).`);
    }
    if (response.status === 401) {
      window.location.assign("/login");
      throw new Error("Сессия завершена. Выполните вход снова.");
    }
    if (!response.ok) throw new Error(body.error || `Ошибка сервера (${response.status}).`);
    return body;
  }

  let refreshInFlight = false;
  let renderPending = false;

  async function refresh(showError = true, forceRender = false) {
    if (refreshInFlight) return false;
    refreshInFlight = true;
    try {
      const nextData = await request("/api/state");
      if (state.view === "music" && nextData.identity?.owner) nextData.music = await request("/api/music");
      const changed = JSON.stringify(nextData) !== JSON.stringify(state.data);
      state.data = nextData;
      renderPending ||= changed;
      updateShell();
      $("#connection-error").hidden = true;
      $("#sync-status").textContent = `Обновлено ${new Date().toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" })}`;
      $("#sync-status").dataset.state = "connected";
      const editing = $("#editor").open || $("#confirm").open || $("#content").contains(document.activeElement)
        || $("#content form[data-dirty=true]") || $("#content form[data-submitting=true]");
      if (forceRender || (renderPending && !editing)) render();
      return true;
    } catch (error) {
      $("#sync-status").textContent = "Нет соединения с панелью";
      $("#sync-status").dataset.state = "error";
      const banner = $("#connection-error");
      banner.textContent = showError ? error.message : "Связь с панелью потеряна. Пробуем подключиться снова…";
      banner.hidden = false;
      if (!state.data) {
        $("#content").innerHTML = empty("Не удалось загрузить панель", "Проверьте подключение и нажмите «Обновить данные» в верхней панели.");
      }
      return false;
    } finally {
      refreshInFlight = false;
      $("#content").setAttribute("aria-busy", "false");
    }
  }

  let contentEnter = true;

  function setView(view) {
    const nextView = Object.hasOwn(labels, view) ? view : "overview";
    if (nextView === state.view && state.data) { closeMenu(); return; }
    state.view = nextView;
    state.query = "";
    $("#search").value = "";
    $("#search").closest(".search-field").hidden = ["overview", "music"].includes(state.view);
    contentEnter = true;
    const [title, eyebrow, description] = labels[state.view];
    $("#breadcrumb-current").textContent = title;
    $("#page-title").textContent = title;
    $("#page-eyebrow").textContent = eyebrow;
    $("#page-description").textContent = description;
    const addLabels = { commands: "Новая команда", timers: "Новый таймер", quotes: "Добавить цитату", categories: "Добавить алиас" };
    $("#page-add").hidden = !addLabels[state.view];
    $("#page-add").disabled = !state.data;
    $("#page-add").dataset.action = `add-${state.view}`;
    $("#page-add span:last-child").textContent = addLabels[state.view] || "Добавить";
    document.title = `${title} · Immortal`;
    $$(".nav-link[data-view]").forEach((link) => {
      const active = link.dataset.view === state.view;
      link.classList.toggle("active", active);
      if (active) link.setAttribute("aria-current", "page");
      else link.removeAttribute("aria-current");
    });
    closeMenu();
    render();
    window.scrollTo(0, 0);
  }

  function matching(items, text) {
    const query = state.query.trim().toLocaleLowerCase("ru-RU");
    return !query || text(items).toLocaleLowerCase("ru-RU").includes(query);
  }

  function empty(title, description = "Попробуйте добавить первую запись.") {
    return `<div class="empty-state"><span class="empty-symbol">${icon("layers")}</span><strong>${escapeHtml(title)}</strong><p>${escapeHtml(description)}</p></div>`;
  }

  function currentChannel(data = state.data) {
    const list = data?.channels || [];
    if (!list.length) return null;
    if (!list.includes(state.channel)) state.channel = list[0];
    return state.channel;
  }

  let channelMenuOpen = false;

  function setChannelMenu(open) {
    const switcher = $("#channel-switcher");
    if (!switcher || channelMenuOpen === open) return;
    channelMenuOpen = open;
    switcher.classList.toggle("is-open", open);
    $("#channel-switch")?.setAttribute("aria-expanded", String(open));
  }

  function channelMenuHtml(channel) {
    const channels = state.data?.channels || [];
    if (!channels.length) return `<p class="channel-menu-empty">Нет подключённых каналов</p>`;
    return channels.map((name, index) => `<button type="button" role="option" aria-selected="${name === channel}" class="channel-option ${name === channel ? "active" : ""}" style="--i:${index}" data-action="select-channel:${escapeHtml(name)}">${icon("twitch")}<span class="option-name">#${escapeHtml(name)}</span><span class="option-check">${icon("check")}</span></button>`).join("");
  }

  function flashChannelName() {
    const name = $("#sidebar-channel-name");
    if (!name) return;
    name.classList.remove("is-switching");
    void name.offsetWidth;
    name.classList.add("is-switching");
    name.addEventListener("animationend", () => name.classList.remove("is-switching"), { once: true });
  }

  function noChannels(data = state.data) {
    return data?.identity?.owner
      ? empty("Нет каналов", "Укажите TWITCH_CHANNELS в файле .env или подключите канал на странице «Обзор».")
      : empty("Канал не подключён", "Подключите бота к своему каналу в разделе «Состояние бота» на странице «Обзор».");
  }

  function actionButton(action, label, symbol, extra = "") {
    return `<button class="icon-button ${extra}" type="button" data-action="${action}" aria-label="${escapeHtml(label)}" title="${escapeHtml(label)}">${icon(symbol)}</button>`;
  }

  function metric(title, value, foot, symbol, view, ratio = null) {
    const progress = ratio === null ? "" : `<div class="metric-track" aria-hidden="true"><span style="--progress:${Math.max(0, Math.min(100, ratio * 100))}%"></span></div>`;
    return `<a href="#${view}" data-view="${view}" class="metric-card"><div class="metric-top"><span>${escapeHtml(title)}</span><span class="metric-icon">${icon(symbol)}</span></div><div class="metric-value">${escapeHtml(value)}</div><div class="metric-foot">${escapeHtml(foot)}</div>${progress}</a>`;
  }

  function runtimeStatus(runtime) {
    if (runtime.connected) return ["online", "Подключён"];
    if (runtime.status === "connecting") return ["connecting", "Подключается"];
    if (runtime.status === "running") return ["connecting", "Переподключение"];
    if (runtime.status === "error") return ["error", "Ошибка"];
    return ["", "Остановлен"];
  }

  function renderOverview(data) {
    const runtime = data.runtime;
    const [statusClass, statusText] = runtimeStatus(runtime);
    const action = runtime.status === "running" || runtime.status === "connecting" ? "stop" : "start";
    const logs = runtime.logs.slice(-8).reverse();
    const selectedChannel = data.channels.includes(state.chatChannel) ? state.chatChannel : data.channels[0];
    const options = data.channels.map((channel) => `<option value="${escapeHtml(channel)}" ${channel === selectedChannel ? "selected" : ""}>#${escapeHtml(channel)}</option>`).join("");
    const protectedChannels = data.moderation.filter((record) => record.settings.enabled).length;
    const channelCount = data.moderation.length;
    const channel = currentChannel(data);
    const stats = (channel && data.stats[channel]) || { commands: 0, timers: 0, active_timers: 0, users: 0, points: 0, quotes: 0 };
    const botCaption = runtime.connected ? "Подключён к Twitch" : runtime.status === "connecting" ? "Устанавливаем соединение…" : "Управление подключением к Twitch";
    const channelsText = data.channels.length ? data.channels.map((channel) => `#${channel}`).join(", ") : "Не настроены";
    const identity = data.identity || { username: "owner", owner: true };
    const channelOwners = data.channel_owners || {};
    const addChannel = data.add_channel || { allowed: false, channel: null, needs_link: false, hint: "" };
    const removableChannel = (channel) => {
      const channelOwner = channelOwners[channel];
      return Boolean(channelOwner) && (identity.owner || channelOwner === identity.username);
    };
    const channelChips = data.channels.length
      ? `<div class="channel-chip-row">${data.channels.map((channel) => `<span class="channel-chip">#${escapeHtml(channel)}${removableChannel(channel) ? `<button class="chip-remove" type="button" data-action="remove-channel:${encodeURIComponent(channel)}" title="Отключить бота от #${escapeHtml(channel)}" aria-label="Отключить бота от #${escapeHtml(channel)}">${icon("close")}</button>` : ""}</span>`).join("")}</div>`
      : "";
    const addChannelButton = addChannel.allowed
      ? `<button class="button secondary small" type="button" data-action="add-channel">${icon("plus")}${identity.owner ? "Подключить канал" : `Подключить мой канал${addChannel.channel ? ` #${escapeHtml(addChannel.channel)}` : ""}`}</button>`
      : "";
    const addChannelHint = !addChannel.allowed && addChannel.hint
      ? `<p class="channel-hint">${escapeHtml(addChannel.hint)}${addChannel.needs_link ? ` <a href="/access">Привязать Twitch</a>` : ""}</p>`
      : "";
    const botControl = identity.owner
      ? `<button class="button ${action === "start" ? "primary" : "secondary"}" data-action="bot-${action}" type="button" ${state.botBusy ? "disabled" : ""}>${icon(action === "start" ? "play" : "stop")}${state.botBusy ? "Подождите…" : action === "start" ? "Запустить бота" : "Остановить бота"}</button>`
      : `<p class="channel-hint">Ботом управляет владелец панели: запуск и остановка доступны только ему.</p>`;
    return `
      <div class="overview-intro">
        <section class="hero" aria-labelledby="hero-title">
          <div class="hero-copy">
            <p class="hero-kicker">${icon("spark")} IMMORTAL AUTOMATION</p>
            <h2 id="hero-title">Больше эфира.<br><span>Меньше рутины.</span></h2>
            <p class="hero-description">Занимайтесь тем, что любите.<br>Команды, таймеры и чат — под контролем.</p>
            <a class="text-link" href="#reference" data-view="reference">Возможности бота ${icon("arrow")}</a>
          </div>
          <div class="hero-art" aria-hidden="true"><img src="/static/automation.svg?v=3" width="480" height="400" alt=""></div>
        </section>
        <section class="panel connection-card" aria-label="Подключение бота">
          <div class="connection-heading"><h2>Состояние бота</h2><span class="status-badge ${statusClass}"><span class="status-inline"></span>${statusText}</span></div>
          <div class="bot-identity"><span class="bot-avatar">${icon("bot")}</span><div><h2 class="bot-name">${escapeHtml(runtime.nick || "Immortal Bot")}</h2><p class="bot-caption">${botCaption}</p></div></div>
          <div class="connection-meta"><span>Каналы</span><strong>${escapeHtml(channelsText)}</strong></div>
          <div class="channel-manager" aria-label="Каналы бота">${channelChips}${addChannelButton}${addChannelHint}</div>
          ${runtime.error ? `<p class="bot-error">${escapeHtml(runtime.error)}</p>` : ""}
          ${botControl}
        </section>
      </div>
      <section class="metric-grid" aria-label="Показатели канала">
        ${metric("Команды", stats.commands, "из 30 пользовательских", "terminal", "commands", stats.commands / 30)}
        ${metric("Активные таймеры", stats.active_timers, `Всего создано: ${stats.timers}`, "clock", "timers", stats.active_timers / 5)}
        ${metric("Зрители", Number(stats.users).toLocaleString("ru-RU"), `${Number(stats.points).toLocaleString("ru-RU")} ${data.points_name} на балансе`, "users", "users")}
        ${metric("Модерация", `${protectedChannels} / ${channelCount}`, "каналов с антифлудом", "shield", "moderation", channelCount ? protectedChannels / channelCount : 0)}
      </section>
      <section class="quick-actions" aria-label="Быстрые действия">
        <div class="quick-heading"><h2>Быстрые действия</h2><span>Всё нужное — в один клик</span></div>
        <div class="quick-grid">${[
          ["add-commands", "terminal", "Новая команда", "Создать ответ для чата"],
          ["add-timers", "clock", "Новый таймер", "Настроить автосообщение"],
          ["open-moderation", "shield", "Модерация", "Управлять защитой чата"]
        ].map(([actionName, symbol, title, description]) => `<button type="button" class="quick-card" data-action="${actionName}"><span class="quick-icon">${icon(symbol)}</span><span><strong>${title}</strong><small>${description}</small></span><span class="quick-arrow">${icon("arrow")}</span></button>`).join("")}</div>
      </section>
      <div class="dashboard-grid">
        <section class="panel chat-panel">
          <div class="panel-heading"><h2 class="panel-title">Сообщение в чат</h2><span class="metric-icon">${icon("twitch")}</span></div>
          <p class="chat-intro">Общайтесь со зрителями от имени бота.</p>
          <form class="chat-form" id="chat-form">
            <label class="field"><span class="field-label">Канал</span><select name="channel" required>${options || '<option value="">Нет настроенных каналов</option>'}</select></label>
            <label class="field"><span class="field-label-row"><span class="field-label">Сообщение</span><span class="character-count" id="chat-count">${state.chatDraft.length} / 450</span></span><textarea name="message" maxlength="450" placeholder="Напишите что-нибудь своему чату…" rows="3" required aria-describedby="chat-hint">${escapeHtml(state.chatDraft)}</textarea></label>
            <div class="chat-form-footer"><span class="chat-hint" id="chat-hint"></span><button class="button primary" type="submit">${icon("send")}Отправить</button></div>
          </form>
        </section>
        <section class="panel activity-panel">
          <div class="panel-heading"><h2 class="panel-title">Последние события</h2><span class="toolbar-note">Журнал бота</span></div>
          ${logs.length ? `<div class="activity-list">${logs.map((log) => `<article class="activity-item"><span class="activity-icon ${escapeHtml(log.level)}">${icon(log.level === "error" ? "shield" : "activity")}</span><div><p class="activity-message">${escapeHtml(log.message)}</p><time class="activity-time" datetime="${new Date(log.time * 1000).toISOString()}">${new Date(log.time * 1000).toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit", second: "2-digit" })}</time></div></article>`).join("")}</div>` : `<div class="activity-empty"><span>${icon("activity")}</span><strong>Всё начинается с подключения</strong><p>${identity.owner ? "Запустите бота — здесь появятся события и сообщения из чата." : "Запуском занимается владелец панели. События бота появятся здесь."}</p></div>`}
        </section>
      </div>`;
  }

  function renderCommands(data) {
    const channel = currentChannel(data);
    if (!channel) return noChannels(data);
    const all = data.commands.filter((item) => item.channel === channel);
    const items = all.filter((item) => matching(item, (value) => `${value.name} ${value.response} ${(value.aliases || []).join(" ")}`));
    return `<section class="panel data-panel"><div class="panel-heading"><div><p class="eyebrow">СВОИ ОТВЕТЫ</p><h2 class="panel-title">Пользовательские команды <span class="count-chip">${all.length}/30</span></h2></div><span class="toolbar-note">Канал #${escapeHtml(channel)} · префикс: ${escapeHtml(data.prefix)}</span></div>${items.length ? `<div class="table-wrap"><table><thead><tr><th>Команда</th><th>Ответ</th><th>Кулдаун</th><th></th></tr></thead><tbody>${items.map((item) => `<tr><td><strong class="command-name">${escapeHtml(data.prefix + item.name)}</strong>${item.aliases?.length ? `<span class="table-secondary">Алиасы: ${escapeHtml(item.aliases.join(", "))}</span>` : ""}</td><td class="table-message text-wrap">${escapeHtml(item.response)}</td><td>${Number(item.cooldown_seconds)} сек.</td><td class="table-action-cell"><div class="table-actions">${actionButton(`edit-command:${encodeURIComponent(item.name)}`, "Изменить", "edit")}${actionButton(`delete-command:${encodeURIComponent(item.name)}`, "Удалить", "trash", "danger")}</div></td></tr>`).join("")}</tbody></table></div>` : empty(state.query ? "Ничего не найдено" : "Пока нет команд", state.query ? "Измените поисковый запрос." : "Добавьте команду и ответ для чата.")}</section><section class="panel section-note"><strong>Подсказка</strong><p>В ответах можно использовать переменные, например <code>{sender}</code>, <code>{query}</code>, <code>{randint 1 100}</code> и <code>{weather}</code>. Полный список находится в справочнике.</p></section>`;
  }

  function renderTimers(data) {
    const channel = currentChannel(data);
    if (!channel) return noChannels(data);
    const all = data.timers.filter((item) => item.channel === channel);
    const items = all.filter((item) => matching(item, (value) => value.message));
    const active = all.filter((item) => item.enabled).length;
    return `<section class="panel data-panel"><div class="panel-heading"><div><p class="eyebrow">АВТОСООБЩЕНИЯ</p><h2 class="panel-title">Таймеры <span class="count-chip">${active}/5 активных</span></h2></div><span class="toolbar-note">Канал #${escapeHtml(channel)} · интервал 1–1440 минут</span></div>${items.length ? `<div class="table-wrap"><table><thead><tr><th>Сообщение</th><th>Интервал</th><th>Состояние</th><th></th></tr></thead><tbody>${items.map((item) => `<tr><td class="table-message text-wrap">${escapeHtml(item.message)}</td><td><span class="timer-interval">${Number(item.interval_minutes)} мин.</span></td><td><span class="timer-state ${item.enabled ? "enabled" : ""}">${item.enabled ? "Включён" : "Выключен"}</span></td><td class="table-action-cell"><div class="table-actions">${actionButton(`edit-timer:${item.id}`, "Изменить", "edit")}${actionButton(`delete-timer:${item.id}`, "Удалить", "trash", "danger")}</div></td></tr>`).join("")}</tbody></table></div>` : empty(state.query ? "Ничего не найдено" : "Нет таймеров", state.query ? "Измените поисковый запрос." : "Создайте таймер для регулярного сообщения.")}</section><p class="result-note">Таймеры отправляются только в свой канал. В них нельзя использовать <code>{sender}</code> и <code>{user}</code>.</p>`;
  }

  function renderUsers(data) {
    const channel = currentChannel(data);
    if (!channel) return noChannels(data);
    const all = data.users.filter((item) => item.channel === channel);
    const items = all.filter((item) => matching(item, (value) => value.name));
    return `<section class="panel data-panel"><div class="panel-heading"><div><p class="eyebrow">ЛИДЕРЫ КАНАЛА</p><h2 class="panel-title">Баланс зрителей <span class="count-chip">${all.length}</span></h2></div><span class="toolbar-note">Канал #${escapeHtml(channel)} · баллы: ${escapeHtml(data.points_name)}</span></div>${items.length ? `<div class="table-wrap"><table><thead><tr><th>Место</th><th>Зритель</th><th>Баланс</th><th></th></tr></thead><tbody>${items.map((item, index) => `<tr><td><span class="rank">${index + 1}</span></td><td><span class="user-name"><span class="user-avatar">${escapeHtml(item.name.slice(0, 1).toUpperCase())}</span>@${escapeHtml(item.name)}</span></td><td><strong class="points-value">${Number(item.points).toLocaleString("ru-RU")}</strong></td><td class="table-action-cell">${actionButton(`edit-points:${encodeURIComponent(item.name)}`, "Изменить баланс", "edit")}</td></tr>`).join("")}</tbody></table></div>` : empty(state.query ? "Ничего не найдено" : "Пока нет зрителей", state.query ? "Измените поисковый запрос." : "Зрители появятся здесь после активности в чате.")}</section><p class="result-note">Баллы считаются отдельно для каждого канала. Изменение баланса задаётся числом: положительное начисляет баллы, отрицательное списывает.</p>`;
  }

  function field(name, label, value, type = "number", attributes = "") {
    if (type === "checkbox") return `<label class="toggle-field"><input type="checkbox" name="${name}" ${value ? "checked" : ""}><span>${escapeHtml(label)}</span></label>`;
    return `<label class="field"><span class="field-label">${escapeHtml(label)}</span><input type="${type}" name="${name}" value="${escapeHtml(value)}" ${attributes}></label>`;
  }

  function renderModeration(data) {
    const channel = currentChannel(data);
    if (!channel) return noChannels(data);
    const selected = data.moderation.find((item) => item.channel === channel);
    if (!selected) return noChannels(data);
    const settings = selected.settings;
    const banned = data.banned_words.filter((item) => item.channel === channel && matching(item, (value) => value.word));
    const defaults = (data.default_banned_words || []).filter((word) => matching(word, (value) => value));
    return `<div class="moderation-layout"><form class="panel moderation-form" id="moderation-form" data-channel="${escapeHtml(selected.channel)}"><div class="panel-heading"><div><p class="eyebrow">АНТИФЛУД</p><h2 class="panel-title">Настройки #${escapeHtml(selected.channel)}</h2></div></div><div class="form-grid">${field("enabled", "Включить антифлуд", settings.enabled, "checkbox")}${field("video", "Проверять видеоссылки", settings.video, "checkbox")}${field("lang", "Проверять язык", settings.lang, "checkbox")}${field("mode", "Режим (1–3)", settings.mode, "number", 'min="1" max="3" required')}${field("size", "Размер сообщения (условные пиксели)", settings.size, "number", 'min="0" max="10000" required')}${field("main_size", "Размер для проверки длинных повторов", settings.main_size, "number", 'min="0" max="10000" required')}${field("percent", "Объём относительно других зрителей (%)", settings.percent, "number", 'min="0" max="100" required')}${field("spam_messages", "Сообщений для спам-фильтра", settings.spam_messages, "number", 'min="0" max="50" required')}${field("spam_seconds", "Интервал спам-фильтра (сек.)", settings.spam_seconds, "number", 'min="1" max="60" required')}${field("max_link_length", "Максимальная длина ссылки", settings.max_link_length, "number", 'min="1" max="2000" required')}${field("timeouts", "Таймауты (сек., через запятую)", settings.timeouts.join(", "), "text", 'required')}</div><button class="button primary form-submit" type="submit">Сохранить настройки</button></form><section class="panel data-panel"><div class="panel-heading"><div><p class="eyebrow">ФИЛЬТР СООБЩЕНИЙ</p><h2 class="panel-title">Запрещённые слова <span class="count-chip">${banned.length}/30 пользовательских</span></h2></div></div><form class="inline-add-form" id="banned-word-form"><input type="hidden" name="channel" value="${escapeHtml(selected.channel)}"><input name="word" maxlength="60" placeholder="Добавить слово или фразу" aria-label="Новое запрещённое слово" required><button class="button primary small" type="submit">Добавить</button></form><div class="word-group"><p class="word-group-title">Встроенный список · ${data.default_banned_words.length}</p><div class="tag-list">${defaults.map((word) => `<span class="word-tag default-word">${escapeHtml(word)}<span class="word-lock" aria-label="Стандартное слово фильтра">Стандарт</span></span>`).join("") || `<p class="mini-empty">${state.query ? "Совпадений нет." : "Список пуст."}</p>`}</div></div><div class="word-group"><p class="word-group-title">Ваши слова · ${banned.length}/30</p><div class="tag-list">${banned.map((item) => `<span class="word-tag">${escapeHtml(item.word)}${actionButton(`delete-word:${encodeURIComponent(item.word)}`, "Удалить слово", "close", "danger")}</span>`).join("") || `<p class="mini-empty">${state.query ? "Совпадений нет." : "Дополнительных слов пока нет."}</p>`}</div></div><p class="section-note">Встроенные слова берутся из кода и TWITCH_BANNED_WORDS в .env. Собственные слова действуют только в канале #${escapeHtml(selected.channel)}.</p></section></div>`;
  }

  function renderQuotes(data) {
    const channel = currentChannel(data);
    if (!channel) return noChannels(data);
    const all = data.quotes.filter((item) => item.channel === channel);
    const items = all.filter((item) => matching(item, (value) => `${value.text} ${value.author}`));
    return `<section class="cards-grid">${items.length ? items.map((item) => `<article class="quote-card"><div class="quote-top"><span class="quote-number">#${Number(item.id)}</span><div class="quote-tools">${actionButton(`edit-quote:${item.id}`, "Изменить", "edit")}${actionButton(`delete-quote:${item.id}`, "Удалить", "trash", "danger")}</div></div><blockquote class="quote-text">${escapeHtml(item.text)}</blockquote><p class="quote-author">— ${escapeHtml(item.author)}</p></article>`).join("") : empty(state.query ? "Ничего не найдено" : "Цитат пока нет", state.query ? "Измените поисковый запрос." : "Добавьте запомнившуюся фразу из эфира.")}</section>`;
  }

  function renderCategories(data) {
    const channel = currentChannel(data);
    if (!channel) return noChannels(data);
    const all = data.category_aliases.filter((item) => item.channel === channel);
    const items = all.filter((item) => matching(item, (value) => `${value.alias} ${value.category}`));
    return `<section class="panel data-panel"><div class="panel-heading"><div><p class="eyebrow">ИГРОВЫЕ КАТЕГОРИИ</p><h2 class="panel-title">Алиасы Twitch <span class="count-chip">${all.length}/20</span></h2></div><span class="toolbar-note">Канал #${escapeHtml(channel)}</span></div><p class="chat-intro">Алиасы помогают боту находить правильную категорию по короткому названию. Работают только в своём канале.</p><form class="inline-add-form" id="category-form"><input type="hidden" name="channel" value="${escapeHtml(channel)}"><input name="alias" maxlength="30" placeholder="Алиас, например valo" aria-label="Алиас" required><input name="category" maxlength="100" placeholder="Название категории Twitch" aria-label="Категория Twitch" required><button class="button primary" type="submit">Добавить / обновить</button></form>${items.length ? `<div class="table-wrap"><table><thead><tr><th>Алиас</th><th>Категория Twitch</th><th></th></tr></thead><tbody>${items.map((item) => `<tr><td><strong>${escapeHtml(item.alias)}</strong></td><td>${escapeHtml(item.category)}</td><td class="table-action-cell">${actionButton(`delete-category:${encodeURIComponent(item.alias)}`, "Удалить алиас", "trash", "danger")}</td></tr>`).join("")}</tbody></table></div>` : empty(state.query ? "Ничего не найдено" : "Нет алиасов", state.query ? "Измените поисковый запрос." : "Добавьте первый алиас категории.")}</section>`;
  }

  const variableRows = [
    ["{sender} / {user}", "Имя автора команды."],
    ["{channel}", "Логин текущего канала."],
    ["{query}", "Весь текст после имени команды."],
    ["{index1}, {index2}", "Первое и последующие слова из запроса."],
    ["{randint 1 100}", "Случайное целое число в заданных границах."],
    ["{pick да|нет}", "Случайный выбор из вариантов."],
    ["{math 2 + 3}", "Вычисление арифметического выражения."],
    ["{commands}", "Список пользовательских команд."],
    ["{weather}", "Погода для города из аргумента команды."],
    ["{horoscope}", "Гороскоп по знаку из аргумента команды."],
    ["{uptime}", "Длительность эфира и категория."],
    ["{followage}, {accountage}, {subage}", "Срок подписки, фоллоу или возраст аккаунта."],
    ["{followcount}, {subcount}", "Количество фолловеров или платных подписок."],
    ["{emote}, {chatter}", "Случайный Twitch-эмот или знакомый участник чата."],
    ["{countdown дата}, {countup дата}", "Обратный отсчёт или время с указанной даты."]
  ];

  function renderReference(data) {
    const builtins = data.builtin_commands.filter((item) => matching(item, (value) => `${value.name} ${value.description} ${value.aliases.join(" ")}`));
    const channel = currentChannel(data);
    const allCustomVariables = data.custom_variables.filter((item) => item.channel === channel);
    const variables = variableRows.filter((item) => matching(item, (value) => value.join(" ")));
    const customVariables = allCustomVariables.filter((item) => matching(item, (value) => `${value.name} ${value.value} ${value.description}`));
    return `<div class="reference-grid"><section class="panel reference-card"><div class="panel-heading"><div><p class="eyebrow">ВСТРОЕННЫЕ ФУНКЦИИ</p><h2 class="panel-title">Команды бота</h2></div></div>${builtins.length ? `<div class="table-wrap"><table><thead><tr><th>Команда</th><th>Описание</th><th>Алиасы</th></tr></thead><tbody>${builtins.map((item) => `<tr><td><code>${escapeHtml(data.prefix + item.name)}</code></td><td>${escapeHtml(item.description)}</td><td>${escapeHtml(item.aliases.join(", ") || "—")}</td></tr>`).join("")}</tbody></table></div>` : empty("Ничего не найдено", "Измените поисковый запрос.")}</section><section class="panel reference-card"><div class="panel-heading"><div><p class="eyebrow">ШАБЛОНЫ ОТВЕТОВ</p><h2 class="panel-title">Переменные <span class="count-chip">${allCustomVariables.length}/50</span></h2></div><button class="button secondary" type="button" data-action="add-variable" ${channel ? "" : "disabled"}>${icon("plus")}Добавить</button></div>${variables.length || customVariables.length ? `<div class="table-wrap"><table><thead><tr><th>Переменная</th><th>Что делает</th>${customVariables.length ? "<th>Значение</th><th></th>" : ""}</tr></thead><tbody>${variables.map(([name, description]) => `<tr><td><code>${escapeHtml(name)}</code></td><td>${escapeHtml(description)}</td>${customVariables.length ? "<td>—</td><td></td>" : ""}</tr>`).join("")}${customVariables.map((item) => `<tr><td><code>{${escapeHtml(item.name)}}</code></td><td>${escapeHtml(item.description)}</td><td class="table-message text-wrap">${escapeHtml(item.value)}</td><td class="table-action-cell"><div class="table-actions">${actionButton(`edit-variable:${encodeURIComponent(item.name)}`, "Изменить", "edit")}${actionButton(`delete-variable:${encodeURIComponent(item.name)}`, "Удалить", "trash", "danger")}</div></td></tr>`).join("")}</tbody></table></div>` : empty(state.query ? "Ничего не найдено" : "Пользовательских переменных пока нет", state.query ? "Измените поисковый запрос." : "Нажмите «Добавить», чтобы создать переменную.")}<p class="section-note">Пользовательские переменные работают только в выбранном канале. Добавьте переменную в ответ как <code>{имя}</code>. Подробные примеры и ограничения — в файле VARIABLES.md.</p></section></div>`;
  }

  function renderMusic(data) {
    const music = data.music;
    if (!music) return empty("Раздел владельца панели", "Музыкальный виджет для OBS настраивает только владелец панели.");
    const settings = music.settings;
    const track = music.track;
    const url = new URL(music.overlay_path, location.origin).href;
    const sources = [...new Set([...music.sources, settings.source].filter(Boolean))];
    const sourceLabel = (id) => ({ "spotify-web": "Spotify Web", "yandex-music": "Яндекс Музыка", "youtube-music": "YouTube Music" }[id]) || (/spotify/i.test(id) ? "Spotify" : /yandexmusic/i.test(id) ? "Яндекс Музыка" : id);
    return `<div class="music-layout"><section class="panel"><div class="panel-heading"><div><p class="eyebrow">СЕЙЧАС ИГРАЕТ</p><h2 class="panel-title">Музыка на вашем стриме</h2></div><span class="status-badge">${settings.enabled ? "Включён" : "Выключен"}</span></div>
      <div class="music-preview"><article class="now-playing ${track?.playing ? "" : "is-paused"}"><div class="music-artwork"><div class="music-disc" aria-hidden="true"><span>♪</span></div>${track?.cover ? `<img src="${escapeHtml(track.cover)}" alt="Обложка трека" hidden>` : ""}</div><div class="music-details"><div class="music-eyebrow">${track?.playing ? "СЕЙЧАС ИГРАЕТ" : "ПРЕДПРОСМОТР"}</div><h1>${escapeHtml(track?.title || "Название композиции")}</h1><p>${escapeHtml(track?.artist || "Исполнитель")}</p></div></article></div>
      <p class="section-note">${music.error ? escapeHtml(music.error) : !settings.enabled ? "Включите виджет и сохраните настройки, чтобы начать читать музыку." : !track ? "Запустите музыку на этом компьютере. Если выбранный источник закрыт, виджет будет скрыт." : !track.playing ? "Плеер на паузе." : "Название и исполнитель обновляются автоматически."}</p>
      <form id="music-form" class="music-settings"><div class="form-grid">
      ${field("enabled", "Включить музыкальный виджет", settings.enabled, "checkbox")}
      ${field("hide_paused", "Скрывать виджет при паузе", settings.hide_paused, "checkbox")}
      <label class="field full"><span class="field-label">Источник музыки</span><select name="source"><option value="">Автоматически — активный плеер</option>${sources.map(id => `<option value="${escapeHtml(id)}" ${settings.source === id ? "selected" : ""}>${escapeHtml(sourceLabel(id))}</option>`).join("")}</select><span class="field-hint">Приложение Spotify определяется автоматически. Для музыкальных сайтов установите расширение ниже. Обычные браузерные медиасессии исключены.</span></label></div><button class="button primary form-submit" type="submit">Сохранить настройки</button></form>
      </section><section class="panel"><div class="panel-heading"><div><p class="eyebrow">ПОДКЛЮЧЕНИЕ К OBS</p><h2 class="panel-title">Добавьте на сцену</h2></div></div><ol class="music-steps"><li>В OBS нажмите «Источники → + → Браузер».</li><li>Вставьте ссылку из поля ниже.</li><li>Укажите ширину <strong>520</strong> и высоту <strong>180</strong>.</li><li>Запустите музыку. Фон виджета прозрачный.</li></ol><label class="field"><span class="field-label">Ссылка для OBS</span><input id="music-url" readonly value="${escapeHtml(url)}" aria-label="Ссылка музыкального виджета для OBS"></label><div class="music-actions"><button class="button primary" data-action="copy-music-url" type="button">Копировать ссылку</button><a class="button secondary" href="${escapeHtml(music.overlay_path)}" target="_blank" rel="noopener">Открыть виджет</a></div><div class="music-browser-setup"><h3 class="panel-title">Музыка из браузера</h3><p class="section-note">Для Яндекс Музыки, YouTube Music и Spotify Web: скачайте архив, распакуйте, откройте управление расширениями Chrome / Edge / Яндекс Браузера, включите режим разработчика и нажмите «Загрузить распакованное расширение».</p><a class="button secondary" href="/static/music-extension.zip" download>Скачать расширение</a><label class="field"><span class="field-label">Ссылка подключения расширения</span><input readonly value="${escapeHtml(new URL(music.browser_path, location.origin).href)}" aria-label="Ссылка подключения расширения"></label><p class="section-note">Вставьте эту ссылку в настройки расширения, сохраните и обновите вкладку с музыкой. Twitch и обычный youtube.com расширение не читает.</p></div><p class="section-note">Панель и плеер должны работать на этом ПК в одном сеансе Windows. Виджет показывает данные, но не передаёт звук — звук настраивается в OBS отдельно. Не публикуйте ссылку: она открывает название текущего трека без входа в панель.</p></section></div>`;
  }

  function render() {
    if (!state.data) return;
    if (state.view === "music" && !state.data.identity?.owner) { setView("overview"); return; }
    const view = {
      music: renderMusic,
      overview: renderOverview,
      commands: renderCommands,
      timers: renderTimers,
      users: renderUsers,
      moderation: renderModeration,
      quotes: renderQuotes,
      categories: renderCategories,
      reference: renderReference
    }[state.view];
    const content = $("#content");
    if (contentEnter) content.setAttribute("data-enter", ""); else content.removeAttribute("data-enter");
    contentEnter = false;
    content.innerHTML = view(state.data);
    $$(".music-artwork img", content).forEach((img) => {
      img.addEventListener("load", () => { img.hidden = false; });
      img.addEventListener("error", () => { img.hidden = true; });
      if (img.complete && img.naturalWidth) img.hidden = false;
    });
    $$("#content select").forEach(enhanceSelect);
    content.setAttribute("aria-busy", "false");
    renderPending = false;
    updateShell();
  }

  let openSelectShell = null;

  function closeSelectShell(focusTrigger = false) {
    if (!openSelectShell) return;
    openSelectShell.classList.remove("is-open");
    const trigger = openSelectShell.querySelector(".select-trigger");
    const list = openSelectShell.querySelector(".select-list");
    if (trigger) {
      trigger.classList.remove("is-open");
      trigger.setAttribute("aria-expanded", "false");
      if (focusTrigger) trigger.focus();
    }
    if (list) list.hidden = true;
    openSelectShell = null;
  }

  function enhanceSelect(select) {
    if (select.selectEnhanced) return;
    select.selectEnhanced = true;
    const shell = document.createElement("span");
    shell.className = "select-shell";
    select.replaceWith(shell);
    const trigger = document.createElement("button");
    trigger.type = "button";
    trigger.className = "select-trigger";
    trigger.setAttribute("aria-haspopup", "listbox");
    trigger.setAttribute("aria-expanded", "false");
    trigger.setAttribute("aria-label", select.getAttribute("aria-label") || "Канал");
    const list = document.createElement("ul");
    list.className = "select-list";
    list.setAttribute("role", "listbox");
    list.hidden = true;
    shell.append(select, trigger, list);
    const options = [...select.options];
    list.innerHTML = options.map((option, index) => `<li class="select-option" role="option" tabindex="-1" aria-selected="${option.selected}" data-value="${escapeHtml(option.value)}" id="${select.name || "select"}-opt-${index}"><span class="select-mark">${escapeHtml(option.textContent.trim()) || "&nbsp;"}</span><span class="select-check" aria-hidden="true">${icon("check")}</span></li>`).join("");
    const syncLabel = () => {
      const current = select.selectedOptions[0];
      trigger.innerHTML = `<span class="select-value">${escapeHtml(current ? current.textContent.trim() : "") || "&nbsp;"}</span><span class="chev" aria-hidden="true"></span>`;
    };
    select.addEventListener("change", syncLabel);
    syncLabel();
    const items = () => [...list.querySelectorAll(".select-option")];
    const open = () => {
      if (openSelectShell && openSelectShell !== shell) closeSelectShell();
      openSelectShell = shell;
      shell.classList.add("is-open");
      trigger.classList.add("is-open");
      trigger.setAttribute("aria-expanded", "true");
      list.hidden = false;
      const current = items().find((item) => item.getAttribute("aria-selected") === "true") || items()[0];
      if (current) current.focus();
      if (current) current.scrollIntoView({ block: "nearest" });
    };
    trigger.addEventListener("click", () => (list.hidden ? open() : closeSelectShell(true)));
    list.addEventListener("mousedown", (event) => event.preventDefault());
    list.addEventListener("click", (event) => {
      const item = event.target.closest(".select-option");
      if (!item) return;
      closeSelectShell(true);
      if (select.value !== item.dataset.value) {
        select.value = item.dataset.value;
        select.dispatchEvent(new Event("change", { bubbles: true }));
      }
    });
    list.addEventListener("keydown", (event) => {
      if (event.key !== "ArrowDown" && event.key !== "ArrowUp" && event.key !== "Enter" && event.key !== " " && event.key !== "Escape" && event.key !== "Home" && event.key !== "End" && event.key !== "Tab") return;
      const all = items();
      const index = all.indexOf(document.activeElement);
      event.preventDefault();
      if (event.key === "ArrowDown") all[Math.min(index + 1, all.length - 1)]?.focus();
      else if (event.key === "ArrowUp") all[Math.max(index - 1, 0)]?.focus();
      else if (event.key === "Home") all[0]?.focus();
      else if (event.key === "End") all[all.length - 1]?.focus();
      else if (event.key === "Enter" || event.key === " ") {
        const item = all[index];
        closeSelectShell(true);
        if (item && select.value !== item.dataset.value) {
          select.value = item.dataset.value;
          select.dispatchEvent(new Event("change", { bubbles: true }));
        }
      }
      else if (event.key === "Escape") closeSelectShell(true);
      else if (event.key === "Tab") closeSelectShell();
    });
    shell.addEventListener("focusout", (event) => {
      const next = event.relatedTarget;
      if (!next || !shell.contains(next)) closeSelectShell();
    });
    document.addEventListener("click", (event) => {
      if (openSelectShell === shell && !shell.contains(event.target)) closeSelectShell();
    });
  }

  function updateChatAvailability() {
    const form = $("#chat-form");
    if (!form || !state.data) return;
    const canSend = state.data.runtime.connected && state.data.channels.length > 0;
    $("button[type=submit]", form).disabled = !canSend || !form.elements.message.value.trim() || form.dataset.submitting === "true";
    $("#chat-hint").textContent = !state.data.channels.length ? "Сначала подключите канал бота — это делается на странице «Обзор»."
      : !canSend ? "Для отправки запустите бота." : "Ctrl + Enter — отправить одной строкой";
    $("#chat-count").textContent = `${form.elements.message.value.length} / 450`;
  }

  function updateShell() {
    if (!state.data) return;
    const ownerAccount = Boolean(state.data.identity?.owner);
    $$("[data-owner-only]").forEach((element) => { element.hidden = !ownerAccount; });
    $("#sidebar-nick").textContent = state.data.runtime.nick || "Twitch-бот";
    const channel = currentChannel(state.data);
    $("#sidebar-channel-name").textContent = channel ? `#${channel}` : "Нет каналов";
    const menu = $("#channel-menu");
    if (menu) {
      const html = channelMenuHtml(channel);
      if (menu.dataset.signature !== html) {
        menu.innerHTML = html;
        menu.dataset.signature = html;
      }
    }
    if (!channel) setChannelMenu(false);
    const stats = (channel && state.data.stats[channel]) || { commands: 0 };
    $("#nav-command-count").textContent = stats.commands;
    $("#page-add").disabled = false;
    const [statusClass, statusText] = runtimeStatus(state.data.runtime);
    $("#sidebar-state").textContent = statusText;
    $(".sidebar-led").className = `sidebar-led ${statusClass}`;
    const botButton = $(".connection-card [data-action^=bot-]");
    if (botButton) {
      const stop = ["running", "connecting"].includes(state.data.runtime.status);
      botButton.dataset.action = stop ? "bot-stop" : "bot-start";
      botButton.disabled = state.botBusy;
      botButton.className = `button ${stop ? "secondary" : "primary"}`;
      botButton.innerHTML = `${icon(stop ? "stop" : "play")}${state.botBusy ? "Подождите…" : stop ? "Остановить бота" : "Запустить бота"}`;
      const badge = $(".connection-card .status-badge");
      badge.className = `status-badge ${statusClass}`;
      badge.innerHTML = `<span class="status-inline"></span>${statusText}`;
    }
    updateChatAvailability();
    $("#current-date").textContent = new Intl.DateTimeFormat("ru-RU", { day: "numeric", month: "long" }).format(new Date());
  }

  function editorField(fieldInfo) {
    const { name, label, value = "", type = "text", required = false, hint = "", min, max, placeholder = "" } = fieldInfo;
    const requiredAttr = required ? "required" : "";
    const hintHtml = hint ? `<span class="field-hint">${escapeHtml(hint)}</span>` : "";
    if (type === "textarea") {
      return `<label class="field full"><span class="field-label">${escapeHtml(label)}</span><textarea name="${name}" ${requiredAttr} maxlength="${fieldInfo.maxlength || 4000}" placeholder="${escapeHtml(placeholder)}">${escapeHtml(value)}</textarea>${hintHtml}</label>`;
    }
    if (type === "checkbox") {
      return `<label class="toggle-field"><input type="checkbox" name="${name}" ${value ? "checked" : ""}><span>${escapeHtml(label)}</span></label>`;
    }
    return `<label class="field"><span class="field-label">${escapeHtml(label)}</span><input name="${name}" type="${type}" value="${escapeHtml(value)}" ${requiredAttr} ${min !== undefined ? `min="${min}"` : ""} ${max !== undefined ? `max="${max}"` : ""} ${fieldInfo.maxlength ? `maxlength="${fieldInfo.maxlength}"` : ""} placeholder="${escapeHtml(placeholder)}">${hintHtml}</label>`;
  }

  let editorSave = null;

  function openEditor(title, fields, save, description = "") {
    $("#dialog-title").textContent = title;
    $("#dialog-eyebrow").textContent = description || "НАСТРОЙКИ";
    $("#dialog-fields").innerHTML = fields.map(editorField).join("");
    $("#dialog-error").hidden = true;
    $("#dialog-error").textContent = "";
    $("#dialog-submit").disabled = false;
    $("#dialog-submit").textContent = "Сохранить";
    editorSave = save;
    $("#editor").showModal();
    $$("#dialog-fields input, #dialog-fields textarea").find((input) => input.type !== "checkbox")?.focus();
  }

  async function saveEditor(event) {
    event.preventDefault();
    if (!editorSave) return;
    const button = $("#dialog-submit");
    if (button.disabled) return;
    button.disabled = true;
    button.textContent = "Сохраняем…";
    const result = {};
    for (const [key, value] of new FormData($("#editor-form"))) result[key] = value;
    $$("#dialog-fields input[type=checkbox]").forEach((input) => { result[input.name] = input.checked; });
    try {
      await editorSave(result);
      $("#editor").close();
      await refresh(false, true);
      toast("Изменения сохранены.");
    } catch (error) {
      const message = $("#dialog-error");
      message.textContent = error.message;
      message.hidden = false;
      button.disabled = false;
    } finally {
      button.textContent = "Сохранить";
    }
  }

  function openCommand(item = null) {
    const channel = item?.channel || currentChannel();
    openEditor(item ? "Изменить команду" : "Новая команда", [
      { name: "name", label: "Имя команды", value: item?.name || "", required: true, maxlength: 60, placeholder: "привет" },
      { name: "response", label: "Ответ бота", value: item?.response || "", type: "textarea", required: true, hint: "До 4000 символов. Переменные доступны в справочнике." },
      { name: "cooldown_seconds", label: "Кулдаун (секунды)", value: item?.cooldown_seconds ?? 0, type: "number", min: 0, max: 86400 },
      { name: "aliases", label: "Алиасы через запятую", value: (item?.aliases || []).join(", "), maxlength: 300, placeholder: "прив, hi" }
    ], async (values) => {
      const payload = { ...values, channel, cooldown_seconds: Number(values.cooldown_seconds || 0), aliases: values.aliases.split(",").map((alias) => alias.trim()).filter(Boolean) };
      const url = item ? `/api/commands/${encodeURIComponent(item.name)}` : "/api/commands";
      await request(url, { method: item ? "PUT" : "POST", body: payload });
    }, `КОМАНДА КАНАЛА${channel ? ` #${channel.toUpperCase()}` : ""}`);
  }

  function openTimer(item = null) {
    const channel = item?.channel || currentChannel();
    openEditor(item ? "Изменить таймер" : "Новый таймер", [
      { name: "message", label: "Сообщение", value: item?.message || "", type: "textarea", required: true, maxlength: 450 },
      { name: "interval_minutes", label: "Интервал (минуты)", value: item?.interval_minutes ?? 15, type: "number", required: true, min: 1, max: 1440 },
      { name: "enabled", label: "Включить таймер", value: item?.enabled ?? true, type: "checkbox" }
    ], async (values) => {
      const payload = { channel, message: values.message, interval_minutes: Number(values.interval_minutes), enabled: values.enabled };
      const url = item ? `/api/timers/${item.id}` : "/api/timers";
      await request(url, { method: item ? "PUT" : "POST", body: payload });
    }, `ТАЙМЕР КАНАЛА${channel ? ` #${channel.toUpperCase()}` : ""} — только в свой канал`);
  }

  function openQuote(item = null) {
    const channel = item?.channel || currentChannel();
    openEditor(item ? "Изменить цитату" : "Новая цитата", [
      { name: "text", label: "Текст цитаты", value: item?.text || "", type: "textarea", required: true, maxlength: 2000 },
      { name: "author", label: "Автор", value: item?.author || "", required: true, maxlength: 100 }
    ], async (values) => {
      const url = item ? `/api/quotes/${item.id}` : "/api/quotes";
      await request(url, { method: item ? "PUT" : "POST", body: { ...values, channel } });
    }, `АРХИВ ЭФИРА${channel ? ` #${channel.toUpperCase()}` : ""}`);
  }

  function openCustomVariable(item = null) {
    const channel = item?.channel || currentChannel();
    openEditor(item ? "Изменить переменную" : "Новая переменная", [
      { name: "name", label: "Имя переменной", value: item?.name || "", required: true, maxlength: 30, placeholder: "название", hint: "Только латинские буквы; в ответе используйте {название}." },
      { name: "value", label: "Значение", value: item?.value || "", type: "textarea", required: true, maxlength: 1000, hint: "Этот текст будет подставляться вместо переменной." },
      { name: "description", label: "Описание", value: item?.description || "", type: "textarea", required: true, maxlength: 300, hint: "Кратко поясните назначение переменной." }
    ], async (values) => {
      const url = item ? `/api/variables/${encodeURIComponent(item.name)}` : "/api/variables";
      await request(url, { method: item ? "PUT" : "POST", body: { ...values, channel } });
    }, `ПОЛЬЗОВАТЕЛЬСКИЕ ПЕРЕМЕННЫЕ${channel ? ` #${channel.toUpperCase()}` : ""}`);
  }

  function openPoints(channel, name) {
    openEditor(`Баланс @${name} · #${channel}`, [
      { name: "amount", label: "Изменение баланса", value: 0, type: "number", required: true, min: -1000000000, max: 1000000000, hint: "Положительное число начисляет, отрицательное списывает." }
    ], async (values) => {
      await request("/api/points", { method: "POST", body: { channel, name, amount: Number(values.amount) } });
    }, `БАЛЛЫ КАНАЛА #${channel.toUpperCase()}`);
  }

  let confirmResolve = null;

  function confirmAction(message, { title = "Подтвердите удаление", submit = "Удалить" } = {}) {
    return new Promise((resolve) => {
      confirmResolve = resolve;
      $("#confirm-title").textContent = title;
      $("#confirm-ok").textContent = submit;
      $("#confirm-text").textContent = message;
      $("#confirm").showModal();
    });
  }

  function settleConfirm(result) {
    const resolve = confirmResolve;
    confirmResolve = null;
    $("#confirm").close();
    resolve?.(result);
  }

  $("#confirm-form").addEventListener("submit", (event) => {
    event.preventDefault();
    settleConfirm(true);
  });
  $("#confirm-cancel").addEventListener("click", () => $("#confirm").close());
  $("#confirm").addEventListener("close", () => {
    const resolve = confirmResolve;
    confirmResolve = null;
    resolve?.(false);
  });

  async function deleteRecord(url, message) {
    if (!(await confirmAction(message))) return;
    try {
      await request(url, { method: "DELETE" });
      await refresh(false, true);
      toast("Запись удалена.");
    } catch (error) {
      toast(error.message, "error");
    }
  }

  async function clickAction(action) {
    if (action === "toggle-channels") {
      if (!state.data?.channels.length) {
        toast("Сначала подключите канал: «Обзор» → «Подключить канал».", "error");
        return;
      }
      setChannelMenu(!channelMenuOpen);
      return;
    }
    if (action === "open-moderation") {
      window.location.hash = "moderation";
      return;
    }
    if (action === "bot-start" || action === "bot-stop") {
      if (state.botBusy) return;
      state.botBusy = true;
      updateShell();
      try {
        const response = await request(`/api/bot/${action.slice(4)}`, { method: "POST", body: {} });
        if (state.data) state.data.runtime = response.runtime;
        await refresh(false);
        toast(action === "bot-start" ? "Запрос на запуск бота отправлен." : "Бот остановлен.");
      } catch (error) { toast(error.message, "error"); }
      finally { state.botBusy = false; updateShell(); }
      return;
    }
    if (action === "copy-music-url") {
      const input = $("#music-url");
      try { await navigator.clipboard.writeText(input.value); toast("Ссылка для OBS скопирована."); }
      catch (_) { input.focus(); input.select(); toast("Нажмите Ctrl+C, чтобы скопировать ссылку."); }
      return;
    }
    if (action === "add-channel") {
      const info = state.data?.add_channel || {};
      if (!info.allowed) {
        toast(info.hint || "Подключение канала сейчас недоступно.", "error");
        return;
      }
      if (!window.ImmortalTwitch) { toast("Окно Twitch не загрузилось. Обновите страницу.", "error"); return; }
      window.ImmortalTwitch.open("channel", (result) => {
        refresh(false, true);
        toast(result?.joined === false
          ? `Канал #${result.login} подключён. Бот зайдёт в него при следующем запуске.`
          : `Бот подключён к каналу #${result?.login || ""}.`);
      });
      return;
    }
    if (action === "add-variable") {
      if (!currentChannel()) { toast("Сначала подключите канал к боту.", "error"); return; }
      openCustomVariable();
      return;
    }
    if (action.startsWith("add-")) {
      if (!currentChannel()) { toast("Сначала подключите канал к боту — это делается на странице «Обзор».", "error"); return; }
      const resource = action.slice(4);
      if (resource === "commands") openCommand();
      if (resource === "timers") openTimer();
      if (resource === "quotes") openQuote();
      if (resource === "categories") $("#category-form input[name=alias]")?.focus();
      return;
    }
    const [type, rawValue] = action.split(":");
    const value = decodeURIComponent(rawValue || "");
    if (type === "select-channel") {
      if (state.channel !== value) {
        state.channel = value;
        contentEnter = true;
        render();
        flashChannelName();
      }
      setChannelMenu(false);
    } else if (type === "edit-command") {
      const item = state.data.commands.find((entry) => entry.channel === currentChannel() && entry.name === value);
      if (item) openCommand(item);
    } else if (type === "delete-command") {
      const channel = currentChannel();
      await deleteRecord(`/api/commands/${encodeURIComponent(value)}?channel=${encodeURIComponent(channel)}`, `Удалить команду «${value}» в канале #${channel}?`);
    } else if (type === "edit-timer") {
      const item = state.data.timers.find((entry) => entry.id === Number(value));
      if (item) openTimer(item);
    } else if (type === "delete-timer") {
      const item = state.data.timers.find((entry) => entry.id === Number(value));
      if (!item) return;
      await deleteRecord(`/api/timers/${value}?channel=${encodeURIComponent(item.channel)}`, `Удалить таймер #${value} в канале #${item.channel}?`);
    } else if (type === "edit-points") {
      const channel = currentChannel();
      openPoints(channel, value);
    } else if (type === "edit-quote") {
      const item = state.data.quotes.find((entry) => entry.id === Number(value));
      if (item) openQuote(item);
    } else if (type === "delete-quote") {
      const item = state.data.quotes.find((entry) => entry.id === Number(value));
      if (!item) return;
      await deleteRecord(`/api/quotes/${value}?channel=${encodeURIComponent(item.channel)}`, `Удалить цитату #${value} в канале #${item.channel}?`);
    } else if (type === "delete-word") {
      const channel = currentChannel();
      await deleteRecord(`/api/banned-words/${encodeURIComponent(value)}?channel=${encodeURIComponent(channel)}`, `Удалить слово «${value}» в канале #${channel}?`);
    } else if (type === "delete-category") {
      const channel = currentChannel();
      await deleteRecord(`/api/categories/${encodeURIComponent(value)}?channel=${encodeURIComponent(channel)}`, `Удалить алиас «${value}» в канале #${channel}?`);
    } else if (type === "edit-variable") {
      const item = state.data.custom_variables.find((entry) => entry.channel === currentChannel() && entry.name === value);
      if (item) openCustomVariable(item);
    } else if (type === "delete-variable") {
      const channel = currentChannel();
      await deleteRecord(`/api/variables/${encodeURIComponent(value)}?channel=${encodeURIComponent(channel)}`, `Удалить переменную «{${value}}» в канале #${channel}?`);
    } else if (type === "remove-channel") {
      if (!(await confirmAction(`Отключить бота от #${value}? Настройки канала сохранятся в базе.`, { title: "Подтвердите отключение", submit: "Отключить" }))) return;
      try {
        await request(`/api/channels/${encodeURIComponent(value)}`, { method: "DELETE" });
        await refresh(false, true);
        toast(`Бот покидает канал #${value}.`);
      } catch (error) { toast(error.message, "error"); }
    }
  }

  async function formSubmit(event) {
    const form = event.target;
    if (!(form instanceof HTMLFormElement) || !["music-form", "chat-form", "moderation-form", "banned-word-form", "category-form"].includes(form.id)) return;
    event.preventDefault();
    if (form.dataset.submitting === "true") return;
    form.dataset.submitting = "true";
    const values = Object.fromEntries(new FormData(form));
    const submitButton = $("button[type=submit]", form);
    if (submitButton) submitButton.disabled = true;
    try {
      if (form.id === "music-form") {
        await request("/api/music", { method: "PUT", body: { enabled: form.elements.enabled.checked, hide_paused: form.elements.hide_paused.checked, source: values.source } });
        await refresh(false, true);
        toast("Настройки музыкального виджета сохранены.");
      } else if (form.id === "chat-form") {
        values.message = values.message.replace(/[\r\n]+/g, " ").trim();
        await request("/api/chat", { method: "POST", body: values });
        state.chatDraft = "";
        form.elements.message.value = "";
        await refresh(false, true);
        toast("Сообщение отправлено в чат.");
      } else if (form.id === "moderation-form") {
        const payload = { ...values };
        for (const key of ["enabled", "video", "lang"]) payload[key] = form.elements[key].checked;
        for (const key of ["mode", "size", "main_size", "percent", "spam_messages", "spam_seconds", "max_link_length"]) payload[key] = Number(values[key]);
        payload.timeouts = values.timeouts.split(",").map((value) => Number(value.trim()));
        await request(`/api/moderation/${encodeURIComponent(form.dataset.channel)}`, { method: "PUT", body: payload });
        await refresh(false, true);
        toast("Настройки модерации сохранены.");
      } else if (form.id === "banned-word-form") {
        await request("/api/banned-words", { method: "POST", body: values });
        await refresh(false, true);
        toast("Слово добавлено.");
      } else if (form.id === "category-form") {
        await request("/api/categories", { method: "POST", body: values });
        await refresh(false, true);
        toast("Алиас категории сохранён.");
      }
    } catch (error) {
      toast(error.message, "error");
    } finally {
      delete form.dataset.submitting;
      if (submitButton) submitButton.disabled = false;
      updateChatAvailability();
    }
  }

  const mobileLayout = window.matchMedia("(max-width: 880px)");

  function closeMenu(restoreFocus = false) {
    const sidebar = $("#sidebar");
    if (mobileLayout.matches && sidebar.classList.contains("is-open") && (restoreFocus || sidebar.contains(document.activeElement))) {
      $("#menu-toggle").focus();
    }
    setChannelMenu(false);
    $("#sidebar").classList.remove("is-open");
    $("#sidebar-overlay").classList.remove("is-open");
    $("#menu-toggle").setAttribute("aria-expanded", "false");
    document.body.classList.remove("menu-open");
    sidebar.inert = mobileLayout.matches;
  }

  document.addEventListener("click", (event) => {
    const actionTarget = event.target.closest("[data-action]");
    if (actionTarget) clickAction(actionTarget.dataset.action);
    const viewTarget = event.target.closest("[data-view]");
    if (viewTarget && !event.ctrlKey && !event.metaKey && !event.shiftKey && event.button === 0) setView(viewTarget.dataset.view);
    if (channelMenuOpen && !event.target.closest("#channel-switcher")) setChannelMenu(false);
  });

  document.addEventListener("submit", formSubmit);
  $("#editor-form").addEventListener("submit", saveEditor);
  $("#dialog-close").addEventListener("click", () => $("#editor").close());
  $("#dialog-cancel").addEventListener("click", () => $("#editor").close());
  $("#editor").addEventListener("click", (event) => {
    const dialog = $("#editor");
    const rect = dialog.getBoundingClientRect();
    if (event.target === dialog && (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom)) dialog.close();
  });
  $("#search").addEventListener("input", (event) => {
    state.query = event.target.value;
    render();
  });
  function rememberForm(event) {
    const form = event.target.closest("form");
    if (!form) return;
    if (form.id === "chat-form") {
      state.chatDraft = form.elements.message.value;
      state.chatChannel = form.elements.channel.value;
      updateChatAvailability();
    } else if (["moderation-form", "category-form", "banned-word-form"].includes(form.id)) {
      form.dataset.dirty = "true";
    }
  }
  $("#content").addEventListener("input", rememberForm);
  $("#content").addEventListener("change", rememberForm);
  $("#sidebar-overlay").addEventListener("click", () => closeMenu(true));
  $("#menu-close").addEventListener("click", () => closeMenu(true));
  $("#menu-toggle").addEventListener("click", () => {
    const open = !$("#sidebar").classList.contains("is-open");
    if (!open) { closeMenu(true); return; }
    $("#sidebar").inert = false;
    $("#sidebar").classList.toggle("is-open", open);
    $("#sidebar-overlay").classList.toggle("is-open", open);
    $("#menu-toggle").setAttribute("aria-expanded", String(open));
    document.body.classList.toggle("menu-open", open);
    $(".nav-link.active")?.focus();
  });
  mobileLayout.addEventListener("change", () => closeMenu());
  $("#refresh").addEventListener("click", async () => {
    const button = $("#refresh");
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    try { if (await refresh()) toast("Данные обновлены."); }
    finally { button.disabled = false; button.setAttribute("aria-busy", "false"); }
  });
  $("#logout").addEventListener("click", async () => {
    try {
      await request("/logout", { method: "POST", body: {} });
      window.location.assign("/login");
    } catch (error) { toast(error.message, "error"); }
  });
  window.addEventListener("hashchange", () => {
    if (location.hash.slice(1) !== state.view) setView(location.hash.slice(1));
  });
  window.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && channelMenuOpen) { event.preventDefault(); setChannelMenu(false); return; }
    if (mobileLayout.matches && $("#sidebar").classList.contains("is-open")) {
      if (event.key === "Escape") { event.preventDefault(); closeMenu(true); return; }
      if (event.key === "Tab") {
        const targets = $$("a[href], button:not(:disabled)", $("#sidebar")).filter((element) => element.getClientRects().length);
        const first = targets[0], last = targets[targets.length - 1];
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
      }
      return;
    }
    const typing = event.target instanceof HTMLInputElement
      || event.target instanceof HTMLTextAreaElement
      || event.target instanceof HTMLSelectElement || event.target.isContentEditable;
    if (event.key === "/" && !typing && !event.ctrlKey && !event.metaKey && !$("#editor").open && !$("#confirm").open && !$("#search").closest(".search-field").hidden) {
      event.preventDefault();
      $("#search").focus();
    }
    if (event.key === "Escape" && event.target === $("#search") && state.query) {
      state.query = "";
      $("#search").value = "";
      render();
    }
    if (event.key === "Enter" && (event.ctrlKey || event.metaKey) && event.target.closest("#chat-form")) {
      const form = $("#chat-form");
      if (!$("button[type=submit]", form).disabled) { event.preventDefault(); form.requestSubmit(); }
    }
  });
  $("#current-date").textContent = new Intl.DateTimeFormat("ru-RU", { day: "numeric", month: "long", year: "numeric" }).format(new Date());
  $$("[data-icon]").forEach((element) => { element.innerHTML = icon(element.dataset.icon); });
  setView(location.hash.slice(1) || "overview");
  refresh();
  window.setInterval(() => { if (!document.hidden) refresh(false); }, 5000);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(false); });
})();
