import { useCallback, useEffect, useState } from "react";
import {
  ambient,
  resolveTiming,
  TUNING_BOUNDS,
  type AckKind,
  type AmbientConfig,
  type Patience,
  type WakePhrase,
} from "../voice/ambient";

/** The two timing fields show the *effective* value (preset +
 * override) on the slider, so the operator always sees what's
 * actually in effect. These helpers compute it from the config. */
function resolveSilenceMs(cfg: AmbientConfig): number {
  return resolveTiming(cfg).silenceMs;
}
function resolveWakeGraceMs(cfg: AmbientConfig): number {
  return resolveTiming(cfg).wakeGraceMs;
}
import {
  clearIntegrationSecret,
  deleteConnectedAccount,
  detectTelegramChat,
  fetchAgents,
  fetchCodingEngines,
  fetchInstalledSkills,
  fetchConnectedAccounts,
  fetchGovernorStatus,
  fetchGrants,
  fetchIntegrationSecrets,
  fetchProviders,
  fetchTelegramBotInfo,
  fetchXAUUSDSettings,
  grantAgentAccess,
  pilk,
  revokeAgentAccess,
  sendTelegramTest,
  setDefaultConnectedAccount,
  setGovernorConfig,
  setGovernorOverride,
  setIntegrationSecret,
  setXAUUSDExecutionMode,
  startOAuthConnection,
  type AgentRow,
  type CodingEngineHealth,
  type InstalledPack,
  type ConnectedAccount,
  type GovernorStatus,
  type IntegrationSecretEntry,
  type OverrideMode,
  type ProviderInfo,
  type XAUUSDExecutionMode,
  type XAUUSDSettings,
} from "../state/api";
import { humanizeAgentName } from "../lib/humanize";

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

const VOICE_RATES: Array<{ value: number; label: string }> = [
  { value: 1.0, label: "Normal" },
  { value: 1.15, label: "Brisk" },
  { value: 1.25, label: "Fast" },
  { value: 1.5, label: "Rushed" },
];

const CAP_OPTIONS: Array<{ value: number; label: string }> = [
  { value: 1, label: "$1" },
  { value: 5, label: "$5" },
  { value: 10, label: "$10" },
  { value: 25, label: "$25" },
  { value: 50, label: "$50" },
  { value: 100, label: "$100" },
  { value: 0, label: "Unlimited" },
];

/** Settings is a gallery-first springboard (matches /agents). The
 * table below defines every category that gets a card. Each entry
 * maps to a single `<section>` block in the render; tapping a card
 * swaps the pane to just that section with a back button. Keep this
 * list in order of most-common usage first so a new operator sees
 * the important stuff up top. */
type SettingsCategory =
  | "api-keys"
  | "accounts"
  | "telegram"
  | "voice"
  | "budget"
  | "xauusd"
  | "coding"
  | "trust";

interface SettingsCategoryDef {
  id: SettingsCategory;
  avatar: string;
  label: string;
  blurb: string;
}

const SETTINGS_CATEGORIES: SettingsCategoryDef[] = [
  {
    id: "api-keys",
    avatar: "🔑",
    label: "API Keys",
    blurb:
      "Paste HubSpot, Hunter, Nano Banana, Higgsfield, Browserbase, and the rest — live instantly, no redeploy.",
  },
  {
    id: "accounts",
    avatar: "🔗",
    label: "Connected Accounts",
    blurb:
      "Sign in to Google, LinkedIn, and friends with OAuth so agents can send email, post, and read on your behalf.",
  },
  {
    id: "telegram",
    avatar: "📲",
    label: "Telegram",
    blurb:
      "How PILK pings you when you're away from the dashboard. Step-through setup with token verify + chat auto-detect.",
  },
  {
    id: "voice",
    avatar: "🎤",
    label: "Voice & Listening",
    blurb:
      "Wake phrase, acknowledgement style, listen patience, and TTS speed — how PILK hears you and talks back.",
  },
  {
    id: "budget",
    avatar: "💰",
    label: "Reasoning & Budget",
    blurb:
      "Model tier routing (Haiku / Sonnet / Opus), daily cost caps, and the premium-gate approval flow.",
  },
  {
    id: "xauusd",
    avatar: "🪙",
    label: "XAU/USD Agent",
    blurb:
      "Execution mode for the gold-trading agent: approve every order, or let it run autonomously inside its risk caps.",
  },
  {
    id: "coding",
    avatar: "⌨️",
    label: "Coding Engines",
    blurb:
      "Which backend routes code tasks — Claude Code bridge, Agent SDK, or the bare API engine.",
  },
  {
    id: "trust",
    avatar: "🛡️",
    label: "Trust Rules",
    blurb:
      "Fine-grained per-tool trust editor. Placeholder — arriving in a follow-up batch.",
  },
];

