import { create } from "zustand";

// Types matching the backend contracts
export type MoodLabel = "deep_focus" | "watchful" | "drift_alert" | "critical_panic";
export type EnergyLabel = "full" | "steady" | "low" | "depleted";

export interface Task {
  id: string;
  title: string;
  category: string;
  priority: number;
  complexity: number;
  status: string;
  scheduled_start: string | null;
  scheduled_end: string | null;
  deadline: string | null;
  duration_minutes?: number;
}

export interface Slot {
  task_id: string;
  start: string;
  end: string;
  at_risk: boolean;
  is_break: boolean;
  category: string;
}

export interface ArchyMessage {
  text: string;
  mood_label: MoodLabel;
  timestamp: string;
}

interface EventStoreState {
  // Connection
  connected: boolean;
  setConnected: (v: boolean) => void;

  // Latest state
  mood: { score: number; label: MoodLabel; reasons: string[] } | null;
  energy: { score: number; label: EnergyLabel; recommendations: string[] } | null;
  risk: { overall_risk: number; label: string; message: string; highest_task_title: string; trajectory: any[] } | null;
  tasks: Task[];
  archivedTasks: Task[]; // trash folder — completed/cancelled/missed tasks
  slots: Slot[];
  assistantMessage: ArchyMessage | null;

  // Event log (last 100 events)
  eventLog: { type: string; payload: any; timestamp: string }[];

  // Web mode UI state — dashboard open/close
  dashboardOpen: boolean;
  setDashboardOpen: (v: boolean) => void;

  // Actions
  handleEvent: (type: string, payload: any) => void;
  setTasks: (tasks: Task[]) => void;
  archiveTask: (taskId: string) => void;
  restoreTask: (taskId: string) => void;
  removeArchivedTask: (taskId: string) => void;  // FIX: permanently delete from trash
  emptyTrash: () => void;
}

/// Safe helper: ensure value is an array
function safeArray<T>(val: any): T[] {
  if (Array.isArray(val)) return val;
  return [];
}

/// Safe helper: ensure value is a string
function safeString(val: any, fallback = ""): string {
  if (typeof val === "string") return val;
  return fallback;
}

