import { useEffect, useState } from "react";
import {
  ambient,
  type AckKind,
  type AmbientConfig,
  type WakePhrase,
} from "../voice/ambient";

const WAKE_LABELS: Record<WakePhrase, string> = {
  "hey pilk": "Hey PILK",
  pilk: "PILK",
};

const ACK_LABELS: Record<AckKind, string> = {
  yes: "\u201cYes?\u201d",
  "im-here": "\u201cI'm here.\u201d",
  mm: "\u201cMm?\u201d",
  tone: "Subtle tone",
  none: "No acknowledgement",
};

export default function Settings() {
  const [cfg, setCfg] = useState<AmbientConfig>(ambient.getConfig());
  const [supported] = useState<boolean>(ambient.supported);
  const [permissionError, setPermissionError] = useState<string | null>(null);

  useEffect(() => ambient.subscribeConfig(setCfg), []);
  useEffect(() =>
    ambient.subscribe((_s, caption) => {
      if (caption?.toLowerCase().includes("permission")) {
        setPermissionError(caption);
      }
    }),
  []);

  const requestMic = async () => {
    setPermissionError(null);
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      stream.getTracks().forEach((t) => t.stop());
      ambient.setConfig({ enabled: true });
    } catch (e: any) {
      setPermissionError(e?.message ?? "Microphone permission denied");
    }
  };

  return (
    <div className="settings">
      <header className="settings-head">
        <div className="placeholder-eyebrow">Settings</div>
        <h2>Voice, permissions, budgets, and keys</h2>
        <p>
          Configure how PILK listens, what it says back, and which risk classes
          need your explicit approval. Budgets and the session vault arrive in a
          follow-up batch.
        </p>
      </header>

      <section className="settings-card">
        <div className="settings-card-head">
          <div className="settings-card-title">Ambient listening</div>
          <label className="settings-switch">
            <input
              type="checkbox"
              checked={cfg.enabled}
              disabled={!supported}
              onChange={(e) => {
                if (e.target.checked) {
                  void requestMic();
                } else {
                  ambient.setConfig({ enabled: false });
                }
              }}
            />
            <span className="settings-switch-track" />
          </label>
        </div>
        <p className="settings-card-body">
          When on, PILK listens in the background and wakes the moment you say
          the wake phrase. Tap-to-talk on the orb keeps working either way.
        </p>

        {!supported && (
          <div className="settings-note settings-note--warn">
            Your browser doesn't expose Web Speech API. Ambient listening isn't
            available here — the tap-to-talk flow still works. Chrome, Edge, or
            Safari recommended.
          </div>
        )}
        {permissionError && (
          <div className="settings-note settings-note--warn">
            {permissionError}. Grant microphone access in your browser settings,
            then toggle ambient listening again.
          </div>
        )}

        <div className="settings-row">
          <div className="settings-row-label">Wake phrase</div>
          <div className="settings-segmented">
            {(Object.keys(WAKE_LABELS) as WakePhrase[]).map((w) => (
              <button
                key={w}
                type="button"
                className={`settings-seg${cfg.wakePhrase === w ? " settings-seg--on" : ""}`}
                onClick={() => ambient.setConfig({ wakePhrase: w })}
                disabled={!cfg.enabled}
              >
                {WAKE_LABELS[w]}
              </button>
            ))}
          </div>
        </div>

        <div className="settings-row">
          <div className="settings-row-label">Acknowledgement</div>
          <div className="settings-segmented settings-segmented--wrap">
            {(Object.keys(ACK_LABELS) as AckKind[]).map((k) => (
              <button
                key={k}
                type="button"
                className={`settings-seg${cfg.ack === k ? " settings-seg--on" : ""}`}
                onClick={() => ambient.setConfig({ ack: k })}
                disabled={!cfg.enabled}
              >
                {ACK_LABELS[k]}
              </button>
            ))}
          </div>
        </div>

        <div className="settings-row">
          <div className="settings-row-label">Use the premium voice for the acknowledgement</div>
          <label className="settings-switch settings-switch--sm">
            <input
              type="checkbox"
              checked={cfg.useElevenLabsAck}
              disabled={!cfg.enabled || cfg.ack === "tone" || cfg.ack === "none"}
              onChange={(e) =>
                ambient.setConfig({ useElevenLabsAck: e.target.checked })
              }
            />
            <span className="settings-switch-track" />
          </label>
        </div>
        <div className="settings-note">
          Off: uses your browser's built-in voice (free, instant). On: routes
          through ElevenLabs like the main replies (slightly slower, better
          sounding).
        </div>
      </section>

      <section className="settings-card settings-card--muted">
        <div className="settings-card-title">Budgets, permissions, session vault</div>
        <p className="settings-card-body">
          Daily spend ceilings, trust rules, and the key vault arrive in a
          follow-up batch.
        </p>
      </section>
    </div>
  );
}
