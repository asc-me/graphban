import { NewProjectWizard } from "@/features/onboarding/NewProjectWizard";

export { PROJECT_ACCENTS } from "@/features/onboarding/NewProjectWizard";

/** The New project dialog is the split-window wizard (GRPH-1584). */
export function NewProjectDialog({
  open,
  onOpenChange,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  return <NewProjectWizard open={open} onOpenChange={onOpenChange} />;
}
