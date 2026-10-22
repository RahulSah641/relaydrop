/* RelayDrop front-end settings — the only file you edit to point the UI at a different API.
   apiBase = where the Python API lives.
     ""                        → same origin (the Python server also serves this folder)
     "https://your-api.host"   → API on another host (e.g. UI on GitHub Pages, API on Render/Railway/Fly…)
   The GitHub Pages workflow rewrites this file from the RELAYDROP_API_URL repository variable. */
window.RELAYDROP_CONFIG = { apiBase: "" };

/* Local convenience: if you open the HTML from VS Code Live Server (or any other local port), talk to the
   Python API on port 5000 of the same machine. (Opening the file directly as file:// is not supported.) */
(function () {
  var c = window.RELAYDROP_CONFIG, h = location.hostname;
  if (!c.apiBase && (h === "localhost" || h === "127.0.0.1") && location.port !== "5000") c.apiBase = "http://" + h + ":5000";
})();
