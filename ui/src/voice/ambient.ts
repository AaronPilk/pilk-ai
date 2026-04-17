// Ambient wake-word listener for PILK.
//
// Owns one persistent Web Speech API SpeechRecognition session. Scans interim
// transcripts for a wake phrase ("Hey PILK" by default); on match, fires a
// short acknowledgement (browser SpeechSynthesis by default, or an
// ElevenLabs-quality line via /voice/speak), captures the follow-up sentence,
// sends it as a `chat.user` message, then mutes the recognizer while PILK's
// TTS reply plays.
//
// Everything after activation reuses the existing pilkd chat + voice paths —
// this module is purely the frontend's "always-listening" brain.

import { pilk, voiceSpeak } from "../state/api";

export type AmbientState =
  | "off" // disabled / not supported
  | "passive" // listening for wake phrase
  | "wake" // just heard the wake phrase; acknowledging
  | "active" // capturing the user's request
  | "thinking" // waiting for PILK's reply
  | "speaking" // playing PILK's reply
  | "followup" // post-reply conversation window (no wake needed)
  | "error";

export interface AmbientConfig {
  enabled: boolean;
  wakePhrase: WakePhrase;
  ack: AckKind;
  useElevenLabsAck: boolean;
  patience: Patience;
}

export type WakePhrase = "hey pilk" | "pilk";
export type AckKind = "yes" | "im-here" | "mm" | "tone" | "none";
export type Patience = "snappy" | "normal" | "patient" | "very-patient";

const STORAGE_KEY = "pilk.ambient.v1";

const DEFAULT_CONFIG: AmbientConfig = {
  enabled: false,
  wakePhrase: "hey pilk",
  ack: "yes",
  useElevenLabsAck: false,
  patience: "patient",
};

// How long to wait before auto-finalizing the utterance.
//   wakeGraceMs          — after the wake phrase if user hasn't started yet
//   silenceMs            — after any speech activity (interim OR final) stops
//   followupWindowMs     — after PILK finishes speaking, stay open for a
//                          follow-up question without requiring the wake phrase
interface PatiencePreset {
  wakeGraceMs: number;
  silenceMs: number;
  followupWindowMs: number;
}
const PATIENCE: Record<Patience, PatiencePreset> = {
  snappy: { wakeGraceMs: 3500, silenceMs: 1400, followupWindowMs: 15_000 },
  normal: { wakeGraceMs: 5000, silenceMs: 2200, followupWindowMs: 20_000 },
  patient: { wakeGraceMs: 7000, silenceMs: 3200, followupWindowMs: 28_000 },
  "very-patient": {
    wakeGraceMs: 10_000,
    silenceMs: 5000,
    followupWindowMs: 45_000,
  },
};

const ACK_TEXT: Record<Exclude<AckKind, "tone" | "none">, string> = {
  yes: "Yes?",
  "im-here": "I'm here.",
  mm: "Mm?",
};

// Fuzzy variants the recognizer commonly produces for each wake phrase.
const WAKE_VARIANTS: Record<WakePhrase, string[]> = {
  "hey pilk": [
    "hey pilk",
    "hey polk",
    "hey peak",
    "hey peek",
    "hey pink",
    "hey pil",
    "hi pilk",
    "hi polk",
    "a pilk",
    "hey milk",
  ],
  pilk: ["pilk", "polk", "peak", "peek", "pink", "pil"],
};

type StateListener = (s: AmbientState, detail?: string) => void;
type ConfigListener = (c: AmbientConfig) => void;

class AmbientController {
  private state: AmbientState = "off";
  private config: AmbientConfig = loadConfig();
  private recognition: SpeechRecognitionLike | null = null;
  private listeners = new Set<StateListener>();
  private configListeners = new Set<ConfigListener>();
  private lastCaption: string | null = null;

  private activeBuffer = "";
  private activeTimer: number | null = null;
  private audio: HTMLAudioElement | null = null;
  private restartTimer: number | null = null;
  private followupTimer: number | null = null;
  private suppressUntil = 0; // ignore recognizer output until this timestamp

  supported =
    typeof window !== "undefined" &&
    (typeof (window as any).SpeechRecognition === "function" ||
      typeof (window as any).webkitSpeechRecognition === "function");

