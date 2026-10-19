(() => {
  "use strict";

  const form = document.querySelector("#login");
  const button = form.querySelector("button");
  const error = document.querySelector("#login-error");

  const twitch = document.querySelector("#twitch-login");
  if (twitch) twitch.addEventListener("click", () => window.ImmortalTwitch.open("login", () => window.location.assign("/")));

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (button.disabled) return;
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    button.textContent = "Входим…";
    error.hidden = true;
    form.elements.password.removeAttribute("aria-invalid");
    try {
      const response = await fetch("/login", {
        method: "POST",
        signal: AbortSignal.timeout(15000),
        headers: { "Content-Type": "application/json", "X-CSRF-Token": document.querySelector('meta[name="csrf-token"]').content },
        body: JSON.stringify({ username: form.elements.username.value, password: form.elements.password.value })
      });
      const data = await response.json();
      if (!response.ok) {
        if (response.status === 401) form.elements.password.setAttribute("aria-invalid", "true");
        throw new Error(data.error || "Не удалось войти. Попробуйте ещё раз.");
      }
      window.location.assign("/");
    } catch (cause) {
      error.textContent = cause.name === "TimeoutError" || cause instanceof TypeError ? "Нет связи с панелью. Проверьте подключение и повторите вход." : cause.message;
      error.hidden = false;
      form.elements.password.focus();
    } finally {
      button.disabled = false;
      button.setAttribute("aria-busy", "false");
      button.textContent = "Войти в панель →";
    }
  });
})();
