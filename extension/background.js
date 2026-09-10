// The reason this extension exists: a bookmarklet's fetch runs in the page's
// origin and obeys the page's Content-Security-Policy. Wikipedia, GitHub and
// most news sites forbid connecting to localhost, so the bookmarklet dies
// there. A service worker's fetch is not subject to the page's CSP.

const DEFAULTS = { host: "http://127.0.0.1:8788", token: "" };

// Extension storage is per Chrome profile. `sync` shares settings across
// profiles signed into the same Google account; `local` is the fallback when
// sync is unavailable. Read both, prefer sync.
async function settings() {
  const sync = await chrome.storage.sync.get(DEFAULTS).catch(() => ({}));
  const local = await chrome.storage.local.get(DEFAULTS).catch(() => ({}));
  return {
    host: sync.host || local.host || DEFAULTS.host,
    token: sync.token || local.token || DEFAULTS.token,
  };
}

function toast(tabId, message, ok) {
  // Injected into the page purely to show a result. It touches nothing else.
  chrome.scripting
    .executeScript({
      target: { tabId },
      func: (msg, good) => {
        const el = document.createElement("div");
        el.textContent = "readcast: " + msg;
        el.style.cssText =
          "position:fixed;top:12px;right:12px;z-index:2147483647;padding:8px 12px;" +
          "background:" + (good ? "#111" : "#7a1f2b") + ";color:#fff;" +
          "font:13px system-ui;border-radius:6px;box-shadow:0 2px 10px rgba(0,0,0,.35)";
        document.body.appendChild(el);
        setTimeout(() => el.remove(), 2600);
      },
      args: [message, ok],
    })
    .catch(() => {
      // Some pages (chrome://, the Web Store) refuse injection. Fall back.
      chrome.notifications?.create({
        type: "basic",
        iconUrl: "icons/icon48.png",
        title: "readcast",
        message,
      });
    });
}

async function send(tab) {
  const { host, token } = await settings();
  if (!token) {
    // Say why, so a second Chrome profile does not read as a broken extension.
    toast(tab.id, "no token saved for this Chrome profile — opening options", false);
    chrome.runtime.openOptionsPage();
    return;
  }

  let html = "";
  try {
    // The page is already rendered and already carries your session. That is
    // the whole point: a paywalled article arrives intact.
    const [result] = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      func: () => document.documentElement.outerHTML.slice(0, 4e6),
    });
    html = result?.result || "";
  } catch (e) {
    // No page access: let the server fetch the URL itself.
  }

  try {
    const response = await fetch(host.replace(/\/$/, "") + "/jobs", {
      method: "POST",
      headers: {
        "content-type": "application/json",
        authorization: "Bearer " + token,
      },
      body: JSON.stringify({ url: tab.url, title: tab.title, client_html: html }),
    });
    if (response.ok) {
      const { id } = await response.json();
      toast(tab.id, "queued " + id, true);
    } else if (response.status === 401) {
      toast(tab.id, "bad token — check the options page", false);
    } else if (response.status === 413) {
      toast(tab.id, "page too large", false);
    } else {
      toast(tab.id, "failed (" + response.status + ")", false);
    }
  } catch (e) {
    toast(tab.id, "cannot reach " + host + " — is readcast running?", false);
  }
}

chrome.action.onClicked.addListener((tab) => {
  if (tab?.id != null) send(tab);
});
