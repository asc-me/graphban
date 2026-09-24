import { useParams, useSearchParams } from "react-router-dom";

import { FeedbackWidget } from "@/features/feedback/FeedbackWidget";
import { fromParams } from "@/features/feedback/config";
import { PublicPageShell } from "./PublicPageShell";

export function PublicFeedbackPage() {
  const { token } = useParams<{ token: string }>();
  const [search] = useSearchParams();

  if (!token) {
    return (
      <PublicPageShell>
        <div className="py-20 text-center">
          <div className="text-[16px] font-semibold text-fg">Not found</div>
          <p className="mt-2 text-[13px] text-muted">
            This feedback form does not exist or is not public.
          </p>
        </div>
      </PublicPageShell>
    );
  }

  // Build config from URL params, ensuring the token is set.
  search.set("token", token);
  const config = fromParams(search);

  return (
    <PublicPageShell>
      <div className="mx-auto max-w-lg">
        <div className="mb-6">
          <h1 className="text-[20px] font-bold tracking-tight">Feedback</h1>
          <p className="mt-1 text-[12px] text-muted">Public feedback form</p>
        </div>
        <FeedbackWidget config={config} />
      </div>
    </PublicPageShell>
  );
}