  constructor() {
    if (typeof window === "undefined") return;
    // Auto-start if the user previously enabled ambient mode.
    if (this.config.enabled && this.supported) {
      queueMicrotask(() => this.start());
    }
  }

  getState(): AmbientState {
    return this.state;
  }

  getConfig(): AmbientConfig {
    return { ...this.config };
  }

  getCaption(): string | null {
    return this.lastCaption;
  }

  subscribe(fn: StateListener): () => void {
    this.listeners.add(fn);
    fn(this.state, this.lastCaption ?? undefined);
    return () => {
      this.listeners.delete(fn);
    };
  }

  subscribeConfig(fn: ConfigListener): () => void {
    this.configListeners.add(fn);
    fn(this.getConfig());
    return () => {
      this.configListeners.delete(fn);
    };
  }

  setConfig(partial: Partial<AmbientConfig>): void {
    const wasEnabled = this.config.enabled;
    this.config = { ...this.config, ...partial };
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(this.config));
    } catch {
      /* storage unavailable */
    }
    this.configListeners.forEach((fn) => fn(this.getConfig()));
    if (this.config.enabled && !wasEnabled) this.start();
    else if (!this.config.enabled && wasEnabled) this.stop();
  }

  start(): void {
    if (!this.supported || !this.config.enabled) return;
    if (this.recognition) return; // already running
    const SR =
      (window as any).SpeechRecognition ??
      (window as any).webkitSpeechRecognition;
    if (!SR) {
      this.setState("error", "Wake-word detection not supported in this browser");
      return;
    }
    const recog: SpeechRecognitionLike = new SR();
    recog.continuous = true;
    recog.interimResults = true;
    recog.lang = "en-US";

    recog.onresult = (ev) => {
      const r = ev.results[ev.results.length - 1];
      if (!r) return;
      const transcript = String(r[0].transcript).toLowerCase().trim();
      this.handleTranscript(transcript, r.isFinal);
    };
    recog.onerror = (ev) => {
      const err = String((ev as any).error ?? "");
      if (err === "not-allowed" || err === "service-not-allowed") {
        this.setState("error", "Microphone permission denied");
        this.config.enabled = false;
        try {
          localStorage.setItem(STORAGE_KEY, JSON.stringify(this.config));
        } catch {
          /* ignore */
        }
      } else if (err === "no-speech" || err === "aborted" || err === "audio-capture") {
        // benign; onend will restart
      } else {
        // transient — keep going
      }
    };
    recog.onend = () => {
      // Auto-restart unless we've gone fully off or are actively playing audio
      // (we manually stop before playback).
      if (
        this.config.enabled &&
        this.state !== "off" &&
        this.state !== "speaking"
      ) {
        this.scheduleRestart();
      }
    };

    try {
      recog.start();
      this.recognition = recog;
      this.setState("passive");
    } catch {
      this.recognition = null;
      this.setState("error", "Couldn't start the ambient listener");
    }
  }

  stop(): void {
    if (this.activeTimer) {
      clearTimeout(this.activeTimer);
      this.activeTimer = null;
    }
    if (this.restartTimer) {
      clearTimeout(this.restartTimer);
      this.restartTimer = null;
    }
    this.clearFollowupTimer();
    if (this.recognition) {
      try {
        this.recognition.onend = null;
        this.recognition.stop();
      } catch {
        /* ignore */
      }
      this.recognition = null;
    }
    if (this.audio) {
      try {
        this.audio.pause();
      } catch {
        /* ignore */
      }
      this.audio = null;
    }
    this.activeBuffer = "";
    this.setState("off");
  }

  /** Called by the orb button — forces an immediate wake without a phrase. */
  async forceWake(): Promise<void> {
    if (!this.config.enabled) {
      // Ambient is off; nothing to do here — tap-to-talk flow handles this case.
      return;
    }
    if (this.state === "speaking" || this.state === "thinking") return;
    this.enterWake("");
  }

  // ── internals ────────────────────────────────────────────────────────

  private scheduleRestart(): void {
    if (this.restartTimer) return;
    this.restartTimer = window.setTimeout(() => {
      this.restartTimer = null;
      if (!this.config.enabled) return;
      if (this.recognition) return;
      // Cold-restart the recognizer.
      this.start();
    }, 250);
  }

  private setState(s: AmbientState, detail?: string): void {
    if (s === this.state && !detail) return;
    this.state = s;
    if (detail !== undefined) this.lastCaption = detail;
    this.listeners.forEach((fn) => fn(this.state, this.lastCaption ?? undefined));
  }

  private handleTranscript(text: string, isFinal: boolean): void {
    if (!text) return;
    if (this.state === "speaking" || this.state === "thinking") return;
    if (Date.now() < this.suppressUntil) return;
    void isFinal; // reserved for future end-of-speech heuristics

    if (this.state === "passive") {
      const idx = this.findWake(text);
      if (idx >= 0) {
        const tail = text.slice(idx).replace(wakeRegex(this.config.wakePhrase), "").trim();
        this.enterWake(tail);
      }
      return;
    }

    if (this.state === "followup") {
      // Post-reply window: any speech counts as a follow-up without the
      // wake phrase. Cancel the "return to passive" timer and drop
      // straight into active capture.
      this.clearFollowupTimer();
      this.activeBuffer = text;
      this.setState("active", this.activeBuffer || "Listening…");
      if (this.activeTimer) clearTimeout(this.activeTimer);
      const { silenceMs } = PATIENCE[this.config.patience];
      this.activeTimer = window.setTimeout(() => this.finalizeActive(), silenceMs);
      return;
    }

    if (this.state === "active" || this.state === "wake") {
      this.activeBuffer = stripWake(text, this.config.wakePhrase);
      this.setState("active", this.activeBuffer || "Listening…");
      if (this.activeTimer) clearTimeout(this.activeTimer);
      const { silenceMs } = PATIENCE[this.config.patience];
      this.activeTimer = window.setTimeout(() => this.finalizeActive(), silenceMs);
    }
  }

  private clearFollowupTimer(): void {
    if (this.followupTimer) {
      clearTimeout(this.followupTimer);
      this.followupTimer = null;
    }
  }

  private findWake(text: string): number {
    for (const v of WAKE_VARIANTS[this.config.wakePhrase]) {
      const i = text.indexOf(v);
      if (i >= 0) return i;
    }
    return -1;
  }

  private enterWake(tail: string): void {
    this.setState("wake", "Wake phrase detected");
    this.activeBuffer = tail;
    this.playAcknowledgement();
    this.setState("active", this.activeBuffer || "Listening…");
    if (this.activeTimer) clearTimeout(this.activeTimer);
    // Initial grace after wake: how long to wait for the user to start
    // speaking (or continue after a trailing sentence). Every new
    // transcript resets this via handleTranscript.
    const preset = PATIENCE[this.config.patience];
    const grace = tail ? preset.silenceMs : preset.wakeGraceMs;
    this.activeTimer = window.setTimeout(() => this.finalizeActive(), grace);
  }

  private async playAcknowledgement(): Promise<void> {
    const kind = this.config.ack;
    if (kind === "none") return;
    if (kind === "tone") {
      playTone();
      return;
    }
    const text = ACK_TEXT[kind];
    try {
      if (this.config.useElevenLabsAck) {
        const played = await this.playServerTTS(text, { suppressMs: 1200 });
        if (!played) {
          speakLocally(text);
          this.suppressUntil = Date.now() + 900;
        }
      } else {
        speakLocally(text);
        this.suppressUntil = Date.now() + 900;
      }
    } catch {
      speakLocally(text);
      this.suppressUntil = Date.now() + 900;
    }
  }

  private async finalizeActive(): Promise<void> {
    const text = this.activeBuffer.trim();
    this.activeBuffer = "";
    if (this.activeTimer) {
      clearTimeout(this.activeTimer);
      this.activeTimer = null;
    }
    if (!text) {
      this.setState("passive", "Didn't catch that");
      return;
    }
    this.dispatchUtterance(text);
  }

  private dispatchUtterance(text: string): void {
    this.setState("thinking", `"${text}"`);
    // Mute the recognizer while the reply is in flight + playing.
    this.shutRecognizer();

    const off = pilk.onMessage(async (m: any) => {
      if (m.type === "chat.assistant" && typeof m.text === "string") {
        off();
        await this.speakReply(m.text);
      } else if (m.type === "system.error") {
        off();
        this.setState("error", m.text ?? "Something went wrong");
        this.resumePassiveSoon();
      }
    });

    pilk.send({ type: "chat.user", id: randId(), text });

    // Safety timeout: if no reply comes in 60s, give up and resume.
    window.setTimeout(() => {
      if (this.state === "thinking") {
        off();
        this.resumePassiveSoon();
      }
    }, 60_000);
  }

  private async speakReply(text: string): Promise<void> {
    this.setState("speaking", text.slice(0, 160));
    try {
      const played = await this.playServerTTS(text, { suppressMs: 1500 });
      if (!played) {
        // Server TTS came back but the browser refused to play it
        // (autoplay policy, device issue). Fall back to the browser's
        // built-in voice so the user still hears *something*.
        console.warn("[ambient] server TTS couldn't play, falling back to local voice");
        speakLocally(text);
        await delay(estimateSpeechMs(text));
      }
    } catch (e) {
      console.warn("[ambient] server TTS failed, falling back to local voice:", e);
      speakLocally(text);
      await delay(estimateSpeechMs(text));
    } finally {
      this.resumePassiveSoon();
    }
  }

  /**
   * Called after a reply is finished or an error resolves. We don't drop all
   * the way to passive immediately — instead we hold the mic open for a
   * follow-up window so the user can keep the conversation going without
   * re-saying the wake phrase. The window resets every time they speak.
   */
  private resumePassiveSoon(): void {
    this.suppressUntil = Date.now() + 800;
    window.setTimeout(() => {
      if (!this.config.enabled) return;
      this.enterFollowup();
    }, 400);
  }

  private enterFollowup(): void {
    this.activeBuffer = "";
    if (this.activeTimer) {
      clearTimeout(this.activeTimer);
      this.activeTimer = null;
    }
    this.clearFollowupTimer();
    // Need the recognizer live — start() is a no-op if it's already running.
    this.start();
    this.setState("followup", "Still listening — keep going");
    const { followupWindowMs } = PATIENCE[this.config.patience];
    this.followupTimer = window.setTimeout(() => {
      this.followupTimer = null;
      // Only fall back to passive if nobody jumped in.
      if (this.state === "followup") this.setState("passive");
    }, followupWindowMs);
  }

  private shutRecognizer(): void {
    if (this.recognition) {
      try {
        this.recognition.onend = null;
        this.recognition.stop();
      } catch {
        /* ignore */
      }
      this.recognition = null;
    }
  }

  private async playServerTTS(
    text: string,
    opts: { suppressMs: number },
  ): Promise<boolean> {
    const r = await voiceSpeak(text);
    const bin = atob(r.audio_b64);
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    const blob = new Blob([bytes], { type: r.audio_mime || "audio/mpeg" });
    const url = URL.createObjectURL(blob);
    const audio = new Audio(url);
    audio.preload = "auto";
    audio.crossOrigin = "anonymous";
    this.audio = audio;
    return new Promise<boolean>((resolve) => {
      let settled = false;
      const finish = (ok: boolean) => {
        if (settled) return;
        settled = true;
        try {
          URL.revokeObjectURL(url);
        } catch {
          /* ignore */
        }
        this.audio = null;
        if (ok) this.suppressUntil = Date.now() + opts.suppressMs;
        resolve(ok);
      };
      audio.onended = () => finish(true);
      audio.onerror = () => {
        console.warn("[ambient] audio element error", audio.error);
        finish(false);
      };
      const p = audio.play();
      if (p && typeof p.then === "function") {
        p.then(() => {
          /* playback started; wait for onended */
        }).catch((err) => {
          // NotAllowedError (autoplay), NotSupportedError, etc.
          console.warn("[ambient] audio.play() rejected:", err?.name ?? err);
          finish(false);
        });
      }
    });
  }
}

