import { useCallback, useEffect, useState } from "react";
import {
  ambient,
  type AckKind,
  type AmbientConfig,
  type Patience,
  type WakePhrase,
} from "../voice/ambient";
import {
  deleteConnectedAccount,
  fetchAgents,
  fetchCodingEngines,
  fetchConnectedAccounts,
  fetchGovernorStatus,
  fetchGrants,
  fetchProviders,
  grantAgentAccess,
  pilk,
  revokeAgentAccess,
  setDefaultConnectedAccount,
  setGovernorConfig,
  setGovernorOverride,
  startOAuthConnection,
  type AgentRow,
  type CodingEngineHealth,
  type ConnectedAccount,
  type GovernorStatus,
  type OverrideMode,
  type ProviderInfo,
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

export default function Settings() {
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
  const [connectOpen, setConnectOpen] = useState<
    null | { provider: string; role: "system" | "user" }
  >(null);
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
          OAuth-connected services PILK can use. <strong>PILK</strong>{" "}
          accounts are PILK acting as itself (signups, verifications,
          reports). <strong>You</strong> accounts are PILK acting on your
          behalf (triage, replies). Every outgoing message from a{" "}
          <em>You</em> account requires fresh approval.
        </p>

        {accounts === null ? (
          <div className="settings-note">Reading connected accounts…</div>
        ) : accounts.length === 0 ? (
          <div className="settings-note">
            No accounts connected yet. Click Connect below to link a Gmail
            account.
          </div>
        ) : (
          <div className="accounts-list">
            {accounts.map((a) => {
              const isDefault =
                accountDefaults[`${a.provider}:${a.role}`] === a.account_id;
              return (
                <AccountRow
                  key={a.account_id}
                  account={a}
                  isDefault={isDefault}
                  grantedAgents={grants[a.account_id] ?? []}
                  onRemove={async () => {
                    await deleteConnectedAccount(a.account_id).catch(() => {});
                    refreshAccounts();
                  }}
                  onSetDefault={async () => {
                    await setDefaultConnectedAccount(a.account_id).catch(
                      () => {},
                    );
                    refreshAccounts();
                  }}
                  onManageAccess={() => setManageAccessFor(a.account_id)}
                />
              );
            })}
          </div>
        )}

        <div className="accounts-connect">
          <div className="accounts-connect-label">Connect a new account</div>
          <div className="accounts-provider-grid">
            {providers.flatMap((p) =>
              p.supports_roles.map((r) => (
                <button
                  key={`${p.name}:${r}`}
                  type="button"
                  className="accounts-provider-chip"
                  onClick={() => {
                    setConnectError(null);
                    setConnectOpen({ provider: p.name, role: r });
                  }}
                >
                  <span className="accounts-provider-chip-label">{p.label}</span>
                  <span className="accounts-provider-chip-role">
                    {r === "system" ? "PILK" : "You"}
                  </span>
                </button>
              )),
            )}
          </div>
        </div>

        {connectOpen && (
          <div className="accounts-confirm">
            <div className="accounts-confirm-text">
              About to open Google sign-in for the{" "}
              <strong>{connectOpen.role === "system" ? "PILK" : "You"}</strong>{" "}
              role.
            </div>
            <div className="accounts-confirm-actions">
              <button
                type="button"
                className="btn"
                onClick={() => setConnectOpen(null)}
                disabled={connectBusy}
              >
                Cancel
              </button>
              <button
                type="button"
                className="btn btn--primary"
                disabled={connectBusy}
                onClick={async () => {
                  if (!connectOpen) return;
                  setConnectBusy(true);
                  setConnectError(null);
                  try {
                    const r = await startOAuthConnection({
                      provider: connectOpen.provider,
                      role: connectOpen.role,
                    });
                    window.open(r.auth_url, "_blank", "noopener,noreferrer");
                    setConnectOpen(null);
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
        )}
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
      </section>

      <section className="settings-card settings-card--muted">
        <div className="settings-card-title">Session vault &amp; trust rules</div>
        <p className="settings-card-body">
          Encrypted credential vault and fine-grained trust rule editor arrive
          in a follow-up batch.
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

function AccountRow({
  account,
  isDefault,
  grantedAgents,
  onRemove,
  onSetDefault,
  onManageAccess,
}: {
  account: ConnectedAccount;
  isDefault: boolean;
  grantedAgents: string[];
  onRemove: () => Promise<void> | void;
  onSetDefault: () => Promise<void> | void;
  onManageAccess: () => void;
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
