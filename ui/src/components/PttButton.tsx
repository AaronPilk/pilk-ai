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

type Mode = "idle" | "recording" | "uploading" | "playing" | "error";

export default function PttButton() {
  const [mode, setMode] = useState<Mode>("idle");
  const [status, setStatus] = useState<VoiceStatus | null>(null);
  const [remoteState, setRemoteState] = useState<VoicePipelineState>("idle");
  const [lastTranscript, setLastTranscript] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);

  const recorderRef = useRef<MediaRecorder | null>(null);
  const chunksRef = useRef<BlobPart[]>([]);
  const streamRef = useRef<MediaStream | null>(null);
  const audioRef = useRef<HTMLAudioElement | null>(null);

  useEffect(() => {
    fetchVoiceStatus().then(setStatus).catch(() => {});
    return pilk.onMessage((m) => {
      if (m.type === "voice.state") setRemoteState(m.state);
    });
  }, []);

  const startRecording = async () => {
    if (mode !== "idle" && mode !== "playing") return;
    setErr(null);
    if (mode === "playing" && audioRef.current) {
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
          setMode("idle");
          await voiceCancel().catch(() => {});
          return;
        }
        setMode("uploading");
        try {
          const r = await voiceUtterance(blob);
          setLastTranscript(r.transcript);
          if (r.audio_b64) {
            const bin = atob(r.audio_b64);
            const bytes = new Uint8Array(bin.length);
            for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
            const audioBlob = new Blob([bytes], { type: r.audio_mime || "audio/mpeg" });
            const url = URL.createObjectURL(audioBlob);
            const audio = new Audio(url);
            audioRef.current = audio;
            setMode("playing");
            audio.onended = async () => {
              URL.revokeObjectURL(url);
              audioRef.current = null;
              setMode("idle");
              await voiceDone().catch(() => {});
            };
            audio.onerror = async () => {
              URL.revokeObjectURL(url);
              audioRef.current = null;
              setMode("idle");
              await voiceDone().catch(() => {});
            };
            await audio.play();
          } else {
            setMode("idle");
            await voiceDone().catch(() => {});
          }
        } catch (e: any) {
          setErr(e?.message ?? String(e));
          setMode("error");
          await voiceCancel().catch(() => {});
          setTimeout(() => setMode("idle"), 2000);
        }
      };
      await voiceListen().catch(() => {});
      recorder.start();
      recorderRef.current = recorder;
      setMode("recording");
    } catch (e: any) {
      setErr(`mic error: ${e?.message ?? e}`);
      setMode("error");
      setTimeout(() => setMode("idle"), 2000);
    }
  };

  const stopRecording = () => {
    const recorder = recorderRef.current;
    if (recorder && recorder.state !== "inactive") {
      recorder.stop();
      recorderRef.current = null;
    }
  };

  const label = describe(mode, remoteState, status);
  const dot = dotColor(mode, remoteState);
  const disabled = status?.enabled === false;

  return (
    <div className="ptt">
      <button
        className={`ptt-btn ptt-btn--${mode}`}
        onMouseDown={startRecording}
        onMouseUp={stopRecording}
        onMouseLeave={stopRecording}
        onTouchStart={(e) => {
          e.preventDefault();
          startRecording();
        }}
        onTouchEnd={(e) => {
          e.preventDefault();
          stopRecording();
        }}
        disabled={disabled}
        title={disabled ? "voice pipeline offline" : "Hold to talk"}
      >
        <span className="dot" style={{ background: dot }} />
        <span>{label}</span>
      </button>
      {(lastTranscript || err) && (
        <span className="ptt-hint">
          {err ? `err: ${err}` : `"${lastTranscript}"`}
        </span>
      )}
    </div>
  );
}

function describe(
  mode: Mode,
  remote: VoicePipelineState,
  status: VoiceStatus | null,
): string {
  if (status?.enabled === false) return "Voice offline";
  if (mode === "recording" || remote === "listening") return "Listening…";
  if (mode === "uploading" || remote === "transcribing") return "Transcribing…";
  if (mode === "playing" || remote === "speaking") return "Speaking…";
  if (mode === "error") return "Error";
  return "Hold to talk";
}

function dotColor(mode: Mode, remote: VoicePipelineState): string {
  if (mode === "recording" || remote === "listening") return "#e55a5a";
  if (mode === "uploading" || remote === "transcribing") return "#e0b84a";
  if (mode === "playing" || remote === "speaking") return "#7aa7ff";
  if (mode === "error") return "#e55a5a";
  return "#606775";
}

function pickMimeType(): string {
  const candidates = [
    "audio/webm;codecs=opus",
    "audio/webm",
    "audio/ogg;codecs=opus",
    "audio/mp4",
  ];
  for (const c of candidates) {
    if (typeof MediaRecorder !== "undefined" && MediaRecorder.isTypeSupported(c)) {
      return c;
    }
  }
  return "audio/webm";
}