export default function Settings() {
  const [selectedCategory, setSelectedCategory] =
    useState<SettingsCategory | null>(null);
  const [cfg, setCfg] = useState<AmbientConfig>(ambient.getConfig());
  const [supported] = useState<boolean>(ambient.supported);
  const [permissionError, setPermissionError] = useState<string | null>(null);
  const [gov, setGov] = useState<GovernorStatus | null>(null);
  const [govBusy, setGovBusy] = useState(false);
  const [accounts, setAccounts] = useState<ConnectedAccount[] | null>(null);
  const [accountDefaults, setAccountDefaults] = useState<Record<string, string>>(
    {},
  );
  const [providers, setProviders] = useState<ProviderInfo[]>([]);
  interface ConnectDialog {
    provider: string;
    role: "system" | "user";
    groups: string[];
    accountEmail?: string;
    // When set, the user is *expanding* an existing account's scopes
    // rather than linking a new one. Copy changes accordingly.
    expandingAccountId?: string;
  }
  const [connectDialog, setConnectDialog] = useState<ConnectDialog | null>(null);
  const [connectBusy, setConnectBusy] = useState(false);
  const [connectError, setConnectError] = useState<string | null>(null);
  const [grants, setGrants] = useState<Record<string, string[]>>({});
  const [agentsList, setAgentsList] = useState<AgentRow[]>([]);
  const [manageAccessFor, setManageAccessFor] = useState<string | null>(null);
  const [codingEngines, setCodingEngines] = useState<CodingEngineHealth[] | null>(
    null,
  );

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

  const refreshAccounts = useCallback(() => {
    fetchConnectedAccounts()
      .then((r) => {
        setAccounts(r.accounts);
        setAccountDefaults(r.defaults);
      })
      .catch(() => setAccounts([]));
    fetchProviders()
      .then((r) => setProviders(r.providers))
      .catch(() => setProviders([]));
    fetchGrants()
      .then((r) => {
        const byAccount: Record<string, string[]> = {};
        for (const [agentName, g] of Object.entries(r.grants)) {
          for (const accountId of g.accounts) {
            (byAccount[accountId] ??= []).push(agentName);
          }
        }
        for (const accountId of Object.keys(byAccount)) {
          byAccount[accountId].sort();
        }
        setGrants(byAccount);
      })
      .catch(() => setGrants({}));
    fetchAgents()
      .then((r) => setAgentsList(r.agents))
      .catch(() => setAgentsList([]));
  }, []);

  const refreshCodingEngines = useCallback(() => {
    fetchCodingEngines()
      .then((r) => setCodingEngines(r.engines))
      .catch(() => setCodingEngines([]));
  }, []);

  useEffect(() => {
    refreshGov();
    refreshAccounts();
    refreshCodingEngines();
    return pilk.onMessage((m) => {
      if (m.type === "cost.updated" || m.type === "plan.completed") refreshGov();
      if (
        m.type === "account.linked" ||
        m.type === "account.removed" ||
        m.type === "account.default_changed" ||
        m.type === "agent.created" ||
        m.type === "agent.grant_added" ||
        m.type === "agent.grant_removed"
      ) {
        refreshAccounts();
      }
    });
  }, [refreshGov, refreshAccounts, refreshCodingEngines]);

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

      {selectedCategory === null && (
        <div className="settings-gallery agents-gallery">
          {SETTINGS_CATEGORIES.map((c) => (
            <button
              key={c.id}
              className="agent-card"
              onClick={() => setSelectedCategory(c.id)}
            >
              <div className="agent-card-avatar" aria-hidden>
                {c.avatar}
              </div>
              <div className="agent-card-body">
                <div className="agent-card-name">{c.label}</div>
                <div className="agent-card-blurb">{c.blurb}</div>
              </div>
            </button>
          ))}
        </div>
      )}

      {selectedCategory !== null && (
        <button
          type="button"
          className="agents-back"
          onClick={() => setSelectedCategory(null)}
        >
          ← All settings
        </button>
      )}

      {selectedCategory === "voice" && (
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

        <div className="settings-row">
          <div className="settings-row-label">Voice speed</div>
          <div className="settings-segmented settings-segmented--wrap">
            {VOICE_RATES.map((r) => (
              <button
                key={r.value}
                type="button"
                className={`settings-seg${Math.abs(cfg.voiceRate - r.value) < 0.01 ? " settings-seg--on" : ""}`}
                onClick={() => ambient.setConfig({ voiceRate: r.value })}
              >
                {r.label}{" "}
                <span style={{ opacity: 0.6 }}>{r.value.toFixed(2)}&times;</span>
              </button>
            ))}
          </div>
        </div>
        <div className="settings-note">
          Applies to both the acknowledgement and PILK's reply.
        </div>

        <div className="voice-tuning">
          <div className="voice-tuning-title">
            Fine-tuning (advanced)
          </div>
          <div className="voice-tuning-hint">
            Start with the patience preset above. Only drop into these
            sliders if PILK still cuts you off, hangs on ambient noise,
            or triggers on stray speech. Changes save automatically
            and apply on the next utterance.
          </div>

          <TuningSlider
            label="Silence before finalize"
            unit="ms"
            value={resolveSilenceMs(cfg)}
            override={cfg.silenceMsOverride ?? null}
            bounds={TUNING_BOUNDS.silenceMs}
            onChange={(v) =>
              ambient.setConfig({ silenceMsOverride: v })
            }
            help={
              "How long PILK waits after you stop talking before calling your turn done. " +
              "Too low → gets cut off mid-sentence. Too high → hangs waiting for more words."
            }
          />

          <TuningSlider
            label="Wake grace window"
            unit="ms"
            value={resolveWakeGraceMs(cfg)}
            override={cfg.wakeGraceMsOverride ?? null}
            bounds={TUNING_BOUNDS.wakeGraceMs}
            onChange={(v) =>
              ambient.setConfig({ wakeGraceMsOverride: v })
            }
            help={
              'After you say "hey pilk", how long to hold the mic open if ' +
              "you haven't started the actual question yet."
            }
          />

          <TuningSlider
            label="Minimum utterance length"
            unit="chars"
            value={cfg.minUtteranceChars ?? 0}
            override={cfg.minUtteranceChars ?? null}
            bounds={TUNING_BOUNDS.minUtteranceChars}
            onChange={(v) => ambient.setConfig({ minUtteranceChars: v })}
            help={
              'Drops captures shorter than this. Kills stray "hi", "um", ' +
              "single-word false starts from ambient speech. 0 disables."
            }
          />

          <TuningSlider
            label="Minimum confidence"
            unit=""
            value={cfg.minConfidence ?? 0}
            override={cfg.minConfidence ?? null}
            bounds={TUNING_BOUNDS.minConfidence}
            format={(v) => v.toFixed(2)}
            onChange={(v) => ambient.setConfig({ minConfidence: v })}
            help={
              "Rejects low-confidence transcriptions (0 = off, 0.9 = very strict). " +
              "Raise this if PILK keeps triggering on TV audio or nearby conversations. " +
              "Most browsers report low numbers so keep this below 0.5."
            }
          />
        </div>
      </section>
      )}

      {selectedCategory === "budget" && (
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
                  {gov.budget && gov.budget.cap_usd > 0
                    ? `$${gov.budget.cap_usd.toFixed(2)}`
                    : "unlimited"}{" "}
                  today
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
            <div className="settings-row">
              <div className="settings-row-label">Daily cap</div>
              <div className="settings-segmented settings-segmented--wrap">
                {CAP_OPTIONS.map((c) => {
                  const active =
                    Math.abs((gov.budget?.cap_usd ?? -1) - c.value) < 0.01;
                  return (
                    <button
                      key={c.value}
                      type="button"
                      className={`settings-seg${active ? " settings-seg--on" : ""}`}
                      onClick={async () => {
                        setGovBusy(true);
                        try {
                          const s = await setGovernorConfig({
                            daily_cap_usd: c.value,
                          });
                          setGov(s);
                        } finally {
                          setGovBusy(false);
                        }
                      }}
                      disabled={govBusy}
                    >
                      {c.label}
                    </button>
                  );
                })}
              </div>
            </div>
            <div className="settings-note">
              Soft warning at 80 %, hard stop at 100 %. "Unlimited" turns the
              cap off.
            </div>

            <div className="settings-row">
              <div className="settings-row-label">Ask before Deep Reasoning</div>
              <label className="settings-switch">
                <input
                  type="checkbox"
                  checked={gov.premium_gate === "ask"}
                  disabled={govBusy}
                  onChange={async (e) => {
                    setGovBusy(true);
                    try {
                      const s = await setGovernorConfig({
                        premium_gate: e.target.checked ? "ask" : "auto",
                      });
                      setGov(s);
                    } finally {
                      setGovBusy(false);
                    }
                  }}
                />
                <span className="settings-switch-track" />
              </label>
            </div>
            <div className="settings-note">
              When on, tasks the router classifies as premium are downgraded to
              Balanced instead of running on Deep Reasoning. Per-task "run this
              one on Deep anyway" approval prompt arrives in the next batch.
            </div>
          </>
        ) : (
          <div className="settings-note">
            Governor isn't available — pilkd may be starting up. Refresh in a
            moment.
          </div>
        )}
      </section>
      )}

      {selectedCategory === "accounts" && (
      <section className="settings-card">
        <div className="settings-card-head">
          <div className="settings-card-title">Connected accounts</div>
          <button
            type="button"
            className="btn"
            onClick={refreshAccounts}
            title="Refresh connection status"
          >
            Refresh
          </button>
        </div>
        <p className="settings-card-body">
          OAuth-connected services PILK can use. <strong>PILK identity</strong>{" "}
          is PILK acting as itself (signups, verifications, reports).{" "}
          <strong>Your identity</strong> is PILK acting on your behalf —
          every outgoing message from one of your accounts requires fresh
          approval.
        </p>

        {(() => {
          if (accounts === null) {
            return (
              <div className="settings-note">Reading connected accounts…</div>
            );
          }
          const total = accounts.length;
          const needsReauth = accounts.filter(
            (a) => a.status !== "connected",
          ).length;
          const system = accounts.filter((a) => a.role === "system");
          const user = accounts.filter((a) => a.role === "user");
          return (
            <>
              <div className="accounts-summary">
                <span className="accounts-summary-count">
                  {total} connected
                </span>
                {needsReauth > 0 && (
                  <span className="accounts-summary-warn">
                    · {needsReauth} need re-auth
                  </span>
                )}
              </div>
              {total === 0 ? (
                <div className="settings-note">
                  No accounts connected yet. Pick a service below to
                  start.
                </div>
              ) : (
                <>
                  <AccountsSection
                    title="PILK identity"
                    subtitle="Accounts PILK acts from as itself."
                    emptyCopy={
                      "No PILK-identity account linked yet. This is the " +
                      "Gmail PILK uses to sign itself up for tools and send " +
                      "you reports."
                    }
                    accounts={system}
                    providers={providers}
                    grants={grants}
                    accountDefaults={accountDefaults}
                    refresh={refreshAccounts}
                    setConnectError={setConnectError}
                    setConnectDialog={setConnectDialog}
                    setManageAccessFor={setManageAccessFor}
                  />
                  <AccountsSection
                    title="Your identity"
                    subtitle="Accounts PILK acts from on your behalf."
                    emptyCopy={
                      "No working accounts linked yet. This is where " +
                      "your real Gmail, Slack, LinkedIn, or X accounts " +
                      "live."
                    }
                    accounts={user}
                    providers={providers}
                    grants={grants}
                    accountDefaults={accountDefaults}
                    refresh={refreshAccounts}
                    setConnectError={setConnectError}
                    setConnectDialog={setConnectDialog}
                    setManageAccessFor={setManageAccessFor}
                  />
                </>
              )}
            </>
          );
        })()}

        <div className="accounts-connect">
          <div className="accounts-connect-label">Connect a new account</div>
          <div className="accounts-provider-grid">
            {providers.map((p) => {
              // When a provider supports both roles, default to the most
              // common user-facing role for that provider (user). The
              // dialog exposes a toggle if both roles are supported.
              const defaultRole: "system" | "user" = p.supports_roles.includes(
                "user",
              )
                ? "user"
                : "system";
              return (
                <button
                  key={p.name}
                  type="button"
                  className="accounts-provider-chip"
                  onClick={() => {
                    setConnectError(null);
                    setConnectDialog({
                      provider: p.name,
                      role: defaultRole,
                      groups: [...p.default_scope_groups],
                    });
                  }}
                >
                  <span className="accounts-provider-chip-label">{p.label}</span>
                </button>
              );
            })}
          </div>
        </div>

        {connectDialog && (() => {
          const provider = providers.find(
            (p) => p.name === connectDialog.provider,
          );
          if (!provider) return null;
          const expanding = connectDialog.expandingAccountId != null;
          return (
            <div className="accounts-confirm">
              <div className="accounts-confirm-text">
                {expanding ? (
                  <>
                    Re-link <strong>{connectDialog.accountEmail}</strong> with
                    wider scopes. {provider.label} remembers existing
                    access, so you only approve the added scopes.
                  </>
                ) : (
                  <>
                    About to open {provider.label} sign-in. This account
                    will be used as{" "}
                    <strong>
                      {connectDialog.role === "system" ? "PILK" : "you"}
                    </strong>
                    .
                  </>
                )}
              </div>
              {!expanding && provider.supports_roles.length > 1 && (
                <div className="accounts-confirm-groups">
                  <div className="accounts-confirm-groups-label">
                    Who is this for
                  </div>
                  <div className="accounts-confirm-groups-list">
                    {provider.supports_roles.map((r) => {
                      const selected = connectDialog.role === r;
                      return (
                        <label
                          key={r}
                          className="accounts-confirm-group"
                          title={
                            r === "system"
                              ? "PILK acting as itself (reports, signups)."
                              : "PILK acting on your behalf."
                          }
                        >
                          <input
                            type="radio"
                            name="connect-role"
                            checked={selected}
                            onChange={() =>
                              setConnectDialog((prev) =>
                                prev ? { ...prev, role: r } : prev,
                              )
                            }
                          />
                          <span>{r === "system" ? "PILK" : "You"}</span>
                        </label>
                      );
                    })}
                  </div>
                </div>
              )}
              {provider.scope_groups.length > 1 && (
                <div className="accounts-confirm-groups">
                  <div className="accounts-confirm-groups-label">
                    Request access to
                  </div>
                  <div className="accounts-confirm-groups-list">
                    {provider.scope_groups.map((g) => {
                      const checked = connectDialog.groups.includes(g.name);
                      return (
                        <label key={g.name} className="accounts-confirm-group">
                          <input
                            type="checkbox"
                            checked={checked}
                            onChange={(e) => {
                              setConnectDialog((prev) =>
                                prev
                                  ? {
                                      ...prev,
                                      groups: e.target.checked
                                        ? Array.from(
                                            new Set([...prev.groups, g.name]),
                                          )
                                        : prev.groups.filter(
                                            (x) => x !== g.name,
                                          ),
                                    }
                                  : prev,
                              );
                            }}
                          />
                          <span>{g.label}</span>
                        </label>
                      );
                    })}
                  </div>
                </div>
              )}
              <div className="accounts-confirm-actions">
                <button
                  type="button"
                  className="btn"
                  onClick={() => setConnectDialog(null)}
                  disabled={connectBusy}
                >
                  Cancel
                </button>
                <button
                  type="button"
                  className="btn btn--primary"
                  disabled={connectBusy || connectDialog.groups.length === 0}
                  onClick={async () => {
                    setConnectBusy(true);
                    setConnectError(null);
                    try {
                      const r = await startOAuthConnection({
                        provider: connectDialog.provider,
                        role: connectDialog.role,
                        scope_groups: connectDialog.groups,
                      });
                      window.open(r.auth_url, "_blank", "noopener,noreferrer");
                      setConnectDialog(null);
                    } catch (e) {
                      setConnectError(
                        e instanceof Error ? e.message : String(e),
                      );
                    } finally {
                      setConnectBusy(false);
                    }
                  }}
                >
                  {connectBusy ? "Opening…" : "Open sign-in"}
                </button>
              </div>
            </div>
          );
        })()}
        {connectError && (
          <div className="settings-note settings-note--warn">{connectError}</div>
        )}

        {manageAccessFor && (
          <ManageAccessModal
            accountId={manageAccessFor}
            account={accounts?.find((a) => a.account_id === manageAccessFor) ?? null}
            agents={agentsList}
            grantedAgents={grants[manageAccessFor] ?? []}
            onClose={() => setManageAccessFor(null)}
            onChanged={refreshAccounts}
          />
        )}
      </section>
      )}

      {selectedCategory === "coding" && (
      <section className="settings-card">
        <div className="settings-card-head">
          <div className="settings-card-title">Coding engines</div>
          <button
            type="button"
            className="btn"
            onClick={refreshCodingEngines}
            title="Refresh engine health"
          >
            Refresh
          </button>
        </div>
        <p className="settings-card-body">
          PILK picks the best backend for a coding task: a local Claude Code
          bridge for repo work, the Anthropic Agent SDK as an intermediate
          fallback, or a bare Anthropic API call for quick snippets. File
          edits always route through the approval queue separately.
        </p>

        {codingEngines === null ? (
          <div className="settings-note">Reading engine health…</div>
        ) : codingEngines.length === 0 ? (
          <div className="settings-note">No coding engines configured.</div>
        ) : (
          <div className="coding-engines">
            {codingEngines.map((e) => (
              <div key={e.name} className="coding-engine">
                <div className="coding-engine-head">
                  <div className="coding-engine-label">{e.label}</div>
                  <span
                    className={`connected-account-pill${
                      e.available ? " connected-account-pill--ok" : ""
                    }`}
                  >
                    {e.available ? "Available" : "Unavailable"}
                  </span>
                </div>
                {e.detail && (
                  <div className="coding-engine-detail">{e.detail}</div>
                )}
              </div>
            ))}
          </div>
        )}
        <InstalledSkillsPanel />
      </section>
      )}

      {selectedCategory === "api-keys" && <IntegrationSecretsSection />}

      {selectedCategory === "telegram" && <TelegramConnectSection />}

      {selectedCategory === "xauusd" && <XAUUSDSettingsSection />}

      {selectedCategory === "trust" && (
      <section className="settings-card settings-card--muted">
        <div className="settings-card-title">Trust rule editor</div>
        <p className="settings-card-body">
          Fine-grained per-tool trust rule editor arrives in a follow-up batch.
        </p>
      </section>
      )}
    </div>
  );
}

