import { describe, expect, it } from "vitest";

import type { InstancePosture } from "@/lib/types";

import { postureView, postureWarnings } from "./posture";

const GOOD: InstancePosture = {
  jwt_secret_set: true, jwt_secret_strong: true, signup_open: false,
  secret_encryption_key_set: true, app_base_url_set: true, request_on_loopback: false,
};

describe("postureView", () => {
  it("SABOTAGE: the placeholder JWT_SECRET raises the hint and a real one clears it", () => {
    const bad = { ...GOOD, jwt_secret_set: false, jwt_secret_strong: false };
    expect(postureWarnings(bad).map((w) => w.id)).toEqual(["jwt"]);
    expect(postureView(bad, "dedicated_server")?.main).toHaveLength(1);
    expect(postureView(GOOD, "dedicated_server")).toBeNull();
  });

  it("an unread posture is hidden, not clean", () => {
    expect(postureView(null, "dedicated_server")).toBeNull();
    expect(postureView(undefined, "this_computer")).toBeNull();
  });

  it("this computer collapses every warning into one line; the env lines go under Advanced", () => {
    const v = postureView({ ...GOOD, signup_open: true, secret_encryption_key_set: false, request_on_loopback: true }, "this_computer")!;
    expect(v.main).toEqual([]);
    expect(v.collapsed).toMatch(/2 settings to fix/);
    expect(v.collapsed).toMatch(/loopback/);
    expect(v.advanced.map((w) => w.env)).toContain("SIGNUP_MODE=invite_only");
    expect(v.proxyHint).toBe(false);
  });

  it("a dedicated server shows them on the main path and adds the proxy/HTTPS advice", () => {
    const v = postureView({ ...GOOD, signup_open: true }, "dedicated_server")!;
    expect(v.main.map((w) => w.id)).toEqual(["signup"]);
    expect(v.main[0].text).toMatch(/anyone who can reach this box/);
    expect(v.collapsed).toBeNull();
    expect(v.proxyHint).toBe(true);
  });

  it("no answer to ⓪ yet is treated like this computer: quiet", () => {
    expect(postureView({ ...GOOD, signup_open: true }, null)?.main).toEqual([]);
  });
});
