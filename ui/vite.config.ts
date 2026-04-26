import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import path from "node:path";

export default defineConfig({
  plugins: [react()],
  // Read VITE_* env from the repo root, not the ``ui/`` package dir.
  // The repo's single ``.env`` lives at the project root (next to
  // ``pyproject.toml``) and pilkd reads from there too — having one
  // file as the source of truth means ``VITE_PILK_API`` /
  // ``VITE_PILK_WS`` / ``PILK_TAILNET_HOSTS`` all stay in sync. Without
  // this, Vite silently fell back to its hardcoded localhost defaults
  // and the dashboard tried to WebSocket to 127.0.0.1 from the phone.
  envDir: path.resolve(__dirname, ".."),
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "src"),
    },
  },
  server: {
    // Listen on all interfaces so the dashboard is reachable via
    // the Mac's Tailscale Magic DNS name (e.g. from iPhone).
    // Tailscale is the security boundary — anyone NOT on your
    // tailnet can't reach this port unless your Mac's firewall
    // is off and you're on a public network. Change back to
    // "127.0.0.1" while traveling on untrusted WiFi.
    host: "0.0.0.0",
    port: 1420,
    strictPort: true,
    // Vite blocks requests whose Host header doesn't match
    // ``allowedHosts`` as a CSRF / DNS-rebinding guard. Allow
    // every device in this tailnet — the ``.tail27a331.ts.net``
    // suffix is unique to the operator's Tailscale account, so
    // only their own devices match. Adjust the suffix if the
    // tailnet ever changes (visible in the Tailscale Mac app
    // under Magic DNS).
    allowedHosts: [".tail27a331.ts.net"],
  },
});
