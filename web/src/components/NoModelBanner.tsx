import { AlertTriangle } from "lucide-react";
import { Link } from "react-router-dom";

import { useProjectCtx } from "@/features/ProjectContext";
import { cn } from "@/lib/cn";
import { usePlatform } from "@/lib/queries";

/**
 * Warns when no real AI provider is active — the offline stub is answering, so drafting,
 * grill, and risk generation produce deterministic placeholder text rather than real
 * output. Driven by the live `effective_chat_provider` (not the saved one), so it also
 * catches the case where a saved model isn't actually applied. Renders nothing when a
 * real provider is in effect.
 */
export function NoModelBanner({ withSettingsLink = true, className }: { withSettingsLink?: boolean; className?: string }) {
  const { activeId } = useProjectCtx();
  const { data: cfg } = usePlatform(activeId);
  if (!cfg || cfg.effective_chat_provider !== "stub") return null;
  return (
    <div
      className={cn(
        "flex items-center gap-2 rounded-[10px] border border-[#3a2f1a] bg-[rgba(224,179,74,0.08)] px-3 py-2 text-[12px] text-[#e0b34a]",
        className,
      )}
    >
      <AlertTriangle size={13} className="flex-none" />
      <span className="min-w-0 flex-1">
        No AI model is active — responses use the offline stub.
        {withSettingsLink && (
          <>
            {" "}
            <Link to="/settings" className="underline underline-offset-2 hover:text-fg-2">
              Configure a provider
            </Link>
            .
          </>
        )}
      </span>
    </div>
  );
}
