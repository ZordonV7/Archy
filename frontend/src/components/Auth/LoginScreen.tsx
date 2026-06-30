import { motion } from "framer-motion";

interface LoginScreenProps {
  onLogin: () => void;
  error?: string | null;
}

/**
 * LoginScreen — shown when the user is not authenticated and require_auth is true.
 *
 * Minimal, on-brand with the retro TV mascot theme: dark background, purple
 * glow, a single "Sign in with Google" button. The button triggers the OAuth
 * flow by redirecting to the backend's /auth/google/login endpoint.
 */
export default function LoginScreen({ onLogin, error }: LoginScreenProps) {
  return (
    <div
      style={{
        position: "fixed",
        inset: 0,
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        justifyContent: "center",
        background:
          "radial-gradient(circle at 15% 20%, rgba(139, 92, 246, 0.15), transparent 50%)," +
          "radial-gradient(circle at 85% 80%, rgba(232, 121, 249, 0.10), transparent 50%)," +
          "linear-gradient(180deg, #0f0a1f 0%, #1a0f2e 100%)",
        color: "#f3f0ff",
        fontFamily: "-apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif",
        zIndex: 9999,
      }}
    >
      <motion.div
        initial={{ opacity: 0, y: 20 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.6, ease: "easeOut" }}
        style={{
          textAlign: "center",
          maxWidth: 400,
          padding: "2rem",
        }}
      >
        {/* Logo / title */}
        <motion.div
          animate={{ scale: [1, 1.03, 1] }}
          transition={{ duration: 4, repeat: Infinity, ease: "easeInOut" }}
          style={{
            fontSize: "3rem",
            marginBottom: "0.5rem",
            filter: "drop-shadow(0 0 20px rgba(167, 139, 250, 0.5))",
          }}
        >
          📺
        </motion.div>
        <h1
          style={{
            fontSize: "2rem",
            fontWeight: 700,
            marginBottom: "0.5rem",
            background: "linear-gradient(135deg, #a78bfa, #e879f9)",
            WebkitBackgroundClip: "text",
            WebkitTextFillColor: "transparent",
            backgroundClip: "text",
          }}
        >
          Archy
        </h1>
        <p
          style={{
            fontSize: "0.95rem",
            color: "#a5a0b8",
            marginBottom: "2rem",
            lineHeight: 1.5,
          }}
        >
          Your adorable scheduling companion.
          <br />
          Sign in to sync your tasks, mood, and schedule.
        </p>

        {/* Google sign-in button */}
        <motion.button
          onClick={onLogin}
          whileHover={{ scale: 1.04, y: -2 }}
          whileTap={{ scale: 0.98 }}
          style={{
            display: "flex",
            alignItems: "center",
            gap: "0.75rem",
            padding: "0.75rem 1.5rem",
            fontSize: "1rem",
            fontWeight: 600,
            color: "#f3f0ff",
            background: "rgba(45, 39, 64, 0.75)",
            backdropFilter: "blur(20px)",
            WebkitBackdropFilter: "blur(20px)",
            border: "1px solid rgba(167, 139, 250, 0.45)",
            borderRadius: "12px",
            cursor: "pointer",
            boxShadow: "0 8px 24px rgba(0,0,0,0.4), 0 0 24px rgba(139,92,246,0.3)",
            transition: "all 0.2s ease",
            margin: "0 auto",
          }}
        >
          {/* Google "G" logo (inline SVG) */}
          <svg width="20" height="20" viewBox="0 0 24 24">
            <path
              fill="#4285F4"
              d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92c-.26 1.37-1.04 2.53-2.21 3.31v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.09z"
            />
            <path
              fill="#34A853"
              d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z"
            />
            <path
              fill="#FBBC05"
              d="M5.84 14.09c-.22-.66-.35-1.36-.35-2.09s.13-1.43.35-2.09V7.07H2.18C1.43 8.55 1 10.22 1 12s.43 3.45 1.18 4.93l2.85-2.22.81-.62z"
            />
            <path
              fill="#EA4335"
              d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.07l3.66 2.84c.87-2.6 3.3-4.53 6.16-4.53z"
            />
          </svg>
          Sign in with Google
        </motion.button>

        {/* Error message */}
        {error && (
          <motion.div
            initial={{ opacity: 0, y: 10 }}
            animate={{ opacity: 1, y: 0 }}
            style={{
              marginTop: "1.5rem",
              padding: "0.75rem 1rem",
              fontSize: "0.85rem",
              color: "#f87171",
              background: "rgba(239, 68, 68, 0.1)",
              border: "1px solid rgba(239, 68, 68, 0.3)",
              borderRadius: "8px",
              textAlign: "center",
            }}
          >
            ⚠️ {error}
          </motion.div>
        )}

        {/* Footer */}
        <p
          style={{
            marginTop: "2rem",
            fontSize: "0.75rem",
            color: "#6b6880",
          }}
        >
          By signing in, you grant Archy access to your Google Calendar
          and Docs (for scheduling sync and daily briefs).
        </p>
      </motion.div>
    </div>
  );
}