/** Settings → API Keys.
 *
 * Grouped card layout over `/integration-secrets`. Each secret lives
 * in a category inferred from `SECRET_CATEGORY` — with ~30-50 keys
 * coming, a flat vertical list becomes unusable.
 *
 * The dashboard never reads stored values back. Once saved, the input
 * clears and only the `updated_at` timestamp changes. Env-var fallback
 * stays live, so Railway vars keep working when nothing's been set via
 * this UI.
 */
type ApiKeyCategory =
  | "Sales-ops"
  | "Trading"
  | "Creative"
  | "Design"
  | "Core"
  | "Other";

const CATEGORY_ORDER: ApiKeyCategory[] = [
  "Sales-ops",
  "Creative",
  "Trading",
  "Design",
  "Core",
  "Other",
];

const CATEGORY_BLURB: Record<ApiKeyCategory, string> = {
  "Sales-ops": "Prospecting, enrichment, and CRM sync keys.",
  Trading: "Price feeds and remote-browser broker sessions.",
  Creative: "Image and video generation providers.",
  Design: "Per-site credentials for web-design delivery.",
  Core: "Platform-wide keys used by every agent.",
  Other: "Uncategorised or legacy entries — review and clean up.",
};

/** Static map: secret name → category. Keep this table in sync with
 * `core/api/routes/integration_secrets.KNOWN_SECRETS`. An entry that
 * doesn't appear here falls into "Other". */
