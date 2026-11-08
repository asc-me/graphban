/**
 * Step ① "Is this box safe to expose?" (GRPH-1365). A pure function of the server's boolean
 * posture and step ⓪'s answer, so the rule is testable without a dialog.
 *
 * Warnings, never blockers. Shown only when something is wrong: no warnings → nothing, which is
 * why a posture that could not be read must be passed as `null` (hidden, "nobody looked") and
 * not as an all-clear.
 */
import type { InstancePosture } from "@/lib/types";

export type Where = "this_computer" | "dedicated_server";

export interface PostureWarning {
  id: "jwt" | "signup" | "encryption" | "base_url";
  text: string;
  /** The one copyable env line that fixes it. */
  env: string;
}

export interface PostureView {
  /** Main-path warnings (dedicated server). */
  main: PostureWarning[];
  /** One collapsed line on "this computer" — details live under Advanced. */
  collapsed: string | null;
  /** Always under Advanced: every warning's env line, plus the server-only extras. */
  advanced: PostureWarning[];
  /** TRUSTED_HOPS / HTTPS advice, dedicated server only. */
  proxyHint: boolean;
}

export function postureWarnings(p: InstancePosture): PostureWarning[] {
  const out: PostureWarning[] = [];
  if (!p.jwt_secret_set || !p.jwt_secret_strong) {
    out.push({
      id: "jwt",
      text: p.jwt_secret_set
        ? "JWT_SECRET is shorter than 32 characters."
        : "JWT_SECRET is still the placeholder, so anyone can forge a login.",
      env: "JWT_SECRET=$(openssl rand -hex 32)",
    });
  }
  if (p.signup_open) {
    out.push({
      id: "signup",
      text: "Your account exists, but anyone who can reach this box can still create one.",
      env: "SIGNUP_MODE=invite_only",
    });
  }
  if (!p.secret_encryption_key_set) {
    out.push({
      id: "encryption",
      text: "SECRET_ENCRYPTION_KEY is unset, so stored credentials are not encrypted at rest.",
      env: "SECRET_ENCRYPTION_KEY=$(openssl rand -hex 32)",
    });
  }
  if (!p.app_base_url_set) {
    out.push({
      id: "base_url",
      text: "APP_BASE_URL is still localhost, so invite links point at the wrong place.",
      env: "APP_BASE_URL=https://graphban.example.com",
    });
  }
  return out;
}

export function postureView(p: InstancePosture | null | undefined, where: Where | null): PostureView | null {
  if (!p) return null;
  const all = postureWarnings(p);
  if (all.length === 0) return null;
  if (where === "dedicated_server") return { main: all, collapsed: null, advanced: [], proxyHint: true };
  // "This computer" (or not yet answered): collapse to one line. A loopback-only box is not
  // exposed at all, so the line says so rather than alarming.
  const n = all.length;
  const what = `${n} setting${n === 1 ? "" : "s"} to fix before you expose this box`;
  return {
    main: [],
    collapsed: p.request_on_loopback ? `${what} (you're on loopback, so nothing is exposed yet).` : `${what}.`,
    advanced: all,
    proxyHint: false,
  };
}
