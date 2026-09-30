import { useQuery } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";

import { Avatar } from "@/components/ui/avatar";
import { Button } from "@/components/ui/button";
import { useAuth } from "@/features/auth/AuthContext";
import { api } from "@/lib/api";

export function ProfileView() {
  const { user } = useAuth();
  const navigate = useNavigate();
  const { data: memberships = [] } = useQuery({
    queryKey: ["my-memberships"],
    queryFn: () => api.myMemberships(),
  });

  if (!user) return null;

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex-none border-b border-line px-5 py-4">
        <h1 className="text-[18px] font-semibold tracking-tight">Profile</h1>
        <p className="mt-0.5 text-body text-muted">Your account and project access.</p>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto p-6">
        <div className="mb-6 flex items-center gap-4 rounded-[14px] border border-line-2 bg-surface-2 p-5">
          <Avatar initials={user.initials} color={user.avatar} size={56} />
          <div className="min-w-0">
            <div className="text-[17px] font-semibold">{user.name}</div>
            <div className="font-mono text-[12px] text-muted">@{user.handle}</div>
            <div className="mt-0.5 text-body text-muted">{user.email}</div>
          </div>
          <Button variant="outline" size="sm" className="ml-auto" onClick={() => navigate("/settings")}>
            Settings
          </Button>
        </div>

        <div className="max-w-2xl">
          <div className="mb-2 font-mono text-[10px] uppercase tracking-wide text-faint">Project access</div>
          <div className="space-y-2">
            {memberships.map((m) => (
              <div key={m.project_id} className="flex items-center gap-3 rounded-[11px] border border-line-2 bg-surface-2 px-3 py-2.5">
                <span className="h-2.5 w-2.5 flex-none rounded-[3px]" style={{ background: m.accent }} />
                <span className="min-w-0 flex-1 truncate text-[13px] text-fg-2">{m.project_name}</span>
                <span className="rounded-md border border-line-2 px-2 py-0.5 font-mono text-[10px] uppercase tracking-wide text-muted">
                  {m.role}
                </span>
                <span className="w-12 text-right font-mono text-[10px] text-faint">{m.access}</span>
              </div>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}
