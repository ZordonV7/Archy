import { useState } from "react";
import { motion } from "framer-motion";
import { quitApp } from "../../hooks/useTauriWindow";

interface SettingsPanelProps {
  onConnectGoogle: () => Promise<any>;
}

export default function SettingsPanel({ onConnectGoogle }: SettingsPanelProps) {
  const [googleStatus, setGoogleStatus] = useState<"idle" | "connecting" | "connected" | "error">("idle");
  const [googleMessage, setGoogleMessage] = useState("");
  const [showQuitConfirm, setShowQuitConfirm] = useState(false);

  const handleConnectGoogle = async () => {
    setGoogleStatus("connecting");
    setGoogleMessage("");
    try {
      const result = await onConnectGoogle();
      setGoogleStatus("connected");
      setGoogleMessage(result.message || "Google Calendar connected!");
    } catch (err) {
      setGoogleStatus("error");
      setGoogleMessage(err instanceof Error ? err.message : "Connection failed");
    }
  };

  return (
    <div className="settings-section">
      <h2>Settings</h2>

      {/* Google Integration */}
      <div className="settings-group">
        <h3>Google Integration</h3>
        <p className="settings-description">
          Connect your Google account so Archy can sync tasks to your Calendar and generate daily briefs.
        </p>
        <div className="integration-card">
          <div className="integration-icon">📅</div>
          <div className="integration-info">
            <h3>Google Calendar</h3>
            <p>Auto-sync scheduled tasks as calendar events</p>
          </div>
          <motion.button
            className={`connect-btn ${googleStatus}`}
            onClick={handleConnectGoogle}
            disabled={googleStatus === "connecting" || googleStatus === "connected"}
            whileHover={{ scale: 1.02 }}
            whileTap={{ scale: 0.98 }}
          >
            {googleStatus === "idle" && "Connect"}
            {googleStatus === "connecting" && "Connecting..."}
            {googleStatus === "connected" && "✓ Connected"}
            {googleStatus === "error" && "Retry"}
          </motion.button>
        </div>
        {googleMessage && (
          <p className={`settings-message ${googleStatus}`}>{googleMessage}</p>
        )}
        <div className="integration-note">
          <p><strong>To enable Google integration:</strong></p>
          <ol>
            <li>Create OAuth credentials in Google Cloud Console</li>
            <li>Add <code>GOOGLE_CLIENT_ID</code> and <code>GOOGLE_CLIENT_SECRET</code> to <code>.env</code></li>
            <li>Run <code>archy auth</code> from CLI to complete OAuth flow</li>
          </ol>
        </div>
      </div>

      {/* App Controls */}
      <div className="settings-group">
        <h3>Application</h3>
        <p className="settings-description">
          Quit Archy completely. The mascot will close and the app will exit.
        </p>
        {!showQuitConfirm ? (
          <button
            className="quit-btn"
            onClick={() => setShowQuitConfirm(true)}
          >
            Quit Archy
          </button>
        ) : (
          <div className="quit-confirm">
            <p>Are you sure you want to quit Archy?</p>
            <div className="quit-confirm-buttons">
              <button
                className="action-btn"
                onClick={() => setShowQuitConfirm(false)}
              >
                Cancel
              </button>
              <button
                className="action-btn danger"
                onClick={() => quitApp()}
              >
                Yes, Quit
              </button>
            </div>
          </div>
        )}
      </div>

      {/* About */}
      <div className="settings-group">
        <h3>About</h3>
        <div className="about-info">
          <p><strong>Archy</strong> v0.2.0</p>
          <p>Your adorable scheduling companion.</p>
          <p className="about-tagline">LLMs advise, deterministic systems decide, Archy speaks.</p>
        </div>
      </div>
    </div>
  );
}