const SECRET_CATEGORY: Record<string, ApiKeyCategory> = {
  hubspot_private_token: "Sales-ops",
  hunter_io_api_key: "Sales-ops",
  google_places_api_key: "Sales-ops",
  pagespeed_api_key: "Sales-ops",
  twelvedata_api_key: "Trading",
  browserbase_api_key: "Trading",
  browserbase_project_id: "Trading",
  nano_banana_api_key: "Creative",
  higgsfield_api_key: "Creative",
};

/** Deep-link each card to the provider's API-key page. If the user
 * isn't logged in, the provider's sign-in flow will bounce them back
 * to the same path — landing them as close to the key as possible
 * without us having to scrape each provider's UI. URLs chosen to be
 * the most-specific stable path per provider; a provider redesign
 * might demote one of these to a generic dashboard, which is fine
 * (still way better than "hunt through the settings menu yourself").
 *
 * Keys that aren't in this map render without a "Get key" link — the
 * description already carries enough context in that case. */
const SECRET_GET_KEY_URL: Record<string, string> = {
  hubspot_private_token:
    "https://app.hubspot.com/l/private-apps",
  hunter_io_api_key: "https://hunter.io/api-keys",
  google_places_api_key:
    "https://console.cloud.google.com/apis/credentials",
  pagespeed_api_key:
    "https://console.cloud.google.com/apis/credentials",
  twelvedata_api_key: "https://twelvedata.com/account/api-keys",
  browserbase_api_key: "https://www.browserbase.com/settings/api-keys",
  browserbase_project_id: "https://www.browserbase.com/projects",
  nano_banana_api_key: "https://aistudio.google.com/app/apikey",
  higgsfield_api_key: "https://cloud.higgsfield.ai/",
};

function categorize(name: string): ApiKeyCategory {
  if (name in SECRET_CATEGORY) return SECRET_CATEGORY[name];
  // Pattern-matched secrets (e.g. wordpress_<slug>_app_password) all
  // land under Design for now; any future patterns can extend this.
  if (/^wordpress_.+_app_password$/.test(name)) return "Design";
  return "Other";
}

