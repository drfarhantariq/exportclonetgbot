"use strict";
(async () => {
  try {
    const response = await fetch("/auth/widget-config");
    if (!response.ok)
      throw new Error("Login expired. Return to the workspace and try again.");
    const config = await response.json();
    const script = document.createElement("script");
    script.src = "https://telegram.org/js/telegram-widget.js?22";
    script.async = true;
    script.setAttribute("data-telegram-login", config.bot);
    script.setAttribute("data-size", "large");
    script.setAttribute("data-userpic", "false");
    script.setAttribute("data-auth-url", config.callback);
    script.onerror = () => {
      document.getElementById("widget-help").textContent =
        "Could not load Telegram login. Check your connection or browser extensions, then reload.";
    };
    document.getElementById("telegram-login-widget").append(script);
  } catch (error) {
    document.getElementById("widget-help").textContent = error.message;
  }
})();
