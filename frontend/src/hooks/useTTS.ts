import { useState, useCallback, useRef } from "react";

/// Hook for Text-to-Speech playback.
/// Uses the browser's built-in SpeechSynthesis API (no backend needed).
/// When TTS is enabled, Archy's messages are spoken aloud.
export function useTTS() {
  const [enabled, setEnabled] = useState(false);
  const [isSpeaking, setIsSpeaking] = useState(false);
  const utteranceRef = useRef<SpeechSynthesisUtterance | null>(null);

  const enable = useCallback(() => {
    setEnabled(true);
  }, []);

  const disable = useCallback(() => {
    setEnabled(false);
    // Stop any ongoing speech
    if (window.speechSynthesis) {
      window.speechSynthesis.cancel();
    }
    setIsSpeaking(false);
  }, []);

  const toggle = useCallback(() => {
    if (enabled) disable();
    else enable();
  }, [enabled, enable, disable]);

  const speak = useCallback(
    (text: string) => {
      if (!enabled || !window.speechSynthesis) return;
      // Cancel any ongoing speech
      window.speechSynthesis.cancel();

      const utterance = new SpeechSynthesisUtterance(text);
      // Warm, friendly voice settings
      utterance.rate = 0.95;      // slightly slower = gentler
      utterance.pitch = 1.15;     // slightly higher = friendlier
      utterance.volume = 0.8;

      // Try to pick a female voice (Archy is gentle/adorable)
      const voices = window.speechSynthesis.getVoices();
      const preferred = voices.find(
        (v) => v.name.includes("Female") || v.name.includes("Samantha") || v.name.includes("Zira")
      );
      if (preferred) utterance.voice = preferred;

      utterance.onstart = () => setIsSpeaking(true);
      utterance.onend = () => setIsSpeaking(false);
      utterance.onerror = () => setIsSpeaking(false);

      utteranceRef.current = utterance;
      window.speechSynthesis.speak(utterance);
    },
    [enabled]
  );

  return { enabled, isSpeaking, enable, disable, toggle, speak };
}