function IntegrationSecretsSection() {
  const [entries, setEntries] = useState<IntegrationSecretEntry[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [query, setQuery] = useState("");

  const refresh = useCallback(() => {
    fetchIntegrationSecrets()
      .then((r) => setEntries(r.entries))
      .catch((e: Error) => setErr(e.message));
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const filtered = (entries ?? []).filter((e) => {
    if (!query.trim()) return true;
    const q = query.trim().toLowerCase();
    return (
      e.label.toLowerCase().includes(q) ||
      e.name.toLowerCase().includes(q) ||
      (e.description ?? "").toLowerCase().includes(q)
    );
  });

  const byCategory = new Map<ApiKeyCategory, IntegrationSecretEntry[]>();
  for (const e of filtered) {
    const cat = categorize(e.name);
    const bucket = byCategory.get(cat) ?? [];
    bucket.push(e);
    byCategory.set(cat, bucket);
  }

  const configuredCount = (entries ?? []).filter((e) => e.configured).length;
  const totalCount = (entries ?? []).length;

  return (
    <section className="settings-card">
      <div className="apikeys-header">
        <div>
          <div className="settings-card-title">API Keys</div>
          <p className="settings-card-body">
            Paste third-party keys here and they become live immediately —
            no Railway redeploy. Values are never echoed back after save.
          </p>
        </div>
        {entries !== null && (
          <div className="apikeys-summary">
            <span className="apikeys-summary-num">{configuredCount}</span>
            <span className="apikeys-summary-denom">/ {totalCount}</span>
            <span className="apikeys-summary-label">configured</span>
          </div>
        )}
      </div>

      <input
        type="search"
        className="apikeys-search"
        placeholder="Search API keys…"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
      />

      {err && <div className="settings-error">Could not load: {err}</div>}
      {entries === null && (
        <div className="settings-card-body">Loading…</div>
      )}

      {entries !== null &&
        CATEGORY_ORDER.filter((c) => (byCategory.get(c) ?? []).length > 0).map(
          (cat) => {
            const bucket = byCategory.get(cat) ?? [];
            return (
              <div key={cat} className="apikeys-category">
                <div className="apikeys-category-head">
                  <span className="apikeys-category-name">{cat}</span>
                  <span className="apikeys-category-count">
                    {bucket.filter((e) => e.configured).length} / {bucket.length}
                  </span>
                </div>
                <p className="apikeys-category-blurb">{CATEGORY_BLURB[cat]}</p>
                <div className="apikeys-grid">
                  {bucket.map((e) => (
                    <IntegrationSecretRow
                      key={e.name}
                      entry={e}
                      onChanged={refresh}
                    />
                  ))}
                </div>
              </div>
            );
          },
        )}

      {entries !== null && filtered.length === 0 && (
        <div className="apikeys-empty">
          No keys match &ldquo;{query}&rdquo;.
        </div>
      )}
    </section>
  );
}

function IntegrationSecretRow({
  entry,
  onChanged,
}: {
  entry: IntegrationSecretEntry;
  onChanged: () => void;
}) {
  const [editing, setEditing] = useState<boolean>(!entry.configured);
  const [value, setValue] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const save = async () => {
    const trimmed = value.trim();
    if (!trimmed) {
      setErr("Paste a key first.");
      return;
    }
    setBusy(true);
    setErr(null);
    try {
      await setIntegrationSecret(entry.name, trimmed);
      setValue("");
      setEditing(false);
      onChanged();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const clear = async () => {
    if (
      !confirm(
        `Clear the stored ${entry.label} key? The agent will fall back ` +
          `to the ${entry.env} env var (if set).`,
      )
    ) {
      return;
    }
    setBusy(true);
    setErr(null);
    try {
      await clearIntegrationSecret(entry.name);
      onChanged();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const getKeyUrl = SECRET_GET_KEY_URL[entry.name];

  return (
    <div className="apikeys-row">
      <div className="apikeys-row-head">
        <div className="apikeys-row-label">
          {entry.label}
          {entry.configured ? (
            <span className="apikeys-badge apikeys-badge--ok">Configured</span>
          ) : (
            <span className="apikeys-badge apikeys-badge--off">Not set</span>
          )}
        </div>
        <div className="apikeys-row-env">env: {entry.env}</div>
      </div>
      <div className="apikeys-row-desc">{entry.description}</div>
      {getKeyUrl && (
        <a
          className="apikeys-get-key"
          href={getKeyUrl}
          target="_blank"
          rel="noopener noreferrer"
          title={
            `Opens ${new URL(getKeyUrl).hostname} in a new tab. If ` +
            `you're not signed in, the provider will prompt you and ` +
            `redirect back to the API-key page.`
          }
        >
          Get key ↗
        </a>
      )}
      {editing ? (
        <div className="apikeys-row-form">
          <input
            type="password"
            className="apikeys-input"
            placeholder={`Paste your ${entry.label} key`}
            value={value}
            onChange={(e) => setValue(e.target.value)}
            autoComplete="off"
            spellCheck={false}
            disabled={busy}
          />
          <div className="apikeys-row-actions">
            <button
              type="button"
              className="apikeys-btn apikeys-btn--primary"
              onClick={() => void save()}
              disabled={busy}
            >
              {busy ? "Saving…" : "Save"}
            </button>
            {entry.configured && (
              <button
                type="button"
                className="apikeys-btn"
                onClick={() => {
                  setEditing(false);
                  setValue("");
                  setErr(null);
                }}
                disabled={busy}
              >
                Cancel
              </button>
            )}
          </div>
        </div>
      ) : (
        <div className="apikeys-row-actions">
          <button
            type="button"
            className="apikeys-btn"
            onClick={() => setEditing(true)}
            disabled={busy}
          >
            Replace key
          </button>
          <button
            type="button"
            className="apikeys-btn apikeys-btn--danger"
            onClick={() => void clear()}
            disabled={busy}
          >
            Clear
          </button>
          {entry.updated_at && (
            <span className="apikeys-row-updated">
              Updated {new Date(entry.updated_at).toLocaleString()}
            </span>
          )}
        </div>
      )}
      {err && <div className="settings-error">{err}</div>}
    </div>
  );
}

function clamp01(x: number): number {
  if (!Number.isFinite(x)) return 0;
  if (x < 0) return 0;
  if (x > 1) return 1;
  return x;
}

function AccountsSection({
  title,
  subtitle,
  emptyCopy,
  accounts,
  providers,
  grants,
  accountDefaults,
  refresh,
  setConnectError,
  setConnectDialog,
  setManageAccessFor,
}: {
  title: string;
  subtitle: string;
  emptyCopy: string;
  accounts: ConnectedAccount[];
  providers: ProviderInfo[];
  grants: Record<string, string[]>;
  accountDefaults: Record<string, string>;
  refresh: () => void;
  setConnectError: (v: string | null) => void;
  setConnectDialog: (
    v:
      | null
      | {
          provider: string;
          role: "system" | "user";
          groups: string[];
          accountEmail?: string;
          expandingAccountId?: string;
        },
  ) => void;
  setManageAccessFor: (v: string | null) => void;
}) {
  return (
    <div className="accounts-section">
      <div className="accounts-section-head">
        <div className="accounts-section-title">{title}</div>
        <div className="accounts-section-subtitle">{subtitle}</div>
      </div>
      {accounts.length === 0 ? (
        <div className="accounts-section-empty">{emptyCopy}</div>
      ) : (
        <div className="accounts-list">
          {accounts.map((a) => {
            const isDefault =
              accountDefaults[`${a.provider}:${a.role}`] === a.account_id;
            const provider = providers.find((p) => p.name === a.provider);
            return (
              <AccountRow
                key={a.account_id}
                account={a}
                isDefault={isDefault}
                grantedAgents={grants[a.account_id] ?? []}
                canExpand={(provider?.scope_groups.length ?? 0) > 1}
                onRemove={async () => {
                  await deleteConnectedAccount(a.account_id).catch(() => {});
                  refresh();
                }}
                onSetDefault={async () => {
                  await setDefaultConnectedAccount(a.account_id).catch(
                    () => {},
                  );
                  refresh();
                }}
                onManageAccess={() => setManageAccessFor(a.account_id)}
                onExpand={() => {
                  if (!provider) return;
                  setConnectError(null);
                  setConnectDialog({
                    provider: a.provider,
                    role: a.role,
                    groups: [...provider.default_scope_groups],
                    accountEmail: a.email ?? undefined,
                    expandingAccountId: a.account_id,
                  });
                }}
              />
            );
          })}
        </div>
      )}
    </div>
  );
}

function AccountRow({
  account,
  isDefault,
  grantedAgents,
  canExpand,
  onRemove,
  onSetDefault,
  onManageAccess,
  onExpand,
}: {
  account: ConnectedAccount;
  isDefault: boolean;
  grantedAgents: string[];
  canExpand: boolean;
  onRemove: () => Promise<void> | void;
  onSetDefault: () => Promise<void> | void;
  onManageAccess: () => void;
  onExpand: () => void;
}) {
  const roleWord = account.role === "system" ? "PILK" : "You";
  const statusLabel =
    account.status === "connected"
      ? "Connected"
      : account.status === "expired"
        ? "Needs re-auth"
        : account.status === "revoked"
          ? "Revoked"
          : "Pending";
  const statusTone = account.status === "connected" ? "ok" : "warn";
  return (
    <div className="connected-account">
      <div className="connected-account-head">
        <div className="connected-account-label">
          {account.label || `${account.provider} · ${roleWord}`}
        </div>
        <div className="connected-account-pills">
          <span className="connected-account-tag">{roleWord}</span>
          {isDefault && (
            <span className="connected-account-tag connected-account-tag--default">
              Default
            </span>
          )}
          <span
            className={`connected-account-pill connected-account-pill--${statusTone}`}
          >
            {statusLabel}
          </span>
        </div>
      </div>
      <div className="connected-account-email">
        {account.email ?? account.username ?? "(identity unknown)"}
      </div>
      <div className="connected-account-scopes">
        {account.scopes.length} scope
        {account.scopes.length === 1 ? "" : "s"} granted
        {account.linked_at && (
          <>
            {" · linked "}
            {new Date(account.linked_at).toLocaleString()}
          </>
        )}
      </div>
      <div className="connected-account-grants">
        <span className="connected-account-grants-label">Agents with access</span>
        {grantedAgents.length === 0 ? (
          <span className="connected-account-grants-empty">
            none — top-level chat only
          </span>
        ) : (
          <span className="connected-account-grants-list">
            {grantedAgents.map((name) => (
              <span key={name} className="connected-account-grant-chip">
                {humanizeAgentName(name)}
              </span>
            ))}
          </span>
        )}
        <button
          type="button"
          className="connected-account-grants-manage"
          onClick={onManageAccess}
        >
          Manage access
        </button>
      </div>
      <div className="connected-account-actions">
        {!isDefault && (
          <button type="button" className="btn" onClick={() => onSetDefault()}>
            Set as default
          </button>
        )}
        {canExpand && (
          <button type="button" className="btn" onClick={onExpand}>
            Expand access
          </button>
        )}
        <button
          type="button"
          className="btn btn--danger"
          onClick={() => {
            if (confirm(`Remove ${account.email ?? account.label}?`)) {
              void onRemove();
            }
          }}
        >
          Remove
        </button>
      </div>
    </div>
  );
}

function ManageAccessModal({
  accountId,
  account,
  agents,
  grantedAgents,
  onClose,
  onChanged,
}: {
  accountId: string;
  account: ConnectedAccount | null;
  agents: AgentRow[];
  grantedAgents: string[];
  onClose: () => void;
  onChanged: () => void;
}) {
  const granted = new Set(grantedAgents);
  return (
    <div className="manage-access-backdrop" onClick={onClose}>
      <div
        className="manage-access-modal"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="manage-access-head">
          <div>
            <div className="manage-access-eyebrow">Manage access</div>
            <div className="manage-access-title">
              {account?.label ?? accountId}
            </div>
            <div className="manage-access-sub">
              {account?.email ?? "(identity unknown)"}
            </div>
          </div>
          <button type="button" className="btn" onClick={onClose}>
            Done
          </button>
        </div>
        {agents.length === 0 ? (
          <div className="settings-note">
            No agents registered yet. Build one from Chat.
          </div>
        ) : (
          <ul className="manage-access-list">
            {agents.map((a) => {
              const has = granted.has(a.name);
              return (
                <li key={a.name} className="manage-access-row">
                  <label className="manage-access-label">
                    <input
                      type="checkbox"
                      checked={has}
                      onChange={async () => {
                        try {
                          if (has) {
                            await revokeAgentAccess(accountId, a.name);
                          } else {
                            await grantAgentAccess(accountId, a.name);
                          }
                          onChanged();
                        } catch {
                          // swallow — refresh anyway
                          onChanged();
                        }
                      }}
                    />
                    <span className="manage-access-agent-name">
                      {humanizeAgentName(a.name)}
                    </span>
                  </label>
                  <span className="manage-access-agent-state">{a.state}</span>
                </li>
              );
            })}
          </ul>
        )}
      </div>
    </div>
  );
}

/** Settings → XAU/USD Agent.
 *
 * Execution-mode toggle. "Approve" queues every trade for operator
 * confirmation via the ApprovalManager; "Autonomous" lets the agent
 * trade within its risk caps without per-trade approval. Safe default
 * is "Approve" — operator flips when they've watched enough live
 * decisions to trust the model on fast-moving bars.
 */
function XAUUSDSettingsSection() {
  const [settings, setSettings] = useState<XAUUSDSettings | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(() => {
    fetchXAUUSDSettings()
      .then(setSettings)
      .catch((e: Error) => setErr(e.message));
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const pick = async (mode: XAUUSDExecutionMode) => {
    if (!settings || settings.execution_mode === mode || busy) return;
    setBusy(true);
    setErr(null);
    try {
      await setXAUUSDExecutionMode(mode);
      refresh();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="settings-card">
      <div className="settings-card-title">XAU/USD Agent</div>
      <p className="settings-card-body">
        Execution mode controls how the gold agent places trades.
        <strong> Approve</strong> — every order is queued for you to
        confirm before the broker click. Safe but slow on fast bars.
        <strong> Autonomous</strong> — the agent trades within its risk
        caps without per-trade approval. Flip once you've watched enough
        live decisions to trust it.
      </p>
      {err && <div className="settings-error">Could not load: {err}</div>}
      {settings === null ? (
        <div className="settings-card-body">Loading…</div>
      ) : (
        <div className="xauusd-mode-toggle">
          {settings.allowed_modes.map((m) => {
            const active = settings.execution_mode === m;
            return (
              <button
                type="button"
                key={m}
                className={`xauusd-mode-btn${active ? " xauusd-mode-btn--active" : ""}`}
                onClick={() => pick(m)}
                disabled={busy || active}
              >
                <span className="xauusd-mode-btn-label">
                  {m === "approve" ? "Approve each trade" : "Autonomous"}
                </span>
                <span className="xauusd-mode-btn-sub">
                  {m === "approve"
                    ? "Confirm before every order"
                    : "Trade within risk caps, no prompt"}
                </span>
              </button>
            );
          })}
        </div>
      )}
      {settings && settings.updated_at && (
        <div className="settings-card-hint">
          Last changed {settings.updated_at}
        </div>
      )}
      {settings && settings.is_default && (
        <div className="settings-card-hint">
          Using default ({settings.execution_mode}).
        </div>
      )}
    </section>
  );
}

/** One tuning knob in the Voice panel. Shows the effective current
 * value, a range slider, and a "Reset to preset" button when an
 * override is active. Debounced only by React's own rendering —
 * `ambient.setConfig` writes are cheap (localStorage + Set<Listener>
 * walk). */
function TuningSlider({
  label,
  unit,
  value,
  override,
  bounds,
  onChange,
  help,
  format,
}: {
  label: string;
  unit: string;
  value: number;
  override: number | null;
  bounds: { min: number; max: number; step: number };
  onChange: (v: number | null) => void;
  help: string;
  format?: (v: number) => string;
}) {
  const display = format ? format(value) : String(Math.round(value));
  return (
    <div className="voice-tuning-row">
      <div className="voice-tuning-row-head">
        <label className="voice-tuning-label">{label}</label>
        <div className="voice-tuning-value">
          {display}
          {unit && <span className="voice-tuning-unit"> {unit}</span>}
          {override !== null && (
            <button
              type="button"
              className="voice-tuning-reset"
              onClick={() => onChange(null)}
              title="Reset to the patience preset default"
            >
              reset
            </button>
          )}
        </div>
      </div>
      <input
        type="range"
        className="voice-tuning-slider"
        min={bounds.min}
        max={bounds.max}
        step={bounds.step}
        value={value}
        onChange={(e) => onChange(Number(e.target.value))}
      />
      <div className="voice-tuning-help">{help}</div>
    </div>
  );
}

/** Read-only inventory of what Claude Code will load on next
 * invocation. Scans ~/.claude/skills and ~/.claude/plugins on the
 * pilkd host (which is your Mac in local mode). Not configurable
 * from here — these are managed via `git clone` on disk. */
function InstalledSkillsPanel() {
  const [inv, setInv] = useState<{
    skills: InstalledPack[];
    plugins: InstalledPack[];
  } | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(() => {
    setBusy(true);
    fetchInstalledSkills()
      .then((r) => {
        setInv(r);
        setErr(null);
      })
      .catch((e: Error) => setErr(e.message))
      .finally(() => setBusy(false));
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  return (
    <div className="skills-panel">
      <div className="skills-panel-head">
        <div className="skills-panel-title">Skills &amp; plugins</div>
        <button
          type="button"
          className="skills-panel-refresh"
          onClick={load}
          disabled={busy}
        >
          {busy ? "Refreshing…" : "Refresh"}
        </button>
      </div>
      <p className="settings-card-body">
        Everything Claude Code will load the next time PILK invokes it.
        Managed on disk — clone a repo into{" "}
        <code>~/.claude/skills/</code> or <code>~/.claude/plugins/</code>
        {" "}to add a new one, then click Refresh.
      </p>
      {err && <div className="settings-error">Could not load: {err}</div>}
      {inv === null && !err && (
        <div className="settings-card-body">Loading…</div>
      )}
      {inv && (
        <>
          <SkillList
            label="Skills"
            items={inv.skills}
            emptyCopy={
              <>
                No skills installed. Try{" "}
                <code>git clone https://github.com/anthropics/skills.git ~/.claude/skills/anthropic</code>
                .
              </>
            }
          />
          <SkillList
            label="Plugins"
            items={inv.plugins}
            emptyCopy={
              <>
                No plugins installed. Try{" "}
                <code>git clone https://github.com/thedotmack/claude-mem.git ~/.claude/plugins/claude-mem</code>
                .
              </>
            }
          />
        </>
      )}
    </div>
  );
}

function SkillList({
  label,
  items,
  emptyCopy,
}: {
  label: string;
  items: InstalledPack[];
  emptyCopy: React.ReactNode;
}) {
  return (
    <div className="skills-panel-section">
      <div className="skills-panel-section-head">
        <span className="skills-panel-section-label">{label}</span>
        <span className="skills-panel-section-count">{items.length}</span>
      </div>
      {items.length === 0 ? (
        <div className="skills-panel-empty">{emptyCopy}</div>
      ) : (
        <ul className="skills-panel-list">
          {items.map((p) => (
            <li key={p.path} className="skills-panel-item">
              <div className="skills-panel-item-name">{p.name}</div>
              {p.description && (
                <div className="skills-panel-item-desc">{p.description}</div>
              )}
              <div className="skills-panel-item-path" title={p.path}>
                {p.path}
              </div>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

/** Settings → Telegram.
 *
 * Step-through connect flow so the operator doesn't have to hunt
 * through Telegram's docs. Each step is verified against the bot
 * API before the next one unlocks; the final "send test" is a real
 * end-to-end proof that PILK can reach the operator's phone.
 *
 * This card deliberately doesn't store anything in local state that
 * isn't reflected in settings — the auth pipeline goes through
 * setIntegrationSecret like every other key, so the "API Keys"
 * view stays the source of truth. */
function TelegramConnectSection(): JSX.Element {
  // Token input state — mirrors the other secret-entry UX so muscle
  // memory carries across Settings sections.
  const [tokenDraft, setTokenDraft] = useState("");
  const [tokenSaving, setTokenSaving] = useState(false);
  const [tokenError, setTokenError] = useState<string | null>(null);

  // chat_id input state + auto-detect state.
  const [chatDraft, setChatDraft] = useState("");
  const [chatSaving, setChatSaving] = useState(false);
  const [chatError, setChatError] = useState<string | null>(null);
  const [chatSavedId, setChatSavedId] = useState<string | null>(null);
  const [detectBusy, setDetectBusy] = useState(false);
  const [detectHint, setDetectHint] = useState<string | null>(null);

  // Token save success (bot verification is authoritative — once
  // saveToken returns, we flip a transient flag so the step renders
  // a visible tick even before the bot-info refresh completes).
  const [tokenSaved, setTokenSaved] = useState(false);

  // Bot info from the backend (drives verify + open-bot link).
  const [botInfo, setBotInfo] = useState<{
    configured: boolean;
    valid?: boolean;
    username?: string;
    first_name?: string;
    t_me_url?: string | null;
    error?: string;
  } | null>(null);
  const [botBusy, setBotBusy] = useState(false);

  // Test-send state.
  const [testBusy, setTestBusy] = useState(false);
  const [testResult, setTestResult] = useState<
    { ok: true; message_id?: number }
    | { ok: false; error: string }
    | null
  >(null);

  const refreshBotInfo = useCallback(async () => {
    setBotBusy(true);
    try {
      const info = await fetchTelegramBotInfo();
      setBotInfo(info);
    } catch (e) {
      setBotInfo({
        configured: false,
        error: e instanceof Error ? e.message : String(e),
      });
    } finally {
      setBotBusy(false);
    }
  }, []);

  useEffect(() => {
    void refreshBotInfo();
  }, [refreshBotInfo]);

  const saveToken = async () => {
    const trimmed = tokenDraft.trim();
    if (!trimmed) {
      setTokenError("Paste the token from @BotFather first.");
      return;
    }
    setTokenSaving(true);
    setTokenError(null);
    setTokenSaved(false);
    try {
      await setIntegrationSecret("telegram_bot_token", trimmed);
      // Clear the pasted token so it doesn't sit on-screen as
      // plaintext, but raise a transient "saved" flag so the
      // operator gets an obvious visual confirmation even if the
      // follow-up getMe call takes a second to return.
      setTokenDraft("");
      setTokenSaved(true);
      await refreshBotInfo();
    } catch (e) {
      setTokenError(e instanceof Error ? e.message : String(e));
    } finally {
      setTokenSaving(false);
    }
  };

  const saveChatId = async () => {
    const trimmed = chatDraft.trim();
    if (!trimmed) {
      setChatError("Paste or detect a chat_id first.");
      return;
    }
    setChatSaving(true);
    setChatError(null);
    try {
      await setIntegrationSecret("telegram_chat_id", trimmed);
      // Keep the value on-screen and track what we last saved — the
      // previous UX cleared the field and the operator had no idea
      // whether the save landed. Now they see "✓ Saved: 12345".
      setChatSavedId(trimmed);
      setDetectHint(null);
    } catch (e) {
      setChatError(e instanceof Error ? e.message : String(e));
    } finally {
      setChatSaving(false);
    }
  };

  const detect = async () => {
    setDetectBusy(true);
    setChatError(null);
    setDetectHint(null);
    try {
      const r = await detectTelegramChat();
      if (r.detected && r.chat_id) {
        setChatDraft(r.chat_id);
        setDetectHint(
          `Found: ${r.chat_title ?? r.chat_id} (${r.chat_type ?? "chat"}). ` +
            "Click Save to keep it.",
        );
      } else {
        setChatError(r.error ?? "No chat detected yet.");
      }
    } catch (e) {
      setChatError(e instanceof Error ? e.message : String(e));
    } finally {
      setDetectBusy(false);
    }
  };

  const sendTest = async () => {
    setTestBusy(true);
    setTestResult(null);
    try {
      const r = await sendTelegramTest();
      if (r.sent) {
        setTestResult({ ok: true, message_id: r.message_id });
      } else {
        setTestResult({ ok: false, error: r.error ?? "Unknown error" });
      }
    } catch (e) {
      setTestResult({
        ok: false,
        error: e instanceof Error ? e.message : String(e),
      });
    } finally {
      setTestBusy(false);
    }
  };

  const tokenValid = !!botInfo?.valid;
  const botLink = botInfo?.t_me_url ?? null;

  return (
    <section className="telegram-connect">
      <div className="settings-card-title">Connect Telegram</div>
      <p className="settings-card-body">
        Telegram uses a bot token from @BotFather — there's no
        one-click OAuth for bots. This card walks you through each
        step with a verify button so you know it works before moving
        on. Takes under a minute.
      </p>

      <ol className="telegram-steps">
        <li
          className={`telegram-step${
            tokenValid ? " telegram-step--done" : ""
          }`}
        >
          <div className="telegram-step-head">
            <span className="telegram-step-num">1</span>
            <span className="telegram-step-title">Create a bot</span>
            {tokenValid && (
              <span className="telegram-step-check">✓</span>
            )}
          </div>
          <div className="telegram-step-body">
            Open BotFather in Telegram and send it <code>/newbot</code>.
            Follow the prompts and copy the token it hands back.
          </div>
          <div className="telegram-step-actions">
            <a
              className="btn btn--ghost"
              href="https://t.me/BotFather"
              target="_blank"
              rel="noopener noreferrer"
            >
              Open @BotFather
            </a>
          </div>
        </li>

        <li
          className={`telegram-step${
            tokenValid ? " telegram-step--done" : ""
          }`}
        >
          <div className="telegram-step-head">
            <span className="telegram-step-num">2</span>
            <span className="telegram-step-title">Paste + verify the token</span>
            {tokenValid && (
              <span className="telegram-step-check">✓</span>
            )}
          </div>
          {botInfo?.configured && botInfo?.valid && (
            <div className="telegram-step-ok">
              Connected to{" "}
              <strong>@{botInfo.username}</strong>
              {botInfo.first_name ? ` — "${botInfo.first_name}"` : ""}
              . To rotate, paste a new token below and save.
            </div>
          )}
          {botInfo?.configured && botInfo?.valid === false && (
            <div className="telegram-step-err">
              Token saved but Telegram rejected it: {botInfo.error}
            </div>
          )}
          <div className="telegram-step-row">
            <input
              type="password"
              className="telegram-input"
              placeholder="Paste bot token (e.g. 123456:ABC-...)"
              value={tokenDraft}
              onChange={(e) => setTokenDraft(e.target.value)}
              disabled={tokenSaving}
              autoComplete="off"
              spellCheck={false}
            />
            <button
              type="button"
              className="btn btn--primary"
              onClick={() => void saveToken()}
              disabled={tokenSaving || !tokenDraft.trim()}
            >
              {tokenSaving ? "Saving…" : "Save + verify"}
            </button>
            <button
              type="button"
              className="btn btn--ghost"
              onClick={() => void refreshBotInfo()}
              disabled={botBusy}
            >
              {botBusy ? "…" : "Re-verify"}
            </button>
          </div>
          {tokenError && (
            <div className="telegram-step-err">{tokenError}</div>
          )}
          {tokenSaved && !tokenError && !tokenValid && (
            <div className="telegram-step-hint">
              Saved. Verifying with Telegram…
            </div>
          )}
        </li>

        <li
          className={`telegram-step${
            tokenValid ? "" : " telegram-step--locked"
          }`}
        >
          <div className="telegram-step-head">
            <span className="telegram-step-num">3</span>
            <span className="telegram-step-title">Message your bot once</span>
          </div>
          <div className="telegram-step-body">
            Open your bot in Telegram and send it any message
            (e.g. <code>/start</code>). Telegram won't show the bot
            in your chat list until you've talked to it at least
            once — that first message is also what makes the chat
            ID visible to us.
          </div>
          <div className="telegram-step-actions">
            {botLink ? (
              <a
                className="btn btn--primary"
                href={botLink}
                target="_blank"
                rel="noopener noreferrer"
              >
                Open @{botInfo?.username}
              </a>
            ) : (
              <span className="telegram-step-locked-note">
                Verify the bot token above first.
              </span>
            )}
          </div>
        </li>

        <li
          className={`telegram-step${
            tokenValid ? "" : " telegram-step--locked"
          }`}
        >
          <div className="telegram-step-head">
            <span className="telegram-step-num">4</span>
            <span className="telegram-step-title">Detect + save chat_id</span>
          </div>
          <div className="telegram-step-body">
            After you've messaged the bot, hit <strong>Detect</strong>.
            We'll ask Telegram for the latest message and auto-fill
            your chat ID — no poking around in raw JSON.
          </div>
          <div className="telegram-step-row">
            <input
              type="text"
              className="telegram-input"
              placeholder="chat_id (numeric)"
              value={chatDraft}
              onChange={(e) => setChatDraft(e.target.value)}
              disabled={chatSaving || !tokenValid}
              inputMode="numeric"
            />
            <button
              type="button"
              className="btn btn--ghost"
              onClick={() => void detect()}
              disabled={!tokenValid || detectBusy}
              title="Call Telegram getUpdates and auto-fill chat_id"
            >
              {detectBusy ? "Detecting…" : "Detect"}
            </button>
            <button
              type="button"
              className="btn btn--primary"
              onClick={() => void saveChatId()}
              disabled={!tokenValid || chatSaving || !chatDraft.trim()}
            >
              {chatSaving ? "Saving…" : "Save"}
            </button>
          </div>
          {detectHint && (
            <div className="telegram-step-hint">{detectHint}</div>
          )}
          {chatSavedId && !chatError && (
            <div className="telegram-step-ok">
              ✓ Saved chat_id <code>{chatSavedId}</code>. Send yourself
              a test below to confirm the round-trip.
            </div>
          )}
          {chatError && (
            <div className="telegram-step-err">{chatError}</div>
          )}
        </li>

        <li
          className={`telegram-step${
            tokenValid ? "" : " telegram-step--locked"
          }`}
        >
          <div className="telegram-step-head">
            <span className="telegram-step-num">5</span>
            <span className="telegram-step-title">Send yourself a test</span>
          </div>
          <div className="telegram-step-body">
            Hits <code>sendMessage</code> with the saved credentials.
            If it lands on your phone, you're done.
          </div>
          <div className="telegram-step-actions">
            <button
              type="button"
              className="btn btn--primary"
              onClick={() => void sendTest()}
              disabled={!tokenValid || testBusy}
            >
              {testBusy ? "Sending…" : "Send test message"}
            </button>
          </div>
          {testResult?.ok && (
            <div className="telegram-step-ok">
              ✓ Delivered — message_id {testResult.message_id}.
              Setup is done.
            </div>
          )}
          {testResult && !testResult.ok && (
            <div className="telegram-step-err">
              Send failed: {testResult.error}
            </div>
          )}
        </li>
      </ol>
    </section>
  );
}
