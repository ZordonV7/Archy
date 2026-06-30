import { useRef, useState, useCallback } from "react";
import { API_BASE } from "../lib/apiConfig";

/// Hook for recording audio from the microphone and sending it to the backend.
/// Only records when `isRecording` is true.
/// Sends the audio to the backend's /ingest/audio endpoint.
export function useAudioRecorder(onTranscript?: (text: string) => void) {
  const [isRecording, setIsRecording] = useState(false);
  const [isProcessing, setIsProcessing] = useState(false);
  const mediaRecorderRef = useRef<MediaRecorder | null>(null);
  const chunksRef = useRef<Blob[]>([]);
  const streamRef = useRef<MediaStream | null>(null);

  const startRecording = useCallback(async () => {
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      streamRef.current = stream;
      chunksRef.current = [];

      const mr = new MediaRecorder(stream);
      mr.ondataavailable = (e) => {
        if (e.data.size > 0) chunksRef.current.push(e.data);
      };
      mr.onstop = async () => {
        const audioBlob = new Blob(chunksRef.current, { type: "audio/webm" });
        // Stop all tracks so the mic indicator turns off
        streamRef.current?.getTracks().forEach((t) => t.stop());
        streamRef.current = null;

        // Send to backend
        setIsProcessing(true);
        try {
          const formData = new FormData();
          formData.append("file", audioBlob, "recording.webm");
          const resp = await fetch(`${API_BASE}/ingest/audio`, {
            method: "POST",
            body: formData,
            credentials: "include",
          });
          if (resp.ok) {
            const task = await resp.json();
            if (onTranscript && task?.raw_transcript) {
              onTranscript(task.raw_transcript);
            }
          } else {
            console.error("[Audio] STT failed:", resp.status, await resp.text());
          }
        } catch (err) {
          console.error("[Audio] Failed to send audio:", err);
        } finally {
          setIsProcessing(false);
        }
      };
      mr.start();
      mediaRecorderRef.current = mr;
      setIsRecording(true);
    } catch (err) {
      console.error("[Audio] Failed to start recording:", err);
      alert("Could not access microphone. Please grant permission and try again.");
    }
  }, [onTranscript]);

  const stopRecording = useCallback(() => {
    if (mediaRecorderRef.current && isRecording) {
      mediaRecorderRef.current.stop();
      setIsRecording(false);
    }
  }, [isRecording]);

  const toggle = useCallback(() => {
    if (isRecording) stopRecording();
    else startRecording();
  }, [isRecording, startRecording, stopRecording]);

  return { isRecording, isProcessing, startRecording, stopRecording, toggle };
}
