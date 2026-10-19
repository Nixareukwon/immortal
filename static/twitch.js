(() => {
  "use strict";

  const csrf = () => document.querySelector('meta[name="csrf-token"]').content;

  async function api(url, body) {
    const response = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf() },
      body: JSON.stringify(body)
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Не удалось выполнить действие.");
    return data;
  }

  let dialog = null;
  let stop = null;

  function close() {
    if (!dialog) return;
    const active = dialog;
    dialog = null;
    if (stop) { stop(); stop = null; }
    active.close();
    active.remove();
  }

  function card() {
    const element = document.createElement("dialog");
    element.innerHTML = [
      '<div class="dialog-heading"><h2 id="twitch-dialog-title">Twitch</h2></div>',
      '<div class="device-body">',
      '<p class="section-note" id="twitch-dialog-note">Получаем код у Twitch…</p>',
      '<p class="device-code" id="twitch-dialog-code" aria-live="polite"></p>',
      '<a class="button primary" id="twitch-dialog-open" target="_blank" rel="noopener" href="https://www.twitch.tv/activate">Открыть twitch.tv/activate</a>',
      '<p class="section-note" id="twitch-dialog-status" aria-live="polite"></p>',
      "</div>",
      '<div class="dialog-footer"><button class="button secondary" id="twitch-dialog-cancel" type="button">Отмена</button></div>'
    ].join("");
    element.addEventListener("cancel", close);
    element.querySelector("#twitch-dialog-cancel").addEventListener("click", close);
    document.body.append(element);
    dialog = element;
    return element;
  }

  async function run(mode, onSuccess) {
    close();
    const element = card();
    const title = element.querySelector("#twitch-dialog-title");
    const note = element.querySelector("#twitch-dialog-note");
    const code = element.querySelector("#twitch-dialog-code");
    const open = element.querySelector("#twitch-dialog-open");
    const status = element.querySelector("#twitch-dialog-status");
    title.textContent = mode === "link" ? "Привязка Twitch"
      : mode === "channel" ? "Подключение канала" : "Вход через Twitch";
    code.textContent = "————";
    note.textContent = mode === "link"
      ? "Код привяжет выбранный аккаунт Twitch к вашему аккаунту панели."
      : mode === "channel"
        ? "Войдите на Twitch в аккаунт канала, введите код и подтвердите доступ — панель подключит бота к этому каналу."
        : "Введите код на сайте Twitch и подтвердите доступ — панель дождётся ответа.";
    element.showModal();
    let alive = true;
    let timer = null;
    stop = () => { alive = false; clearTimeout(timer); };
    const wait = seconds => new Promise(resolve => { timer = setTimeout(resolve, seconds * 1000); });
    try {
      const start = await api("/auth/twitch/start", { purpose: mode });
      if (!alive) return;
      if (mode === "link" && start.full) {
        note.textContent = "Владелец: Twitch покажет полный список прав (эфир, модерация, статистика). Панель сохранит токен, подставит его боту и будет обновлять автоматически.";
      }
      code.textContent = start.code || "————";
      open.href = start.uri;
      const interval = Math.max(3, Number(start.interval) || 5);
      for (;;) {
        if (!alive) return;
        status.textContent = "Ожидаем подтверждения в Twitch…";
        await wait(interval);
        if (!alive) return;
        let data;
        try {
          data = await api("/auth/twitch/poll", { flow: start.flow });
        } catch (error) {
          if (alive) status.textContent = error.message;
          return;
        }
        if (!alive) return;
        if (data.status === "pending") continue;
        if (data.status === "ok") {
          status.textContent = "Готово. Открываем панель…";
          stop = null;
          close();
          onSuccess?.();
          return;
        }
        if (data.status === "linked") {
          status.textContent = data.full
            ? `Готово: привязан Twitch ${data.login}. Полные права получены — токен сохранён, бот получит его автоматически.`
            : `Готово: привязан Twitch ${data.login}.`;
          stop = null;
          onSuccess?.(data);
          return;
        }
        if (data.status === "channel") {
          status.textContent = data.joined
            ? `Готово: бот подключён к каналу #${data.login}.`
            : `Канал #${data.login} подключён. Бот зайдёт в него при следующем запуске.`;
          stop = null;
          onSuccess?.(data);
          return;
        }
        if (data.status === "foreign") {
          status.textContent = `Вы вошли как Twitch ${data.login}, а к панели привязан ${data.expected || "другой аккаунт"}. Подключить можно только свой канал. Закройте окно и войдите в свой аккаунт.`;
          return;
        }
        if (data.status === "channel-error") {
          status.textContent = data.error || "Не удалось подключить канал.";
          return;
        }
        if (data.status === "unlinked") {
          status.textContent = `Аккаунт Twitch ${data.login} не привязан к панели. Войдите с логином и паролем, затем привяжите Twitch в разделе «Доступ к панели».`;
          return;
        }
        if (data.status === "denied") {
          status.textContent = "Доступ в Twitch отклонён. Закройте окно и попробуйте ещё раз, если это ошибка.";
          return;
        }
        if (data.status === "config") {
          status.textContent = "Twitch отклонил приложение: владельцу нужно проверить TWITCH_CLIENT_ID и TWITCH_CLIENT_SECRET в .env.";
          return;
        }
        if (data.status === "failed") {
          status.textContent = "Не удалось определить аккаунт Twitch. Попробуйте ещё раз.";
          return;
        }
        status.textContent = "Код истёк. Закройте окно и попробуйте снова.";
        return;
      }
    } catch (error) {
      if (alive) status.textContent = error.name === "TypeError" ? "Нет связи с панелью. Проверьте подключение." : error.message;
    }
  }

  window.ImmortalTwitch = { open(mode, onSuccess) { run(mode, onSuccess).catch(() => {}); } };
})();
