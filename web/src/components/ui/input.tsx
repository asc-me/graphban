import * as React from "react";

import { cn } from "@/lib/cn";

const fieldClass = cn(
  "w-full rounded-(--radius-control) border border-line-2 bg-surface-2 px-3 text-[13px] text-fg outline-none",
  // Placeholder at secondary contrast — not sole label (caller supplies <label>).
  "placeholder:text-muted-2 transition-colors",
  "[@media(hover:hover)_and_(pointer:fine)]:hover:border-line-hover",
  "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus",
  "focus-visible:border-line-hover focus-visible:bg-surface-3",
  "aria-invalid:border-st-blocked aria-invalid:text-st-blocked",
  "aria-invalid:focus-visible:outline-st-blocked",
);

export const Input = React.forwardRef<HTMLInputElement, React.InputHTMLAttributes<HTMLInputElement>>(
  ({ className, ...props }, ref) => (
    <input ref={ref} className={cn("h-9", fieldClass, className)} {...props} />
  ),
);
Input.displayName = "Input";

export const Textarea = React.forwardRef<
  HTMLTextAreaElement,
  React.TextareaHTMLAttributes<HTMLTextAreaElement>
>(({ className, ...props }, ref) => (
  <textarea ref={ref} className={cn(fieldClass, "resize-none py-2", className)} {...props} />
));
Textarea.displayName = "Textarea";