// ── helpers ────────────────────────────────────────────────────────────

function loadConfig(): AmbientConfig {
  if (typeof localStorage === "undefined") return { ...DEFAULT_CONFIG };
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return { ...DEFAULT_CONFIG };
    const parsed = JSON.parse(raw);
    return { ...DEFAULT_CONFIG, ...parsed };
  } catch {
    return { ...DEFAULT_CONFIG };
  }
}

function stripWake(text: string, wake: WakePhrase): string {
  const variants = WAKE_VARIANTS[wake];
  let out = text;
  for (const v of variants) {
    const i = out.indexOf(v);
    if (i >= 0) out = out.slice(i + v.length);
  }
  return out.trim();
}

function wakeRegex(wake: WakePhrase): RegExp {
  const alts = WAKE_VARIANTS[wake]
    .map((v) => v.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"))
    .join("|");
  return new RegExp(`^(?:${alts})[,.!?\\s]*`, "i");
}

function speakLocally(text: string): void {
  try {
    if (typeof window === "undefined") return;
    const synth = window.speechSynthesis;
    if (!synth) return;
    const u = new SpeechSynthesisUtterance(text);
    u.rate = 1.05;
    u.pitch = 1;
    u.volume = 0.9;
    synth.speak(u);
  } catch {
    /* best-effort */
  }
}

function playTone(): void {
  try {
    const Ctx = (window as any).AudioContext ?? (window as any).webkitAudioContext;
    if (!Ctx) return;
    const ctx = new Ctx();
    const o = ctx.createOscillator();
    const g = ctx.createGain();
    o.connect(g);
    g.connect(ctx.destination);
    o.frequency.value = 880;
    g.gain.value = 0.06;
    o.start();
    o.stop(ctx.currentTime + 0.12);
    setTimeout(() => ctx.close().catch(() => {}), 400);
  } catch {
    /* ignore */
  }
}

function estimateSpeechMs(text: string): number {
  // 150 wpm ≈ 400ms/word.
  const words = text.split(/\s+/).filter(Boolean).length;
  return Math.min(30_000, Math.max(1200, words * 380));
}

function delay(ms: number): Promise<void> {
  return new Promise((r) => setTimeout(r, ms));
}

function randId(): string {
  return Math.random().toString(36).slice(2) + Date.now().toString(36);
}

// Minimal shape of SpeechRecognition we actually use (the DOM lib types
// aren't always present in bundler TS setups).
interface SpeechRecognitionLike {
  continuous: boolean;
  interimResults: boolean;
  lang: string;
  start(): void;
  stop(): void;
  onresult: ((ev: { results: SpeechRecognitionResultList }) => void) | null;
  onerror: ((ev: unknown) => void) | null;
  onend: ((ev: unknown) => void) | null;
}
interface SpeechRecognitionResult {
  isFinal: boolean;
  [index: number]: { transcript: string };
}
interface SpeechRecognitionResultList {
  length: number;
  [index: number]: SpeechRecognitionResult;
}

export const ambient = new AmbientController();

// ── audio-unlock ──────────────────────────────────────────────────────
//
// Chrome's autoplay policy silently blocks Audio.play() until the user
// has interacted with the page. A speech-recognition callback does not
// count as user activation, so the TTS reply can come back and "play"
// (onended fires) without any sound ever reaching the speakers. The
// first real click/keydown on the page primes a silent Audio element,
// which unlocks every subsequent playback for the session.

let audioUnlocked = false;

function primeAudio(): void {
  if (audioUnlocked || typeof window === "undefined") return;
  try {
    const silence =
      "data:audio/mpeg;base64,/+MYxAAAAANIAAAAAExBTUUzLjk4LjIAAAAAAAAAACQCwAA" +
      "DAAAAA0gAAAAAAAAAAAAAAAAAAAA//MUZAAAAANIAAAAAExBTUUzLjk4LjJVVQ==";
    const a = new Audio(silence);
    a.volume = 0;
    const p = a.play();
    if (p && typeof p.then === "function") {
      p.then(() => {
        audioUnlocked = true;
      }).catch(() => {
        /* not unlocked; we'll try again on the next gesture */
      });
    } else {
      audioUnlocked = true;
    }
  } catch {
    /* ignore */
  }
}

if (typeof window !== "undefined") {
  const onGesture = () => {
    primeAudio();
    if (audioUnlocked) {
      window.removeEventListener("click", onGesture);
      window.removeEventListener("keydown", onGesture);
      window.removeEventListener("touchstart", onGesture);
    }
  };
  window.addEventListener("click", onGesture, { passive: true });
  window.addEventListener("keydown", onGesture, { passive: true });
  window.addEventListener("touchstart", onGesture, { passive: true });
}
