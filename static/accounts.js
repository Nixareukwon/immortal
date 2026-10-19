(() => {
  "use strict";
  const message = document.querySelector("#message");
  async function api(url, method = "GET", body) {
    const response = await fetch(url, {method, headers: {"Content-Type": "application/json", "X-CSRF-Token": document.querySelector('meta[name="csrf-token"]').content}, body: body ? JSON.stringify(body) : undefined});
    const data = await response.json();
    if (!response.ok) {
      const failure = new Error(data.error || "Не удалось выполнить действие.");
      failure.status = response.status;
      throw failure;
    }
    return data;
  }
  async function action(button, task) {
    button.disabled = true;
    message.textContent = "";
    try { await task(); } catch (error) { message.textContent = error.message; }
    finally { button.disabled = false; }
  }
  const form = document.querySelector("#register");
  if (form) {
    const invite = new URLSearchParams(location.search).get("invite") || "";
    history.replaceState(null, "", "/register");
    form.addEventListener("submit", event => {
      event.preventDefault();
      action(form.querySelector("button"), async () => {
        await api("/register", "POST", {...Object.fromEntries(new FormData(form)), invite});
        location.assign("/");
      });
    });
    return;
  }
  const TWITCH_GLYPH = '<svg class="twitch-glyph" viewBox="0 0 24 24" aria-hidden="true">'
    + '<path fill="currentColor" stroke="none" d="M11.571 4.714h1.715v5.143H11.57zm4.715 0H18v5.143h-1.714zM6 0L1.714 4.286v15.428h5.143V24l4.286-4.286h3.428L22.286 12V0zm14.571 11.143l-3.428 3.428h-3.429l-3 3v-3H6.857V1.714h13.714Z"/></svg>';

  function twitchChip(login) {
    const chip = document.createElement("span");
    chip.className = "account-twitch";
    chip.innerHTML = TWITCH_GLYPH;
    chip.append(document.createTextNode(login));
    return chip;
  }

  function accountRow(username, login) {
    const row = document.createElement("article");
    row.className = "account-row";
    const avatar = document.createElement("span");
    avatar.className = "account-avatar";
    avatar.textContent = (username.charAt(0) || "?").toUpperCase();
    const info = document.createElement("div");
    info.className = "account-info";
    const name = document.createElement("strong");
    name.textContent = username;
    info.append(name);
    if (login) info.append(twitchChip(login));
    else {
      const none = document.createElement("span");
      none.className = "account-twitch none";
      none.textContent = "Twitch не привязан";
      info.append(none);
    }
    const actions = document.createElement("div");
    actions.className = "account-actions";
    if (login) {
      const untie = document.createElement("button");
      untie.className = "button small secondary";
      untie.textContent = "Отвязать Twitch";
      untie.addEventListener("click", () => {
        if (confirm(`Отвязать Twitch у ${username}?`)) action(untie, async () => {
          await api("/api/twitch/unlink", "POST", {username});
          await refresh();
        });
      });
      actions.append(untie);
    }
    const revoke = document.createElement("button");
    revoke.className = "button small danger-ghost";
    revoke.textContent = "Отозвать доступ";
    revoke.addEventListener("click", () => {
      if (confirm(`Отозвать доступ у ${username}?`)) action(revoke, async () => {
        await api("/api/access/" + encodeURIComponent(username), "DELETE");
        await refresh();
      });
    });
    actions.append(revoke);
    row.append(avatar, info, actions);
    return row;
  }

  function emptyAccounts() {
    const box = document.createElement("div");
    box.className = "accounts-empty";
    box.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M17 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/></svg>';
    const text = document.createElement("p");
    const title = document.createElement("strong");
    title.textContent = "Пока никто не зарегистрировался.";
    const hint = document.createElement("span");
    hint.textContent = "Нажмите «Создать приглашение» и отправьте ссылку — человек сам придумает логин и пароль.";
    text.append(title, hint);
    box.append(text);
    return box;
  }

  async function refresh() {
    const data = await api("/api/access");
    document.querySelector("#access-controls").hidden = false;
    const list = document.querySelector("#account-list");
    const count = document.querySelector("#account-count");
    if (count) count.textContent = String(data.users.length);
    list.replaceChildren();
    if (!data.users.length) list.append(emptyAccounts());
    data.users.forEach((username, index) => {
      const row = accountRow(username, data.twitch[username]);
      row.style.animationDelay = `${80 + Math.min(index, 6) * 55}ms`;
      list.append(row);
    });
  }
  document.querySelector("#invite").addEventListener("click", event => action(event.currentTarget, async () => {
    const data = await api("/api/access/invite", "POST", {});
    document.querySelector("#invite-result").hidden = false;
    document.querySelector("#invite-url").value = data.url;
  }));
  document.querySelector("#copy-invite").addEventListener("click", event => action(event.currentTarget, async () => {
    const input = document.querySelector("#invite-url");
    try { await navigator.clipboard.writeText(input.value); message.textContent = "Ссылка скопирована."; }
    catch { input.select(); message.textContent = "Нажмите Ctrl+C, чтобы скопировать ссылку."; }
  }));
  const twitchState = document.querySelector("#twitch-state");
  if (twitchState) {
    const linkButton = document.querySelector("#twitch-link");
    const unlinkButton = document.querySelector("#twitch-unlink");
    const refreshTwitch = async () => {
      const data = await api("/api/twitch");
      if (!data.configured) {
        twitchState.dataset.state = "off";
        twitchState.textContent = "Вход через Twitch выключен: в .env не указан TWITCH_CLIENT_ID.";
        linkButton.hidden = true; unlinkButton.hidden = true;
        return;
      }
      if (data.link) {
        const notes = [];
        if (data.owner) notes.push(data.enhanced
          ? "Полные права включены: панель сама обновляет токен для бота."
          : "Полные права ещё не выданы — нажмите «Отвязать» и привяжите Twitch заново.");
        if (data.refresh_error) notes.push(data.refresh_error);
        twitchState.textContent = `Привязан Twitch: ${data.link.login}.`
          + (notes.length ? " " + notes.join(" ") : "");
        twitchState.dataset.state = data.refresh_error ? "error" : "linked";
        linkButton.hidden = true; unlinkButton.hidden = false;
      } else {
        twitchState.dataset.state = "off";
        twitchState.textContent = "Twitch не привязан. Привяжите его, чтобы входить в панель по Twitch.";
        linkButton.hidden = false; unlinkButton.hidden = true;
      }
    };
    linkButton.addEventListener("click", () => window.ImmortalTwitch.open("link", async () => {
      await refreshTwitch();
      message.textContent = "Twitch привязан.";
    }));
    unlinkButton.addEventListener("click", event => {
      if (confirm("Отвязать Twitch? Вход через Twitch станет недоступен, пароль продолжит работать. Сохранённый токен для бота тоже удалится.")) action(event.currentTarget, async () => {
        await api("/api/twitch/unlink", "POST", {});
        await refreshTwitch();
        message.textContent = "Twitch отвязан.";
      });
    });
    refreshTwitch().catch(error => { twitchState.textContent = error.message; });
  }
  refresh().catch(error => { if (error.status !== 403) message.textContent = error.message; });
})();
