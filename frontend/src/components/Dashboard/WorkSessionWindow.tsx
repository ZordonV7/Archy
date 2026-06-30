import { motion, AnimatePresence } from "framer-motion";
import { useEffect, useState, useCallback, useRef } from "react";
import { useEventStore } from "../../stores/eventStore";
import { API_BASE } from "../../lib/apiConfig";

async function apiPost(path: string, params?: Record<string, any>) {
  const url = new URL(`${API_BASE}${path}`);
  if (params) {
    Object.entries(params).forEach(([k, v]) => url.searchParams.set(k, String(v)));
  }
  const resp = await fetch(url.toString(), { method: "POST", credentials: "include" });
  return resp.json();
}

/// WorkSessionWindow — shows when a task is started.
/// Displays timer, progress bar, pause/resume/end controls.
/// This is the "Execution Mode" — Archy becomes a work companion.
export default function WorkSessionWindow() {
  const [session, setSession] = useState<any>(null);
  const [elapsed, setElapsed] = useState(0); // seconds
  const [progress, setProgress] = useState(0); // percentage

  // Listen for SessionStarted / SessionEnded events
  useEffect(() => {
    const unsub = useEventStore.subscribe((state) => {
      // Check latest event
      const log = state.eventLog;
      if (log.length === 0) return;
      const last = log[log.length - 1];
      if (last.type === "SessionStarted") {
        setSession(last.payload);
      } else if (last.type === "SessionEnded" || last.type === "SessionPaused" || last.type === "SessionResumed") {
        // Fetch fresh session state
        fetch(`${API_BASE}/sessions/active`, { credentials: "include" })
          .then((r) => r.json())
          .then((data) => {
            if (data && data.session_id) {
              setSession(data);
            } else if (last.type === "SessionEnded") {
              setSession(null);
            }
          })
          .catch(() => {});
      }
    });
    return unsub;
  }, []);

  // Fetch active session on mount
  useEffect(() => {
    fetch(`${API_BASE}/sessions/active`, { credentials: "include" })
      .then((r) => r.json())
      .then((data) => {
        if (data && data.session_id) setSession(data);
      })
      .catch(() => {});
  }, []);

  // Tick the timer every second.
  // FIX: previously `elapsed` was in the deps array, so the interval was
  // torn down and recreated every tick — wasteful and produced stale-closure
  // math (progress used an outdated `elapsed`). Use a ref for elapsed and keep
  // the interval stable across ticks; only restart it when the session
  // (and therefore estimated_minutes / started_at) actually changes.
  const elapsedRef = useRef(0);
  useEffect(() => { elapsedRef.current = 0; setElapsed(0); setProgress(0); }, [session?.session_id]);
  useEffect(() => {
    if (!session) return;
    const estSeconds = Math.max(1, (session.estimated_minutes || 0) * 60);
    // If session was resumed from the server, seed the elapsed time from the
    // server's own accounting so we don't restart at zero.
    if (typeof session.elapsed_seconds === "number") {
      elapsedRef.current = Math.max(0, session.elapsed_seconds);
      setElapsed(elapsedRef.current);
      setProgress(Math.min(100, Math.round((elapsedRef.current / estSeconds) * 100)));
    }
    const interval = setInterval(() => {
      if (session.status !== "active") return;
      elapsedRef.current += 1;
      const next = elapsedRef.current;
      setElapsed(next);
      setProgress(Math.min(100, Math.round((next / estSeconds) * 100)));
    }, 1000);
    return () => clearInterval(interval);
  }, [session?.session_id, session?.status, session?.estimated_minutes, session?.elapsed_seconds]);

  const handlePause = useCallback(() => {
    apiPost("/sessions/pause").then((data) => {
      if (data.session_id) setSession({ ...session, ...data });
    });
  }, [session]);

  const handleResume = useCallback(() => {
    apiPost("/sessions/resume").then((data) => {
      if (data.session_id) setSession({ ...session, ...data });
    });
  }, [session]);

  const handleEnd = useCallback(() => {
    apiPost("/sessions/end", { completion_percentage: 100, complete_task: true }).then(() => {
      setSession(null);
      setElapsed(0);
      setProgress(0);
      elapsedRef.current = 0;
    });
  }, []);

  const formatTime = (seconds: number) => {
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    const s = seconds % 60;
    if (h > 0) return `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
    return `${m}:${String(s).padStart(2, "0")}`;
  };

  return (
    <AnimatePresence>
      {session && (
        <motion.div
          className="work-session"
          initial={{ opacity: 0, y: 50, scale: 0.9 }}
          animate={{ opacity: 1, y: 0, scale: 1 }}
          exit={{ opacity: 0, y: 50, scale: 0.9 }}
          transition={{ type: "spring", stiffness: 300, damping: 25 }}
        >
          <div className="session-header">
            <span className="session-icon">⚡</span>
            <span className="session-title">{session.task_title}</span>
          </div>

          {/* Timer */}
          <div className="session-timer">
            <motion.div
              className="timer-display"
              animate={session.status === "active" ? { scale: [1, 1.02, 1] } : {}}
              transition={{ duration: 1, repeat: Infinity }}
            >
              {formatTime(elapsed)}
            </motion.div>
            <div className="timer-label">
              / {session.estimated_minutes}:00
            </div>
          </div>

          {/* Progress bar */}
          <div className="session-progress">
            <motion.div
              className="progress-fill"
              style={{
                background: progress >= 100 ? "#ef4444" : progress >= 75 ? "#f59e0b" : "#8b5cf6",
              }}
              animate={{ width: `${progress}%` }}
              transition={{ duration: 0.5 }}
            />
            <span className="progress-label">{progress}%</span>
          </div>

          {/* Interruption count */}
          {session.interruption_count > 0 && (
            <div className="session-interruptions">
              ⏸ {session.interruption_count} interruption{session.interruption_count > 1 ? "s" : ""}
            </div>
          )}

          {/* Controls */}
          <div className="session-controls">
            {session.status === "active" ? (
              <button className="session-btn pause" onClick={handlePause}>
                ⏸ Pause
              </button>
            ) : (
              <button className="session-btn resume" onClick={handleResume}>
                ▶ Resume
              </button>
            )}
            <button className="session-btn end" onClick={handleEnd}>
              ✓ Complete
            </button>
          </div>

          {/* Archy encouragement */}
          <div className="session-encouragement">
            {progress >= 100
              ? "Running over — consider wrapping up. 🌟"
              : progress >= 75
              ? "Almost there! You're doing great."
              : progress >= 50
              ? "Halfway done — keep going!"
              : progress > 0
              ? "You've got this. Focused work. 🎯"
              : "Let's begin. I'm right here. 💜"}
          </div>
        </motion.div>
      )}
    </AnimatePresence>
  );
}
