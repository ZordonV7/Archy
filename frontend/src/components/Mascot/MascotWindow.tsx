import { motion } from "framer-motion";
import { useEffect, useState } from "react";
import { useDraggable } from "../../hooks/useDraggable";
import { toggleDashboard, minimizeToTray, showBubble, hideBubble, positionBubble, isTauri } from "../../hooks/useTauriWindow";
import { useEventStore } from "../../stores/eventStore";
import { useAudioRecorder } from "../../hooks/useAudioRecorder";
import { useTTS } from "../../hooks/useTTS";
import { API_BASE } from "../../lib/apiConfig";
import RiveMascot from "./RiveMascot";
import { useRetroMascotMood } from "./useRetroMascotMood";

export default function MascotWindow() {
  const { dragHandleProps } = useDraggable();
  const retroMood = useRetroMascotMood();
  const energyScore = useEventStore((s) => s.energy?.score ?? 80);
  const moodScore = useEventStore((s) => s.mood?.score ?? 75);
  const assistantMessage = useEventStore((s) => s.assistantMessage);
  const connected = useEventStore((s) => s.connected);
  const setConnected = useEventStore((s) => s.setConnected);
  const handleEvent = useEventStore((s) => s.handleEvent);
  const [loading, setLoading] = useState(true);

  // Poll energy + mood every 15 seconds for real-time mascot updates
  useEffect(() => {
    const fetchState = async () => {
      try {
        // Fetch energy
        const energyResp = await fetch(`${API_BASE}/energy`, { credentials: "include" });
        if (energyResp.ok) {
          const energyData = await energyResp.json();
          if (energyData.score !== undefined) {
            handleEvent("EnergyChanged", energyData);
          }
        }
        // Fetch mood
        const moodResp = await fetch(`${API_BASE}/mood`, { credentials: "include" });
        if (moodResp.ok) {
          const moodData = await moodResp.json();
          if (moodData.score !== undefined) {
            handleEvent("MoodChanged", moodData);
          }
        }
      } catch {
        // Backend might be down — keep last known
      }
    };

    fetchState();
    const interval = setInterval(fetchState, 10000); // every 10 seconds
    return () => clearInterval(interval);
  }, [handleEvent]);

  // TTS hook — foundation ready, but NOT auto-wired to messages.
  const tts = useTTS();
  // FIX: previously the TTS toggle button existed but `speak()` was never
  // invoked when a new assistant message arrived, so enabling TTS did
  // nothing. Wire them together here.
  useEffect(() => {
    if (tts.enabled && assistantMessage?.text) {
      tts.speak(assistantMessage.text);
    }
  }, [assistantMessage, tts.enabled]);

  const recorder = useAudioRecorder((transcript) => {
    console.log("[Mascot] Transcript received:", transcript);
    // Refresh the task list so the new task shows up in the UI.
    useEventStore.getState().handleEvent("IngestStarted", { text: transcript, source: "audio" });
    fetch(`${API_BASE}/tasks`, { credentials: "include" })
      .then((r) => r.ok ? r.json() : null)
      .then((data) => {
        if (data && Array.isArray(data.tasks)) {
          useEventStore.getState().setTasks(data.tasks);
        }
      })
      .catch(() => { /* backend might be down — keep last known */ });
  });

  useEffect(() => {
    const t = setTimeout(() => setLoading(false), 2000);
    return () => clearTimeout(t);
  }, []);

  // Track mascot window position so bubble stays attached
  useEffect(() => {
    const trackPosition = async () => {
      if (!(await isTauri())) return;
      try {
        const { getCurrentWindow } = await import("@tauri-apps/api/window");
        const win = getCurrentWindow();
        const pos = await win.outerPosition();
        const size = await win.outerSize();
        // Position bubble above the mascot window, centered horizontally
        // Bubble is 300x90; mascot is 180x240
        const bubbleX = pos.x + (size.width / 2) - 150; // 150 = half bubble width (300)
        const bubbleY = pos.y - 85; // 85 = bubble height (90) - 5px overlap
        await positionBubble(Math.max(0, bubbleX), Math.max(0, bubbleY));
      } catch (e) {
        console.error("[Mascot] Failed to track position:", e);
      }
    };

    // Track on mount
    trackPosition();

    // Re-track on window move/drag
    let unlisten: (() => void) | null = null;
    (async () => {
      if (!(await isTauri())) return;
      try {
        const { getCurrentWindow } = await import("@tauri-apps/api/window");
        const win = getCurrentWindow();
        unlisten = await win.onMoved(() => {
          trackPosition();
        });
      } catch {}
    })();

    return () => {
      if (unlisten) unlisten();
    };
  }, []);

  // Show the separate bubble window when a message arrives
  useEffect(() => {
    if (assistantMessage?.text) {
      showBubble();
      const t = setTimeout(() => {
        hideBubble();
      }, 6000); // shorter — messages are now 1 sentence
      return () => clearTimeout(t);
    }
  }, [assistantMessage]);

  const handleClick = async () => {
    await toggleDashboard();
  };

  if (loading) {
    return <LoadingScreen />;
  }

  return (
    <div className="mascot-window" {...dragHandleProps}>
      {/* Connection indicator */}
      <div className={`conn-dot ${connected ? "online" : "offline"}`} />

      {/* Main mascot area: retro TV mascot + vertical energy bar */}
      <div className="mascot-main">
        <motion.div
          className="mascot-sprite"
          onClick={handleClick}
          whileHover={{ scale: 1.08, cursor: "pointer" }}
          whileTap={{ scale: 0.95 }}
        >
          <RiveMascot mood={retroMood} />
        </motion.div>

        {/* Vertical energy bar */}
        <div className="energy-vertical" onClick={(e) => e.stopPropagation()}>
          <div className="energy-vertical-label">⚡</div>
          <div className="energy-vertical-track">
            <motion.div
              className="energy-vertical-fill"
              style={{
                background: energyScore >= 60 ? "#10b981" : energyScore >= 40 ? "#f59e0b" : "#ef4444",
              }}
              animate={{ height: `${energyScore}%` }}
              transition={{ type: "spring", stiffness: 200, damping: 20 }}
            />
          </div>
          <div className="energy-vertical-score">{energyScore}</div>
        </div>
      </div>

      {/* Audio buttons */}
      <div className="audio-buttons" onMouseDown={(e) => e.stopPropagation()}>
        <button
          className={`audio-btn mic ${recorder.isRecording ? "active" : ""} ${recorder.isProcessing ? "processing" : ""}`}
          onClick={() => recorder.toggle()}
          title={recorder.isRecording ? "Stop recording" : "Start recording"}
          disabled={recorder.isProcessing}
        >
          {recorder.isProcessing ? "⏳" : recorder.isRecording ? "⏹" : "🎤"}
        </button>
        <button
          className={`audio-btn headphone ${tts.enabled ? "active" : ""}`}
          onClick={() => tts.toggle()}
          title={tts.enabled ? "Mute Archy" : "Enable Archy voice"}
        >
          {tts.enabled ? "🔊" : "🎧"}
        </button>
      </div>

      {/* Minimize button */}
      <button
        className="minimize-btn"
        onMouseDown={(e) => e.stopPropagation()}
        onClick={(e) => {
          e.stopPropagation();
          minimizeToTray();
        }}
        title="Hide Archy"
      >
        −
      </button>
    </div>
  );
}

