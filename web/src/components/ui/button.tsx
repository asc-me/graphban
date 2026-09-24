import { Slot } from "@radix-ui/react-slot";
import { cva, type VariantProps } from "class-variance-authority";
import { Loader2 } from "lucide-react";
import * as React from "react";

import { cn } from "@/lib/cn";

const buttonVariants = cva(
  [
    "gb-pressable inline-flex items-center justify-center gap-2 rounded-(--radius-control) font-medium",
    "outline-none transition-colors disabled:pointer-events-none",
    "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus",
  ].join(" "),
  {
    variants: {
      variant: {
        default:
          "bg-accent text-bg font-semibold shadow-[0_6px_18px_rgba(198,242,78,0.18)] " +
          "[@media(hover:hover)_and_(pointer:fine)]:hover:bg-accent-hi " +
          // Disabled: a clean neutral look instead of a faded accent (50%-opacity lime muddies to olive).
          "disabled:opacity-100 disabled:bg-surface-3 disabled:text-faint disabled:shadow-none",
        outline:
          "border border-line-2 bg-surface-2 text-fg " +
          "[@media(hover:hover)_and_(pointer:fine)]:hover:border-line-hover " +
          "[@media(hover:hover)_and_(pointer:fine)]:hover:bg-surface-3",
        ghost:
          "text-muted [@media(hover:hover)_and_(pointer:fine)]:hover:text-fg " +
          "[@media(hover:hover)_and_(pointer:fine)]:hover:bg-surface-3",
        agent:
          "border border-[#2a2440] bg-[rgba(167,139,250,0.08)] text-purple-2 " +
          "[@media(hover:hover)_and_(pointer:fine)]:hover:border-[#3a3358]",
        danger:
          "border border-[rgba(255,107,107,0.25)] bg-[rgba(255,107,107,0.06)] text-st-blocked " +
          "[@media(hover:hover)_and_(pointer:fine)]:hover:bg-[rgba(255,107,107,0.12)]",
      },
      size: {
        default: "h-9 px-3.5 text-[12.5px]",
        sm: "h-8 px-3 text-[12px]",
        lg: "h-10 px-5 text-sm",
        icon: "h-9 w-9",
      },
    },
    defaultVariants: { variant: "default", size: "default" },
  },
);

export interface ButtonProps
  extends React.ButtonHTMLAttributes<HTMLButtonElement>,
    VariantProps<typeof buttonVariants> {
  asChild?: boolean;
  loading?: boolean;
}

export const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(
  ({ className, variant, size, asChild = false, loading = false, disabled, children, ...props }, ref) => {
    const Comp = asChild ? Slot : "button";
    return (
      <Comp
        className={cn(buttonVariants({ variant, size, className }))}
        ref={ref}
        disabled={disabled || loading}
        aria-busy={loading || undefined}
        {...props}
      >
        {loading ? <Loader2 className="size-4 shrink-0 animate-spin" aria-hidden /> : null}
        {children}
      </Comp>
    );
  },
);
Button.displayName = "Button";

export { buttonVariants };
