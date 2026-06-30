import { useEventStore } from "../stores/eventStore";
import { API_BASE } from "../lib/apiConfig";

export async function apiGet(path: string): Promise<any> {
  const resp = await fetch(`${API_BASE}${path}`, {
    credentials: "include",
  });
  if (!resp.ok) throw new Error(`API ${path} failed: ${resp.status}`);
  return resp.json();
}

export async function apiPost(path: string, body?: any): Promise<any> {
  const resp = await fetch(`${API_BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
    credentials: "include",
  });
  if (!resp.ok) throw new Error(`API ${path} failed: ${resp.status}`);
  return resp.json();
}

export function useArchyApi() {
  const setTasks = useEventStore((s) => s.setTasks);

  return {
    ingestText: async (text: string) => apiPost("/ingest/text", { text }),
    ingestAudio: async (file: File) => {
      const formData = new FormData();
      formData.append("file", file);
      const resp = await fetch(`${API_BASE}/ingest/audio`, {
        method: "POST",
        body: formData,
        credentials: "include",
      });
      return resp.json();
    },
    startTask: (id: string) => apiPost(`/tasks/${id}/start`),
    completeTask: (id: string) => apiPost(`/tasks/${id}/complete`),
    cancelTask: (id: string) => apiPost(`/tasks/${id}/cancel`),
    refreshTasks: async () => {
      const data = await apiGet("/tasks");
      setTasks(data.tasks);
      return data.tasks;
    },
    getMood: () => apiGet("/mood"),
    getEnergy: () => apiGet("/energy"),
    getSchedule: () => apiGet("/schedule"),
    reportDrift: (kind: string, minutes: number, description: string) =>
      apiPost("/drift", { kind, minutes_lost: minutes, description }),
    triggerAuth: () => apiPost("/auth/google"),
    syncCalendar: () => apiPost("/calendar/sync"),
    generateBrief: () => apiPost("/brief/daily"),
  };
}
