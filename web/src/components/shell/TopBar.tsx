import { ChevronDown, LogOut, Megaphone, Search, Settings, UserRound } from "lucide-react";
import * as React from "react";
import { useNavigate } from "react-router-dom";

import { Avatar } from "@/components/ui/avatar";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { useAuth } from "@/features/auth/AuthContext";
import { ReportIssueDialog } from "@/features/reports/ReportIssueDialog";
import { useApiKeys, useMcpTools } from "@/lib/queries";

export const TOP_BAR_JUMP_PLACEHOLDER = "Jump to a page, item, or PRD…";

export function TopBar({
  agentOpen,
  onToggleAgent,
  onOpenPalette,
}: {
  agentOpen: boolean;
  onToggleAgent: () => void;
  onOpenPalette: () => void;
}) {
  const { user, logout } = useAuth();
  const { data: keys } = useApiKeys();
  const { data: mcp } = useMcpTools();
  const navigate = useNavigate();
  const liveTools = mcp?.live ?? 0;
  const [reportOpen, setReportOpen] = React.useState(false);

  return (
    <header className="flex h-14 flex-none items-center gap-4 border-b border-line bg-header px-5 backdrop-blur-md">
      <div className="flex items-center gap-2.5">
        <LogoMark />
        <div className="leading-none">
          <div className="text-[15px] font-bold tracking-tight">Graphban</div>
          <div className="mt-0.5 font-mono text-[9.5px] tracking-[0.6px] text-faint">
            AGENT MEMORY · LINEAR EXECUTION
          </div>
        </div>
      </div>

      <div className="h-6 w-px bg-line-2" />

      <div className="relative max-w-[340px] flex-1">
        <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-muted opacity-60" />
        <input
          readOnly
          aria-label="Jump to"
          placeholder={TOP_BAR_JUMP_PLACEHOLDER}
          onFocus={(e) => {
            e.target.blur();
            onOpenPalette();
          }}
          className="h-[34px] w-full cursor-pointer rounded-[9px] border border-line-2 bg-surface-2 pl-9 pr-12 text-[13px] outline-none transition-colors focus:border-line-hover focus:bg-surface-3"
        />
        <button
          type="button"
          aria-label="Open command palette"
          onClick={onOpenPalette}
          className="absolute right-2.5 top-1/2 -translate-y-1/2 rounded-[5px] border border-line-2 px-1.5 py-0.5 font-mono text-[10px] text-faint-2 hover:border-line-hover hover:text-faint"
        >
          ⌘K
        </button>
      </div>

      <div className="flex-1" />

      <div className="flex items-center gap-1.5 rounded-lg border border-[#1c2620] bg-[rgba(95,208,122,0.05)] px-2.5 py-1.5 font-mono text-[10.5px] text-st-done">
        <span className="blink h-1.5 w-1.5 rounded-full bg-st-done shadow-[0_0_8px_#5fd07a]" />
        MCP · {liveTools} TOOLS LIVE
        {keys && keys.length > 0 && <span className="text-faint">· {keys.length} KEYS</span>}
      </div>

      <Button variant="agent" size="sm" onClick={onToggleAgent} aria-pressed={agentOpen}>
        <BrainIcon />
        Agent
      </Button>

      <div className="h-6 w-px bg-line-2" />

      {user && (
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <button className="flex h-9 items-center gap-2 rounded-full border border-line-2 bg-surface py-0.5 pl-0.5 pr-2 transition-colors hover:border-line-hover">
              <Avatar initials={user.initials} color={user.avatar} size={28} />
              <ChevronDown size={12} className="text-faint" />
            </button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end" className="w-[250px]">
            <div className="flex items-center gap-3 px-2 pb-3 pt-2">
              <Avatar initials={user.initials} color={user.avatar} size={38} />
              <div className="min-w-0">
                <div className="truncate text-[13px] font-semibold">{user.name}</div>
                <div className="truncate font-mono text-[11px] text-muted">@{user.handle}</div>
              </div>
            </div>
            <DropdownMenuSeparator />
            <DropdownMenuItem onSelect={() => navigate("/profile")}>
              <UserRound size={14} className="text-muted" />
              Profile
            </DropdownMenuItem>
            <DropdownMenuItem onSelect={() => navigate("/settings")}>
              <Settings size={14} className="text-muted" />
              Settings
            </DropdownMenuItem>
            <DropdownMenuSeparator />
            <DropdownMenuItem onSelect={() => setReportOpen(true)}>
              <Megaphone size={14} className="text-muted" />
              Report an issue
            </DropdownMenuItem>
            <DropdownMenuItem onSelect={() => logout()}>
              <LogOut size={14} className="text-muted" />
              Sign out
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      )}
      <ReportIssueDialog open={reportOpen} onOpenChange={setReportOpen} />
    </header>
  );
}

function LogoMark() {
  return (
    <div
      className="flex h-[26px] w-[26px] items-center justify-center rounded-[7px]"
      style={{
        background: "linear-gradient(150deg,#c6f24e,#8fd12e)",
        boxShadow: "0 0 0 1px rgba(198,242,78,.35),0 6px 18px rgba(198,242,78,.18)",
      }}
    >
      <svg width="15" height="15" viewBox="0 0 16 16" fill="none">
        <path d="M3 2v12M3 4h8M3 8h6M3 12h9" stroke="#0a0c0e" strokeWidth="1.8" strokeLinecap="round" />
      </svg>
    </div>
  );
}

export function BrainIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 16 16" fill="none">
      <path
        d="M8 1.5c-2 0-3.2 1.4-3.2 3 0 .8.3 1.4.7 1.9-1 .5-1.7 1.5-1.7 2.8 0 1.9 1.4 3.3 3.2 3.3M8 1.5c2 0 3.2 1.4 3.2 3 0 .8-.3 1.4-.7 1.9 1 .5 1.7 1.5 1.7 2.8 0 1.9-1.4 3.3-3.2 3.3M8 1.5v11"
        stroke="#a78bfa"
        strokeWidth="1.3"
        strokeLinecap="round"
      />
    </svg>
  );
}
