import { useCallback, useEffect, useState } from "react";
import {
  ambient,
  type AckKind,
  type AmbientConfig,
  type Patience,
  type WakePhrase,
} from "../voice/ambient";
import {
  fetchGovernorStatus,
  pilk,
  setGovernorOverride,
  type GovernorStatus,
  type OverrideMode,
} from "../state/api";

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

const PATIENCE_LABELS: Record<Patience, string> = {
  snappy: "Snappy",
  normal: "Normal",
  patient: "Patient",
  "very-patient": "Very patient",
};

const OVERRIDE_LABELS: Record<OverrideMode, string> = {
  auto: "Auto",
  light: "Force Fast",
  standard: "Force Balanced",
  premium: "Force Deep",
};

export default function Settings() {
  const [cfg, setCfg] = useState<AmbientConfig>(ambient.getConfig());
  const [supported] = useState<boolean>(ambient.supported);
  const [permissionError, setPermissionError] = useState<string | null>(null);
  const [gov, setGov] = useState<GovernorStatus | null>(null);
  const [govBusy, setGovBusy] = useState(false);

  useEffect(() => ambient.subscribeConfig(setCfg), []);
  useEffect(() =>
    ambient.subscribe((_s, caption) => {
      if (caption?.toLowerCase().includes("permission")) {
        setPermissionError(caption);
      }
    }),
  []);

  const refreshGov = useCallback(() => {
    fetchGovernorStatus().then(setGov).catch(() => {});
  }, []);

  useEffect(() => {
    refreshGov();
    return pilk.onMessage((m) => {
      if (m.type === "cost.updated" || m.type === "plan.completed") refreshGov();
    });
  }, [refreshGov]);

  const onOverride = async (mode: OverrideMode) => {
    setGovBusy(true);
    try {
      await setGovernorOverride(mode);
      refreshGov();
    } finally {
      setGovBusy(false);
    }
  };

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
          <div className="settings-row-label">Listen patience</div>
          <div className="settings-segmented settings-segmented--wrap">
            {(Object.keys(PATIENCE_LABELS) as Patience[]).map((p) => (
              <button
                key={p}
                type="button"
                className={`settings-seg${cfg.patience === p ? " settings-seg--on" : ""}`}
                onClick={() => ambient.setConfig({ patience: p })}
                disabled={!cfg.enabled}
              >
                {PATIENCE_LABELS[p]}
              </button>
            ))}
          </div>
        </div>
        <div className="settings-note">
          How long PILK waits after the wake phrase, and between pauses
          mid-sentence, before sending your request. Bump this up if it cuts
          you off; bring it down for quicker one-liners.
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

      <section className="settings-card">
        <div className="settings-card-head">
          <div className="settings-card-title">Reasoning &amp; budget</div>
        </div>
        <p className="settings-card-body">
          Which AI model PILK picks for each task, and how much it's allowed to
          spend per day. Light chat runs on a cheap model automatically; only
          complex reasoning escalates to the expensive one.
        </p>

        {gov?.enabled && gov.tiers ? (
          <>
            <div className="settings-row">
              <div className="settings-row-label">Model tiers</div>
            </div>
            <div className="governor-tiers">
              {(["light", "standard", "premium"] as const).map((k) => {
                const t = gov.tiers![k];
                return (
                  <div key={k} className="governor-tier">
                    <div className="governor-tier-label">{t.label}</div>
                    <div className="governor-tier-model" title={t.model}>
                      {t.model}
                    </div>
                    <div className="governor-tier-provider">{t.provider}</div>
                  </div>
                );
              })}
            </div>
            <div className="settings-note">
              Edit via <code>PILK_TIER_LIGHT_MODEL</code> /{" "}
              <code>PILK_TIER_STANDARD_MODEL</code> /{" "}
              <code>PILK_TIER_PREMIUM_MODEL</code> in <code>.env</code>. In-UI
              editing arrives in a follow-up batch.
            </div>

            <div className="settings-row">
              <div className="settings-row-label">Session override</div>
              <div className="settings-segmented settings-segmented--wrap">
                {(Object.keys(OVERRIDE_LABELS) as OverrideMode[]).map((m) => (
                  <button
                    key={m}
                    type="button"
                    className={`settings-seg${gov.override === m ? " settings-seg--on" : ""}`}
                    onClick={() => onOverride(m)}
                    disabled={govBusy}
                  >
                    {OVERRIDE_LABELS[m]}
                  </button>
                ))}
              </div>
            </div>
            <div className="settings-note">
              <strong>Auto</strong> lets the router pick per request.
              Forcing a tier applies to every plan until you change it back.
            </div>

            <div className="settings-row">
              <div className="settings-row-label">Daily budget</div>
              <div className="governor-budget">
                <div className="governor-budget-figures">
                  <strong>${gov.budget?.spent_usd.toFixed(4)}</strong> of{" "}
                  ${gov.budget?.cap_usd.toFixed(2)} today
                </div>
                <div className="governor-budget-bar">
                  <div
                    className={`governor-budget-fill${
                      gov.budget?.is_over
                        ? " governor-budget-fill--over"
                        : gov.budget?.is_warn
                          ? " governor-budget-fill--warn"
                          : ""
                    }`}
                    style={{
                      width: `${clamp01(
                        (gov.budget?.spent_usd ?? 0) /
                          Math.max(0.0001, gov.budget?.cap_usd ?? 0),
                      ) * 100}%`,
                    }}
                  />
                </div>
              </div>
            </div>
            <div className="settings-note">
              Soft warning at 80 %, hard stop at 100 %. Override via{" "}
              <code>PILK_DAILY_CAP_USD</code> in <code>.env</code>.
            </div>

            <div className="settings-row">
              <div className="settings-row-label">Premium gate</div>
              <div className="governor-gate">
                {gov.premium_gate === "ask" ? (
                  <span className="governor-gate-pill governor-gate-pill--on">
                    Ask before Deep Reasoning · ON
                  </span>
                ) : (
                  <span className="governor-gate-pill">
                    Ask before Deep Reasoning · OFF
                  </span>
                )}
              </div>
            </div>
            <div className="settings-note">
              When on, tasks the router classifies as premium are downgraded to
              Balanced. Override via <code>PILK_PREMIUM_GATE=auto</code> in{" "}
              <code>.env</code>. In-UI toggle + per-task approval UI arrive
              next.
            </div>
          </>
        ) : (
          <div className="settings-note">
            Governor isn't available — pilkd may be starting up. Refresh in a
            moment.
          </div>
        )}
      </section>

      <section className="settings-card settings-card--muted">
        <div className="settings-card-title">Permissions &amp; session vault</div>
        <p className="settings-card-body">
          Trust rules and the key vault arrive in a follow-up batch.
        </p>
      </section>
    </div>
  );
}

function clamp01(x: number): number {
  if (!Number.isFinite(x)) return 0;
  if (x < 0) return 0;
  if (x > 1) return 1;
  return x;
}