export const useEventStore = create<EventStoreState>((set, get) => ({
  connected: false,
  setConnected: (v) => set({ connected: v }),

  mood: null,
  energy: null,
  risk: null,
  tasks: [],
  archivedTasks: [],
  slots: [],
  assistantMessage: null,
  eventLog: [],

  // Web mode UI state — dashboard starts closed
  dashboardOpen: false,
  setDashboardOpen: (v) => set({ dashboardOpen: v }),

  handleEvent: (type, payload) => {
    // Defensive: payload might be null/undefined
    if (!payload || typeof payload !== "object") {
      payload = {};
    }

    // Add to event log (keep last 100)
    const log = [...get().eventLog, { type, payload, timestamp: new Date().toISOString() }].slice(-100);

    switch (type) {
      case "MoodChanged":
      case "MoodAnalyzed":
        // Defensive: only update if payload has the expected fields
        if (typeof payload.score === "number" && typeof payload.label === "string") {
          set({
            mood: {
              score: payload.score,
              label: payload.label as MoodLabel,
              reasons: safeArray<string>(payload.reasons),
            },
          });
        }
        break;

      case "EnergyChanged":
        if (typeof payload.score === "number" && typeof payload.label === "string") {
          set({
            energy: {
              score: payload.score,
              label: payload.label as EnergyLabel,
              recommendations: safeArray<string>(payload.recommendations),
            },
          });
        }
        break;

      case "RiskChanged":
        if (typeof payload.overall_risk === "number") {
          set({
            risk: {
              overall_risk: payload.overall_risk,
              label: safeString(payload.label, "low"),
              message: safeString(payload.message, ""),
              highest_task_title: safeString(payload.highest_task_title, ""),
              trajectory: safeArray<any>(payload.trajectory),
            },
          });
        }
        break;

      case "TaskCreated":
      case "TaskScheduled":
      case "TaskStarted":
      case "TaskCompleted":
      case "TaskMissed":
      case "TaskCancelled":
        // Defensive: payload must have an id
        if (typeof payload.id === "string") {
          const tasks = get().tasks;
          const archived = get().archivedTasks;
          const idx = tasks.findIndex((t) => t.id === payload.id);

          // If task is completed/cancelled/missed, move to archive (trash) INSTANTLY
          // No mood penalty — just moves the card to the trash tab
          if (type === "TaskCompleted" || type === "TaskCancelled" || type === "TaskMissed") {
            if (idx >= 0) {
              const archivedTask = { ...tasks[idx], ...payload };
              const remaining = tasks.filter((t) => t.id !== payload.id);
              set({
                tasks: remaining,
                archivedTasks: [archivedTask, ...archived.filter((t) => t.id !== payload.id)],
              });
            } else {
              // Task not in active list — just add to archive
              set({
                archivedTasks: [{ ...payload } as Task, ...archived.filter((t) => t.id !== payload.id)],
              });
            }
          } else {
            // Active task update
            if (idx >= 0) {
              const updated = [...tasks];
              updated[idx] = { ...updated[idx], ...payload };
              set({ tasks: updated });
            } else {
              // New task — add to front
              const newTask: Task = {
                id: payload.id,
                title: safeString(payload.title, "Untitled"),
                category: safeString(payload.category, "general"),
                priority: typeof payload.priority === "number" ? payload.priority : 50,
                complexity: typeof payload.complexity === "number" ? payload.complexity : 50,
                status: safeString(payload.status, "pending"),
                scheduled_start: payload.scheduled_start ?? null,
                scheduled_end: payload.scheduled_end ?? null,
                deadline: payload.deadline ?? null,
                duration_minutes: payload.duration_minutes,
              };
              set({ tasks: [newTask, ...tasks] });
            }
          }
        }
        break;

      case "PlanReady":
        if (Array.isArray(payload.slots)) {
          set({ slots: payload.slots });
          if (payload.mood && typeof payload.mood.score === "number") {
            set({
              mood: {
                score: payload.mood.score,
                label: payload.mood.label as MoodLabel,
                reasons: safeArray<string>(payload.mood.reasons),
              },
            });
          }
          if (payload.energy && typeof payload.energy.score === "number") {
            set({
              energy: {
                score: payload.energy.score,
                label: payload.energy.label as EnergyLabel,
                recommendations: safeArray<string>(payload.energy.recommendations),
              },
            });
          }
        }
        break;

      case "ArchyMessage":
        if (typeof payload.text === "string" && payload.text.length > 0) {
          set({
            assistantMessage: {
              text: payload.text,
              mood_label: (payload.mood_label as MoodLabel) || "watchful",
              timestamp: new Date().toISOString(),
            },
          });
        }
        break;
    }

    set({ eventLog: log });
  },

  setTasks: (tasks) => set({ tasks: safeArray<Task>(tasks) }),

  archiveTask: (taskId) => {
    const tasks = get().tasks;
    const archived = get().archivedTasks;
    const task = tasks.find((t) => t.id === taskId);
    if (task) {
      set({
        tasks: tasks.filter((t) => t.id !== taskId),
        archivedTasks: [{ ...task, status: "archived" }, ...archived],
      });
    }
  },

  restoreTask: (taskId) => {
    const tasks = get().tasks;
    const archived = get().archivedTasks;
    const task = archived.find((t) => t.id === taskId);
    if (task) {
      set({
        tasks: [{ ...task, status: "pending" }, ...tasks],
        archivedTasks: archived.filter((t) => t.id !== taskId),
      });
    }
  },

  // FIX: permanently remove a task from the trash UI (the DELETE /tasks/{id}
  // call on the backend actually wipes the row; this just syncs local state).
  removeArchivedTask: (taskId) => {
    set({ archivedTasks: get().archivedTasks.filter((t) => t.id !== taskId) });
  },

  emptyTrash: () => set({ archivedTasks: [] }),
}));
