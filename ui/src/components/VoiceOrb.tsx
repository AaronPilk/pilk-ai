import { useEffect, useRef, useState } from "react";
import {
  fetchVoiceStatus,
  pilk,
  voiceCancel,
  voiceDone,
  voiceListen,
  voiceUtterance,
  type VoicePipelineState,
  type VoiceStatus,
} from "../state/api";
import Orb, { type OrbMode, type OrbSize } from "./Orb";

type Local = "idle" | "recording" | "uploading" | "playing" | "error";

interface VoiceOrbProps {
  size?: OrbSize;
  showLabel?: boolean;
  showCaption?: boolean;
}

export default function VoiceOrb({
  size = "large",
  showLabel = true,
  showCaption = true,
}: VoiceOrbProps) {
  const [local, setLocal] = useState<Local>("idle");
  const [status, setStatus] = useState<VoiceStatus | null>(null);
  const [remote, setRemote] = useState<VoicePipelineState>("idle");
  const [lastTranscript, setLastTranscript] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);

  const recorderRef = useRef<MediaRecorder | null>(null);
  const chunksRef = useRef<BlobPart[]>([]);
  const streamRef = useRef<MediaStream | null>(null);
  const audioRef = useRef<HTMLAudioElement | null>(null);

  useEffect(() => {
    fetchVoiceStatus().then(setStatus).catch(() => {});
    return pilk.onMessage((m) => {
      if (m.type === "voice.state") setRemote(m.state);
    });
  }, []);

  useEffect(() => {
    return () => {
      const r = recorderRef.current;
      if (r && r.state !== "inactive") {
        try {
          r.stop();
        } catch {}
      }
      streamRef.current?.getTracks().forEach((t) => t.stop());
      streamRef.current = null;
      recorderRef.current = null;
      if (audioRef.current) {
        try {
          audioRef.current.pause();
        } catch {}
        audioRef.current = null;
      }
    };
  }, []);

  const startRecording = async () => {
    if (local !== "idle" && local !== "playing") return;
    setErr(null);
    if (local === "playing" && audioRef.current) {
      audioRef.current.pause();
      audioRef.current = null;
      await voiceDone().catch(() => {});
    }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      streamRef.current = stream;
      const mimeType = pickMimeType();
      const recorder = new MediaRecorder(stream, { mimeType });
      chunksRef.current = [];
      recorder.ondataavailable = (e) => {
        if (e.data.size > 0) chunksRef.current.push(e.data);
      };
      recorder.onstop = async () => {
        const blob = new Blob(chunksRef.current, { type: mimeType });
        streamRef.current?.getTracks().forEach((t) => t.stop());
        streamRef.current = null;
        if (blob.size === 0) {
          setLocal("idle");
          await voiceCancel().catch(() => {});
          return;
        }
        setLocal("uploading");
        try {
          const r = await voiceUtterance(blob);
          setLastTranscript(r.transcript);
          if (r.audio_b64) {
            const bin = atob(r.audio_b64);
            const bytes = new Uint8Array(bin.length);
            for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
            const audioBlob = new Blob([bytes], {
              type: r.audio_mime || "audio/mpeg",
            });
            const url = URL.createObjectURL(audioBlob);
            const audio = new Audio(url);
            audioRef.current = audio;
            setLocal("playing");
            audio.onended = async () => {
              URL.revokeObjectURL(url);
              audioRef.current = null;
              setLocal("idle");
              await voiceDone().catch(() => {});
            };
            audio.onerror = async () => {
              URL.revokeObjectURL(url);
              audioRef.current = null;
              setLocal("idle");
              await voiceDone().catch(() => {});
            };
            await audio.play();
          } else {
            setLocal("idle");
            await voiceDone().catch(() => {});
          }
        } catch (e: any) {
          setErr(e?.message ?? String(e));
          setLocal("error");
          await voiceCancel().catch(() => {});
          setTimeout(() => setLocal("idle"), 2000);
        }
      };
      await voiceListen().catch(() => {});
      recorder.start();
      recorderRef.current = recorder;
      setLocal("recording");
    } catch (e: any) {
      setErr(`mic error: ${e?.message ?? e}`);
      setLocal("error");
      setTimeout(() => setLocal("idle"), 2000);
    }
  };

  const stopRecording = () => {
    const r = recorderRef.current;
    if (r && r.state !== "inactive") {
      r.stop();
      recorderRef.current = null;
    }
  };

  const toggle = () => {
    if (local === "idle" || local === "playing") void startRecording();
    else if (local === "recording") stopRecording();
    // uploading / error: ignore until state returns to idle
  };

  const disabled = status?.enabled === false;
  const orbMode = toOrbMode(local, remote);
  const label = describe(local, remote, status);

  return (
    <div className={`voice-orb voice-orb--${size}`}>
      <Orb
        size={size}
        mode={orbMode}
        onClick={toggle}
        disabled={disabled}
        aria-label={disabled ? "Voice pipeline offline" : label}
        title={disabled ? "Voice pipeline offline" : "Tap to talk"}
      />
      {showLabel && size !== "small" && (
        <div className="voice-orb-label">{label}</div>
      )}
      {showCaption && size !== "small" && (lastTranscript || err) && (
        <div className="voice-orb-caption">
          {err ? `Error — ${err}` : `"${lastTranscript}"`}
        </div>
      )}
    </div>
  );
}

function toOrbMode(local: Local, remote: VoicePipelineState): OrbMode {
  if (local === "error") return "error";
  if (local === "recording" || remote === "listening") return "listening";
  if (local === "uploading" || remote === "transcribing") return "uploading";
  if (local === "playing" || remote === "speaking") return "speaking";
  return "idle";
}

function describe(
  local: Local,
  remote: VoicePipelineState,
  status: VoiceStatus | null,
): string {
  if (status?.enabled === false) return "Voice offline";
  if (local === "recording" || remote === "listening") return "Listening…";
  if (local === "uploading" || remote === "transcribing") return "Thinking…";
  if (local === "playing" || remote === "speaking") return "Speaking…";
  if (local === "error") return "Something went wrong";
  return "Tap to talk";
}

function pickMimeType(): string {
  const candidates = [
    "audio/webm;codecs=opus",
    "audio/webm",
    "audio/ogg;codecs=opus",
    "audio/mp4",
  ];
  for (const c of candidates) {
    if (
      typeof MediaRecorder !== "undefined" &&
      MediaRecorder.isTypeSupported(c)
    ) {
      return c;
    }
  }
  return "audio/webm";
}
