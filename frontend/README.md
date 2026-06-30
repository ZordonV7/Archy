# Archy Frontend — Tauri + React

Desktop app with two windows:
1. **Mascot overlay** — small, transparent, always-on-top, draggable
2. **Dashboard** — full window with tasks, schedule, settings

## Prerequisites

1. **Node.js 18+** — https://nodejs.org
2. **Rust** (for Tauri) — https://rustup.rs
3. **Archy backend running** — `python run.py serve` from the project root

## Quick Start (Browser Mode)

```bash
cd frontend
npm install
npm run dev
```

Open http://localhost:5173 — works in browser for development.
- Mascot appears at `/#/mascot`
- Dashboard appears at `/#/dashboard`

## Desktop App (Tauri)

### First time setup

1. Install Rust: https://rustup.rs
2. Install Tauri CLI:
   ```bash
   npm install -D @tauri-apps/cli
   ```
3. Generate icons from the SVG:
   ```bash
   npm run tauri icon src-tauri/icons/icon.svg
   ```
   This generates all required PNG/ICO/ICNS formats.

### Run in dev mode

```bash
npm run tauri dev
```

This launches two native windows:
- Mascot: 200×240px, transparent, always-on-top, in bottom-left
- Dashboard: 1000×700px, hidden initially — click mascot to show

### Build production app

```bash
npm run tauri build
```

Output: `src-tauri/target/release/bundle/` with installers for your OS:
- Windows: `.msi` and `.exe`
- macOS: `.dmg` and `.app`
- Linux: `.deb` and `.AppImage`

## Architecture

```
┌──────────────────┐     ┌──────────────────────┐
│  MASCOT WINDOW   │     │   DASHBOARD WINDOW   │
│  (always-on-top) │     │   (on demand)        │
│                  │     │                      │
│  • Draggable     │     │  • Draggable titlebar│
│  • Transparent   │     │  • Tabs: Tasks/      │
│  • Click → opens │────▶│    Schedule/Settings │
│    dashboard     │     │  • Window controls   │
│  • Speech bubble │     │    (min/hide/quit)   │
│  • Mood/Energy   │     │  • Mood/Energy       │
│    bars          │     │    widgets           │
│  • Minimize to   │     │                      │
│    tray          │     │                      │
└──────────────────┘     └──────────────────────┘
         │                          │
         └──────────┬───────────────┘
                    │
            WebSocket + REST
                    │
                    ▼
         ┌──────────────────┐
         │  FastAPI Backend │
         │  (port 8000)     │
         └──────────────────┘
```

### Window management

- **Click mascot** → toggles dashboard window
- **Minimize button** (−) → hides mascot to system tray
- **Tray icon** → right-click for menu (Show/Open Dashboard/Quit)
- **Tray icon** → left-click shows mascot
- **Close mascot** → minimizes to tray (doesn't quit)
- **Quit button** (×) in dashboard → exits app

### Draggable regions

- **Mascot window**: entire window is draggable (grab anywhere)
- **Dashboard window**: title bar is draggable (top 40px)

In Tauri, dragging uses native OS-level `startDragging()` for smooth movement.
In browser mode, falls back to CSS transform-based dragging.

## System Tray

The app creates a system tray icon on launch:
- **Left-click**: show mascot
- **Right-click**: menu with Show / Open Dashboard / Quit

The mascot window minimizes to tray instead of closing, so Archy stays
available in the background.

## File Structure

```
frontend/
├── package.json
├── vite.config.ts
├── tsconfig.json
├── index.html              # Routes via #/mascot or #/dashboard
├── src/
│   ├── main.tsx
│   ├── App.tsx             # Routes to MascotWindow or DashboardWindow
│   ├── stores/
│   │   └── eventStore.ts   # Zustand — handles WebSocket events
│   ├── hooks/
│   │   ├── useWebSocket.ts          # Connects to backend /events
│   │   ├── useArchyApi.ts          # REST client
│   │   ├── useTauriWindow.ts        # Window management (show/hide/quit)
│   │   └── useDraggable.ts          # Draggable hook (Tauri + browser)
│   ├── components/
│   │   ├── Mascot/
│   │   │   ├── MascotWindow.tsx     # Mascot overlay window
│   │   │   └── mascotStates.ts      # State derivation from mood+energy
│   │   ├── Dashboard/
│   │   │   ├── DashboardWindow.tsx  # Dashboard window with titlebar
│   │   │   ├── TaskList.tsx
│   │   │   ├── MoodWidget.tsx
│   │   │   ├── EnergyWidget.tsx
│   │   │   └── QuickIngest.tsx
│   │   └── Settings/
│   │       └── GoogleConnect.tsx
│   └── styles/
│       └── global.css
└── src-tauri/
    ├── Cargo.toml
    ├── tauri.conf.json      # Two-window config + tray
    ├── capabilities/
    │   └── default.json     # Tauri 2 permissions
    ├── build.rs
    ├── src/
    │   ├── main.rs
    │   └── lib.rs           # Window commands + tray icon
    └── icons/
        └── icon.svg         # Source icon (run `tauri icon` to generate)
```

## Troubleshooting

### "No such command" error in Tauri
Make sure you ran `npm install` and the Tauri CLI is installed:
```bash
npm install -D @tauri-apps/cli
```

### Mascot not appearing
The mascot window is positioned at x:50, y:700. If your screen is smaller,
edit `src-tauri/tauri.conf.json` and adjust the x/y values.

### Dashboard doesn't open on click
Ensure the backend is running (`python run.py serve`). The WebSocket
connection indicator (green/red dot on mascot) shows connection status.

### Transparent window shows black background (Linux)
Install a compositor like Picom. Transparent windows require a compositor
on Linux.

### Browser dev mode
In browser mode, both windows share the same tab. Use the URL hash to switch:
- `http://localhost:5173/#/mascot` — mascot view
- `http://localhost:5173/#/dashboard` — dashboard view
