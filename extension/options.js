const DEFAULTS = { host: "http://127.0.0.1:8788", token: "" };

const hostEl = document.getElementById("host");
const tokenEl = document.getElementById("token");
const stateEl = document.getElementById("state");

// Settings live in chrome.storage.sync so that profiles signed into the same
// Google account share them. Storage is per profile otherwise: a second Chrome
// session is a second setup. `local` is the fallback when sync is unavailable.
async function load() {
  const sync = await chrome.storage.sync.get(DEFAULTS).catch(() => ({}));
  const local = await chrome.storage.local.get(DEFAULTS).catch(() => ({}));
  return {
    host: sync.host || local.host || DEFAULTS.host,
    token: sync.token || local.token || DEFAULTS.token,
  };
}

async function save() {
  const values = {
    host: hostEl.value.trim().replace(/\/$/, "") || DEFAULTS.host,
    token: tokenEl.value.trim(),
  };
  await chrome.storage.sync.set(values).catch(() => {});
  await chrome.storage.local.set(values).catch(() => {});
  return values;
}

function show(message, cls = "") {
  stateEl.innerHTML = cls ? `<span class="${cls}">${message}</span>` : message;
}

function mask(token) {
  if (!token) return "none stored";
  return token.length <= 6 ? "••••" : "••••" + token.slice(-4);
}

async function describe() {
  const { host, token } = await load();
  show(`stored for this profile: ${host} · token ${mask(token)}`);
}

// Save on every edit. The old version saved only on a button click, so typing
// the token and pressing Enter stored nothing at all.
let timer = null;
for (const el of [hostEl, tokenEl]) {
  el.addEventListener("input", () => {
    clearTimeout(timer);
    show("saving…");
    timer = setTimeout(async () => {
      await save();
      describe();
    }, 250);
  });
  el.addEventListener("blur", async () => {
    await save();
    describe();
  });
  el.addEventListener("keydown", async (e) => {
    if (e.key === "Enter") {
      await save();
      test();
    }
  });
}

async function test() {
  const { host, token } = await save();
  show("testing " + host + " …");
  try {
    const response = await fetch(host + "/jobs?limit=1", {
      headers: { authorization: "Bearer " + token },
    });
    if (response.ok) show("connected — token accepted. You are ready.", "ok");
    else if (response.status === 401) show("reached readcast, but the token is wrong.", "bad");
    else if (response.status === 503) show("readcast still has api_token set to CHANGE_ME.", "bad");
    else show("reached readcast, but it answered " + response.status + ".", "bad");
  } catch (e) {
    show("cannot reach " + host + " — is `readcast serve` running?", "bad");
  }
}

document.getElementById("test").addEventListener("click", test);
document.getElementById("reveal").addEventListener("click", (e) => {
  const hidden = tokenEl.type === "password";
  tokenEl.type = hidden ? "text" : "password";
  e.target.textContent = hidden ? "Hide token" : "Show token";
});

(async () => {
  const { host, token } = await load();
  hostEl.value = host;
  tokenEl.value = token;

  // A new profile can be set up in one paste: open
  //   chrome-extension://<id>/options.html#host=...&token=...
  // The fragment never leaves the browser. `readcast extension-link` prints it.
  const fragment = new URLSearchParams(location.hash.slice(1));
  if (fragment.get("token") || fragment.get("host")) {
    if (fragment.get("host")) hostEl.value = fragment.get("host");
    if (fragment.get("token")) tokenEl.value = fragment.get("token");
    await save();
    history.replaceState(null, "", location.pathname); // do not leave it in the URL
    await test();
    return;
  }
  describe();
})();
