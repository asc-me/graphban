import { cn } from "@/lib/cn";
import { dotFill, dotGlow } from "@/lib/dot";
import type { ProjectPattern } from "@/lib/types";

export function Avatar({
  initials,
  color,
  color2,
  pattern,
  size = 28,
  className,
}: {
  initials: string;
  color: string;
  /** Ombre second colour; absent draws solid. */
  color2?: string | null;
  pattern?: ProjectPattern | null;
  size?: number;
  className?: string;
}) {
  return (
    <span
      className={cn(
        "inline-flex flex-none items-center justify-center rounded-full font-mono font-bold text-bg",
        className,
      )}
      style={{
        width: size,
        height: size,
        background: dotFill({ colour: color, colour2: color2, pattern }),
        fontSize: size * 0.4,
        textShadow: dotGlow(color, pattern),
      }}
    >
      {initials}
    </span>
  );
}