function LoadingScreen() {
  return (
    <div className="loading-screen">
      <motion.div
        className="loading-mascot"
        animate={{ scale: [1, 1.15, 1], opacity: [0.6, 1, 0.6] }}
        transition={{ duration: 1.5, repeat: Infinity, ease: "easeInOut" }}
      >
        <svg width="80" height="80" viewBox="0 0 120 120">
          <circle cx="30" cy="25" r="14" fill="#8b5cf6" opacity="0.8" />
          <circle cx="90" cy="25" r="14" fill="#8b5cf6" opacity="0.8" />
          <ellipse cx="60" cy="65" rx="40" ry="38" fill="#8b5cf6" />
          <ellipse cx="60" cy="75" rx="28" ry="26" fill="#f0abfc" opacity="0.4" />
          <circle cx="47" cy="56" r="5" fill="#1f2937" />
          <circle cx="73" cy="56" r="5" fill="#1f2937" />
          <ellipse cx="60" cy="70" rx="4" ry="3" fill="#1f2937" />
          <path d="M 55 78 Q 60 81 65 78" stroke="#1f2937" strokeWidth="1.5" fill="none" />
        </svg>
      </motion.div>
      <motion.p
        className="loading-text"
        animate={{ opacity: [0.4, 1, 0.4] }}
        transition={{ duration: 1.5, repeat: Infinity }}
      >
        Archy is waking up...
      </motion.p>
    </div>
  );
}
